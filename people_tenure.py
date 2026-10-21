"""재입사·수습·옛 기록(B5).

  - 재입사 = 옛 계정을 되살린다(같은 사번). 지난 근속 구간은 service_periods 에 한 줄씩 남기고
    users.hire_date 는 새 입사일로. 연차·퇴직금은 users.hire_date 기준이라 새 입사일부터 센다.
      · 그해 이후 연차 이월(leave_balances)은 지운다 — 옛 구간 연차가 새 구간에 섞이지 않게.
      · 연차 사용 합계는 지금 입사일 전 휴가를 빼고 센다(payroll_utils.compute_leave_balance).
      · 급여·근태는 원래 날짜별 기록이라 구간으로 나눠 보면 된다.
  - 수습: 시작·연장(사유 필수)·종료(통과/미통과). 종료 14일 전·당일에 매니저·관리자에게 알림 한 번씩.
  - 입사 전 경력(career)·옛 시스템 이력(legacy) = prior_careers. CSV 의 열쇠는 사번.
  - 중복 의심: 주민번호 같음 · 이름+생년월일 같음 · 전화 같음.
    합치기는 한쪽이 '빈 계정'일 때만(업무 기록이 0건). 둘 다 기록이 많은 계정은 합치지 않는다.
"""
import csv
import io
import json
import re
import sqlite3
from datetime import date, timedelta

import org_admin
import people_history

_DDL = (
    '''CREATE TABLE IF NOT EXISTS service_periods (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        emp_no      TEXT,
        start_date  TEXT,
        end_date    TEXT NOT NULL,
        end_reason  TEXT,
        source      TEXT NOT NULL DEFAULT 'rehire',
        note        TEXT,
        created_by  INTEGER,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_service_periods_user ON service_periods(user_id)',
    '''CREATE TABLE IF NOT EXISTS probation_periods (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        start_date     TEXT NOT NULL,
        end_date       TEXT NOT NULL,
        original_end   TEXT NOT NULL,
        extended_count INTEGER NOT NULL DEFAULT 0,
        extend_reason  TEXT,
        status         TEXT NOT NULL DEFAULT 'active',
        result_note    TEXT,
        decided_by     INTEGER,
        decided_at     TEXT,
        alert_stage    INTEGER,
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_probation_user ON probation_periods(user_id)',
    '''CREATE TABLE IF NOT EXISTS prior_careers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL DEFAULT 'career',
        company     TEXT,
        dept        TEXT,
        title       TEXT,
        start_date  TEXT,
        end_date    TEXT,
        description TEXT,
        source      TEXT NOT NULL DEFAULT 'manual',
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_prior_careers_user ON prior_careers(user_id)',
    '''CREATE TABLE IF NOT EXISTS merge_records (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kept_user_id    INTEGER NOT NULL,
        removed_user_id INTEGER NOT NULL,
        removed_snapshot TEXT,
        moved           TEXT,
        actor_id        INTEGER,
        actor_name      TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
)

PROB_STATUS = {'active': '수습 중', 'passed': '통과', 'failed': '미통과', 'cancelled': '취소'}
KIND_LABEL = {'career': '입사 전 경력', 'legacy': '옛 시스템 이력'}
KIND_IN = {'경력': 'career', '입사 전 경력': 'career', 'career': 'career',
           '옛 이력': 'legacy', '옛 시스템 이력': 'legacy', '이력': 'legacy', 'legacy': 'legacy'}
CAREER_COLUMNS = ['사번', '구분', '회사', '부서', '직무', '시작일', '종료일', '내용']
EMP_LABEL = {'full_time': '정규직', 'contract': '계약직', 'intern': '인턴', 'part_time': '파트타임'}
ALERT_DAYS = (14, 0)

# 합칠 때 옮기거나 지워도 되는 표 — 시스템이 저절로 만들거나 그 사람에 대한 부가 정보.
# 여기 없는 표에 한 줄이라도 있으면 '업무 기록이 있는 계정'으로 보고 합치지 않는다.
LIGHT_TABLES = {
    'notifications', 'field_changes', 'consent_records', 'dashboard_widgets', 'onboarding_progress',
    'perf_reminders', 'copilot_logs', 'emergency_contacts', 'person_ids', 'leave_balances',
    'prior_careers', 'service_periods', 'probation_periods', 'employee_salary',
}
TABLE_LABEL = {
    'leave_requests': '휴가', 'payslips': '급여 명세', 'checkins': '출퇴근', 'performance_goals': '목표',
    'termination_requests': '퇴사 신청', 'personnel_actions': '인사 발령', 'employee_documents': '서류',
    'salary_history': '급여 이력', 'severance_payments': '퇴직금', 'users': '부하 직원·버디',
    'departments': '부서장', 'contracts': '계약서', 'cycle_participants': '평가 명단',
    'peer_assignments': '다면평가 배정', 'job_requisitions': '채용 요청', 'one_on_ones': '1:1',
}
# 합칠 때 남길 계정이 비어 있으면 지울 계정 값으로 채우는 칸
FILL_COLS = ('phone', 'birth_date', 'address', 'personal_email', 'work_phone', 'preferred_name',
             'job_title', 'gender', 'emergency_name', 'emergency_phone', 'emergency_relation',
             'rrn_enc', 'rrn_mask', 'rrn_hash')

people_history.LABEL.update({'probation': '수습', 'prior_career': '경력·옛 이력', 'merge': '계정 합치기',
                             'service_period': '근속 구간'})
people_history.SOURCE_LABEL.setdefault('rehire', '재입사')
people_history.SOURCE_LABEL.setdefault('tenure', '근속·수습')

_done = set()


def _dbkey(c):
    try:
        return c.execute('PRAGMA database_list').fetchone()[2]
    except Exception:
        return None


def ensure_schema(c):
    k = _dbkey(c)
    if k and k in _done:
        return
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
        return
    for q in _DDL:
        c.execute(q)
    c.commit()
    if k:
        _done.add(k)


# ── 날짜 ────────────────────────────────────────────────────
def _d(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def norm_date(s):
    """YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD → ISO. 빈값 None, 오류 False."""
    s = (s or '').strip()
    if not s:
        return None
    for sep in ('-', '.', '/'):
        p = s.split(sep)
        if len(p) == 3:
            try:
                return date(int(p[0]), int(p[1]), int(p[2])).isoformat()
            except ValueError:
                return False
    return False


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = (date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def span_label(days):
    """일수 → 'N년 M개월'(1개월 미만이면 'N일')."""
    if days is None or days < 0:
        return '-'
    if days < 31:
        return '%d일' % (days + 1)
    months = int((days + 1) / 30.4375)
    y, m = divmod(months, 12)
    if y and m:
        return '%d년 %d개월' % (y, m)
    return '%d년' % y if y else '%d개월' % m


# ── 근속 구간 ───────────────────────────────────────────────
def periods(c, uid, today=None):
    """[{start, end, end_reason, current, days, label, source}] 오래된 것부터 + 합계."""
    ensure_schema(c)
    today = today or date.today()
    u = c.execute('SELECT hire_date, termination_date, termination_reason, status FROM users WHERE id=?',
                  (uid,)).fetchone()
    if not u:
        return None
    out = []
    for r in c.execute('SELECT * FROM service_periods WHERE user_id=? ORDER BY COALESCE(start_date, end_date), id',
                       (uid,)).fetchall():
        s, e = _d(r['start_date']), _d(r['end_date'])
        days = (e - s).days if s and e else None
        out.append(dict(id=r['id'], start=r['start_date'], end=r['end_date'], end_reason=r['end_reason'],
                        source=r['source'], note=r['note'], current=False, days=days, label=span_label(days)))
    s = _d(u['hire_date'])
    e = _d(u['termination_date']) if u['status'] == 'resigned' else None
    if s:
        days = ((e or today) - s).days
        out.append(dict(id=None, start=u['hire_date'], end=u['termination_date'] if e else None,
                        end_reason=u['termination_reason'] if e else None, source='now',
                        note=None, current=not e, days=days, label=span_label(days)))
    total = sum(p['days'] for p in out if p['days'] is not None and p['days'] >= 0) + \
        sum(1 for p in out if p['days'] is not None and p['days'] >= 0) - 1 if out else 0
    return dict(rows=out, total_days=total, total_label=span_label(total) if out else '-',
                rehired=sum(1 for p in out if not p['current'] and p['source'] != 'now') > 0)


def last_exit(c, uid):
    """가장 최근 퇴사 신청의 재입사 가능 여부(1/0/None)·사유."""
    try:
        r = c.execute('SELECT is_rehire_eligible, exit_reason_category, request_type FROM termination_requests '
                      'WHERE user_id=? ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


def rehire(c, uid, f, actor_id, actor_name, tenant_id=None, register=None):
    """재입사. 오류 문구를 돌려준다(None = 성공). commit 은 부르는 쪽이."""
    ensure_schema(c)
    u = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return '직원을 찾을 수 없습니다.'
    if u['status'] != 'resigned':
        return '퇴사한 직원만 재입사 처리할 수 있습니다.'
    new = norm_date(f.get('hire_date'))
    if not new:
        return '새 입사일을 적어 주세요.'
    old_end = _d(u['termination_date'])
    if old_end and _d(new) <= old_end:
        return '새 입사일은 퇴사일(%s) 다음 날부터입니다.' % u['termination_date']
    ex = last_exit(c, uid)
    if ex and ex.get('is_rehire_eligible') == 0 and not f.get('confirm_ineligible'):
        return '퇴사 때 "재입사 불가"로 적힌 사람입니다. 확인란에 표시해야 진행됩니다.'
    emp_type = f.get('employment_type') or u['employment_type']
    if emp_type not in EMP_LABEL:
        return '고용 형태를 확인하세요.'

    def _id(k, table):
        v = f.get(k)
        if v in (None, ''):
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            return False
        return v if c.execute('SELECT 1 FROM %s WHERE id=?' % table, (v,)).fetchone() else False

    dept, pos, mgr = _id('department_id', 'departments'), _id('position_id', 'positions'), _id('manager_id', 'users')
    if False in (dept, pos, mgr):
        return '부서·직급·매니저를 확인하세요.'
    if mgr == uid:
        return '본인을 매니저로 정할 수 없습니다.'
    try:
        months = int(f.get('probation_months') or 0)
    except ValueError:
        months = 0
    if months < 0 or months > 12:
        return '수습 기간은 0~12개월입니다.'

    before = people_history.snapshot(c, uid)
    c.execute('INSERT INTO service_periods (user_id, emp_no, start_date, end_date, end_reason, source, note, created_by) '
              "VALUES (?,?,?,?,?,'rehire',?,?)",
              (uid, u['emp_no'], u['hire_date'], u['termination_date'] or new,
               u['termination_reason'], (f.get('note') or '').strip()[:200] or None, actor_id))
    c.execute("UPDATE users SET status='active', hire_date=?, termination_date=NULL, termination_reason=NULL, "
              'department_id=?, position_id=?, manager_id=?, employment_type=? WHERE id=?',
              (new, dept, pos, mgr, emp_type, uid))
    cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    if 'locked_at' in cols:
        c.execute('UPDATE users SET locked_at=NULL, lock_reason=NULL, failed_logins=0 WHERE id=?', (uid,))
    c.execute('DELETE FROM leave_balances WHERE user_id=? AND year>=?', (uid, int(new[:4])))
    people_history.record_diff(c, uid, before, 'rehire', actor_id, actor_name,
                               reason='재입사 · 지난 구간 %s ~ %s' % (u['hire_date'] or '?', u['termination_date'] or '?'))
    if months:
        start_probation(c, uid, new, months, None, actor_id, actor_name)
    if register and u['email']:
        register(u['email'], tenant_id or 1)
    return None


# ── 수습 ───────────────────────────────────────────────────
def probation_of(c, uid):
    ensure_schema(c)
    rows = [dict(r) for r in c.execute('SELECT * FROM probation_periods WHERE user_id=? ORDER BY id DESC',
                                       (uid,)).fetchall()]
    today = date.today()
    for r in rows:
        r['status_label'] = PROB_STATUS.get(r['status'], r['status'])
        e = _d(r['end_date'])
        r['days'] = (e - today).days if e else None
    cur = next((r for r in rows if r['status'] == 'active'), None)
    return dict(current=cur, history=[r for r in rows if r is not cur])


def default_months(c):
    r = c.execute("SELECT value FROM company_settings WHERE key='probation_months'").fetchone()
    try:
        return max(0, min(12, int(r['value'] if r else 3)))
    except (TypeError, ValueError):
        return 3


def start_probation(c, uid, start, months, end, actor_id, actor_name):
    ensure_schema(c)
    if c.execute("SELECT 1 FROM probation_periods WHERE user_id=? AND status='active'", (uid,)).fetchone():
        return '이미 수습 중입니다. 연장하거나 끝낸 뒤 새로 시작하세요.'
    s = norm_date(start)
    if not s:
        return '수습 시작일을 적어 주세요.'
    e = norm_date(end) if end else None
    if e is False:
        return '수습 종료일 형식을 확인하세요.'
    if not e:
        try:
            months = int(months or 0)
        except ValueError:
            months = 0
        if not 1 <= months <= 12:
            return '수습 기간은 1~12개월입니다.'
        e = (add_months(_d(s), months) - timedelta(days=1)).isoformat()
    if e < s:
        return '종료일이 시작일보다 빠릅니다.'
    c.execute('INSERT INTO probation_periods (user_id, start_date, end_date, original_end) VALUES (?,?,?,?)',
              (uid, s, e, e))
    people_history.record_change(c, uid, 'probation', '', '수습 %s ~ %s' % (s, e), 'tenure', actor_id, actor_name)
    return None


def extend_probation(c, uid, new_end, reason, actor_id, actor_name):
    p = c.execute("SELECT * FROM probation_periods WHERE user_id=? AND status='active'", (uid,)).fetchone()
    if not p:
        return '진행 중인 수습이 없습니다.'
    e = norm_date(new_end)
    if not e:
        return '새 종료일을 적어 주세요.'
    if e <= p['end_date']:
        return '새 종료일은 지금 종료일(%s)보다 뒤여야 합니다.' % p['end_date']
    reason = (reason or '').strip()[:200]
    if len(reason) < 2:
        return '연장 사유를 적어 주세요.'
    c.execute('UPDATE probation_periods SET end_date=?, extended_count=extended_count+1, extend_reason=?, '
              'alert_stage=NULL WHERE id=?', (e, reason, p['id']))
    people_history.record_change(c, uid, 'probation', '종료 %s' % p['end_date'], '종료 %s (연장)' % e,
                                 'tenure', actor_id, actor_name, reason=reason)
    return None


def decide_probation(c, uid, result, note, actor_id, actor_name):
    if result not in ('passed', 'failed', 'cancelled'):
        return '결과를 고르세요.'
    p = c.execute("SELECT * FROM probation_periods WHERE user_id=? AND status='active'", (uid,)).fetchone()
    if not p:
        return '진행 중인 수습이 없습니다.'
    note = (note or '').strip()[:300]
    if result == 'failed' and len(note) < 2:
        return '미통과는 사유를 적어야 합니다.'
    c.execute('UPDATE probation_periods SET status=?, result_note=?, decided_by=?, decided_at=CURRENT_TIMESTAMP '
              'WHERE id=?', (result, note or None, actor_id, p['id']))
    people_history.record_change(c, uid, 'probation', '수습 중', PROB_STATUS[result], 'tenure',
                                 actor_id, actor_name, reason=note or None)
    return None


def probation_list(c, status='active'):
    ensure_schema(c)
    q = ('SELECT p.*, u.name, u.emp_no, u.status AS ustatus, d.name AS dept, m.name AS mgr '
         'FROM probation_periods p JOIN users u ON u.id=p.user_id '
         'LEFT JOIN departments d ON d.id=u.department_id LEFT JOIN users m ON m.id=u.manager_id ')
    if status == 'active':
        q += "WHERE p.status='active' ORDER BY p.end_date"
    else:
        q += "WHERE p.status!='active' ORDER BY p.decided_at DESC LIMIT 100"
    today = date.today()
    out = []
    for r in c.execute(q).fetchall():
        d = dict(r)
        d['status_label'] = PROB_STATUS.get(d['status'], d['status'])
        e = _d(d['end_date'])
        d['days'] = (e - today).days if e else None
        out.append(d)
    return out


def sweep(c, notify, today=None):
    """종료 14일 전·당일(지났어도)에 매니저·관리자에게 한 번씩."""
    ensure_schema(c)
    today = today or date.today()
    admins = [r[0] for r in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'").fetchall()]
    n = 0
    for p in c.execute("SELECT p.*, u.name, u.manager_id FROM probation_periods p JOIN users u ON u.id=p.user_id "
                       "WHERE p.status='active' AND u.status='active'").fetchall():
        left = (_d(p['end_date']) - today).days
        stage = next((s for s in sorted(ALERT_DAYS) if left <= s), None)
        if stage is None or (p['alert_stage'] is not None and p['alert_stage'] <= stage):
            continue
        c.execute('UPDATE probation_periods SET alert_stage=? WHERE id=?', (stage, p['id']))
        if stage == 0:
            title = '%s 수습 종료일입니다' % p['name']
            body = '종료일 %s. 통과·미통과·연장을 정해 주세요.' % p['end_date']
        else:
            title = '%s 수습이 %d일 뒤 끝납니다' % (p['name'], left)
            body = '종료일 %s. 평가를 준비해 주세요.' % p['end_date']
        for to in {x for x in [p['manager_id']] + admins if x}:
            notify(to, title, body, '/employees/%d#tab-tenure' % p['user_id'])
            n += 1
    c.commit()
    return n


# ── 입사 전 경력·옛 이력 ─────────────────────────────────────
def careers_of(c, uid):
    ensure_schema(c)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM prior_careers WHERE user_id=? ORDER BY kind, COALESCE(start_date,''), id",
        (uid,)).fetchall()]
    for r in rows:
        s, e = _d(r['start_date']), _d(r['end_date'])
        r['days'] = (e - s).days if s and e else None
        r['label'] = span_label(r['days']) if r['days'] is not None else ''
    car = [r for r in rows if r['kind'] == 'career']
    total = sum(r['days'] + 1 for r in car if r['days'] is not None and r['days'] >= 0)
    return dict(career=car, legacy=[r for r in rows if r['kind'] == 'legacy'],
                career_total=span_label(total - 1) if total else '')


def _career_row(f):
    kind = KIND_IN.get((f.get('kind') or f.get('구분') or 'career').strip())
    if not kind:
        return None, '구분은 경력 또는 옛 이력입니다.'
    s, e = norm_date(f.get('start_date', f.get('시작일'))), norm_date(f.get('end_date', f.get('종료일')))
    if s is False or e is False:
        return None, '날짜 형식을 확인하세요(YYYY-MM-DD).'
    if s and e and e < s:
        return None, '종료일이 시작일보다 빠릅니다.'
    row = dict(kind=kind,
               company=(f.get('company', f.get('회사')) or '').strip()[:80] or None,
               dept=(f.get('dept', f.get('부서')) or '').strip()[:80] or None,
               title=(f.get('title', f.get('직무')) or '').strip()[:80] or None,
               start_date=s, end_date=e,
               description=(f.get('description', f.get('내용')) or '').strip()[:500] or None)
    if not (row['company'] or row['title'] or row['description']):
        return None, '회사·직무·내용 중 하나는 적어 주세요.'
    return row, None


def add_career(c, uid, f, actor_id, actor_name, source='manual'):
    ensure_schema(c)
    row, err = _career_row(f)
    if err:
        return err
    c.execute('INSERT INTO prior_careers (user_id, kind, company, dept, title, start_date, end_date, description, source) '
              'VALUES (?,?,?,?,?,?,?,?,?)',
              (uid, row['kind'], row['company'], row['dept'], row['title'], row['start_date'], row['end_date'],
               row['description'], source))
    if source == 'manual':
        people_history.record_change(c, uid, 'prior_career', '', '%s 추가 · %s' % (
            KIND_LABEL[row['kind']], row['company'] or row['title'] or ''), 'tenure', actor_id, actor_name)
    return None


def delete_career(c, uid, cid, actor_id, actor_name):
    r = c.execute('SELECT * FROM prior_careers WHERE id=? AND user_id=?', (cid, uid)).fetchone()
    if not r:
        return False
    c.execute('DELETE FROM prior_careers WHERE id=?', (cid,))
    people_history.record_change(c, uid, 'prior_career', '%s · %s' % (KIND_LABEL.get(r['kind'], ''), r['company'] or r['title'] or ''),
                                 '(삭제)', 'tenure', actor_id, actor_name)
    return True


def read_csv(raw):
    for enc in ('utf-8-sig', 'cp949'):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except (UnicodeDecodeError, csv.Error):
            continue
    return None


def import_careers(c, rows, actor_id, actor_name, commit=False):
    """사번 열쇠로 경력·옛 이력을 넣는다. commit=False 면 검사만. [(줄, 사번, 이름, 오류)] 와 넣은 수."""
    ensure_schema(c)
    by_no = {r['emp_no']: (r['id'], r['name']) for r in c.execute(
        "SELECT id, name, emp_no FROM users WHERE emp_no IS NOT NULL AND emp_no!=''").fetchall()}
    report, ok = [], 0
    for i, raw in enumerate(rows, start=2):
        f = {k: (raw.get(k) or '').strip() for k in CAREER_COLUMNS}
        who = by_no.get(f['사번'])
        if not f['사번']:
            report.append((i, '', '', '사번 없음'))
            continue
        if not who:
            report.append((i, f['사번'], '', '없는 사번'))
            continue
        row, err = _career_row(f)
        if err:
            report.append((i, f['사번'], who[1], err))
            continue
        report.append((i, f['사번'], who[1], None))
        ok += 1
        if commit:
            add_career(c, who[0], f, actor_id, actor_name, source='csv')
    if commit and ok:
        names = {}
        for i, no, nm, err in report:
            if not err:
                names[by_no[no][0]] = names.get(by_no[no][0], 0) + 1
        for uid, n in names.items():
            people_history.record_change(c, uid, 'prior_career', '', 'CSV 로 %d건 불러옴' % n,
                                         'tenure', actor_id, actor_name)
    return report, ok


def careers_csv(c):
    ensure_schema(c)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CAREER_COLUMNS + ['이름'])
    for r in c.execute('SELECT p.*, u.emp_no, u.name FROM prior_careers p JOIN users u ON u.id=p.user_id '
                       'ORDER BY u.emp_no, p.kind, p.start_date').fetchall():
        w.writerow([r['emp_no'] or '', '경력' if r['kind'] == 'career' else '옛 이력', r['company'] or '',
                    r['dept'] or '', r['title'] or '', r['start_date'] or '', r['end_date'] or '',
                    r['description'] or '', r['name']])
    return '﻿' + out.getvalue()


# ── 퇴사자 ─────────────────────────────────────────────────
def leavers(c, q=''):
    ensure_schema(c)
    sql = ('SELECT u.id, u.name, u.emp_no, u.email, u.hire_date, u.termination_date, u.termination_reason, '
           'd.name AS dept, (SELECT COUNT(*) FROM employee_documents x WHERE x.user_id=u.id) AS docs, '
           '(SELECT COUNT(*) FROM service_periods s WHERE s.user_id=u.id) AS past, '
           '(SELECT t.is_rehire_eligible FROM termination_requests t WHERE t.user_id=u.id ORDER BY t.id DESC LIMIT 1) AS elig '
           "FROM users u LEFT JOIN departments d ON d.id=u.department_id WHERE u.status='resigned' ")
    args = []
    if q:
        sql += 'AND (u.name LIKE ? OR u.emp_no LIKE ? OR u.email LIKE ?) '
        args = ['%' + q + '%'] * 3
    sql += 'ORDER BY u.termination_date DESC, u.id DESC'
    out = []
    for r in c.execute(sql, args).fetchall():
        d = dict(r)
        s, e = _d(d['hire_date']), _d(d['termination_date'])
        d['span'] = span_label((e - s).days) if s and e else '-'
        out.append(d)
    return out


def leavers_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['사번', '이름', '부서', '입사일', '퇴사일', '퇴사 사유', '근속', '지난 구간', '서류', '재입사'])
    for r in rows:
        w.writerow([r['emp_no'] or '', r['name'], r['dept'] or '', r['hire_date'] or '', r['termination_date'] or '',
                    r['termination_reason'] or '', r['span'], r['past'], r['docs'],
                    {1: '가능', 0: '불가'}.get(r['elig'], '')])
    return '﻿' + out.getvalue()


# ── 중복 의심·합치기 ─────────────────────────────────────────
def _digits(s):
    return re.sub(r'\D', '', s or '')


def _nm(s):
    return re.sub(r'\s+', '', (s or '')).lower()


def _pairs(c, only=None):
    cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    sel = 'id, name, birth_date, phone' + (', rrn_hash' if 'rrn_hash' in cols else ', NULL AS rrn_hash')
    us = [dict(r) for r in c.execute("SELECT %s FROM users WHERE role!='guest'" % sel).fetchall()]
    keys = {}
    for u in us:
        if u['rrn_hash']:
            keys.setdefault(('rrn', u['rrn_hash']), []).append(u['id'])
        if u['name'] and u['birth_date']:
            keys.setdefault(('nb', _nm(u['name']) + '|' + str(u['birth_date'])[:10]), []).append(u['id'])
        p = _digits(u['phone'])
        if len(p) >= 9:
            keys.setdefault(('ph', p), []).append(u['id'])
    pairs = {}
    for (kind, _), ids in keys.items():
        if len(ids) < 2 or len(ids) > 5:   # 대표번호 같은 흔한 값은 건너뜀
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                if only and only not in (a, b):
                    continue
                pairs.setdefault((a, b), set()).add(kind)
    return pairs


WHY = {'rrn': '주민등록번호 같음', 'nb': '이름·생년월일 같음', 'ph': '전화 같음'}


def _refs(c):
    """[(표, 칸)] users 를 가리키는 칸 전부."""
    out = []
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        fk = [f[3] for f in c.execute('PRAGMA foreign_key_list("%s")' % t).fetchall() if f[2] == 'users']
        if t != 'users' and not fk:
            fk = [r[1] for r in c.execute('PRAGMA table_info("%s")' % t).fetchall() if r[1] in ('user_id', 'employee_id')]
        out += [(t, col) for col in fk]
    return out


def usage(c, uid, refs=None):
    """{'heavy': {표: 건수}, 'light': {표: 건수}} — heavy 가 비어야 지울 수 있다."""
    heavy, light = {}, {}
    for t, col in refs or _refs(c):
        try:
            n = c.execute('SELECT COUNT(*) FROM "%s" WHERE "%s"=?' % (t, col), (uid,)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if not n:
            continue
        box = light if t in LIGHT_TABLES else heavy
        box[t] = box.get(t, 0) + n
    return dict(heavy=heavy, light=light,
                heavy_label=' · '.join('%s %d' % (TABLE_LABEL.get(t, t), n) for t, n in sorted(heavy.items())))


def duplicates(c, only=None):
    ensure_schema(c)
    pairs = _pairs(c, only)
    if not pairs:
        return []
    refs = _refs(c)
    out, cache = [], {}
    for (a, b), why in sorted(pairs.items()):
        side = []
        for uid in (a, b):
            if uid not in cache:
                u = c.execute('SELECT u.id, u.name, u.emp_no, u.email, u.status, u.hire_date, u.termination_date, '
                              'u.birth_date, u.phone, d.name AS dept FROM users u '
                              'LEFT JOIN departments d ON d.id=u.department_id WHERE u.id=?', (uid,)).fetchone()
                cache[uid] = dict(u, use=usage(c, uid, refs))
            side.append(cache[uid])
        out.append(dict(a=side[0], b=side[1], why=[WHY[k] for k in sorted(why)],
                        can_drop=[s['id'] for s in side if not s['use']['heavy']]))
    return out


def merge(c, keep_id, drop_id, actor_id, actor_name, tenant_id=None, unregister=None):
    """빈 계정(drop)을 남길 계정(keep)에 합친다. 오류 문구 또는 None. commit 은 부르는 쪽이."""
    ensure_schema(c)
    if keep_id == drop_id:
        return '같은 계정입니다.'
    keep = c.execute('SELECT * FROM users WHERE id=?', (keep_id,)).fetchone()
    drop = c.execute('SELECT * FROM users WHERE id=?', (drop_id,)).fetchone()
    if not keep or not drop:
        return '계정을 찾을 수 없습니다.'
    if (min(keep_id, drop_id), max(keep_id, drop_id)) not in _pairs(c, keep_id):
        return '중복 의심 짝이 아닙니다.'
    refs = _refs(c)
    use = usage(c, drop_id, refs)
    if use['heavy']:
        return '지울 계정에 업무 기록이 있어 합칠 수 없습니다(%s).' % use['heavy_label']
    before = people_history.snapshot(c, keep_id)
    filled = []
    cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    for col in FILL_COLS:
        if col in cols and not keep[col] and drop[col]:
            if col == 'rrn_hash' and c.execute('SELECT 1 FROM users WHERE rrn_hash=? AND id NOT IN (?,?)',
                                               (drop[col], keep_id, drop_id)).fetchone():
                continue
            c.execute('UPDATE users SET "%s"=? WHERE id=?' % col, (drop[col], keep_id))
            filled.append(col)
    moved = {}
    for t, col in refs:
        n = c.execute('SELECT COUNT(*) FROM "%s" WHERE "%s"=?' % (t, col), (drop_id,)).fetchone()[0]
        if not n:
            continue
        if t in ('notifications', 'field_changes', 'prior_careers', 'service_periods', 'emergency_contacts',
                 'person_ids', 'consent_records'):
            c.execute('UPDATE OR IGNORE "%s" SET "%s"=? WHERE "%s"=?' % (t, col, col), (keep_id, drop_id))
        c.execute('DELETE FROM "%s" WHERE "%s"=?' % (t, col), (drop_id,))
        moved[t] = n
    snap = {k: drop[k] for k in drop.keys() if k not in ('password_hash', 'rrn_enc')}
    c.execute('INSERT INTO merge_records (kept_user_id, removed_user_id, removed_snapshot, moved, actor_id, actor_name) '
              'VALUES (?,?,?,?,?,?)', (keep_id, drop_id, json.dumps(snap, ensure_ascii=False, default=str),
                                      json.dumps(moved, ensure_ascii=False), actor_id, actor_name))
    c.execute('DELETE FROM users WHERE id=?', (drop_id,))
    people_history.record_diff(c, keep_id, before, 'tenure', actor_id, actor_name, reason='중복 계정 합치기')
    people_history.record_change(c, keep_id, 'merge', '%s (%s)' % (drop['name'], drop['emp_no'] or drop['email']),
                                 '합침', 'tenure', actor_id, actor_name,
                                 reason='채운 칸 %d · 옮긴 기록 %d' % (len(filled), sum(moved.values())))
    if unregister and drop['email'] and drop['email'] != keep['email']:
        unregister(drop['email'], tenant_id or 1)
    return None


# ── 화면 ───────────────────────────────────────────────────
def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for
    from master_db import register_tenant_user, remove_tenant_user

    get_db = h['get_db']
    login_required = h['login_required']
    admin_required = h['admin_required']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')
    need = h.get('need') or (lambda code: admin_required)
    log_audit = h['log_audit']

    def notify(uid, title, content, link):
        get_db().execute('INSERT INTO notifications (user_id, type, category, title, content, link) '
                         'VALUES (?,?,?,?,?,?)', (uid, 'action', 'tenure', title, content, link))

    swept = {}

    @app.before_request
    def tenure_daily_sweep():
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='tenure_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('tenure_sweep_date', ?)",
                           (today,))
                db.commit()
                sweep(db, notify)
            swept[k] = today
        except Exception as e:
            app.logger.error('tenure sweep failed: %s', e)

    def can_see(emp_id):
        if can('tenure') or session.get('user_id') == emp_id:
            return True
        if session.get('user_role') == 'manager':
            r = get_db().execute('SELECT manager_id FROM users WHERE id=?', (emp_id,)).fetchone()
            return bool(r and r['manager_id'] == session.get('user_id'))
        return False

    def view(emp_id):
        db = get_db()
        ensure_schema(db)
        p = periods(db, emp_id)
        if p is None:
            return None
        return dict(periods=p, prob=probation_of(db, emp_id), careers=careers_of(db, emp_id),
                    default_months=default_months(db), status=PROB_STATUS)

    # 직원 상세 [근속·수습] 탭: {% set tn = tenure_view(emp.id) %}
    app.jinja_env.globals['tenure_view'] = view

    def dup_warning(uid):
        """직원 등록 직후 부르는 쪽에서: 중복 의심이 있으면 안내 문구."""
        db = get_db()
        ds = duplicates(db, only=uid)
        if not ds:
            return None
        o = [d['b'] if d['a']['id'] == uid else d['a'] for d in ds]
        return '같은 사람일 수 있는 계정이 있습니다: %s. 인사 > 재입사·수습 > 중복 의심에서 확인하세요. 돌아온 퇴사자라면 새 계정 대신 재입사 처리를 쓰세요.' % ', '.join(
            '%s(%s)' % (x['name'], x['emp_no'] or x['email']) for x in o[:3])

    app.config['TENURE_DUP_WARNING'] = dup_warning

    def back(emp_id):
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-tenure')

    def demo_block():
        if session.get('demo_mode'):
            flash('데모에서는 저장되지 않습니다.', 'warning')
            return True
        return False

    @app.route('/employees/<int:emp_id>/tenure', methods=['POST'])
    @need('tenure')
    def tenure_save(emp_id):
        if demo_block():
            return back(emp_id)
        db = get_db()
        ensure_schema(db)
        if not db.execute('SELECT 1 FROM users WHERE id=?', (emp_id,)).fetchone():
            abort(404)
        f = request.form
        act = f.get('action') or ''
        aid, an = session['user_id'], session.get('user_name')
        if act == 'prob_start':
            err = start_probation(db, emp_id, f.get('start_date'), f.get('months'), f.get('end_date'), aid, an)
            ok = '수습을 시작했습니다.'
        elif act == 'prob_extend':
            err = extend_probation(db, emp_id, f.get('end_date'), f.get('reason'), aid, an)
            ok = '수습을 연장했습니다.'
        elif act == 'prob_decide':
            err = decide_probation(db, emp_id, f.get('result'), f.get('note'), aid, an)
            ok = '수습 결과를 저장했습니다.'
        elif act == 'career':
            err = add_career(db, emp_id, f, aid, an)
            ok = '저장했습니다.'
        elif act == 'career_delete':
            err = None if delete_career(db, emp_id, f.get('career_id', type=int), aid, an) else '찾을 수 없습니다.'
            ok = '지웠습니다.'
        else:
            abort(400)
        if err:
            db.rollback()
            flash(err, 'error')
            return back(emp_id)
        db.commit()
        log_audit('update', 'personal_info', emp_id, '근속·수습 %s' % act)
        flash(ok, 'success')
        if act == 'prob_decide' and f.get('result') == 'failed':
            flash('미통과로 정했습니다. 퇴사 처리는 직원 상세의 [퇴사 처리]에서 이어서 하세요.', 'warning')
        return back(emp_id)

    @app.route('/employees/<int:emp_id>/rehire', methods=['GET', 'POST'])
    @need('tenure')
    def employee_rehire(emp_id):
        db = get_db()
        ensure_schema(db)
        u = db.execute('SELECT * FROM users WHERE id=?', (emp_id,)).fetchone()
        if not u:
            abort(404)
        if u['status'] != 'resigned':
            flash('퇴사한 직원만 재입사 처리할 수 있습니다.', 'warning')
            return back(emp_id)
        error = None
        if request.method == 'POST':
            if demo_block():
                return back(emp_id)
            error = rehire(db, emp_id, request.form, session['user_id'], session.get('user_name'),
                           session.get('tenant_id', 1), register_tenant_user)
            if not error:
                db.commit()
                log_audit('update', 'personal_info', emp_id, '재입사 %s' % request.form.get('hire_date'))
                flash('%s 님을 재입사 처리했습니다. 사번 %s 그대로, 입사일 %s.' % (
                    u['name'], u['emp_no'] or '-', request.form.get('hire_date')), 'success')
                return back(emp_id)
            db.rollback()
        return render_template(
            'tenure/rehire.html', emp=u, error=error, form=request.form, ex=last_exit(db, emp_id),
            periods=periods(db, emp_id), default_months=default_months(db), emp_types=EMP_LABEL,
            depts=org_admin.active_depts(db, cols='id, name'),
            poses=db.execute('SELECT id, name FROM positions ORDER BY id').fetchall(),
            managers=db.execute("SELECT id, name, emp_no FROM users WHERE status='active' AND role!='guest' "
                                'ORDER BY name').fetchall(),
            active_page='employees')

    @app.route('/people/tenure', methods=['GET', 'POST'])
    @need('tenure')
    def tenure_admin():
        db = get_db()
        ensure_schema(db)
        aid, an = session['user_id'], session.get('user_name')
        tab = request.values.get('tab') or 'prob'
        if request.method == 'POST':
            if demo_block():
                return redirect(url_for('tenure_admin', tab=tab))
            act = request.form.get('action')
            if act == 'months':
                m = request.form.get('months', type=int)
                if m is None or not 0 <= m <= 12:
                    flash('수습 기본 기간은 0~12개월입니다.', 'error')
                else:
                    db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('probation_months', ?)",
                               (str(m),))
                    db.commit()
                    flash('수습 기본 기간을 %d개월로 저장했습니다.' % m, 'success')
                return redirect(url_for('tenure_admin', tab='prob'))
            if act == 'merge':
                keep, drop = request.form.get('keep', type=int), request.form.get('drop', type=int)
                err = merge(db, keep, drop, aid, an, session.get('tenant_id', 1), remove_tenant_user)
                if err:
                    db.rollback()
                    flash(err, 'error')
                else:
                    db.commit()
                    log_audit('delete', 'personal_info', drop, '중복 계정 합치기 → %s' % keep)
                    flash('합쳤습니다. 지운 계정의 내용은 합치기 기록에 남겨 두었습니다.', 'success')
                return redirect(url_for('tenure_admin', tab='dup'))
            if act in ('career_check', 'career_commit'):
                fobj = request.files.get('file')
                rows = read_csv(fobj.read()) if fobj and fobj.filename else None
                if not rows:
                    flash('CSV 를 읽지 못했습니다. 양식을 내려받아 UTF-8 또는 엑셀(CP949)로 저장하세요.', 'error')
                    return redirect(url_for('tenure_admin', tab='import'))
                report, ok = import_careers(db, rows, aid, an, commit=(act == 'career_commit'))
                if act == 'career_commit':
                    db.commit()
                    log_audit('create', 'personal_info', None, '경력·옛 이력 CSV %d건' % ok)
                    flash('%d건 넣었습니다. 틀린 줄 %d개는 건너뛰었습니다.' % (ok, len(report) - ok),
                          'success' if ok else 'warning')
                return render_template('tenure/admin.html', tab='import', report=report, ok=ok,
                                       committed=(act == 'career_commit'), active_page='tenure',
                                       counts=_counts(db), columns=CAREER_COLUMNS)
            abort(400)

        if request.args.get('csv') == 'leavers':
            rows = leavers(db, request.args.get('q', '').strip())
            log_audit('download', 'personal_info', None, '퇴사자 목록 CSV')
            return Response(leavers_csv(rows), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=leavers.csv'})
        if request.args.get('csv') == 'careers':
            log_audit('download', 'personal_info', None, '경력·옛 이력 CSV')
            return Response(careers_csv(db), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=prior_careers.csv'})
        if request.args.get('csv') == 'template':
            return Response('﻿' + ','.join(CAREER_COLUMNS) + '\nTC-00001,경력,이전회사(주),개발팀,백엔드 개발,'
                            '2019-03-01,2023-02-28,결제 서버 운영\nTC-00001,옛 이력,,영업1팀,과장 승진,2015-01-01,,'
                            '옛 인사 시스템 발령\n', mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=prior_careers_template.csv'})
        ctx = dict(tab=tab, counts=_counts(db), active_page='tenure', columns=CAREER_COLUMNS)
        if tab == 'prob':
            ctx.update(active=probation_list(db, 'active'), done=probation_list(db, 'done'),
                       default_months=default_months(db))
        elif tab == 'leavers':
            q = request.args.get('q', '').strip()
            ctx.update(rows=leavers(db, q), q=q)
        elif tab == 'dup':
            ctx.update(dups=duplicates(db),
                       merges=db.execute('SELECT * FROM merge_records ORDER BY id DESC LIMIT 20').fetchall())
        return render_template('tenure/admin.html', **ctx)

    def _counts(db):
        return dict(
            prob=db.execute("SELECT COUNT(*) FROM probation_periods WHERE status='active'").fetchone()[0],
            leavers=db.execute("SELECT COUNT(*) FROM users WHERE status='resigned'").fetchone()[0],
            dup=len(_pairs(db)))
