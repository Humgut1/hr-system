"""승계 계획(B14): 핵심 포지션마다 후임 후보 · 준비도.

  - 표 `key_positions`(핵심 포지션 한 줄) + 기존 `succession_plans`(후보 한 줄)에 key_position_id 를 붙인다.
    옛 계획은 (포지션명, 현직자) 로 묶어 key_positions 를 만들어 채운다. position_title 은 계속 채운다
    (인재 카드가 그 칸을 읽는다).
  - 사람은 언제나 사번으로 받는다 — 화면 입력 · CSV · 내보내기 모두.
  - 상태: 후임 없음 / 현직자 퇴사 예정인데 즉시 후임 없음 = 위험, 즉시 후임 없음 · 현직자 비어 있음 = 주의,
    즉시 가능한 재직 후보가 있으면 정상. 퇴사한 후보는 세지 않는다.
  - 관리자 · 권한 묶음 'succession' = 전사, 매니저 = 자기 조직(맡은 조직 + 하위)만.
"""
import csv
import io
from collections import OrderedDict
from datetime import date

READINESS = OrderedDict([('ready_now', '즉시 가능'), ('ready_1y', '1년 내'),
                         ('ready_2y', '2년 내'), ('long_term', '장기 육성')])
_READY_BY_LABEL = {v: k for k, v in READINESS.items()}
IMPORTANCE = OrderedDict([('critical', '핵심'), ('important', '중요')])
STATUS = OrderedDict([('late', '위험'), ('wait', '주의'), ('done', '정상')])
_LEAVING_REQ = ('submitted', 'under_review', 'approved', 'in_progress')

_DDL = (
    '''CREATE TABLE IF NOT EXISTS key_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        dept_id INTEGER REFERENCES departments(id),
        incumbent_id INTEGER REFERENCES users(id),
        importance TEXT NOT NULL DEFAULT 'critical',
        note TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    'CREATE INDEX IF NOT EXISTS idx_key_positions_dept ON key_positions(dept_id)',
)

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
    cols = [r[1] for r in c.execute('PRAGMA table_info(succession_plans)')]
    if cols and 'key_position_id' not in cols:
        try:
            c.execute('ALTER TABLE succession_plans ADD COLUMN key_position_id INTEGER REFERENCES key_positions(id)')
        except Exception as e:
            if 'duplicate column' not in str(e):
                raise
    if cols:
        _migrate(c)
    c.commit()
    if k:
        _done.add(k)


def _migrate(c):
    """옛 계획(key_position_id 없음)을 (포지션명, 현직자) 로 묶어 핵심 포지션을 만든다."""
    olds = c.execute(
        'SELECT sp.id, sp.position_title, sp.incumbent_id, sp.created_by, u.department_id '
        'FROM succession_plans sp LEFT JOIN users u ON u.id = sp.incumbent_id '
        'WHERE sp.key_position_id IS NULL ORDER BY sp.id').fetchall()
    made = {}
    for r in olds:
        key = ((r[1] or '').strip(), r[2])
        kp = made.get(key)
        if not kp:
            cur = c.execute('INSERT INTO key_positions (title, dept_id, incumbent_id, created_by) VALUES (?,?,?,?)',
                            (key[0] or '이름 없는 포지션', r[4], r[2], r[3]))
            kp = made[key] = cur.lastrowid
        c.execute('UPDATE succession_plans SET key_position_id=? WHERE id=?', (kp, r[0]))


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _descendants(c, roots):
    kids = {}
    for r in c.execute('SELECT id, parent_id FROM departments'):
        kids.setdefault(r[1], []).append(r[0])
    out, stack = set(), [x for x in roots if x]
    while stack:
        d = stack.pop()
        if d in out:
            continue
        out.add(d)
        stack.extend(kids.get(d, []))
    return out


def scope_depts(c, uid, dept_id, full):
    """None = 전사. 아니면 매니저가 볼 수 있는 조직 id 묶음(맡은 조직 + 내 부서 + 하위)."""
    if full:
        return None
    roots = [r[0] for r in c.execute('SELECT id FROM departments WHERE leader_id=?', (uid,))]
    if dept_id:
        roots.append(int(dept_id))
    return _descendants(c, roots)


def leaving(c, today):
    """현직자 퇴사 예정 — {uid: 'MM/DD' 또는 '신청'}."""
    out = {}
    for r in c.execute("SELECT id, termination_date FROM users WHERE status='active' "
                       "AND termination_date IS NOT NULL AND termination_date > ?", (today.isoformat(),)):
        out[r[0]] = r[1][5:10].replace('-', '/')
    try:
        q = 'SELECT user_id FROM termination_requests WHERE status IN (%s)' % ','.join('?' * len(_LEAVING_REQ))
        for r in c.execute(q, _LEAVING_REQ):
            out.setdefault(r[0], '신청')
    except Exception:
        pass
    return out


def _person(c, emp_no):
    no = (emp_no or '').strip()
    if not no:
        return None
    r = c.execute("SELECT id, name, department_id FROM users WHERE emp_no=? AND status='active' AND role!='guest'",
                  (no,)).fetchone()
    return dict(id=r[0], name=r[1], dept_id=r[2]) if r else None


def _dept_by(c, val):
    """조직 id(숫자) 또는 조직 코드 → id. 닫힌 조직은 안 받는다."""
    v = (str(val or '')).strip()
    if not v:
        return None
    try:
        r = c.execute("SELECT id FROM departments WHERE (id=? OR code=?) AND COALESCE(status,'active')='active'",
                      (int(v) if v.isdigit() else -1, v.upper())).fetchone()
    except Exception:
        r = c.execute('SELECT id FROM departments WHERE id=?', (int(v) if v.isdigit() else -1,)).fetchone()
    return r[0] if r else None


def board(c, today=None, scope=None):
    """핵심 포지션 목록 + 후보 + 상태. scope=None 이면 전사."""
    ensure_schema(c)
    today = today or date.today()
    lv = leaving(c, today)
    kps = _rows(c, '''SELECT kp.*, d.name AS dept_name, d.code AS dept_code,
                             u.name AS inc_name, u.emp_no AS inc_no, u.status AS inc_status
                      FROM key_positions kp
                      LEFT JOIN departments d ON d.id = kp.dept_id
                      LEFT JOIN users u ON u.id = kp.incumbent_id
                      ORDER BY kp.title, kp.id''')
    if scope is not None:
        kps = [k for k in kps if k['dept_id'] in scope]
    ids = [k['id'] for k in kps]
    cands = {}
    if ids:
        for r in _rows(c, '''SELECT sp.id, sp.key_position_id, sp.candidate_id, sp.readiness, sp.note, sp.created_at,
                                    u.name, u.emp_no, u.status, d.name AS dept, p.name AS grade
                             FROM succession_plans sp JOIN users u ON u.id = sp.candidate_id
                             LEFT JOIN departments d ON d.id = u.department_id
                             LEFT JOIN positions p ON p.id = u.position_id
                             WHERE sp.key_position_id IN (%s)''' % ','.join('?' * len(ids)), ids):
            cands.setdefault(r['key_position_id'], []).append(r)
    # 다른 포지션에도 올라간 후보 — 전사 기준으로 센다(내 조직 밖 포지션도 겹침이다)
    spread = {}
    for r in c.execute('SELECT candidate_id, COUNT(DISTINCT key_position_id) FROM succession_plans '
                       'WHERE key_position_id IS NOT NULL GROUP BY candidate_id'):
        spread[r[0]] = r[1]
    order = list(READINESS)
    out = []
    for k in kps:
        cs = sorted(cands.get(k['id'], []), key=lambda x: (order.index(x['readiness']) if x['readiness'] in order else 9,
                                                         x['name']))
        for x in cs:
            x['left'] = x['status'] != 'active'
            x['ready_label'] = READINESS.get(x['readiness'], x['readiness'])
            x['others'] = max(spread.get(x['candidate_id'], 1) - 1, 0)
        live = [x for x in cs if not x['left']]
        now = [x for x in live if x['readiness'] == 'ready_now']
        vacant = not k['incumbent_id'] or k['inc_status'] != 'active'
        inc_leave = None if vacant else lv.get(k['incumbent_id'])
        if not live:
            st, why = 'late', '후임 후보 없음'
        elif inc_leave and not now:
            st, why = 'late', '현직자 퇴사 예정 · 즉시 후임 없음'
        elif not now:
            st, why = 'wait', '즉시 가능한 후임 없음'
        elif vacant:
            st, why = 'wait', '현직자 비어 있음 · 즉시 후임 %d명' % len(now)
        else:
            st, why = 'done', '즉시 후임 %d명' % len(now)
        k.update(cands=cs, live=len(live), now=len(now), vacant=vacant, inc_leave=inc_leave,
                 status=st, status_label=STATUS[st], why=why,
                 importance_label=IMPORTANCE.get(k['importance'], k['importance']),
                 gone=sum(1 for x in cs if x['left']))
        out.append(k)
    return out


def summary(rows):
    return dict(total=len(rows),
                ready=sum(1 for k in rows if k['now']),
                none=sum(1 for k in rows if not k['live']),
                leaving=sum(1 for k in rows if k['inc_leave']),
                late=sum(1 for k in rows if k['status'] == 'late'),
                wait=sum(1 for k in rows if k['status'] == 'wait'),
                avg=round(sum(k['live'] for k in rows) * 1.0 / len(rows), 1) if rows else 0)


def _in_scope(dept_id, scope):
    return scope is None or dept_id in scope


def _kp(c, kid, scope):
    r = c.execute('SELECT * FROM key_positions WHERE id=?', (kid,)).fetchone()
    if not r or not _in_scope(r['dept_id'], scope):
        return None
    return r


def _sync_plans(c, kid):
    """포지션 이름·현직자가 바뀌면 후보 줄의 옛 칸도 맞춘다(인재 카드가 읽음)."""
    c.execute('UPDATE succession_plans SET position_title=(SELECT title FROM key_positions WHERE id=?), '
              'incumbent_id=(SELECT incumbent_id FROM key_positions WHERE id=?), updated_at=CURRENT_TIMESTAMP '
              'WHERE key_position_id=?', (kid, kid, kid))


def save_position(c, f, uid, scope, kid=None):
    """포지션 추가·고치기 → (id, err). 현직자는 사번, 조직은 비면 현직자 부서."""
    ensure_schema(c)
    title = (f.get('title') or '').strip()[:80]
    if not title:
        return None, '포지션 이름을 적어 주세요.'
    inc = None
    if (f.get('incumbent_no') or '').strip():
        inc = _person(c, f.get('incumbent_no'))
        if not inc:
            return None, '현직자 사번으로 재직자를 찾지 못했습니다.'
    dept = _dept_by(c, f.get('dept'))
    if (f.get('dept') or '').strip() and not dept:
        return None, '없는 조직이거나 닫힌 조직입니다.'
    dept = dept or (inc and inc['dept_id'])
    if not dept:
        return None, '조직을 고르세요(현직자가 있으면 그 부서로 채웁니다).'
    if not _in_scope(dept, scope):
        return None, '내 조직 밖의 포지션은 만들 수 없습니다.'
    imp = f.get('importance') if f.get('importance') in IMPORTANCE else 'critical'
    note = (f.get('note') or '').strip()[:300] or None
    dup = c.execute('SELECT id FROM key_positions WHERE title=? AND dept_id=? AND id!=?',
                    (title, dept, kid or 0)).fetchone()
    if dup:
        return None, '같은 조직에 같은 이름의 포지션이 있습니다.'
    if inc:
        cur = c.execute('SELECT 1 FROM succession_plans WHERE key_position_id=? AND candidate_id=?',
                        (kid or 0, inc['id'])).fetchone()
        if cur:
            return None, '후보 명단에 있는 사람은 현직자로 둘 수 없습니다. 후보에서 먼저 빼세요.'
    if kid:
        if not _kp(c, kid, scope):
            return None, '포지션을 찾지 못했습니다.'
        c.execute('UPDATE key_positions SET title=?, dept_id=?, incumbent_id=?, importance=?, note=?, '
                  'updated_at=CURRENT_TIMESTAMP WHERE id=?', (title, dept, inc and inc['id'], imp, note, kid))
        _sync_plans(c, kid)
        return kid, None
    cur = c.execute('INSERT INTO key_positions (title, dept_id, incumbent_id, importance, note, created_by) '
                    'VALUES (?,?,?,?,?,?)', (title, dept, inc and inc['id'], imp, note, uid))
    return cur.lastrowid, None


def add_candidate(c, kid, emp_no, readiness, note, uid, scope):
    ensure_schema(c)
    kp = _kp(c, kid, scope)
    if not kp:
        return '포지션을 찾지 못했습니다.'
    p = _person(c, emp_no)
    if not p:
        return '후보 사번으로 재직자를 찾지 못했습니다.'
    if p['id'] == kp['incumbent_id']:
        return '현직자는 후보가 될 수 없습니다.'
    if c.execute('SELECT 1 FROM succession_plans WHERE key_position_id=? AND candidate_id=?',
                 (kid, p['id'])).fetchone():
        return '%s 님은 이미 이 포지션의 후보입니다.' % p['name']
    if readiness not in READINESS:
        return '준비도를 고르세요.'
    c.execute('INSERT INTO succession_plans (position_title, incumbent_id, candidate_id, readiness, note, '
              'created_by, key_position_id) VALUES (?,?,?,?,?,?,?)',
              (kp['title'], kp['incumbent_id'], p['id'], readiness, (note or '').strip()[:300] or None, uid, kid))
    return None


def _plan(c, sp_id, scope):
    r = c.execute('SELECT sp.id, kp.dept_id FROM succession_plans sp JOIN key_positions kp '
                  'ON kp.id = sp.key_position_id WHERE sp.id=?', (sp_id,)).fetchone()
    return r if r and _in_scope(r[1], scope) else None


def update_candidate(c, sp_id, readiness, note, scope):
    ensure_schema(c)
    if not _plan(c, sp_id, scope):
        return '후보를 찾지 못했습니다.'
    if readiness not in READINESS:
        return '준비도를 고르세요.'
    c.execute('UPDATE succession_plans SET readiness=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
              (readiness, (note or '').strip()[:300] or None, sp_id))
    return None


def remove_candidate(c, sp_id, scope):
    ensure_schema(c)
    if not _plan(c, sp_id, scope):
        return '후보를 찾지 못했습니다.'
    c.execute('DELETE FROM succession_plans WHERE id=?', (sp_id,))
    return None


def delete_position(c, kid, scope):
    ensure_schema(c)
    if not _kp(c, kid, scope):
        return '포지션을 찾지 못했습니다.'
    n = c.execute('SELECT COUNT(*) FROM succession_plans WHERE key_position_id=?', (kid,)).fetchone()[0]
    if n:
        return '후보 %d명을 먼저 빼야 지울 수 있습니다.' % n
    c.execute('DELETE FROM key_positions WHERE id=?', (kid,))
    return None


CSV_HEAD = ['포지션', '조직코드', '현직자 사번', '후보자 사번', '준비도', '메모']


def import_csv(c, raw, uid, scope):
    """CSV → (저장 건수, 오류 목록). 한 줄이라도 틀리면 아무것도 저장하지 않는다.

    같은 (포지션, 조직) 이 있으면 그 포지션에 후보를 더하고, 없으면 만든다. 이미 있는 후보는 준비도·메모를 고친다.
    """
    ensure_schema(c)
    for enc in ('utf-8-sig', 'cp949'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        return 0, ['파일 글자를 읽지 못했습니다(UTF-8 또는 CP949).']
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [h.strip() for h in rows[0][:4]] != CSV_HEAD[:4]:
        return 0, ['첫 줄은 %s 이어야 합니다.' % ', '.join(CSV_HEAD)]
    errs, plan = [], []
    for i, r in enumerate(rows[1:], 2):
        r = [x.strip() for x in r] + [''] * 6
        if not any(r[:6]):
            continue
        title, code, inc_no, cand_no, ready, note = r[:6]
        if not title or not code or not cand_no:
            errs.append('%d줄: 포지션 · 조직코드 · 후보자 사번은 꼭 있어야 합니다.' % i)
            continue
        dept = _dept_by(c, code)
        if not dept:
            errs.append('%d줄: 조직코드 %s 를 찾지 못했습니다.' % (i, code))
            continue
        if not _in_scope(dept, scope):
            errs.append('%d줄: 내 조직 밖입니다.' % i)
            continue
        inc = _person(c, inc_no) if inc_no else None
        if inc_no and not inc:
            errs.append('%d줄: 현직자 사번 %s 재직자 없음.' % (i, inc_no))
            continue
        cand = _person(c, cand_no)
        if not cand:
            errs.append('%d줄: 후보자 사번 %s 재직자 없음.' % (i, cand_no))
            continue
        rk = ready if ready in READINESS else _READY_BY_LABEL.get(ready, None if ready else 'ready_1y')
        if not rk:
            errs.append('%d줄: 준비도는 %s 중 하나.' % (i, ' / '.join(READINESS.values())))
            continue
        if inc and inc['id'] == cand['id']:
            errs.append('%d줄: 현직자와 후보자가 같습니다.' % i)
            continue
        plan.append((title[:80], dept, inc, cand, rk, note[:300] or None))
    if errs:
        return 0, errs
    n = 0
    for title, dept, inc, cand, rk, note in plan:
        r = c.execute('SELECT id, incumbent_id FROM key_positions WHERE title=? AND dept_id=?', (title, dept)).fetchone()
        if r:
            kid = r[0]
            if inc and r[1] != inc['id']:
                c.execute('UPDATE key_positions SET incumbent_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                          (inc['id'], kid))
                _sync_plans(c, kid)
        else:
            kid = c.execute('INSERT INTO key_positions (title, dept_id, incumbent_id, created_by) VALUES (?,?,?,?)',
                            (title, dept, inc and inc['id'], uid)).lastrowid
        cur_inc = c.execute('SELECT incumbent_id FROM key_positions WHERE id=?', (kid,)).fetchone()[0]
        if cur_inc == cand['id']:
            c.rollback()
            return 0, ['%s: 현직자가 후보로 들어갑니다.' % title]
        ex = c.execute('SELECT id FROM succession_plans WHERE key_position_id=? AND candidate_id=?',
                       (kid, cand['id'])).fetchone()
        if ex:
            c.execute('UPDATE succession_plans SET readiness=?, note=COALESCE(?, note), updated_at=CURRENT_TIMESTAMP '
                      'WHERE id=?', (rk, note, ex[0]))
        else:
            c.execute('INSERT INTO succession_plans (position_title, incumbent_id, candidate_id, readiness, note, '
                      'created_by, key_position_id) VALUES (?,?,?,?,?,?,?)',
                      (title, cur_inc, cand['id'], rk, note, uid, kid))
        n += 1
    return n, []


def export_rows(rows):
    """내보내기 줄 — 후보 한 줄씩, 후보 없는 포지션도 한 줄."""
    head = ['포지션', '조직', '조직코드', '중요도', '상태', '상태 설명', '현직자', '현직자 사번', '현직자 퇴사 예정',
            '후보자', '후보자 사번', '후보자 부서', '후보자 직급', '준비도', '후보 재직', '메모', '등록일']
    out = []
    for k in rows:
        base = [k['title'], k['dept_name'] or '', k['dept_code'] or '', k['importance_label'], k['status_label'],
                k['why'], '' if k['vacant'] else (k['inc_name'] or ''), '' if k['vacant'] else (k['inc_no'] or ''),
                k['inc_leave'] or '']
        if not k['cands']:
            out.append(base + [''] * 8)
        for x in k['cands']:
            out.append(base + [x['name'], x['emp_no'] or '', x['dept'] or '', x['grade'] or '', x['ready_label'],
                               '퇴사' if x['left'] else '재직', x['note'] or '', (x['created_at'] or '')[:10]])
    return head, out


def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    log_audit = h['log_audit']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')

    def full():
        return can('succession')

    def allowed():
        return full() or session.get('user_role') == 'manager'

    app.jinja_env.globals['succession_can'] = allowed

    @app.route('/performance/succession', methods=['GET', 'POST'], endpoint='succession')
    @login_required
    def succession_board():
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        uid = session['user_id']
        scope = scope_depts(db, uid, session.get('dept_id'), full())

        if request.method == 'POST':
            back = request.form.get('back') or url_for('succession')
            if not back.startswith('/') or back.startswith('//'):
                back = url_for('succession')
            if session.get('demo_mode'):
                flash('데모에서는 저장되지 않습니다.', 'warning')
                return redirect(back)
            act = request.form.get('action')
            kid = request.form.get('kp_id', type=int)
            sp = request.form.get('sp_id', type=int)
            err, ok = None, None
            if act == 'add_position':
                kid, err = save_position(db, request.form, uid, scope)
                ok = '핵심 포지션을 추가했습니다.'
                back = url_for('succession', open=kid) + '#kp-%s' % kid if kid else back
            elif act == 'edit_position':
                _, err = save_position(db, request.form, uid, scope, kid=kid)
                ok = '포지션을 고쳤습니다.'
            elif act == 'delete_position':
                err = delete_position(db, kid, scope)
                ok = '포지션을 지웠습니다.'
            elif act == 'add_candidate':
                err = add_candidate(db, kid, request.form.get('emp_no'), request.form.get('readiness'),
                                    request.form.get('note'), uid, scope)
                ok = '후보를 추가했습니다.'
            elif act == 'update_candidate':
                err = update_candidate(db, sp, request.form.get('readiness'), request.form.get('note'), scope)
                ok = '후보를 고쳤습니다.'
            elif act == 'remove_candidate':
                err = remove_candidate(db, sp, scope)
                ok = '후보에서 뺐습니다.'
            elif act == 'import':
                fobj = request.files.get('file')
                if not fobj or not fobj.filename:
                    err = 'CSV 파일을 고르세요.'
                else:
                    n, errs = import_csv(db, fobj.read(), uid, scope)
                    if errs:
                        db.rollback()
                        for e in errs[:20]:
                            flash(e, 'error')
                        if len(errs) > 20:
                            flash('그 밖에 %d줄 더 틀렸습니다.' % (len(errs) - 20), 'error')
                        flash('아무것도 저장하지 않았습니다. 고친 뒤 다시 올려 주세요.', 'error')
                        return redirect(back)
                    ok = 'CSV %d줄을 반영했습니다.' % n
                    log_audit('update', 'performance', None, '승계 계획 CSV %d줄' % n)
            else:
                abort(400)
            if err:
                db.rollback()
                flash(err, 'error')
            else:
                db.commit()
                flash(ok, 'success')
            return redirect(back)

        rows = board(db, scope=scope)
        sm = summary(rows)
        st = request.args.get('st') or ''
        dept = request.args.get('dept', type=int)
        q = (request.args.get('q') or '').strip()
        shown = rows
        if st == 'leaving':
            shown = [k for k in shown if k['inc_leave']]
        elif st in STATUS:
            shown = [k for k in shown if k['status'] == st]
        if dept:
            sub = _descendants(db, [dept])
            shown = [k for k in shown if k['dept_id'] in sub]
        if q:
            ql = q.lower()
            shown = [k for k in shown
                     if ql in (k['title'] or '').lower() or ql in (k['inc_name'] or '') or ql in (k['inc_no'] or '').lower()
                     or any(ql in x['name'] or ql in (x['emp_no'] or '').lower() for x in k['cands'])]
        if request.args.get('csv'):
            head, out = export_rows(shown)
            buf = io.StringIO()
            buf.write('﻿')
            w = csv.writer(buf)
            w.writerow(head)
            w.writerows(out)
            log_audit('download', 'performance', None, '승계 계획 CSV %d줄' % len(out))
            return Response(buf.getvalue(), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': "attachment; filename*=UTF-8''succession.csv"})
        depts = _rows(db, "SELECT id, name, code FROM departments WHERE COALESCE(status,'active')='active' ORDER BY name")
        if scope is not None:
            depts = [d for d in depts if d['id'] in scope]
        people = _rows(db, "SELECT u.emp_no, u.name, d.name AS dept FROM users u "
                           "LEFT JOIN departments d ON d.id = u.department_id "
                           "WHERE u.status='active' AND u.role!='guest' AND COALESCE(u.emp_no,'')!='' ORDER BY u.name")
        return render_template('performance/succession.html', rows=shown, sm=sm, all_n=len(rows),
                               st=st, dept=dept, q=q, depts=depts, people=people, full=full(),
                               readiness=READINESS, importance=IMPORTANCE, csv_head=CSV_HEAD,
                               open_id=request.args.get('open', type=int), active_page='succession')
