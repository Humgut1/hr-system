"""조직 · 직무 체계(B6).

  - 조직 코드: departments.code — 사람의 사번처럼 조직을 묶는 열쇠. 비어 있으면 'D0007' 처럼 채운다.
    엑셀 일괄 등록·수정은 이름 대신 코드도 받는다.
  - 상태: active(운영) · inactive(닫힘) · dissolved(해체 — 되돌릴 수 없음).
      · 닫기는 재직자·진행 중 채용·결재 중 요청서·입사 예정자가 없을 때만. 남은 것을 그대로 보여 준다.
      · 열려 있는 하위 조직은 닫기 전에 새 상위 조직을 정한다(기본 = 닫는 조직의 상위).
      · 정원: 상위 조직 정원에 하위 합계가 들어 있다(정원 함정). 그래서 닫을 때 그 조직 몫만큼
        상위 줄에서 빼고, 하위 조직을 옮길 때는 겹치지 않는 상위 줄끼리만 옮긴다. 올해와 그 뒤 해만.
      · 다시 열어도 정원은 되살리지 않는다(관리자가 다시 넣는다).
  - 이름·코드·상위·상태가 바뀔 때마다 org_events 에 한 줄. 이름 바뀐 역사는 여기서 읽는다.
  - 닫힌 조직은 고르는 목록에서 빠지지만 id 로 잇는 옛 기록(근태·급여·평가·변경 이력)은 그대로 이름이 보인다.
  - 관리 단계(임원·조직장·중간관리자·실무자): management_levels + positions.mgmt_level_id.
  - 미래 날짜 조직 개편 예약은 V1 밖.
"""
import csv
import io
import re
import sqlite3
from datetime import date

STATUS_LABEL = {'active': '운영', 'inactive': '닫힘', 'dissolved': '해체'}
EVENT_LABEL = {'create': '만듦', 'rename': '이름 변경', 'code': '코드 변경', 'move': '상위 조직 변경',
               'close': '닫음', 'reopen': '다시 엶', 'dissolve': '해체', 'leader': '조직장 변경'}
CODE_RE = re.compile(r'^[A-Z0-9_-]{2,20}$')

_DEPT_COLS = (('code', 'TEXT'), ('status', "TEXT NOT NULL DEFAULT 'active'"), ('closed_at', 'TEXT'))
_DDL = (
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_departments_code ON departments(code)',
    '''CREATE TABLE IF NOT EXISTS org_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        dept_id    INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        before     TEXT,
        after      TEXT,
        note       TEXT,
        actor_id   INTEGER,
        actor_name TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_org_events_dept ON org_events(dept_id)',
    '''CREATE TABLE IF NOT EXISTS management_levels (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code       TEXT NOT NULL UNIQUE,
        name       TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0
    )''',
)
_LEVEL_SEED = (('EXEC', '임원', 1), ('HEAD', '조직장', 2), ('MID', '중간관리자', 3), ('IC', '실무자', 4))

_done = set()


def _dbkey(c):
    try:
        return c.execute('PRAGMA database_list').fetchone()[2]
    except Exception:
        return None


def _add_col(c, table, col, typ):
    try:
        c.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, typ))
    except sqlite3.OperationalError as e:
        # 워커 두 개가 동시에 뜨면 한쪽이 먼저 넣는다.
        if 'duplicate column' not in str(e).lower():
            raise


def ensure_schema(c):
    """칸·표가 없으면 만든다. 같은 DB 는 프로세스당 한 번(테넌트 DB 는 부팅 때 init_db 를 안 거친다)."""
    k = _dbkey(c)
    if k and k in _done:
        return
    have = {r[1] for r in c.execute('PRAGMA table_info(departments)').fetchall()}
    if not have:
        return
    for col, typ in _DEPT_COLS:
        if col not in have:
            _add_col(c, 'departments', col, typ)
    if 'mgmt_level_id' not in {r[1] for r in c.execute('PRAGMA table_info(positions)').fetchall()}:
        _add_col(c, 'positions', 'mgmt_level_id', 'INTEGER')
    for q in _DDL:
        c.execute(q)
    if not c.execute('SELECT 1 FROM management_levels LIMIT 1').fetchone():
        for code, name, so in _LEVEL_SEED:
            c.execute('INSERT OR IGNORE INTO management_levels (code, name, sort_order) VALUES (?,?,?)',
                      (code, name, so))
    _fill_codes(c)
    c.commit()
    if k:
        _done.add(k)


def _fill_codes(c):
    for r in c.execute("SELECT id FROM departments WHERE code IS NULL OR code=''").fetchall():
        n = r[0]
        while True:
            code = 'D%04d' % n
            if not c.execute('SELECT 1 FROM departments WHERE code=?', (code,)).fetchone():
                break
            n += 10000
        c.execute('UPDATE departments SET code=? WHERE id=?', (code, r[0]))


def forget(c):
    """DB 파일을 지우고 다시 만든 쪽이 부른다 — 같은 경로라도 칸을 다시 넣게."""
    _done.discard(_dbkey(c))


def fill_codes(c):
    """조직을 새로 넣은 쪽이 부른다(commit 은 부르는 쪽이)."""
    ensure_schema(c)
    _fill_codes(c)


# ── 읽기 ───────────────────────────────────────────────────
def active_depts(c, keep=None, cols='*', where='', order='name'):
    """고르는 목록·필터용 — 운영 중인 조직만. keep(id 하나 또는 여러 개)은 닫혔어도 붙인다(수정 화면의 지금 값)."""
    ensure_schema(c)
    keep = [k for k in (keep if isinstance(keep, (list, tuple, set)) else [keep]) if k not in (None, '')]
    cond = "status='active'"
    args = []
    if keep:
        cond = "(%s OR id IN (%s))" % (cond, ','.join('?' * len(keep)))
        args = [int(k) for k in keep]
    if where:
        cond += ' AND (%s)' % where
    return c.execute('SELECT %s FROM departments WHERE %s ORDER BY %s' % (cols, cond, order), args).fetchall()


def active_ids(c):
    ensure_schema(c)
    return {r[0] for r in c.execute("SELECT id FROM departments WHERE status='active'")}


def dept_lookup(c, active_only=True):
    """엑셀·연동에서 부서 칸 읽기: 이름 또는 코드 → id. 기본은 운영 중인 조직만(닫힌 조직에 사람을 넣지 않게)."""
    ensure_schema(c)
    out = {}
    rows = c.execute("SELECT id, name, code, status FROM departments %s"
                     "ORDER BY CASE status WHEN 'active' THEN 1 ELSE 0 END, id"
                     % ("WHERE status='active' " if active_only else '')).fetchall()
    for r in rows:          # 운영 중인 것을 나중에 넣어 덮어쓴다
        out[r[1]] = r[0]
    for r in rows:
        if r[2]:
            out[r[2]] = r[0]
            out[r[2].lower()] = r[0]
    return out


def find_dept(c, name):
    """이름(또는 코드) 하나로 부서 id. 운영 중인 것 우선."""
    if not name:
        return None
    return dept_lookup(c).get(str(name).strip())


def dept(c, did):
    ensure_schema(c)
    return c.execute(
        'SELECT d.*, p.name AS parent_name, p.code AS parent_code, p.status AS parent_status, '
        '       l.name AS leader_name, l.emp_no AS leader_emp_no '
        'FROM departments d LEFT JOIN departments p ON p.id=d.parent_id '
        'LEFT JOIN users l ON l.id=d.leader_id WHERE d.id=?', (did,)).fetchone()


def events(c, did):
    ensure_schema(c)
    return c.execute('SELECT * FROM org_events WHERE dept_id=? ORDER BY id DESC', (did,)).fetchall()


def levels(c):
    ensure_schema(c)
    return c.execute(
        'SELECT m.*, (SELECT COUNT(*) FROM positions p WHERE p.mgmt_level_id=m.id) AS pos_count '
        'FROM management_levels m ORDER BY sort_order, id').fetchall()


def _ancestors(c, did):
    """did 의 상위들(자기 제외, 가까운 순)."""
    parent = {r[0]: r[1] for r in c.execute('SELECT id, parent_id FROM departments')}
    out, cur, seen = [], parent.get(did), set()
    while cur and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = parent.get(cur)
    return out


def _descendants(c, did):
    kids = {}
    for r in c.execute('SELECT id, parent_id FROM departments'):
        kids.setdefault(r[1], []).append(r[0])
    out, stack = set(), [did]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in out:
                out.add(k)
                stack.append(k)
    return out


def blockers(c, did):
    """닫기 전에 남아 있으면 안 되는 것. [(이름, 건수, 링크)]."""
    d = c.execute('SELECT name FROM departments WHERE id=?', (did,)).fetchone()
    if not d:
        return []
    out = []
    n = c.execute("SELECT COUNT(*) FROM users WHERE department_id=? AND status='active'", (did,)).fetchone()[0]
    if n:
        out.append(('재직자', n, '/employees?dept=%d' % did))
    n = c.execute("SELECT COUNT(*) FROM job_openings WHERE department_id=? AND status IN ('approved','open')",
                  (did,)).fetchone()[0]
    if n:
        out.append(('진행 중인 포지션', n, '/seats'))
    n = c.execute("SELECT COUNT(*) FROM job_requisitions WHERE department_id=? "
                  "AND status IN ('draft','pending_dept','pending_hr','approved')", (did,)).fetchone()[0]
    if n:
        out.append(('결재 중·승인된 채용 요청서', n, '/requisitions'))
    n = c.execute("SELECT COUNT(*) FROM incoming_hires WHERE status='waiting' AND department_name=?",
                  (d[0],)).fetchone()[0]
    if n:
        out.append(('입사 예정자', n, '/hires'))
    return out


def usage(c, did):
    """한 번이라도 쓰인 적이 있는지 — 있으면 지우지 않고 닫는다."""
    checks = (
        ('직원(퇴사자 포함)', 'SELECT COUNT(*) FROM users WHERE department_id=?'),
        ('하위 조직', 'SELECT COUNT(*) FROM departments WHERE parent_id=?'),
        ('채용 요청서', 'SELECT COUNT(*) FROM job_requisitions WHERE department_id=?'),
        ('포지션', 'SELECT COUNT(*) FROM job_openings WHERE department_id=?'),
        ('채용 공고', 'SELECT COUNT(*) FROM job_postings WHERE department_id=?'),
        ('오퍼 결재', 'SELECT COUNT(*) FROM offer_approvals WHERE department_id=?'),
        ('보상 심사', 'SELECT COUNT(*) FROM compensation_reviews WHERE department_id=?'),
        ('조직 목표', 'SELECT COUNT(*) FROM org_goals WHERE department_id=?'),
    )
    out = []
    for label, q in checks:
        try:
            n = c.execute(q, (did,)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if n:
            out.append((label, n))
    return out


# ── 정원 ───────────────────────────────────────────────────
def _hc_years(c):
    fy = date.today().year
    return [r[0] for r in c.execute('SELECT DISTINCT fiscal_year FROM department_headcount WHERE fiscal_year>=?',
                                    (fy,)).fetchall()]


def _hc(c, did, fy):
    r = c.execute('SELECT target_count FROM department_headcount WHERE department_id=? AND fiscal_year=?',
                  (did, fy)).fetchone()
    return r[0] if r else None


def _hc_add(c, did, fy, n):
    cur = _hc(c, did, fy)
    if cur is None:
        return          # 정원을 안 쓰는 상위 조직 — 건드리지 않는다
    c.execute('UPDATE department_headcount SET target_count=? WHERE department_id=? AND fiscal_year=?',
              (max(0, cur + n), did, fy))


def _hc_move(c, did, new_parent):
    """did 를 new_parent 밑으로 옮길 때 정원 숫자를 옮긴다. 겹치는 상위는 그대로."""
    old_chain = _ancestors(c, did)
    new_chain = ([new_parent] + _ancestors(c, new_parent)) if new_parent else []
    common = set(old_chain) & set(new_chain)
    for fy in _hc_years(c):
        n = _hc(c, did, fy) or 0
        if not n:
            continue
        for a in old_chain:
            if a not in common:
                _hc_add(c, a, fy, -n)
        for a in new_chain:
            if a not in common:
                _hc_add(c, a, fy, n)


# ── 쓰기(commit 은 부르는 쪽이) ────────────────────────────
def log(c, did, kind, before=None, after=None, note=None, actor=None):
    actor = actor or {}
    c.execute('INSERT INTO org_events (dept_id, kind, before, after, note, actor_id, actor_name) '
              'VALUES (?,?,?,?,?,?,?)', (did, kind, before, after, note, actor.get('id'), actor.get('name')))


def create(c, name, parent_id, dept_type, actor=None, code=None):
    ensure_schema(c)
    if code:
        code = code.strip().upper()
        err = _code_error(c, code)
        if err:
            raise ValueError(err)
    did = c.execute('INSERT INTO departments (name, parent_id, dept_type, code) VALUES (?,?,?,?)',
                    (name, parent_id, dept_type, code or None)).lastrowid
    _fill_codes(c)
    log(c, did, 'create', None, name, actor=actor)
    return did


def _code_error(c, code, did=None):
    if not CODE_RE.match(code or ''):
        return '코드는 영문 대문자·숫자·-·_ 로 2~20자입니다.'
    r = c.execute('SELECT id, name FROM departments WHERE code=?', (code,)).fetchone()
    if r and r[0] != did:
        return '코드 %s 는 이미 %s 이(가) 쓰고 있습니다.' % (code, r[1])
    return None


def rename(c, did, name, note=None, actor=None):
    ensure_schema(c)
    name = (name or '').strip()
    d = dept(c, did)
    if not d or not name:
        return '이름을 적어 주세요.'
    if name == d['name']:
        return None
    if c.execute("SELECT 1 FROM departments WHERE name=? AND id!=? AND status='active' AND parent_id IS ?",
                 (name, did, d['parent_id'])).fetchone():
        return '같은 상위 조직 밑에 같은 이름이 있습니다.'
    old = d['name']
    c.execute('UPDATE departments SET name=? WHERE id=?', (name, did))
    # 입사 예정자는 부서를 이름으로 들고 있다 — 같이 바꿔야 끊기지 않는다.
    c.execute("UPDATE incoming_hires SET department_name=? WHERE department_name=? AND status='waiting'",
              (name, old))
    log(c, did, 'rename', old, name, note, actor)
    return None


def set_code(c, did, code, actor=None):
    ensure_schema(c)
    code = (code or '').strip().upper()
    d = dept(c, did)
    if not d:
        return '조직이 없습니다.'
    if code == d['code']:
        return None
    err = _code_error(c, code, did)
    if err:
        return err
    c.execute('UPDATE departments SET code=? WHERE id=?', (code, did))
    log(c, did, 'code', d['code'], code, actor=actor)
    return None


def parent_error(c, did, new_parent, dept_type, allowed):
    """새 상위 조직이 맞는지. allowed = DEPT_TYPE_PARENT_ALLOWED."""
    ok_types = allowed.get(dept_type, [])
    if not new_parent:
        return None if not ok_types else '이 종류 조직은 상위 조직이 있어야 합니다.'
    if new_parent == did or new_parent in _descendants(c, did):
        return '자기 자신이나 하위 조직 밑으로는 옮길 수 없습니다.'
    p = c.execute('SELECT dept_type, status, name FROM departments WHERE id=?', (new_parent,)).fetchone()
    if not p:
        return '상위 조직이 없습니다.'
    if p['status'] != 'active':
        return '%s 은(는) 닫힌 조직입니다.' % p['name']
    if p['dept_type'] not in ok_types:
        return '이 종류 조직은 %s 밑에 둘 수 없습니다.' % p['name']
    return None


def move(c, did, new_parent, allowed, note=None, actor=None):
    ensure_schema(c)
    d = dept(c, did)
    if not d:
        return '조직이 없습니다.'
    new_parent = int(new_parent) if new_parent else None
    if new_parent == d['parent_id']:
        return None
    err = parent_error(c, did, new_parent, d['dept_type'], allowed)
    if err:
        return err
    _hc_move(c, did, new_parent)
    old_name = d['parent_name'] or '(최상위)'
    new_name = c.execute('SELECT name FROM departments WHERE id=?', (new_parent,)).fetchone()[0] \
        if new_parent else '(최상위)'
    c.execute('UPDATE departments SET parent_id=? WHERE id=?', (new_parent, did))
    log(c, did, 'move', old_name, new_name, note, actor)
    return None


def close(c, did, child_parents, allowed, note=None, actor=None):
    """닫기. child_parents = {하위 id: 새 상위 id(None=최상위)}. 성공하면 None, 아니면 막힌 이유(글)."""
    ensure_schema(c)
    d = dept(c, did)
    if not d:
        return '조직이 없습니다.'
    if d['status'] != 'active':
        return '이미 닫힌 조직입니다.'
    b = blockers(c, did)
    if b:
        return '먼저 정리해야 합니다: ' + ' · '.join('%s %d' % (x[0], x[1]) for x in b)
    kids = c.execute("SELECT id, name FROM departments WHERE parent_id=? AND status='active'", (did,)).fetchall()
    for k in kids:
        np = child_parents.get(k['id'], d['parent_id'])
        if np == did:
            return '%s 의 새 상위 조직을 골라 주세요.' % k['name']
        err = move(c, k['id'], np, allowed, note='%s 닫힘' % d['name'], actor=actor)
        if err:
            return '%s — %s' % (k['name'], err)
    # 남은 자기 몫을 상위 줄에서 빼고 0 으로
    chain = _ancestors(c, did)
    for fy in _hc_years(c):
        n = _hc(c, did, fy) or 0
        if n:
            for a in chain:
                _hc_add(c, a, fy, -n)
            c.execute('UPDATE department_headcount SET target_count=0 WHERE department_id=? AND fiscal_year=?',
                      (did, fy))
    if d['leader_id']:
        log(c, did, 'leader', d['leader_name'], None, '닫으며 비움', actor)
    c.execute("UPDATE departments SET status='inactive', closed_at=?, leader_id=NULL WHERE id=?",
              (date.today().isoformat(), did))
    log(c, did, 'close', '운영', '닫힘', note, actor)
    return None


def reopen(c, did, new_parent, allowed, note=None, actor=None):
    ensure_schema(c)
    d = dept(c, did)
    if not d or d['status'] != 'inactive':
        return '닫힌 조직만 다시 열 수 있습니다.'
    new_parent = int(new_parent) if new_parent else None
    if new_parent != d['parent_id'] or (d['parent_id'] and d['parent_status'] != 'active'):
        err = parent_error(c, did, new_parent, d['dept_type'], allowed)
        if err:
            return err
        if new_parent != d['parent_id']:
            c.execute('UPDATE departments SET parent_id=? WHERE id=?', (new_parent, did))
            log(c, did, 'move', d['parent_name'] or '(최상위)',
                c.execute('SELECT name FROM departments WHERE id=?', (new_parent,)).fetchone()[0]
                if new_parent else '(최상위)', '다시 열며', actor)
    c.execute("UPDATE departments SET status='active', closed_at=NULL WHERE id=?", (did,))
    log(c, did, 'reopen', '닫힘', '운영', note, actor)
    return None


def dissolve(c, did, note=None, actor=None):
    ensure_schema(c)
    d = dept(c, did)
    if not d or d['status'] != 'inactive':
        return '닫힌 조직만 해체할 수 있습니다.'
    if c.execute("SELECT 1 FROM departments WHERE parent_id=? AND status='active'", (did,)).fetchone():
        return '운영 중인 하위 조직이 있습니다.'
    c.execute("UPDATE departments SET status='dissolved' WHERE id=?", (did,))
    log(c, did, 'dissolve', '닫힘', '해체', note, actor)
    return None


def delete(c, did):
    """한 번도 안 쓰인 조직만 지운다. 쓰인 적이 있으면 이유를 돌려준다."""
    ensure_schema(c)
    u = usage(c, did)
    if u:
        return '기록이 있어 지울 수 없습니다(%s). 대신 [닫기]를 쓰세요.' % ' · '.join('%s %d' % x for x in u)
    c.execute('DELETE FROM department_headcount WHERE department_id=?', (did,))
    c.execute('DELETE FROM org_events WHERE dept_id=?', (did,))
    c.execute('DELETE FROM departments WHERE id=?', (did,))
    return None


# ── 내보내기 ───────────────────────────────────────────────
def export_csv(c, type_label):
    ensure_schema(c)
    fy = date.today().year
    hc = {r[0]: r[1] for r in c.execute(
        'SELECT department_id, target_count FROM department_headcount WHERE fiscal_year=?', (fy,))}
    cnt = {r[0]: r[1] for r in c.execute(
        "SELECT department_id, COUNT(*) FROM users WHERE status='active' GROUP BY department_id")}
    rows = c.execute(
        'SELECT d.*, p.code AS parent_code, p.name AS parent_name, l.emp_no AS leader_emp_no, l.name AS leader_name '
        'FROM departments d LEFT JOIN departments p ON p.id=d.parent_id LEFT JOIN users l ON l.id=d.leader_id '
        "ORDER BY CASE d.status WHEN 'active' THEN 0 WHEN 'inactive' THEN 1 ELSE 2 END, d.code").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['조직코드', '조직명', '구분', '상위코드', '상위조직', '조직장사번', '조직장', '상태',
                '현원', '%d 정원' % fy, '닫은 날'])
    for r in rows:
        w.writerow([r['code'], r['name'], type_label.get(r['dept_type'], r['dept_type']),
                    r['parent_code'] or '', r['parent_name'] or '', r['leader_emp_no'] or '',
                    r['leader_name'] or '', STATUS_LABEL.get(r['status'], r['status']),
                    cnt.get(r['id'], 0), hc.get(r['id'], ''), r['closed_at'] or ''])
    return '﻿' + buf.getvalue()


# ── 화면 ───────────────────────────────────────────────────
def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    admin_required = h['admin_required']
    log_audit = h['log_audit']
    TYPE_LABEL = h['DEPT_TYPE_LABEL']
    TYPE_COLOR = h['DEPT_TYPE_COLOR']
    ALLOWED = h['DEPT_TYPE_PARENT_ALLOWED']

    def parent_options(c, did, dtype):
        """did 가 갈 수 있는 상위 조직(운영 중 · 종류 맞음 · 자기와 하위 제외)."""
        bad = _descendants(c, did) | {did}
        ok = ALLOWED.get(dtype, [])
        return [r for r in active_depts(c, order='dept_type, name')
                if r['id'] not in bad and r['dept_type'] in ok]

    @app.route('/departments/<int:did>', methods=['GET', 'POST'])
    @admin_required
    def org_detail(did):
        c = get_db()
        ensure_schema(c)
        d = dept(c, did)
        if not d:
            abort(404)
        actor = dict(id=session.get('user_id'), name=session.get('user_name'))
        if request.method == 'POST':
            act = request.form.get('action')
            note = (request.form.get('note') or '').strip() or None
            err = None
            if act == 'code':
                err = set_code(c, did, request.form.get('code'), actor)
            elif act == 'rename':
                err = rename(c, did, request.form.get('name'), note, actor)
            elif act == 'move':
                err = move(c, did, request.form.get('parent_id', type=int), ALLOWED, note, actor)
            elif act == 'close':
                cp = {}
                for k in request.form:
                    if k.startswith('child_') and k[6:].isdigit():
                        v = request.form.get(k)
                        cp[int(k[6:])] = int(v) if v and v.isdigit() else None
                err = close(c, did, cp, ALLOWED, note, actor)
            elif act == 'reopen':
                err = reopen(c, did, request.form.get('parent_id', type=int), ALLOWED, note, actor)
            elif act == 'dissolve':
                err = dissolve(c, did, note, actor)
            elif act == 'delete':
                err = delete(c, did)
                if not err:
                    c.commit()
                    flash('%s 을(를) 지웠습니다.' % d['name'], 'success')
                    return redirect(url_for('departments'))
            else:
                err = '알 수 없는 요청입니다.'
            if err:
                c.rollback()
                flash(err, 'error')
            else:
                c.commit()
                log_audit('update', 'personal_info', None, '조직 %s %s' % (d['code'], act))
                flash('저장했습니다.', 'success')
            return redirect(url_for('org_detail', did=did))

        fy = date.today().year
        members = c.execute(
            'SELECT u.id, u.emp_no, u.name, p.name AS pos_name FROM users u '
            'LEFT JOIN positions p ON p.id=u.position_id '
            "WHERE u.department_id=? AND u.status='active' ORDER BY COALESCE(p.level,0) DESC, u.name",
            (did,)).fetchall()
        past = c.execute("SELECT COUNT(*) FROM users WHERE department_id=? AND status!='active'",
                         (did,)).fetchone()[0]
        kids = c.execute(
            'SELECT d.*, (SELECT COUNT(*) FROM users u WHERE u.department_id=d.id AND u.status=\'active\') AS cnt '
            'FROM departments d WHERE parent_id=? ORDER BY status, name', (did,)).fetchall()
        kid_opts = {k['id']: [o for o in parent_options(c, k['id'], k['dept_type']) if o['id'] != did]
                    for k in kids if k['status'] == 'active'}
        return render_template(
            'org/detail.html', d=d, events=events(c, did), members=members, past=past, kids=kids,
            kid_opts=kid_opts, blockers=blockers(c, did) if d['status'] == 'active' else [],
            usage=usage(c, did), parent_opts=parent_options(c, did, d['dept_type']),
            hc=_hc(c, did, fy), fy=fy, type_label=TYPE_LABEL, type_color=TYPE_COLOR,
            status_label=STATUS_LABEL, event_label=EVENT_LABEL, active_page='departments')

    @app.route('/departments/export.csv')
    @admin_required
    def org_export():
        c = get_db()
        log_audit('download', 'export', None, '조직 목록 CSV')
        return Response(export_csv(c, TYPE_LABEL), mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename=organizations.csv'})
