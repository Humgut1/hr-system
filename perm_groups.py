"""B11 권한 묶음 (Workday 대조 로드맵, 2026-10-22).

관리자가 아닌 사람에게 B4~B10 업무 화면 하나씩을 맡긴다.
  - 묶음(perm_groups) = 이름 + 권한 코드 여러 개. 사람(perm_group_members)은 사번으로 넣는다.
  - has() = 관리자이거나, 그 코드를 가진 묶음에 들어 있는 재직자.
  - holders() = 그 코드를 가진 재직자 id(관리자 제외) — 알림 받을 사람을 넓힐 때.
  - 바꾼 것은 perm_group_events 에 남는다(누가·언제·무엇을).
  - 기존 역할(admin/manager/employee/recruiter) 검사는 건드리지 않는다. 묶음을 만들고 고치는 것은 관리자만.
"""
import csv
import io
import re

CODES = {
    'ids': '신분·서류',
    'tenure': '재입사·수습',
    'labor': '단체협약',
    'safety': '안전사고',
    'onboarding_plans': '온보딩 계획',
    'calendars': '근무 달력·교대',
    'consents': '개인정보 동의',
    'data_check': '데이터 점검',
}

_DDL = (
    '''CREATE TABLE IF NOT EXISTS perm_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        codes TEXT NOT NULL DEFAULT '',
        note TEXT,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS perm_group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        added_by INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, user_id))''',
    '''CREATE TABLE IF NOT EXISTS perm_group_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        group_name TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        by_user INTEGER,
        at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    'CREATE INDEX IF NOT EXISTS idx_pgm_user ON perm_group_members(user_id)',
)

ACTION_LABEL = {'create': '묶음 만듦', 'rename': '이름 바꿈', 'codes': '권한 바꿈', 'add': '사람 넣음',
                'remove': '사람 뺌', 'delete': '묶음 지움'}

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


def forget(c=None):
    _done.clear()


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _codes(s):
    return [x for x in (s or '').split(',') if x in CODES]


def _clean_codes(lst):
    return ','.join(k for k in CODES if k in set(lst or []))


# ── 판단 ──────────────────────────────────────────────
def codes_of(c, uid):
    """그 사람이 묶음으로 받은 코드 모음(관리자 여부와 무관)."""
    if not uid:
        return set()
    ensure_schema(c)
    out = set()
    for r in c.execute('SELECT g.codes FROM perm_group_members m JOIN perm_groups g ON g.id=m.group_id '
                       "JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND u.status='active'", (uid,)):
        out.update(_codes(r[0]))
    return out


def has(c, uid, role, code):
    if role == 'admin':
        return True
    return code in codes_of(c, uid)


def holders(c, code):
    """그 코드를 가진 재직자 id(관리자는 부르는 쪽이 따로 더한다)."""
    ensure_schema(c)
    ids = set()
    for r in c.execute('SELECT m.user_id, g.codes FROM perm_group_members m JOIN perm_groups g ON g.id=m.group_id '
                       "JOIN users u ON u.id=m.user_id WHERE u.status='active'"):
        if code in _codes(r[1]):
            ids.add(r[0])
    return sorted(ids)


# ── 묶음 ──────────────────────────────────────────────
def _event(c, gid, gname, action, detail, by):
    c.execute('INSERT INTO perm_group_events (group_id, group_name, action, detail, by_user) VALUES (?,?,?,?,?)',
              (gid, gname, action, detail, by))


def groups(c):
    ensure_schema(c)
    gs = _rows(c, 'SELECT g.*, (SELECT COUNT(*) FROM perm_group_members m JOIN users u ON u.id=m.user_id '
                  "WHERE m.group_id=g.id AND u.status='active') AS n FROM perm_groups g ORDER BY g.name")
    for g in gs:
        g['code_list'] = _codes(g['codes'])
    return gs


def group(c, gid):
    ensure_schema(c)
    r = _rows(c, 'SELECT * FROM perm_groups WHERE id=?', (gid,))
    if not r:
        return None
    g = r[0]
    g['code_list'] = _codes(g['codes'])
    return g


def members(c, gid):
    return _rows(c, 'SELECT m.id, m.user_id, m.added_at, u.emp_no, u.name, u.status, d.name AS dept, '
                    '(SELECT name FROM users WHERE id=m.added_by) AS added_by_name '
                    'FROM perm_group_members m JOIN users u ON u.id=m.user_id '
                    'LEFT JOIN departments d ON d.id=u.department_id WHERE m.group_id=? ORDER BY u.name', (gid,))


def create_group(c, name, codes, note, by):
    ensure_schema(c)
    name = (name or '').strip()
    if not name:
        return None, '묶음 이름을 넣어 주세요.'
    if c.execute('SELECT 1 FROM perm_groups WHERE name=?', (name,)).fetchone():
        return None, '같은 이름의 묶음이 있습니다.'
    cs = _clean_codes(codes)
    if not cs:
        return None, '권한을 하나 이상 고르세요.'
    cur = c.execute('INSERT INTO perm_groups (name, codes, note, created_by) VALUES (?,?,?,?)',
                    (name[:60], cs, (note or '').strip()[:200] or None, by))
    _event(c, cur.lastrowid, name, 'create', ' · '.join(CODES[k] for k in _codes(cs)), by)
    return cur.lastrowid, None


def update_group(c, gid, name, codes, note, by):
    g = group(c, gid)
    if not g:
        return '없는 묶음입니다.'
    name = (name or '').strip()
    if not name:
        return '묶음 이름을 넣어 주세요.'
    if c.execute('SELECT 1 FROM perm_groups WHERE name=? AND id<>?', (name, gid)).fetchone():
        return '같은 이름의 묶음이 있습니다.'
    cs = _clean_codes(codes)
    if not cs:
        return '권한을 하나 이상 고르세요. 묶음을 없애려면 [묶음 지우기]를 쓰세요.'
    c.execute('UPDATE perm_groups SET name=?, codes=?, note=? WHERE id=?',
              (name[:60], cs, (note or '').strip()[:200] or None, gid))
    if name != g['name']:
        _event(c, gid, name, 'rename', '%s → %s' % (g['name'], name), by)
    old, new = set(g['code_list']), set(_codes(cs))
    if old != new:
        parts = ['+' + CODES[k] for k in CODES if k in new - old] + ['−' + CODES[k] for k in CODES if k in old - new]
        _event(c, gid, name, 'codes', ' '.join(parts), by)
    return None


def add_members(c, gid, emp_nos, by):
    """사번 여러 개(쉼표·줄바꿈). (넣은 수, 건너뜀 목록, 오류)."""
    g = group(c, gid)
    if not g:
        return 0, [], '없는 묶음입니다.'
    toks = [t for t in re.split(r'[\s,;]+', emp_nos or '') if t]
    if not toks:
        return 0, [], '사번을 넣어 주세요.'
    n, skipped, names = 0, [], []
    for t in dict.fromkeys(toks):
        u = c.execute('SELECT id, name, status, role FROM users WHERE emp_no=?', (t,)).fetchone()
        if not u:
            skipped.append('%s(없는 사번)' % t)
            continue
        if u['status'] != 'active':
            skipped.append('%s(재직자 아님)' % t)
            continue
        if u['role'] == 'admin':
            skipped.append('%s(관리자는 이미 전부 가짐)' % t)
            continue
        cur = c.execute('INSERT OR IGNORE INTO perm_group_members (group_id, user_id, added_by) VALUES (?,?,?)',
                        (gid, u['id'], by))
        if not cur.rowcount:
            skipped.append('%s(이미 있음)' % t)
            continue
        n += 1
        names.append('%s(%s)' % (u['name'], t))
    if names:
        _event(c, gid, g['name'], 'add', ', '.join(names), by)
    return n, skipped, None


def remove_member(c, gid, mid, by):
    g = group(c, gid)
    r = c.execute('SELECT u.name, u.emp_no FROM perm_group_members m JOIN users u ON u.id=m.user_id '
                  'WHERE m.id=? AND m.group_id=?', (mid, gid)).fetchone()
    if not g or not r:
        return '없는 줄입니다.'
    c.execute('DELETE FROM perm_group_members WHERE id=?', (mid,))
    _event(c, gid, g['name'], 'remove', '%s(%s)' % (r['name'], r['emp_no'] or '-'), by)
    return None


def delete_group(c, gid, by):
    g = group(c, gid)
    if not g:
        return '없는 묶음입니다.'
    n = c.execute('SELECT COUNT(*) FROM perm_group_members WHERE group_id=?', (gid,)).fetchone()[0]
    c.execute('DELETE FROM perm_group_members WHERE group_id=?', (gid,))
    c.execute('DELETE FROM perm_groups WHERE id=?', (gid,))
    _event(c, gid, g['name'], 'delete', '사람 %d명과 함께' % n, by)
    return None


def events(c, gid=None, limit=200):
    ensure_schema(c)
    q = ('SELECT e.*, u.name AS by_name FROM perm_group_events e LEFT JOIN users u ON u.id=e.by_user')
    args = ()
    if gid:
        q += ' WHERE e.group_id=?'
        args = (gid,)
    out = _rows(c, q + ' ORDER BY e.id DESC LIMIT ?', args + (limit,))
    for e in out:
        e['label'] = ACTION_LABEL.get(e['action'], e['action'])
    return out


def to_csv(c):
    """사람×묶음 한 줄씩 — 사번이 첫 칸."""
    rows = _rows(c, 'SELECT u.emp_no, u.name, d.name AS dept, g.name AS grp, g.codes, m.added_at '
                    'FROM perm_group_members m JOIN perm_groups g ON g.id=m.group_id JOIN users u ON u.id=m.user_id '
                    'LEFT JOIN departments d ON d.id=u.department_id ORDER BY u.emp_no, g.name')
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서', '묶음', '권한', '넣은 날'])
    for r in rows:
        w.writerow([r['emp_no'] or '', r['name'], r['dept'] or '', r['grp'],
                    ' · '.join(CODES[k] for k in _codes(r['codes'])), (r['added_at'] or '')[:10]])
    return '﻿' + buf.getvalue()


# ── 화면 ──────────────────────────────────────────────
def init_app(app, h):
    from flask import Response, abort, flash, g, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    admin_required = h['admin_required']
    log_audit = h['log_audit']

    def can(code):
        """지금 로그인한 사람이 그 코드를 가졌나. 요청마다 한 번만 DB 를 본다."""
        if session.get('user_role') == 'admin':
            return True
        uid = session.get('user_id')
        if not uid:
            return False
        cache = g.get('_perm_codes')
        if cache is None:
            try:
                cache = codes_of(get_db(), uid)
            except Exception:
                cache = set()
            g._perm_codes = cache
        return code in cache

    app.jinja_env.globals['perm'] = can

    @app.route('/settings/perm-groups', methods=['GET', 'POST'])
    @admin_required
    def perm_groups_admin():
        db = get_db()
        ensure_schema(db)
        uid = session['user_id']
        if request.method == 'POST':
            a = request.form.get('action')
            f = request.form
            gid = f.get('group_id', type=int)
            err, msg = None, None
            if a == 'new':
                gid, err = create_group(db, f.get('name'), f.getlist('codes'), f.get('note'), uid)
                msg = '묶음을 만들었습니다.'
            elif a == 'edit':
                err = update_group(db, gid, f.get('name'), f.getlist('codes'), f.get('note'), uid)
                msg = '묶음을 고쳤습니다.'
            elif a == 'add':
                n, skipped, err = add_members(db, gid, f.get('emp_nos'), uid)
                if not err:
                    msg = '%d명 넣었습니다.' % n
                    if skipped:
                        msg += ' 건너뜀: %s' % ' · '.join(skipped[:10])
            elif a == 'remove':
                err = remove_member(db, gid, f.get('member_id', type=int), uid)
                msg = '뺐습니다.'
            elif a == 'delete':
                err = delete_group(db, gid, uid)
                msg, gid = '묶음을 지웠습니다.', None
            else:
                abort(400)
            if err:
                db.rollback()
                flash(err, 'error')
            else:
                db.commit()
                log_audit('update', 'auth', None, 'perm_groups %s #%s' % (a, gid or ''))
                flash(msg, 'success')
            return redirect(url_for('perm_groups_admin', id=gid) if gid else url_for('perm_groups_admin'))

        if request.args.get('csv'):
            log_audit('download', 'export', None, 'perm_groups members csv')
            return Response(to_csv(db), mimetype='text/csv',
                            headers={'Content-Disposition': 'attachment; filename=perm_groups.csv'})
        sel = request.args.get('id', type=int)
        cur = group(db, sel) if sel else None
        return render_template('perm_groups/index.html', codes=CODES, gs=groups(db), cur=cur,
                               mem=members(db, sel) if cur else [], evs=events(db, sel if cur else None, 50),
                               active_page='perm_groups')

    return can
