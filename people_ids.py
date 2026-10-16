"""신분 정보·서류·장애(B4).

  - 주민등록번호·여권·면허 번호 같은 고유식별정보는 암호화해 저장한다(개인정보 보호법 제24조).
      열쇠 = 환경 변수 HR_FIELD_KEY(운영은 서버 .env). 저장소·DB 에는 없다. 잃으면 못 읽는다.
      열쇠를 바꿀 때는 새 열쇠를 HR_FIELD_KEY, 옛 열쇠를 HR_FIELD_KEY_OLD 에 두면 둘 다 읽힌다.
  - 화면에는 가린 값(900101-1******)만. 전체 번호는 관리자가 사유를 적어야 보이고 감사 로그에 남는다.
    본인은 자기 번호를 사유 없이 볼 수 있지만 그것도 감사 로그에 남는다.
  - 변경 이력(B1)에도 가린 값만 남긴다.
  - 여권·비자·외국인등록증·운전면허·기타 신분증 = person_ids. 자격증은 원래 표(employee_certs).
    둘 다 만료 30일·7일 전, 만료일에 본인에게 알림, 관리자에게는 하루 한 번 묶어서 알림.
  - 장애인 의무고용(장애인고용촉진 및 직업재활법 제28조·제28조의3, 2026-10-16 확인):
      상시근로자 50명 이상만 의무, 100명 이상이면 부담금. 의무고용률은 연도별 표(코드에 박지 않음).
      중증 장애인은 월 소정근로 60시간 이상이면 2명, 60시간 미만이면 1명.
      경증 장애인은 월 60시간 미만이면 세지 않는다(상시근로자에서도 빠진다).
      상시근로자 = 그해 매달 인원의 평균(소수점 버림). 화면 숫자는 참고용 추정.
  - 사람은 사번으로 묶는다 — CSV 의 열쇠가 사번.
"""
import base64
import csv
import hashlib
import hmac
import io
import os
import re
import sqlite3
from datetime import date, timedelta

import people_history

USER_COLS = (
    ('rrn_enc', 'TEXT'), ('rrn_mask', 'TEXT'), ('rrn_hash', 'TEXT'),
    ('disability_level', 'TEXT'),          # NULL/'' = 해당 없음 · mild 경증 · severe 중증
    ('disability_type', 'TEXT'),
    ('disability_since', 'TEXT'),          # 장애인 등록일(모르면 비움 = 입사 때부터로 셈)
    ('disability_short', 'INTEGER DEFAULT 0'),  # 월 소정근로 60시간 미만
)

KINDS = (
    ('passport', '여권'), ('visa', '비자·체류 자격'), ('alien', '외국인등록증'),
    ('driver', '운전면허'), ('other', '기타 신분증'),
)
KIND_LABEL = dict(KINDS)
LEVELS = (('mild', '경증'), ('severe', '중증'))
LEVEL_LABEL = dict(LEVELS)

ALERT_DAYS = (30, 7, 0)   # 이 날짜에 닿으면 알림 한 번씩

# 민간 사업주 의무고용률(%) — 2026 3.1 / 2027·2028 3.3 / 2029~ 3.5 (시행령 개정, 2026 보도 확인)
RATE_SEED = ((2026, 3.1), (2027, 3.3), (2028, 3.3), (2029, 3.5))
QUOTA_MIN = 50    # 의무고용 대상
LEVY_MIN = 100    # 부담금 대상

_DDL = (
    '''CREATE TABLE IF NOT EXISTS person_ids (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK(kind IN ('passport','visa','alien','driver','other')),
        label       TEXT,
        number_enc  TEXT,
        number_mask TEXT,
        country     TEXT,
        issuer      TEXT,
        issued_date TEXT,
        expiry_date TEXT,
        note        TEXT,
        alert_stage INTEGER,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_person_ids_user ON person_ids(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_person_ids_exp ON person_ids(expiry_date)',
    '''CREATE TABLE IF NOT EXISTS disability_quota_rates (
        year  INTEGER PRIMARY KEY,
        rate  REAL NOT NULL
    )''',
)

people_history.LABEL.update({'rrn': '주민등록번호', 'disability': '장애 정보', 'id_doc': '신분 서류'})
people_history.PERSONAL.update({'rrn', 'disability', 'id_doc'})
people_history.SOURCE_LABEL.setdefault('ids', '신분·서류')

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
    have = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    if not have:
        return
    for col, typ in USER_COLS:
        if col not in have:
            _add(c, 'users', col, typ)
    certs = {r[1] for r in c.execute('PRAGMA table_info(employee_certs)').fetchall()}
    if certs and 'alert_stage' not in certs:
        _add(c, 'employee_certs', 'alert_stage', 'INTEGER')
    for q in _DDL:
        c.execute(q)
    for y, r in RATE_SEED:
        c.execute('INSERT OR IGNORE INTO disability_quota_rates (year, rate) VALUES (?,?)', (y, r))
    c.commit()
    if k:
        _done.add(k)


def _add(c, table, col, typ):
    try:
        c.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, typ))
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e).lower():   # 워커 두 개가 동시에 뜰 때
            raise


# ── 열쇠·암호화 ─────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))


def _env(name):
    v = os.environ.get(name)
    if v:
        return v.strip()
    # 로컬 개발 서버는 .env 를 읽지 않으므로 직접 본다(운영은 systemd 가 넣어 준다).
    try:
        with open(os.path.join(_BASE, '.env'), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _fernet():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        return None
    keys = [k for k in (_env('HR_FIELD_KEY'), _env('HR_FIELD_KEY_OLD')) if k]
    if not keys:
        return None
    try:
        fs = [Fernet(k.encode()) for k in keys]
    except Exception:
        return None
    return MultiFernet(fs)


def ready():
    """암호화 열쇠가 있어 고유식별정보를 저장할 수 있는가."""
    return _fernet() is not None


def encrypt(s):
    f = _fernet()
    if f is None:
        raise RuntimeError('no-key')
    return f.encrypt(s.encode('utf-8')).decode('ascii')


def decrypt(tok):
    f = _fernet()
    if f is None or not tok:
        return None
    try:
        return f.decrypt(tok.encode('ascii')).decode('utf-8')
    except Exception:
        return None


def _hash(digits):
    """같은 번호 찾기용(복호화 없이). 열쇠에서 뽑은 별도 값으로 HMAC."""
    k = _env('HR_FIELD_KEY') or ''
    salt = hashlib.sha256(('rrn-dup:' + k).encode()).digest()
    return base64.urlsafe_b64encode(hmac.new(salt, digits.encode(), hashlib.sha256).digest()).decode()[:43]


# ── 주민등록번호 ────────────────────────────────────────────
def rrn_check(raw):
    """(숫자 13자리, 생년월일 'YYYY-MM-DD') 또는 (None, 오류 문구)."""
    d = re.sub(r'\D', '', raw or '')
    if len(d) != 13:
        return None, '주민등록번호는 숫자 13자리입니다.'
    g = d[6]
    century = {'1': 1900, '2': 1900, '5': 1900, '6': 1900, '3': 2000, '4': 2000, '7': 2000, '8': 2000,
               '9': 1800, '0': 1800}[g]
    try:
        b = date(century + int(d[:2]), int(d[2:4]), int(d[4:6]))
    except ValueError:
        return None, '앞 6자리가 올바른 생년월일이 아닙니다.'
    return d, b.isoformat()


def rrn_mask(d):
    return '%s-%s******' % (d[:6], d[6])


def rrn_fmt(d):
    return '%s-%s' % (d[:6], d[6:]) if d and len(d) == 13 else d


def num_mask(s):
    """여권·면허 번호: 앞 2글자·뒤 2글자만 남기고 가운데 글자·숫자를 가린다."""
    s = (s or '').strip()
    idx = [i for i, ch in enumerate(s) if ch.isalnum()]
    if len(idx) <= 4:
        keep = set(idx[:1])
    else:
        keep = set(idx[:2] + idx[-2:])
    return ''.join(ch if (not ch.isalnum() or i in keep) else '*' for i, ch in enumerate(s))


def set_rrn(c, uid, raw, actor_id, actor_name):
    """저장. 오류 문구 또는 None."""
    d, b = rrn_check(raw)
    if d is None:
        return b
    if not ready():
        return '암호화 열쇠가 설정되지 않아 저장할 수 없습니다. 관리자에게 알려 주세요.'
    h = _hash(d)
    dup = c.execute('SELECT emp_no, name FROM users WHERE rrn_hash=? AND id<>?', (h, uid)).fetchone()
    if dup:
        return '이미 다른 직원(%s)에게 등록된 번호입니다.' % (dup['emp_no'] or dup['name'])
    u = c.execute('SELECT birth_date, rrn_mask FROM users WHERE id=?', (uid,)).fetchone()
    if u['birth_date'] and str(u['birth_date'])[:10] != b:
        return '생년월일(%s)과 번호 앞자리(%s)가 다릅니다. 생년월일을 먼저 확인하세요.' % (str(u['birth_date'])[:10], b)
    m = rrn_mask(d)
    c.execute('UPDATE users SET rrn_enc=?, rrn_mask=?, rrn_hash=? WHERE id=?', (encrypt(d), m, h, uid))
    if not u['birth_date']:
        c.execute('UPDATE users SET birth_date=? WHERE id=?', (b, uid))
        people_history.record_change(c, uid, 'birth_date', None, b, 'ids', actor_id, actor_name,
                                     reason='주민등록번호에서 채움')
    if u['rrn_mask'] != m:
        people_history.record_change(c, uid, 'rrn', u['rrn_mask'], m, 'ids', actor_id, actor_name)
    return None


def clear_rrn(c, uid, actor_id, actor_name):
    u = c.execute('SELECT rrn_mask FROM users WHERE id=?', (uid,)).fetchone()
    if u and u['rrn_mask']:
        c.execute('UPDATE users SET rrn_enc=NULL, rrn_mask=NULL, rrn_hash=NULL WHERE id=?', (uid,))
        people_history.record_change(c, uid, 'rrn', u['rrn_mask'], None, 'ids', actor_id, actor_name)


# ── 장애 ───────────────────────────────────────────────────
def dis_summary(level, typ, short):
    if not level:
        return None
    s = LEVEL_LABEL.get(level, level)
    if typ:
        s += ' · ' + typ
    if short:
        s += ' · 월 60시간 미만'
    return s


def set_disability(c, uid, level, typ, since, short, actor_id, actor_name):
    level = level if level in LEVEL_LABEL else None
    typ = (typ or '').strip()[:40] or None
    since = (since or '').strip() or None
    if since:
        try:
            date.fromisoformat(since)
        except ValueError:
            return '등록일 형식이 올바르지 않습니다.'
    short = 1 if (short and level) else 0
    if not level:
        typ, since = None, None
    u = c.execute('SELECT disability_level, disability_type, disability_short FROM users WHERE id=?', (uid,)).fetchone()
    old = dis_summary(u['disability_level'], u['disability_type'], u['disability_short'])
    new = dis_summary(level, typ, short)
    c.execute('UPDATE users SET disability_level=?, disability_type=?, disability_since=?, disability_short=? '
              'WHERE id=?', (level, typ, since, short, uid))
    if old != new:
        people_history.record_change(c, uid, 'disability', old or '해당 없음', new or '해당 없음', 'ids',
                                     actor_id, actor_name)
    return None


# ── 서류 ───────────────────────────────────────────────────
def docs_of(c, uid):
    return [dict(r) for r in c.execute(
        'SELECT * FROM person_ids WHERE user_id=? ORDER BY CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END, '
        'expiry_date, id', (uid,)).fetchall()]


def _doc_line(kind, label, mask, exp):
    s = KIND_LABEL.get(kind, kind)
    if label:
        s += ' ' + label
    if mask:
        s += ' ' + mask
    if exp:
        s += ' (~%s)' % exp
    return s


def save_doc(c, uid, f, actor_id, actor_name, doc_id=None):
    kind = f.get('kind') or ''
    if kind not in KIND_LABEL:
        return '종류를 고르세요.'
    label = (f.get('label') or '').strip()[:60] or None
    country = (f.get('country') or '').strip()[:40] or None
    issuer = (f.get('issuer') or '').strip()[:60] or None
    note = (f.get('note') or '').strip()[:200] or None
    dates = {}
    for k in ('issued_date', 'expiry_date'):
        v = (f.get(k) or '').strip() or None
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                return '날짜 형식이 올바르지 않습니다.'
        dates[k] = v
    if kind in ('passport', 'visa', 'alien') and not dates['expiry_date']:
        return '%s은 만료일이 있어야 알림을 보낼 수 있습니다.' % KIND_LABEL[kind]
    num = (f.get('number') or '').strip()[:40]
    old = None
    if doc_id:
        old = c.execute('SELECT * FROM person_ids WHERE id=? AND user_id=?', (doc_id, uid)).fetchone()
        if not old:
            return '서류를 찾지 못했습니다.'
    enc, mask = (old['number_enc'], old['number_mask']) if old else (None, None)
    if num:
        if not ready():
            return '암호화 열쇠가 설정되지 않아 번호를 저장할 수 없습니다. 번호 없이 저장하거나 관리자에게 알려 주세요.'
        enc, mask = encrypt(num), num_mask(num)
    if old:
        stage = old['alert_stage'] if old['expiry_date'] == dates['expiry_date'] else None
        c.execute('UPDATE person_ids SET kind=?, label=?, number_enc=?, number_mask=?, country=?, issuer=?, '
                  'issued_date=?, expiry_date=?, note=?, alert_stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                  (kind, label, enc, mask, country, issuer, dates['issued_date'], dates['expiry_date'], note,
                   stage, doc_id))
        a = _doc_line(old['kind'], old['label'], old['number_mask'], old['expiry_date'])
    else:
        c.execute('INSERT INTO person_ids (user_id, kind, label, number_enc, number_mask, country, issuer, '
                  'issued_date, expiry_date, note) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (uid, kind, label, enc, mask, country, issuer, dates['issued_date'], dates['expiry_date'], note))
        a = None
    b = _doc_line(kind, label, mask, dates['expiry_date'])
    if a != b:
        people_history.record_change(c, uid, 'id_doc', a, b, 'ids', actor_id, actor_name)
    return None


def delete_doc(c, uid, doc_id, actor_id, actor_name):
    r = c.execute('SELECT * FROM person_ids WHERE id=? AND user_id=?', (doc_id, uid)).fetchone()
    if not r:
        return False
    c.execute('DELETE FROM person_ids WHERE id=?', (doc_id,))
    people_history.record_change(c, uid, 'id_doc', _doc_line(r['kind'], r['label'], r['number_mask'],
                                                              r['expiry_date']), None, 'ids', actor_id, actor_name)
    return True


def left_label(days):
    if days is None:
        return ''
    if days < 0:
        return '%d일 지남' % -days
    if days == 0:
        return '오늘 만료'
    return '%d일 남음' % days


def expiring(c, within=90, today=None):
    """재직자의 서류·자격증 중 within 일 안에 끝나거나 이미 끝난 것. 사번 포함."""
    today = today or date.today()
    end = (today + timedelta(days=within)).isoformat()
    rows = []
    for r in c.execute(
            "SELECT p.id, p.kind, p.label, p.country, p.expiry_date, u.id uid, u.emp_no, u.name, d.name dept "
            "FROM person_ids p JOIN users u ON u.id=p.user_id LEFT JOIN departments d ON d.id=u.department_id "
            "WHERE u.status='active' AND p.expiry_date IS NOT NULL AND p.expiry_date<=?", (end,)).fetchall():
        x = dict(r)
        x['what'] = KIND_LABEL.get(x['kind'], x['kind']) + (' · ' + x['label'] if x['label'] else '')
        rows.append(x)
    for r in c.execute(
            "SELECT e.id, e.cert_name, e.issued_by, e.expiry_date, u.id uid, u.emp_no, u.name, d.name dept "
            "FROM employee_certs e JOIN users u ON u.id=e.user_id LEFT JOIN departments d ON d.id=u.department_id "
            "WHERE u.status='active' AND e.expiry_date IS NOT NULL AND e.expiry_date<=?", (end,)).fetchall():
        x = dict(r)
        x['kind'] = 'cert'
        x['what'] = '자격증 · ' + x['cert_name']
        x['country'] = None
        rows.append(x)
    for x in rows:
        x['days'] = (date.fromisoformat(str(x['expiry_date'])[:10]) - today).days
        x['left'] = left_label(x['days'])
    rows.sort(key=lambda x: (x['days'], x['name']))
    return rows


def _stage(days):
    """지금 닿은 알림 칸(30/7/0) 또는 None."""
    hit = [s for s in ALERT_DAYS if days <= s]
    return min(hit) if hit else None


def sweep(c, notify, today=None, only_uid=None, admin_link='/people/ids'):
    """만료 알림. 칸마다 한 번만(alert_stage). 본인 알림 수를 돌려준다.
    notify(user_id, title, content, link) — commit 은 여기서."""
    ensure_schema(c)
    today = today or date.today()
    sent = 0
    news = []
    q_uid = ' AND u.id=?' if only_uid else ''
    args = (only_uid,) if only_uid else ()
    for table, what in (('person_ids', None), ('employee_certs', '자격증')):
        if table == 'person_ids':
            sel = 'SELECT p.id, p.kind, p.label, p.expiry_date, p.alert_stage, u.id uid, u.name FROM person_ids p '
        else:
            sel = 'SELECT p.id, p.cert_name label, p.expiry_date, p.alert_stage, u.id uid, u.name FROM employee_certs p '
        for r in c.execute(sel + "JOIN users u ON u.id=p.user_id WHERE u.status='active' AND p.expiry_date IS NOT NULL "
                           "AND p.expiry_date<=?" + q_uid,
                           ((today + timedelta(days=max(ALERT_DAYS))).isoformat(),) + args).fetchall():
            try:
                days = (date.fromisoformat(str(r['expiry_date'])[:10]) - today).days
            except ValueError:
                continue
            st = _stage(days)
            if st is None or (r['alert_stage'] is not None and r['alert_stage'] <= st):
                continue
            name = what or KIND_LABEL.get(r['kind'], r['kind'])
            full = name + (' ' + r['label'] if r['label'] else '') if what is None else '%s "%s"' % (what, r['label'])
            if days < 0:
                body = '%s이(가) %s에 만료됐습니다. 새로 받으면 인사 정보에 올려 주세요.' % (full, r['expiry_date'])
            elif days == 0:
                body = '%s이(가) 오늘 만료됩니다.' % full
            else:
                body = '%s이(가) %d일 뒤(%s) 만료됩니다.' % (full, days, r['expiry_date'])
            notify(r['uid'], '%s 만료 %s' % (name, '지남' if days < 0 else '임박'), body,
                   '/employees/%d#tab-ids' % r['uid'])
            c.execute('UPDATE %s SET alert_stage=? WHERE id=?' % table, (st, r['id']))
            sent += 1
            news.append(r['name'])
    if news and not only_uid:
        for a in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'").fetchall():
            notify(a['id'], '만료 임박 서류 %d건' % len(news),
                   '%s 등 %d명의 서류·자격증이 곧 만료되거나 지났습니다.' % (news[0], len(set(news))), admin_link)
    c.commit()
    return sent


# ── 장애인 의무고용 ─────────────────────────────────────────
def rate_for(c, year):
    r = c.execute('SELECT year, rate FROM disability_quota_rates WHERE year<=? ORDER BY year DESC LIMIT 1',
                  (year,)).fetchone()
    return (r['rate'], r['year']) if r else (None, None)


def _month_end(y, m):
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def quota(c, year=None, today=None):
    """올해 1월~이번 달 기준 추정. 사번 포함 장애 명단도 돌려준다."""
    today = today or date.today()
    year = year or today.year
    last_m = today.month if year == today.year else 12
    people = [dict(r) for r in c.execute(
        "SELECT u.id, u.emp_no, u.name, u.hire_date, u.termination_date, u.status, u.role, "
        "u.disability_level, u.disability_type, u.disability_since, u.disability_short, d.name dept "
        "FROM users u LEFT JOIN departments d ON d.id=u.department_id "
        "WHERE COALESCE(u.role,'')<>'guest' AND u.email NOT LIKE 'learner.%'").fetchall()]

    def on(p, s, e):
        h = str(p['hire_date'] or '')[:10]
        t = str(p['termination_date'] or '')[:10]
        if h and h > e.isoformat():
            return False
        if t and t < s.isoformat():
            return False
        if not t and p['status'] != 'active':
            return False   # 퇴사일 모르는 퇴사자는 언제까지 있었는지 모른다 → 안 셈
        return True

    months = []
    for m in range(1, last_m + 1):
        s, e = date(year, m, 1), _month_end(year, m)
        staff = dis = 0
        for p in people:
            if not on(p, s, e):
                continue
            lv = p['disability_level']
            short = bool(p['disability_short'])
            since = str(p['disability_since'] or '')[:10]
            disabled = lv in ('mild', 'severe') and (not since or since <= e.isoformat())
            if short and not (disabled and lv == 'severe'):
                # 월 60시간 미만: 상시근로자·장애인 모두에서 뺀다(중증만 예외로 1명)
                continue
            staff += 1
            if disabled:
                dis += (1 if short else 2) if lv == 'severe' else 1
        months.append(dict(m=m, staff=staff, dis=dis))
    n = len(months) or 1
    avg_staff = sum(x['staff'] for x in months) // n
    avg_dis = sum(x['dis'] for x in months) / n
    rate, rate_year = rate_for(c, year)
    need = int(avg_staff * rate / 100) if rate is not None else None
    roster = []
    for p in people:
        if p['status'] == 'active' and p['disability_level'] in LEVEL_LABEL:
            lv, short = p['disability_level'], bool(p['disability_short'])
            p['level_label'] = LEVEL_LABEL[lv]
            p['counts'] = (1 if short else 2) if lv == 'severe' else (0 if short else 1)
            roster.append(p)
    roster.sort(key=lambda p: (p['dept'] or '', p['name']))
    out = dict(year=year, months=months, avg_staff=avg_staff, avg_dis=round(avg_dis, 1), rate=rate,
               rate_year=rate_year, need=need, roster=roster, applies=avg_staff >= QUOTA_MIN,
               levy=avg_staff >= LEVY_MIN, through=last_m)
    out['short'] = max(0, need - int(avg_dis)) if (need is not None and out['applies']) else 0
    return out


def rates(c):
    return [dict(r) for r in c.execute('SELECT year, rate FROM disability_quota_rates ORDER BY year').fetchall()]


def roster_csv(q):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서', '장애 정도', '장애 유형', '등록일', '월 60시간 미만', '인정 인원'])
    for p in q['roster']:
        w.writerow([p['emp_no'] or '', p['name'], p['dept'] or '', p['level_label'], p['disability_type'] or '',
                    p['disability_since'] or '', 'Y' if p['disability_short'] else '', p['counts']])
    return '﻿' + buf.getvalue()


def expiring_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서', '서류', '국가', '만료일', '남은 날'])
    for x in rows:
        w.writerow([x['emp_no'] or '', x['name'], x['dept'] or '', x['what'], x['country'] or '',
                    x['expiry_date'], x['days']])
    return '﻿' + buf.getvalue()


# ── 화면 ───────────────────────────────────────────────────
def init_app(app, h):
    from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    admin_required = h['admin_required']
    log_audit = h['log_audit']
    add_notification = h['add_notification']

    def notify(uid, title, content, link):
        get_db().execute('INSERT INTO notifications (user_id, type, category, title, content, link) '
                         'VALUES (?,?,?,?,?,?)', (uid, 'action', 'cert', title, content, link))

    swept = {}

    @app.before_request
    def ids_daily_sweep():
        """만료 알림은 하루 한 번(첫 요청 때). 회사 설정에 날짜를 적어 워커·재시작에도 한 번."""
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='ids_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('ids_sweep_date', ?)",
                           (today,))
                db.commit()
                sweep(db, notify)
            swept[k] = today
        except Exception as e:   # 알림 실패로 화면을 막지 않는다
            app.logger.error('ids sweep failed: %s', e)

    def who(emp_id):
        """(본인인가, 관리자인가). 둘 다 아니면 403."""
        me = session.get('user_id') == emp_id
        adm = session.get('user_role') == 'admin'
        if not (me or adm):
            abort(403)
        return me, adm

    def view(emp_id):
        db = get_db()
        ensure_schema(db)
        u = db.execute('SELECT id, rrn_mask, disability_level, disability_type, disability_since, disability_short '
                       'FROM users WHERE id=?', (emp_id,)).fetchone()
        if not u:
            return None
        docs = docs_of(db, emp_id)
        today = date.today()
        for d in docs:
            d['kind_label'] = KIND_LABEL.get(d['kind'], d['kind'])
            if d['expiry_date']:
                d['days'] = (date.fromisoformat(str(d['expiry_date'])[:10]) - today).days
                d['left'] = left_label(d['days'])
            else:
                d['days'] = None
                d['left'] = ''
        return dict(u=dict(u), docs=docs, ready=ready(), kinds=KINDS, levels=LEVELS,
                    dis=dis_summary(u['disability_level'], u['disability_type'], u['disability_short']))

    # 직원 상세의 [신분·서류] 탭: {% set ids = ids_view(emp.id) %}
    app.jinja_env.globals['ids_view'] = view

    def back(emp_id):
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-ids')

    @app.route('/employees/<int:emp_id>/ids', methods=['POST'])
    @login_required
    def ids_save(emp_id):
        me, adm = who(emp_id)
        if session.get('demo_mode'):
            flash('데모에서는 저장되지 않습니다.', 'warning')
            return back(emp_id)
        db = get_db()
        ensure_schema(db)
        if not db.execute('SELECT 1 FROM users WHERE id=?', (emp_id,)).fetchone():
            abort(404)
        act = request.form.get('action') or ''
        aid, an = session['user_id'], session.get('user_name')
        err, ok = None, None
        if act == 'rrn':
            err = set_rrn(db, emp_id, request.form.get('rrn'), aid, an)
            ok = '주민등록번호를 저장했습니다.'
        elif act == 'rrn_clear':
            if not adm:
                abort(403)
            clear_rrn(db, emp_id, aid, an)
            ok = '주민등록번호를 지웠습니다.'
        elif act == 'dis':
            if not adm:
                abort(403)
            err = set_disability(db, emp_id, request.form.get('level'), request.form.get('type'),
                                 request.form.get('since'), request.form.get('short'), aid, an)
            ok = '장애 정보를 저장했습니다.'
        elif act == 'doc':
            did = request.form.get('doc_id', type=int)
            err = save_doc(db, emp_id, request.form, aid, an, doc_id=did)
            ok = '서류를 저장했습니다.'
        elif act == 'doc_delete':
            if not delete_doc(db, emp_id, request.form.get('doc_id', type=int), aid, an):
                abort(404)
            ok = '서류를 지웠습니다.'
        else:
            abort(400)
        if err:
            db.rollback()
            flash(err, 'error')
        else:
            db.commit()
            if act == 'doc':
                sweep(db, notify, only_uid=emp_id)
            log_audit('update', 'personal_info', emp_id, '신분·서류 %s' % act)
            flash(ok, 'success')
        return back(emp_id)

    @app.route('/employees/<int:emp_id>/ids/reveal', methods=['POST'])
    @login_required
    def ids_reveal(emp_id):
        me, adm = who(emp_id)
        if session.get('demo_mode'):
            return jsonify(ok=False, error='데모에서는 볼 수 없습니다.'), 403
        reason = (request.form.get('reason') or '').strip()[:200]
        if not me and len(reason) < 2:
            return jsonify(ok=False, error='보는 사유를 적어 주세요.'), 400
        db = get_db()
        ensure_schema(db)
        what = request.form.get('what')
        if what == 'rrn':
            r = db.execute('SELECT rrn_enc FROM users WHERE id=?', (emp_id,)).fetchone()
            val = rrn_fmt(decrypt(r['rrn_enc'])) if r and r['rrn_enc'] else None
            name = '주민등록번호'
        elif what == 'doc':
            r = db.execute('SELECT kind, number_enc FROM person_ids WHERE id=? AND user_id=?',
                           (request.form.get('doc_id', type=int), emp_id)).fetchone()
            val = decrypt(r['number_enc']) if r and r['number_enc'] else None
            name = KIND_LABEL.get(r['kind'], '서류') + ' 번호' if r else '서류 번호'
        else:
            abort(400)
        if not val:
            return jsonify(ok=False, error='읽을 수 없습니다(저장된 값이 없거나 열쇠가 다릅니다).'), 404
        log_audit('view', 'personal_info', emp_id,
                  '%s 전체 보기 · 사유: %s' % (name, reason or '본인 확인'))
        return jsonify(ok=True, value=val)

    @app.route('/people/ids', methods=['GET', 'POST'])
    @admin_required
    def ids_admin():
        db = get_db()
        ensure_schema(db)
        if request.method == 'POST':
            y = request.form.get('year', type=int)
            r = request.form.get('rate', type=float)
            if request.form.get('action') == 'rate_delete' and y:
                db.execute('DELETE FROM disability_quota_rates WHERE year=?', (y,))
                db.commit()
                flash('%d년 의무고용률을 지웠습니다.' % y, 'success')
            elif y and 2000 <= y <= 2100 and r is not None and 0 < r < 20:
                db.execute('INSERT OR REPLACE INTO disability_quota_rates (year, rate) VALUES (?,?)', (y, r))
                db.commit()
                flash('%d년 의무고용률을 %.1f%% 로 저장했습니다.' % (y, r), 'success')
            else:
                flash('연도와 비율(%)을 확인하세요.', 'error')
            return redirect(url_for('ids_admin', tab='quota'))
        tab = request.args.get('tab') or 'docs'
        within = request.args.get('within', type=int) or 90
        if within not in (30, 90, 365):
            within = 90
        rows = expiring(db, within)
        q = quota(db)
        if request.args.get('csv') == 'docs':
            log_audit('download', 'personal_info', None, '만료 서류 목록 CSV')
            return Response(expiring_csv(rows), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="expiring_ids.csv"'})
        if request.args.get('csv') == 'dis':
            log_audit('download', 'personal_info', None, '장애인 고용 명단 CSV')
            return Response(roster_csv(q), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="disability_roster.csv"'})
        reveals = [dict(r) for r in db.execute(
            "SELECT a.created_at, a.actor_name, a.detail, u.name target, u.emp_no FROM audit_logs a "
            "LEFT JOIN users u ON u.id=a.target_user_id WHERE a.category='personal_info' AND a.action='view' "
            "AND a.detail LIKE '%전체 보기%' ORDER BY a.id DESC LIMIT 30").fetchall()]
        counts = dict(expired=sum(1 for x in rows if x['days'] < 0),
                      d30=sum(1 for x in rows if 0 <= x['days'] <= 30))
        rrn_n = db.execute("SELECT COUNT(*) FROM users WHERE status='active' AND rrn_enc IS NOT NULL").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        return render_template('ids/admin.html', tab=tab, within=within, rows=rows, q=q, rates=rates(db),
                               reveals=reveals, counts=counts, rrn_n=rrn_n, total=total, ready=ready(),
                               QUOTA_MIN=QUOTA_MIN, LEVY_MIN=LEVY_MIN, active_page='ids')
