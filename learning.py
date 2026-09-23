"""직무 교육(Grow) — 세부 직무와 교육 기록.

TalentCore 가 사람의 주인이다. Grow 는 로그인할 때마다 여기서 사람·직급·세부 직무를
읽고, 진행 상황도 여기에 적는다. 그래서 Grow 에는 따로 계정 표·기록 표가 없다.
  - 세부 직무(job_profiles): 직군(job_families) 아래 한 칸 더. 교육 과정은 이 값으로 갈린다.
  - 교육 기록(learning_records): 사람 × 과정 한 줄. 끝나면 completed_at 이 찍히고
    프로필에 수료 배지로 보인다.
"""
import json

# 채용 직군(TA) 아래 세부 직무 두 개로 시작한다. 코드는 Grow 과정 파일이 가리키는 열쇠라 바꾸지 않는다.
PROFILES = (
    ('TA_RC', '리크루팅 코디네이터', '공고 제작 · 후보자 관리 · 면접 조율', 1),
    ('TA_REC', '리크루터', '공고 확인 · 후보자 관리 · 오퍼', 2),
)


def _cols(c, table):
    return {r[1] for r in c.execute('PRAGMA table_info(%s)' % table).fetchall()}


def _add_col(c, table, col, ddl):
    import sqlite3
    if col in _cols(c, table):
        return
    try:
        c.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, ddl))
    except sqlite3.OperationalError as e:   # 워커 두 개가 동시에 부팅할 때
        if 'duplicate column' not in str(e):
            raise


def ensure_schema(c):
    c.executescript('''
        CREATE TABLE IF NOT EXISTS job_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_family_id  INTEGER REFERENCES job_families(id),
            code           TEXT NOT NULL UNIQUE,
            name           TEXT NOT NULL,
            summary        TEXT NOT NULL DEFAULT '',
            sort_order     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS learning_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            course_id       TEXT NOT NULL,
            course_title    TEXT NOT NULL DEFAULT '',
            profile_code    TEXT,
            missions_done   INTEGER NOT NULL DEFAULT 0,
            missions_total  INTEGER NOT NULL DEFAULT 0,
            state_json      TEXT NOT NULL DEFAULT '{}',
            started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at    TEXT,
            UNIQUE(user_id, course_id)
        );
        CREATE INDEX IF NOT EXISTS idx_learning_user ON learning_records(user_id);
    ''')
    _add_col(c, 'users', 'job_profile_id', 'INTEGER REFERENCES job_profiles(id)')
    _add_col(c, 'incoming_hires', 'job_profile_id', 'INTEGER REFERENCES job_profiles(id)')

    ta = c.execute("SELECT id FROM job_families WHERE code='TA'").fetchone()
    for code, name, summary, order in PROFILES:
        c.execute('INSERT OR IGNORE INTO job_profiles (job_family_id, code, name, summary, sort_order) '
                  'VALUES (?,?,?,?,?)', (ta[0] if ta else None, code, name, summary, order))
        # 직군 표가 나중에 채워지는 테넌트(init_db 두 번 부르는 연습 회사)도 제자리를 찾게 한다
        if ta:
            c.execute('UPDATE job_profiles SET job_family_id=? WHERE code=? AND job_family_id IS NULL',
                      (ta[0], code))


# ── 읽기 ──────────────────────────────────────────────────────────────
def all_profiles(db):
    return db.execute(
        'SELECT jp.*, jf.name AS family_name FROM job_profiles jp '
        'LEFT JOIN job_families jf ON jf.id = jp.job_family_id '
        'ORDER BY jf.sort_order, jp.sort_order, jp.id').fetchall()


def match_profile(db, text):
    """입사 예정자의 직무 글자(job_title)가 세부 직무 이름·코드와 같으면 그 id."""
    t = (text or '').strip().lower().replace(' ', '')
    if not t:
        return None
    for p in all_profiles(db):
        if t in (p['code'].lower(), p['name'].replace(' ', '').lower()):
            return p['id']
    return None


def person(db, user_id):
    """Grow 가 보여 줄 사람 정보 — 이름·부서·직급·직군·세부 직무·입사일."""
    r = db.execute(
        'SELECT u.id, u.emp_no, u.name, u.email, u.role, u.status, u.hire_date, '
        '       d.name AS dept, p.name AS position, p.level AS level, '
        '       jf.code AS jf_code, jf.name AS jf_name, '
        '       jp.code AS jp_code, jp.name AS jp_name, jp.summary AS jp_summary '
        'FROM users u '
        'LEFT JOIN departments  d  ON d.id  = u.department_id '
        'LEFT JOIN positions    p  ON p.id  = u.position_id '
        'LEFT JOIN job_families jf ON jf.id = u.job_family_id '
        'LEFT JOIN job_profiles jp ON jp.id = u.job_profile_id '
        'WHERE u.id = ?', (user_id,)).fetchone()
    if not r:
        return None
    return {
        'emp_no': r['emp_no'] or '',
        'name': r['name'] or '',
        'email': (r['email'] or '').strip().lower() or None,
        'role': r['role'] or 'employee',
        'active': (r['status'] or 'active') == 'active',
        'hire_date': r['hire_date'] or None,
        'dept': r['dept'] or '',
        'position': r['position'] or '',
        'level': r['level'],
        'job_family': {'code': r['jf_code'], 'name': r['jf_name']} if r['jf_code'] else None,
        'job_profile': {'code': r['jp_code'], 'name': r['jp_name'], 'summary': r['jp_summary'] or ''}
                       if r['jp_code'] else None,
    }


def _record(r):
    try:
        state = json.loads(r['state_json'] or '{}')
    except ValueError:
        state = {}
    return {
        'course_id': r['course_id'],
        'course_title': r['course_title'],
        'profile_code': r['profile_code'],
        'done': r['missions_done'],
        'total': r['missions_total'],
        'state': state,
        'started_at': r['started_at'],
        'updated_at': r['updated_at'],
        'completed_at': r['completed_at'],
    }


def records(db, user_id):
    rows = db.execute('SELECT * FROM learning_records WHERE user_id=? ORDER BY started_at',
                      (user_id,)).fetchall()
    return [_record(r) for r in rows]


def summary(db, user_id):
    """프로필·온보딩 화면용 — 세부 직무, 과정 줄, 수료 배지."""
    try:
        p = person(db, user_id)
        recs = records(db, user_id)
    except Exception:          # 스키마가 아직 없는 옛 DB — 화면은 그대로 뜨게
        return None
    if not p:
        return None
    badges = [r for r in recs if r['completed_at']]
    return {'profile': p['job_profile'], 'records': recs, 'badges': badges,
            'grow_url': grow_url(db)}


def grow_url(db):
    r = db.execute("SELECT value FROM company_settings WHERE key='grow_url'").fetchone()
    return ((r['value'] if r else '') or '').strip().rstrip('/')


# ── 쓰기 ──────────────────────────────────────────────────────────────
MAX_STATE = 20000


def save_progress(db, user_id, course_id, course_title, profile_code, done, total, state):
    """과정 진행을 적는다. 끝낸 과정은 다시 적어도 수료일이 바뀌지 않는다.

    반환: (record, newly_completed)
    """
    done = max(0, int(done or 0))
    total = max(0, int(total or 0))
    if total and done > total:
        done = total
    blob = json.dumps(state if isinstance(state, dict) else {}, ensure_ascii=False)
    if len(blob) > MAX_STATE:
        raise ValueError('state too large')
    prev = db.execute('SELECT completed_at FROM learning_records WHERE user_id=? AND course_id=?',
                      (user_id, course_id)).fetchone()
    finished = bool(total) and done >= total
    db.execute(
        'INSERT INTO learning_records (user_id, course_id, course_title, profile_code, '
        '  missions_done, missions_total, state_json, completed_at) '
        "VALUES (?,?,?,?,?,?,?, CASE WHEN ? THEN CURRENT_TIMESTAMP END) "
        'ON CONFLICT(user_id, course_id) DO UPDATE SET '
        '  course_title=excluded.course_title, profile_code=excluded.profile_code, '
        '  missions_done=MAX(learning_records.missions_done, excluded.missions_done), '
        '  missions_total=excluded.missions_total, state_json=excluded.state_json, '
        '  updated_at=CURRENT_TIMESTAMP, '
        '  completed_at=COALESCE(learning_records.completed_at, excluded.completed_at)',
        (user_id, course_id, course_title[:120], profile_code, done, total, blob, 1 if finished else 0))
    row = db.execute('SELECT * FROM learning_records WHERE user_id=? AND course_id=?',
                     (user_id, course_id)).fetchone()
    newly = bool(row['completed_at']) and not (prev and prev['completed_at'])
    return _record(row), newly
