"""단체협약·노조(B7).

  - 협약 대장(collective_agreements): 이름·노조·종류·유효기간·적용 조건. 유효기간은 3년을 넘을 수 없다
    (노동조합법 제32조 제1항). 만료 90일·30일 전·당일에 관리자에게 알림 한 번씩(alert_stage).
  - 적용(agreement_assignments): 조건(부서·고용형태·조직장 제외)으로 자동 + 사람별 예외(agreement_exceptions).
    sync() 가 '지금 적용돼야 할 사람'과 열린 줄을 맞춘다 — 새로 들면 줄을 열고, 빠지면 닫는다(사유 남김).
    닫힌 줄이 곧 이력이다.
  - 일반적 구속력(노동조합법 제35조): 동종 근로자(같은 고용형태) 반수 이상에게 적용되면 안내만 한다.
    사업장은 하나라고 보고 센다. 판단은 노무 검토 몫.
  - 조합원 기록(union_members): 기본 꺼짐(company_settings.union_records_enabled). 켜면 동의 목적
    'union_dues'(조합비 공제, 민감정보 별도 동의)가 생긴다. 노조가 낸 명단으로만 넣고, 동의한 사람만 저장.
    동의를 철회하면 그 사람의 조합원 기록을 지운다. 관리자만 본다(매니저 화면 없음).
  - 협약 조항을 급여·근태에 자동 반영하지 않는다.
"""
import csv
import io
import json
from datetime import date, timedelta

import org_admin

_DDL = (
    '''CREATE TABLE IF NOT EXISTS collective_agreements (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        union_name  TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'collective',
        start_date  TEXT NOT NULL,
        end_date    TEXT NOT NULL,
        scope       TEXT NOT NULL DEFAULT '{}',
        note        TEXT,
        alert_stage INTEGER,
        ended_at    TEXT,
        end_reason  TEXT,
        created_by  INTEGER,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS agreement_assignments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        agreement_id INTEGER NOT NULL REFERENCES collective_agreements(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source       TEXT NOT NULL DEFAULT 'auto',
        start_date   TEXT NOT NULL,
        end_date     TEXT,
        end_reason   TEXT,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_agr_asg_user ON agreement_assignments(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_agr_asg_agr ON agreement_assignments(agreement_id, end_date)',
    '''CREATE TABLE IF NOT EXISTS agreement_exceptions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        agreement_id INTEGER NOT NULL REFERENCES collective_agreements(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        mode         TEXT NOT NULL,
        reason       TEXT NOT NULL,
        created_by   INTEGER,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agreement_id, user_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS union_members (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        union_name  TEXT NOT NULL,
        joined_on   TEXT,
        left_on     TEXT,
        dues_deduct INTEGER NOT NULL DEFAULT 1,
        list_date   TEXT NOT NULL,
        note        TEXT,
        created_by  INTEGER,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT,
        UNIQUE(user_id, union_name)
    )''',
)

KIND_LABEL = {'collective': '단체협약', 'wage': '임금협약', 'supplement': '보충협약'}
STATUS_LABEL = {'upcoming': '시작 전', 'active': '유효', 'expired': '만료', 'ended': '종료'}
ETYPE_LABEL = {'full_time': '정규직', 'part_time': '시간제', 'contract': '계약직', 'intern': '인턴'}
END_REASON = {'scope': '조건에서 빠짐', 'out': '예외(제외)', 'resigned': '퇴사', 'expired': '협약 만료',
              'ended': '협약 종료', 'manual': '예외 해제'}
ALERT_DAYS = (90, 30, 0)
MAX_YEARS = 3
UNION_PURPOSE = dict(
    code='union_dues', title='조합비 공제(노동조합 가입 정보)', kind='optional', sort=21,
    basis='정보주체의 별도 동의(개인정보 보호법 제23조 민감정보)',
    items='가입한 노동조합 이름 · 가입일 · 조합비 공제 여부',
    body='노동조합이 낸 조합원 명단으로 급여에서 조합비를 떼어 노조에 보내는 데만 씁니다. '
         '인사 담당자만 보고, 매니저·동료에게는 보이지 않습니다. 동의하지 않아도 불이익은 없습니다. '
         '철회하면 회사에 있는 가입 정보를 지웁니다.',
    retention='탈퇴·퇴사 후 지체 없이 파기합니다.')
MEMBER_COLUMNS = ('사번', '노조', '가입일', '탈퇴일', '조합비공제', '메모')

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
    for q in _DDL:
        c.execute(q)
    c.commit()
    if k:
        _done.add(k)


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def norm_date(s):
    s = (s or '').strip().replace('.', '-').replace('/', '-')
    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s.split('-')[:3])
        return date(y, m, d).isoformat()
    except (ValueError, TypeError):
        return None


def _plus_years(d, n):
    try:
        return d.replace(year=d.year + n)
    except ValueError:            # 2/29
        return d.replace(year=d.year + n, day=28)


def period_error(start, end):
    if not start or not end:
        return '시작일과 종료일을 넣으세요.'
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s:
        return '종료일이 시작일보다 빠릅니다.'
    if e >= _plus_years(s, MAX_YEARS):
        return '유효기간은 3년을 넘을 수 없습니다(노동조합법 제32조). 종료일을 %s 까지로 정하세요.' % (
            _plus_years(s, MAX_YEARS) - timedelta(days=1)).isoformat()
    return None


# ── 대장 ──────────────────────────────────────────────

def scope_of(a):
    try:
        s = json.loads(a.get('scope') or '{}')
    except ValueError:
        s = {}
    return dict(depts=[int(x) for x in s.get('depts') or []], types=list(s.get('types') or []),
                no_leaders=bool(s.get('no_leaders')))


def status_of(a, today=None):
    today = today or date.today().isoformat()
    if a.get('ended_at'):
        return 'ended'
    if a['start_date'] > today:
        return 'upcoming'
    if a['end_date'] < today:
        return 'expired'
    return 'active'


def _decorate(c, a, today=None):
    today = today or date.today().isoformat()
    a = dict(a)
    a['status'] = status_of(a, today)
    a['status_label'] = STATUS_LABEL[a['status']]
    a['kind_label'] = KIND_LABEL.get(a['kind'], a['kind'])
    a['days'] = (date.fromisoformat(a['end_date']) - date.fromisoformat(today)).days
    a['scope_obj'] = scope_of(a)
    a['scope_label'] = scope_label(c, a['scope_obj'])
    return a


def scope_label(c, s):
    parts = []
    if s['depts']:
        names = {r[0]: r[1] for r in c.execute('SELECT id, name FROM departments')}
        parts.append(' · '.join(names.get(d, '#%d' % d) for d in s['depts']) + ' (하위 포함)')
    else:
        parts.append('전 조직')
    parts.append(' · '.join(ETYPE_LABEL.get(t, t) for t in s['types']) if s['types'] else '모든 고용형태')
    if s['no_leaders']:
        parts.append('조직장 제외')
    return ' / '.join(parts)


def agreements(c):
    ensure_schema(c)
    today = date.today().isoformat()
    out = [_decorate(c, a, today) for a in _rows(c, 'SELECT * FROM collective_agreements ORDER BY end_date DESC, id DESC')]
    if out:
        cnt = {r[0]: r[1] for r in c.execute(
            'SELECT agreement_id, COUNT(*) FROM agreement_assignments WHERE end_date IS NULL GROUP BY agreement_id')}
        for a in out:
            a['applied'] = cnt.get(a['id'], 0)
    return out


def agreement(c, aid):
    ensure_schema(c)
    r = _rows(c, 'SELECT * FROM collective_agreements WHERE id=?', (aid,))
    return _decorate(c, r[0]) if r else None


def _scope_json(depts, types, no_leaders):
    return json.dumps(dict(depts=sorted({int(d) for d in depts if str(d).isdigit()}),
                           types=[t for t in types if t in ETYPE_LABEL], no_leaders=bool(no_leaders)),
                      ensure_ascii=False)


def save(c, form, actor_id, aid=None):
    """폼(dict 처럼 get/getlist) → (id, 오류)."""
    title = (form.get('title') or '').strip()[:120]
    union = (form.get('union_name') or '').strip()[:80]
    kind = form.get('kind') if form.get('kind') in KIND_LABEL else 'collective'
    start, end = norm_date(form.get('start_date')), norm_date(form.get('end_date'))
    if not title or not union:
        return None, '협약 이름과 노조 이름을 넣으세요.'
    err = period_error(start, end)
    if err:
        return None, err
    scope = _scope_json(form.getlist('depts'), form.getlist('types'), form.get('no_leaders'))
    note = (form.get('note') or '').strip()[:500] or None
    if aid:
        old = agreement(c, aid)
        if not old:
            return None, '없는 협약입니다.'
        if old['status'] == 'ended':
            return None, '종료한 협약은 고칠 수 없습니다.'
        stage = old['alert_stage'] if old['end_date'] == end else None   # 종료일이 바뀌면 알림 다시
        c.execute('UPDATE collective_agreements SET title=?, union_name=?, kind=?, start_date=?, end_date=?, scope=?, '
                  'note=?, alert_stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                  (title, union, kind, start, end, scope, note, stage, aid))
    else:
        cur = c.execute('INSERT INTO collective_agreements (title, union_name, kind, start_date, end_date, scope, note, '
                        'created_by) VALUES (?,?,?,?,?,?,?,?)', (title, union, kind, start, end, scope, note, actor_id))
        aid = cur.lastrowid
    sync(c, aid)
    return aid, None


def end_agreement(c, aid, reason):
    a = agreement(c, aid)
    if not a or a['status'] == 'ended':
        return '이미 종료했거나 없는 협약입니다.'
    if not (reason or '').strip():
        return '종료 사유를 넣으세요.'
    c.execute('UPDATE collective_agreements SET ended_at=?, end_reason=? WHERE id=?',
              (date.today().isoformat(), reason.strip()[:200], aid))
    sync(c, aid)
    return None


# ── 적용 ──────────────────────────────────────────────

def _people(c):
    return _rows(c, "SELECT id, name, emp_no, department_id, employment_type, status FROM users "
                    "WHERE status='active' AND role != 'guest'")


def _leaders(c):
    return {r[0] for r in c.execute('SELECT leader_id FROM departments WHERE leader_id IS NOT NULL')}


def matches(c, s, people=None, leaders=None):
    """조건에 맞는 재직자 id 집합."""
    people = _people(c) if people is None else people
    depts = None
    if s['depts']:
        depts = set(s['depts'])
        for d in s['depts']:
            depts |= org_admin._descendants(c, d)
    if s['no_leaders'] and leaders is None:
        leaders = _leaders(c)
    out = set()
    for p in people:
        if depts is not None and p['department_id'] not in depts:
            continue
        if s['types'] and (p['employment_type'] or 'full_time') not in s['types']:
            continue
        if s['no_leaders'] and p['id'] in leaders:
            continue
        out.add(p['id'])
    return out


def exceptions(c, aid):
    return _rows(c, 'SELECT e.*, u.name, u.emp_no FROM agreement_exceptions e JOIN users u ON u.id=e.user_id '
                    'WHERE e.agreement_id=? ORDER BY e.id DESC', (aid,))


def desired(c, a, people=None):
    """(지금 적용돼야 할 id → 'auto'|'manual', 빠지는 이유 {id: 코드})"""
    active = {p['id'] for p in (people if people is not None else _people(c))}
    auto = matches(c, a['scope_obj'], people)
    ex = {e['user_id']: e['mode'] for e in _rows(c, 'SELECT user_id, mode FROM agreement_exceptions WHERE agreement_id=?',
                                                 (a['id'],))}
    want = {}
    for u in auto:
        if ex.get(u) != 'out':
            want[u] = 'auto'
    for u, m in ex.items():
        if m == 'in' and u in active:
            want[u] = 'manual'
    return want, ex, active


def sync(c, aid, today=None):
    """열린 적용 줄을 지금 조건에 맞춘다. 바뀐 줄 수를 돌려준다."""
    today = today or date.today().isoformat()
    a = agreement(c, aid)
    if not a:
        return 0
    open_rows = {r['user_id']: r for r in _rows(
        c, 'SELECT id, user_id, source FROM agreement_assignments WHERE agreement_id=? AND end_date IS NULL', (aid,))}
    st = status_of(a, today)
    n = 0
    if st != 'active':
        why = 'ended' if st == 'ended' else 'expired'
        for r in open_rows.values():   # 시작 전이면 줄이 있을 리 없다
            c.execute('UPDATE agreement_assignments SET end_date=?, end_reason=? WHERE id=?',
                      (a['ended_at'] if st == 'ended' else min(a['end_date'], today), why, r['id']))
            n += 1
        return n
    want, ex, active = desired(c, a)
    first = not c.execute('SELECT 1 FROM agreement_assignments WHERE agreement_id=? LIMIT 1', (aid,)).fetchone()
    hired = {r[0]: r[1] for r in c.execute('SELECT id, hire_date FROM users')} if first else {}
    for u, r in open_rows.items():
        if u in want and want[u] == r['source']:
            continue
        if u not in active:
            why = 'resigned'
        elif u in want:            # auto ↔ manual 만 바뀜: 닫고 새로 연다
            why = 'manual' if r['source'] == 'manual' else 'scope'
        elif ex.get(u) == 'out':
            why = 'out'
        elif r['source'] == 'manual':
            why = 'manual'
        else:
            why = 'scope'
        c.execute('UPDATE agreement_assignments SET end_date=?, end_reason=? WHERE id=?', (today, why, r['id']))
        n += 1
        open_rows[u] = None
    for u, src in want.items():
        r = open_rows.get(u)
        if r and r['source'] == src:
            continue
        # 처음 맞출 때는 협약 시작일(그 뒤 입사면 입사일)부터, 그다음부터 드는 사람은 오늘부터
        start = max(a['start_date'], (hired.get(u) or '')[:10]) if first else max(a['start_date'], today)
        c.execute('INSERT INTO agreement_assignments (agreement_id, user_id, source, start_date) VALUES (?,?,?,?)',
                  (aid, u, src, start))
        n += 1
    return n


def sync_all(c):
    ensure_schema(c)
    n = 0
    for (aid,) in c.execute('SELECT id FROM collective_agreements WHERE ended_at IS NULL OR id IN '
                            '(SELECT agreement_id FROM agreement_assignments WHERE end_date IS NULL)').fetchall():
        n += sync(c, aid)
    return n


def set_exception(c, aid, uid, mode, reason, actor_id):
    if mode not in ('in', 'out'):
        return '예외 종류가 틀렸습니다.'
    if not (reason or '').strip():
        return '예외 사유를 넣으세요.'
    if not c.execute("SELECT 1 FROM users WHERE id=? AND status='active'", (uid,)).fetchone():
        return '재직 중인 직원만 예외로 정할 수 있습니다.'
    c.execute('INSERT INTO agreement_exceptions (agreement_id, user_id, mode, reason, created_by) VALUES (?,?,?,?,?) '
              'ON CONFLICT(agreement_id, user_id) DO UPDATE SET mode=excluded.mode, reason=excluded.reason, '
              'created_by=excluded.created_by, created_at=CURRENT_TIMESTAMP',
              (aid, uid, mode, reason.strip()[:200], actor_id))
    sync(c, aid)
    return None


def drop_exception(c, aid, uid):
    c.execute('DELETE FROM agreement_exceptions WHERE agreement_id=? AND user_id=?', (aid, uid))
    sync(c, aid)


def applied(c, aid, q=''):
    rows = _rows(c, 'SELECT s.*, u.name, u.emp_no, u.employment_type, d.name AS dept FROM agreement_assignments s '
                    'JOIN users u ON u.id=s.user_id LEFT JOIN departments d ON d.id=u.department_id '
                    'WHERE s.agreement_id=? AND s.end_date IS NULL ORDER BY u.emp_no, u.name', (aid,))
    if q:
        rows = [r for r in rows if q in (r['name'] or '') or q in (r['emp_no'] or '') or q in (r['dept'] or '')]
    return rows


def history(c, aid, limit=50):
    rows = _rows(c, 'SELECT s.*, u.name, u.emp_no FROM agreement_assignments s JOIN users u ON u.id=s.user_id '
                    'WHERE s.agreement_id=? AND s.end_date IS NOT NULL ORDER BY s.end_date DESC, s.id DESC LIMIT ?',
                 (aid, limit))
    for r in rows:
        r['why'] = END_REASON.get(r['end_reason'], r['end_reason'] or '')
    return rows


def of_person(c, uid):
    """직원 상세 탭: 그 사람의 적용 줄(열린 것 먼저)."""
    ensure_schema(c)
    rows = _rows(c, 'SELECT s.*, a.title, a.union_name, a.kind, a.end_date AS agr_end FROM agreement_assignments s '
                    'JOIN collective_agreements a ON a.id=s.agreement_id WHERE s.user_id=? '
                    'ORDER BY (s.end_date IS NOT NULL), s.start_date DESC', (uid,))
    ex = {r['agreement_id']: r for r in _rows(c, 'SELECT * FROM agreement_exceptions WHERE user_id=?', (uid,))}
    for r in rows:
        r['why'] = END_REASON.get(r['end_reason'], r['end_reason'] or '')
        r['kind_label'] = KIND_LABEL.get(r['kind'], r['kind'])
        r['exception'] = ex.get(r['agreement_id'])
    outs = [dict(e, title=a['title']) for e in ex.values() if e['mode'] == 'out'
            for a in [agreement(c, e['agreement_id'])] if a]
    return dict(rows=rows, outs=outs)


def binding(c, a, people=None):
    """일반적 구속력 안내(노동조합법 제35조). 동종 = 협약이 정한 고용형태(없으면 전원)."""
    if a['status'] != 'active':
        return None
    people = _people(c) if people is None else people
    types = a['scope_obj']['types']
    same = [p['id'] for p in people if not types or (p['employment_type'] or 'full_time') in types]
    if not same:
        return None
    on = {r[0] for r in c.execute('SELECT user_id FROM agreement_assignments WHERE agreement_id=? AND end_date IS NULL',
                                  (a['id'],))}
    n = len(on & set(same))
    total = len(same)
    return dict(n=n, total=total, rest=total - n, pct=round(n * 100 / total), hit=n * 2 >= total and n < total,
                kind=' · '.join(ETYPE_LABEL.get(t, t) for t in types) if types else '전 직원')


def applied_csv(c, a):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서', '고용형태', '적용', '적용 시작'])
    for r in applied(c, a['id']):
        w.writerow([r['emp_no'] or '', r['name'], r['dept'] or '', ETYPE_LABEL.get(r['employment_type'], ''),
                    '예외(포함)' if r['source'] == 'manual' else '조건', r['start_date']])
    return ('﻿' + buf.getvalue()).encode('utf-8')


# ── 만료 알림 ─────────────────────────────────────────

def sweep(c, notify, today=None):
    """하루 한 번: 적용 맞추기 + 만료 90·30·0일 알림(단계마다 한 번)."""
    ensure_schema(c)
    today = today or date.today().isoformat()
    sync_all(c)
    admins = [r[0] for r in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'")]
    sent = 0
    for a in agreements(c):
        if a['status'] not in ('active', 'upcoming') or a['days'] < 0:
            continue
        stage = next((d for d in sorted(ALERT_DAYS) if a['days'] <= d), None)
        if stage is None or (a['alert_stage'] is not None and a['alert_stage'] <= stage):
            continue
        when = '오늘 만료' if a['days'] == 0 else '%d일 뒤 만료(%s)' % (a['days'], a['end_date'])
        for uid in admins:
            notify(uid, '단체협약 %s' % when, '%s · %s. 새 협약 교섭 일정을 확인하세요.' % (a['title'], a['union_name']),
                   '/people/agreements?id=%d' % a['id'])
        c.execute('UPDATE collective_agreements SET alert_stage=? WHERE id=?', (stage, a['id']))
        sent += 1
    c.commit()
    return sent


# ── 조합원 기록(기본 꺼짐) ────────────────────────────

def union_enabled(c):
    r = c.execute("SELECT value FROM company_settings WHERE key='union_records_enabled'").fetchone()
    return bool(r and str(r[0]) == '1')


def set_union_enabled(c, on):
    import people_consent
    people_consent.ensure_schema(c)
    c.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('union_records_enabled', ?)",
              ('1' if on else '0',))
    p = UNION_PURPOSE
    if c.execute('SELECT 1 FROM consent_purposes WHERE code=?', (p['code'],)).fetchone():
        c.execute('UPDATE consent_purposes SET active=? WHERE code=?', (1 if on else 0, p['code']))
    elif on:
        c.execute('INSERT INTO consent_purposes (code,title,kind,basis,items,body,retention,sort,builtin) '
                  'VALUES (?,?,?,?,?,?,?,?,1)', (p['code'], p['title'], p['kind'], p['basis'], p['items'], p['body'],
                                                p['retention'], p['sort']))


def consented(c):
    """조합비 공제 목적에 지금 판으로 동의한 사람 id 집합."""
    import people_consent
    p = c.execute('SELECT * FROM consent_purposes WHERE code=?', (UNION_PURPOSE['code'],)).fetchone()
    if not p:
        return set()
    last = {}
    for r in c.execute('SELECT user_id, version, action FROM consent_records WHERE purpose_id=? ORDER BY id', (p['id'],)):
        last[r[0]] = r
    return {u for u, r in last.items() if r[2] != 'withdraw' and r[1] == p['version']}


def members(c, q=''):
    ensure_schema(c)
    ok = consented(c)
    rows = _rows(c, 'SELECT m.*, u.name, u.emp_no, u.status, d.name AS dept FROM union_members m '
                    'JOIN users u ON u.id=m.user_id LEFT JOIN departments d ON d.id=u.department_id '
                    'ORDER BY m.union_name, u.emp_no')
    for r in rows:
        r['consent'] = r['user_id'] in ok
    if q:
        rows = [r for r in rows if q in (r['name'] or '') or q in (r['emp_no'] or '') or q in r['union_name']]
    return rows


def member_of(c, uid):
    ensure_schema(c)
    return _rows(c, 'SELECT * FROM union_members WHERE user_id=? ORDER BY union_name', (uid,))


def forget_member(c, uid):
    """동의 철회 → 그 사람의 조합원 기록을 지운다. 지운 줄 수."""
    ensure_schema(c)
    return c.execute('DELETE FROM union_members WHERE user_id=?', (uid,)).rowcount


def read_csv(raw):
    for enc in ('utf-8-sig', 'cp949'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    head = [h.strip() for h in rows[0]]
    if '사번' not in head:
        return None
    return [dict(zip(head, [x.strip() for x in r])) for r in rows[1:] if any(x.strip() for x in r)]


def import_members(c, rows, list_date, actor_id, commit=False):
    """노조가 낸 명단 → (줄별 결과, 넣은 수). 동의 없는 사람은 넣지 않는다."""
    ensure_schema(c)
    ok_ids = consented(c)
    by_no = {r[1]: r[0] for r in c.execute("SELECT id, emp_no FROM users WHERE emp_no IS NOT NULL AND emp_no != ''")}
    report, n = [], 0
    for i, r in enumerate(rows, start=2):
        no, union = r.get('사번', ''), (r.get('노조') or '').strip()[:80]
        uid = by_no.get(no)
        err = None
        joined, left = norm_date(r.get('가입일')), norm_date(r.get('탈퇴일'))
        if not uid:
            err = '없는 사번'
        elif not union:
            err = '노조 이름 없음'
        elif uid not in ok_ids:
            err = '조합비 공제 동의 없음 — 넣지 않음'
        elif (r.get('가입일') and not joined) or (r.get('탈퇴일') and not left):
            err = '날짜 형식(YYYY-MM-DD)'
        dues = 0 if (r.get('조합비공제') or '').strip() in ('N', 'n', '아니오', '0', 'X', 'x') else 1
        report.append(dict(line=i, emp_no=no, union=union, err=err))
        if err:
            continue
        n += 1
        if commit:
            c.execute('INSERT INTO union_members (user_id, union_name, joined_on, left_on, dues_deduct, list_date, note, '
                      'created_by) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id, union_name) DO UPDATE SET '
                      'joined_on=excluded.joined_on, left_on=excluded.left_on, dues_deduct=excluded.dues_deduct, '
                      'list_date=excluded.list_date, note=excluded.note, updated_at=CURRENT_TIMESTAMP',
                      (uid, union, joined, left, dues, list_date, (r.get('메모') or '')[:200] or None, actor_id))
    return report, n


def members_csv(c):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(MEMBER_COLUMNS) + ['명단 날짜'])
    for r in members(c):
        w.writerow([r['emp_no'] or '', r['union_name'], r['joined_on'] or '', r['left_on'] or '',
                    'Y' if r['dues_deduct'] else 'N', r['note'] or '', r['list_date']])
    return ('﻿' + buf.getvalue()).encode('utf-8')


# ── 화면 ──────────────────────────────────────────────

def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    admin_required = h['admin_required']
    log_audit = h['log_audit']

    def notify(uid, title, content, link):
        get_db().execute('INSERT INTO notifications (user_id, type, category, title, content, link) '
                         'VALUES (?,?,?,?,?,?)', (uid, 'action', 'labor', title, content, link))

    swept = {}

    @app.before_request
    def labor_daily_sweep():
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='labor_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('labor_sweep_date', ?)",
                           (today,))
                db.commit()
                sweep(db, notify)
            swept[k] = today
        except Exception as e:
            app.logger.error('labor sweep failed: %s', e)

    def view(emp_id):
        """직원 상세 [단체협약] 탭 — 관리자만 부른다."""
        if session.get('user_role') != 'admin':
            return None
        db = get_db()
        ensure_schema(db)
        sync_all(db)
        db.commit()
        on = union_enabled(db)
        return dict(agr=of_person(db, emp_id), union_on=on,
                    members=member_of(db, emp_id) if on else [],
                    consent=(emp_id in consented(db)) if on else False,
                    agreements=[a for a in agreements(db) if a['status'] in ('active', 'upcoming')])

    app.jinja_env.globals['labor_view'] = view

    def demo_block():
        if session.get('demo_mode'):
            flash('데모에서는 저장되지 않습니다.', 'warning')
            return True
        return False

    @app.route('/employees/<int:emp_id>/agreements', methods=['POST'])
    @admin_required
    def labor_person_save(emp_id):
        back = redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-labor')
        if demo_block():
            return back
        db = get_db()
        ensure_schema(db)
        act = request.form.get('action')
        aid = request.form.get('agreement_id', type=int)
        if act in ('in', 'out'):
            err = set_exception(db, aid, emp_id, act, request.form.get('reason'), session['user_id'])
        elif act == 'drop':
            drop_exception(db, aid, emp_id)
            err = None
        else:
            abort(400)
        if err:
            db.rollback()
            flash(err, 'error')
        else:
            db.commit()
            flash('적용을 바꿨습니다.', 'success')
        return back

    @app.route('/people/agreements', methods=['GET', 'POST'])
    @admin_required
    def labor_admin():
        db = get_db()
        ensure_schema(db)
        aid_ = session['user_id']
        tab = request.values.get('tab') or 'list'
        sel = request.values.get('id', type=int)
        if request.method == 'POST':
            if demo_block():
                return redirect(url_for('labor_admin', tab=tab, id=sel))
            act = request.form.get('action')
            if act == 'save':
                new_id, err = save(db, request.form, aid_, sel)
                if err:
                    db.rollback()
                    flash(err, 'error')
                    return redirect(url_for('labor_admin', tab='new') if not sel else url_for('labor_admin', id=sel))
                db.commit()
                flash('협약을 저장했습니다.', 'success')
                return redirect(url_for('labor_admin', id=new_id))
            if act == 'end':
                err = end_agreement(db, sel, request.form.get('reason'))
                (db.rollback if err else db.commit)()
                flash(err or '협약을 종료했습니다. 적용 줄은 모두 닫혔습니다.', 'error' if err else 'success')
                return redirect(url_for('labor_admin', id=sel))
            if act == 'delete':
                a = agreement(db, sel)
                if a and not db.execute('SELECT 1 FROM agreement_assignments WHERE agreement_id=? LIMIT 1',
                                        (sel,)).fetchone():
                    db.execute('DELETE FROM agreement_exceptions WHERE agreement_id=?', (sel,))
                    db.execute('DELETE FROM collective_agreements WHERE id=?', (sel,))
                    db.commit()
                    flash('지웠습니다.', 'success')
                    return redirect(url_for('labor_admin'))
                flash('한 번이라도 적용된 협약은 지울 수 없습니다. [종료]를 쓰세요.', 'error')
                return redirect(url_for('labor_admin', id=sel))
            if act in ('ex_in', 'ex_out', 'ex_drop'):
                no = (request.form.get('emp_no') or '').strip()
                u = db.execute('SELECT id FROM users WHERE emp_no=?', (no,)).fetchone() if no else None
                uid = u['id'] if u else request.form.get('user_id', type=int)
                if not uid:
                    flash('사번을 찾지 못했습니다.', 'error')
                    return redirect(url_for('labor_admin', id=sel))
                if act == 'ex_drop':
                    drop_exception(db, sel, uid)
                    err = None
                else:
                    err = set_exception(db, sel, uid, act[3:], request.form.get('reason'), aid_)
                (db.rollback if err else db.commit)()
                flash(err or '예외를 저장했습니다.', 'error' if err else 'success')
                return redirect(url_for('labor_admin', id=sel))
            if act == 'union_toggle':
                on = request.form.get('on') == '1'
                set_union_enabled(db, on)
                db.commit()
                log_audit('update', 'personal_info', None, '조합원 기록 %s' % ('켬' if on else '끔'))
                flash('조합원 기록을 켰습니다. 조합비 공제 동의를 받은 사람만 넣을 수 있습니다.' if on else
                      '조합원 기록을 껐습니다. 이미 넣은 기록은 남아 있고 화면에서만 감춥니다.', 'success')
                return redirect(url_for('labor_admin', tab='union'))
            if act in ('union_check', 'union_commit'):
                if not union_enabled(db):
                    abort(403)
                ld = norm_date(request.form.get('list_date'))
                fobj = request.files.get('file')
                rows = read_csv(fobj.read()) if fobj and fobj.filename else None
                if not ld or not rows:
                    flash('명단 날짜와 CSV(사번 칸 필수)를 넣으세요.', 'error')
                    return redirect(url_for('labor_admin', tab='union'))
                report, ok = import_members(db, rows, ld, aid_, commit=(act == 'union_commit'))
                if act == 'union_commit':
                    db.commit()
                    log_audit('create', 'personal_info', None, '노조 조합원 명단 %d건(%s)' % (ok, ld))
                    flash('%d건 넣었습니다. 넣지 않은 줄 %d개.' % (ok, len(report) - ok), 'success' if ok else 'warning')
                return render_template('labor/admin.html', tab='union', report=report, ok=ok, list_date=ld,
                                       committed=(act == 'union_commit'), union_on=True, members=members(db),
                                       counts=_counts(db), active_page='labor', columns=MEMBER_COLUMNS)
            if act == 'union_drop':
                if not union_enabled(db):
                    abort(403)
                db.execute('DELETE FROM union_members WHERE id=?', (request.form.get('mid', type=int),))
                db.commit()
                flash('지웠습니다.', 'success')
                return redirect(url_for('labor_admin', tab='union'))
            abort(400)

        csvq = request.args.get('csv')
        if csvq == 'applied' and sel:
            a = agreement(db, sel)
            if not a:
                abort(404)
            log_audit('download', 'personal_info', None, '단체협약 적용 대상 CSV #%d' % sel)
            return Response(applied_csv(db, a), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=agreement_%d.csv' % sel})
        if csvq in ('members', 'template'):
            if not union_enabled(db):
                abort(403)
            if csvq == 'template':
                return Response('﻿' + ','.join(MEMBER_COLUMNS) + '\nTC-00001,OO노동조합,2024-03-01,,Y,\n',
                                mimetype='text/csv; charset=utf-8',
                                headers={'Content-Disposition': 'attachment; filename=union_members_template.csv'})
            log_audit('download', 'personal_info', None, '노조 조합원 명단 CSV')
            return Response(members_csv(db), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=union_members.csv'})

        sync_all(db)
        db.commit()
        ctx = dict(tab=tab, counts=_counts(db), active_page='labor', union_on=union_enabled(db),
                   kinds=KIND_LABEL, etypes=ETYPE_LABEL, columns=MEMBER_COLUMNS, max_years=MAX_YEARS)
        if sel:
            a = agreement(db, sel)
            if not a:
                abort(404)
            q = request.args.get('q', '').strip()
            ctx.update(tab='detail', a=a, rows=applied(db, sel, q), q=q, ex=exceptions(db, sel),
                       hist=history(db, sel), bind=binding(db, a),
                       depts=org_admin.active_depts(db, cols='id, name'))
        elif tab == 'new':
            ctx.update(depts=org_admin.active_depts(db, cols='id, name'))
        elif tab == 'union':
            if ctx['union_on']:
                q = request.args.get('q', '').strip()
                ctx.update(members=members(db, q), q=q)
        else:
            ctx.update(tab='list', items=agreements(db))
        return render_template('labor/admin.html', **ctx)

    def _counts(db):
        items = agreements(db)
        return dict(list=len(items), soon=sum(1 for a in items if a['status'] == 'active' and a['days'] <= 90),
                    members=db.execute('SELECT COUNT(*) FROM union_members').fetchone()[0])
