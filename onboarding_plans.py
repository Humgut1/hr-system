"""온보딩 계획(B8).

  - 기본 계획 = 지금의 온보딩 자료(onboarding_content 'main'). 모두에게 그대로 간다.
  - 추가 계획(onboarding_plans): 조건(부서·직급·고용형태·세부 직무·근무지)에 맞는 입사자에게 자동으로 붙는다.
    여러 개가 맞으면 전부 붙고, 우선순위 순으로 보인다. 사람별 예외(포함·제외)는 onboarding_plan_people.
  - 항목(onboarding_plan_items): 할 일 · 공지 · 카드 · 영상 · 알림.
    날(day): 1 이상 = 입사일을 1일차로 센 근무일(주말·공휴일 빼고), 음수 = 입사일 며칠 전(달력 날짜).
    할 일은 onboarding_progress 에 'pl-<계획>-<항목>' 키로 들어간다 — 기본 체크리스트와 섞이지 않는다.
    공지·카드·영상은 그날부터 '내 온보딩'에 열린다. 알림은 그날 한 번 본인·매니저·버디에게 간다(onboarding_plan_sent).
  - 대상 = 재직 중이고 입사일이 오늘부터 90일 전 ~ 미래인 사람.
  - 근무지는 사람 칸이 따로 없어서 부서가 앉은 층의 건물(workplace.dept_floor)로 본다.
"""
import json
from datetime import date, timedelta

import org_admin
import workplace

_DDL = (
    '''CREATE TABLE IF NOT EXISTS onboarding_plans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        priority    INTEGER NOT NULL DEFAULT 100,
        active      INTEGER NOT NULL DEFAULT 1,
        scope       TEXT NOT NULL DEFAULT '{}',
        note        TEXT,
        created_by  INTEGER,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS onboarding_plan_items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id    INTEGER NOT NULL REFERENCES onboarding_plans(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL,
        title      TEXT NOT NULL,
        body       TEXT,
        url        TEXT,
        day        INTEGER NOT NULL DEFAULT 1,
        target     TEXT NOT NULL DEFAULT 'self',
        sort       INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_obp_items ON onboarding_plan_items(plan_id, sort)',
    '''CREATE TABLE IF NOT EXISTS onboarding_plan_people (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id    INTEGER NOT NULL REFERENCES onboarding_plans(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        mode       TEXT NOT NULL,
        reason     TEXT NOT NULL,
        created_by INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(plan_id, user_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS onboarding_plan_sent (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id  INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        sent_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(item_id, user_id)
    )''',
)

KIND_LABEL = {'task': '할 일', 'notice': '공지', 'card': '카드', 'video': '영상', 'alert': '알림'}
TARGET_LABEL = {'self': '본인', 'manager': '매니저', 'buddy': '버디'}
ETYPE_LABEL = {'full_time': '정규직', 'part_time': '시간제', 'contract': '계약직', 'intern': '인턴'}
WINDOW_DAYS = 90
MAX_BEFORE, MAX_AFTER = 30, 90
MAX_ITEMS = 40

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


def forget():
    _done.clear()


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scope(raw):
    try:
        s = json.loads(raw or '{}')
    except (TypeError, ValueError):
        s = {}
    return {k: [x for x in (s.get(k) or []) if x not in (None, '')] for k in ('depts', 'positions', 'types', 'profiles', 'sites')}


def safe_url(u):
    u = (u or '').strip()
    if not u:
        return None
    if u.startswith('https://') or (u.startswith('/') and not u.startswith('//')):
        return u[:500]
    return False


def day_label(n):
    return '입사 %d일 전' % -n if n < 0 else '%d일차' % n


def item_date(c, hire, n):
    if not hire:
        return None
    if n < 0:
        return hire + timedelta(days=n)
    return workplace.working_day(c, hire, max(n, 1))


# ── 사전 ─────────────────────────────────────────────

def choices(c):
    out = dict(depts=org_admin.active_depts(c, cols='id, name'),
               positions=_rows(c, 'SELECT id, name FROM positions ORDER BY level, id'),
               types=ETYPE_LABEL, profiles=[], sites=[])
    try:
        out['profiles'] = _rows(c, 'SELECT id, code, name FROM job_profiles ORDER BY sort_order, id')
    except Exception:
        pass
    try:
        out['sites'] = [dict(code=s['code'], name=s['name']) for s in workplace.sites(c)]
    except Exception:
        pass
    return out


def _has_col(c, t, col):
    return any(r[1] == col for r in c.execute('PRAGMA table_info(%s)' % t))


def scope_label(c, s, ch=None):
    ch = ch or choices(c)
    parts = []
    if s['depts']:
        names = {d['id']: d['name'] for d in ch['depts']}
        parts.append('조직 ' + ', '.join(names.get(int(x), '#%s' % x) for x in s['depts']))
    if s['positions']:
        names = {p['id']: p['name'] for p in ch['positions']}
        parts.append('직급 ' + ', '.join(names.get(int(x), '#%s' % x) for x in s['positions']))
    if s['types']:
        parts.append(', '.join(ETYPE_LABEL.get(x, x) for x in s['types']))
    if s['profiles']:
        names = {p['id']: p['name'] for p in ch['profiles']}
        parts.append('직무 ' + ', '.join(names.get(int(x), '#%s' % x) for x in s['profiles']))
    if s['sites']:
        names = {p['code']: p['name'] for p in ch['sites']}
        parts.append('근무지 ' + ', '.join(names.get(x, x) for x in s['sites']))
    return ' · '.join(parts) or '전원'


# ── 계획 ─────────────────────────────────────────────

def plans(c):
    ch = choices(c)
    out = []
    for p in _rows(c, 'SELECT * FROM onboarding_plans ORDER BY priority, id'):
        p['scope_obj'] = _scope(p['scope'])
        p['scope_label'] = scope_label(c, p['scope_obj'], ch)
        p['n_items'] = c.execute('SELECT COUNT(*) FROM onboarding_plan_items WHERE plan_id=?', (p['id'],)).fetchone()[0]
        out.append(p)
    return out


def plan(c, pid):
    r = _rows(c, 'SELECT * FROM onboarding_plans WHERE id=?', (pid,))
    if not r:
        return None
    p = r[0]
    p['scope_obj'] = _scope(p['scope'])
    p['scope_label'] = scope_label(c, p['scope_obj'])
    return p


def save(c, form, actor_id, pid=None):
    name = (form.get('name') or '').strip()[:80]
    if not name:
        return None, '계획 이름을 넣으세요.'
    try:
        prio = max(1, min(999, int(form.get('priority') or 100)))
    except ValueError:
        prio = 100

    def ints(k):
        out = []
        for x in form.getlist(k):
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return sorted(set(out))

    scope = dict(depts=ints('depts'), positions=ints('positions'), profiles=ints('profiles'),
                 types=sorted({x for x in form.getlist('types') if x in ETYPE_LABEL}),
                 sites=sorted({x for x in form.getlist('sites') if x}))
    active = 1 if form.get('active', '1') == '1' else 0
    note = (form.get('note') or '').strip()[:500] or None
    if pid:
        c.execute('UPDATE onboarding_plans SET name=?, priority=?, active=?, scope=?, note=?, updated_at=CURRENT_TIMESTAMP '
                  'WHERE id=?', (name, prio, active, json.dumps(scope), note, pid))
        return pid, None
    cur = c.execute('INSERT INTO onboarding_plans (name, priority, active, scope, note, created_by) VALUES (?,?,?,?,?,?)',
                    (name, prio, active, json.dumps(scope), note, actor_id))
    return cur.lastrowid, None


def delete_plan(c, pid):
    """할 일을 이미 받은 사람이 있으면 지우지 않는다 — [끄기]를 쓴다."""
    keys = ['pl-%d-%d' % (pid, r[0]) for r in c.execute(
        "SELECT id FROM onboarding_plan_items WHERE plan_id=? AND kind='task'", (pid,))]
    if keys and c.execute('SELECT 1 FROM onboarding_progress WHERE task_key IN (%s) LIMIT 1'
                          % ','.join('?' * len(keys)), keys).fetchone():
        return '이미 할 일을 받은 사람이 있어 지울 수 없습니다. [끄기]를 쓰세요.'
    c.execute('DELETE FROM onboarding_plan_sent WHERE item_id IN (SELECT id FROM onboarding_plan_items WHERE plan_id=?)', (pid,))
    c.execute('DELETE FROM onboarding_plan_items WHERE plan_id=?', (pid,))
    c.execute('DELETE FROM onboarding_plan_people WHERE plan_id=?', (pid,))
    c.execute('DELETE FROM onboarding_plans WHERE id=?', (pid,))
    return None


# ── 항목 ─────────────────────────────────────────────

def items(c, pid):
    out = _rows(c, 'SELECT * FROM onboarding_plan_items WHERE plan_id=? ORDER BY day, sort, id', (pid,))
    for i in out:
        i['kind_label'] = KIND_LABEL.get(i['kind'], i['kind'])
        i['day_label'] = day_label(i['day'])
        i['target_label'] = TARGET_LABEL.get(i['target'], '')
    return out


def save_item(c, pid, form, iid=None):
    kind = form.get('kind')
    if kind not in KIND_LABEL:
        return '종류를 고르세요.'
    title = (form.get('title') or '').strip()[:120]
    if not title:
        return '제목을 넣으세요.'
    body = (form.get('body') or '').strip()[:2000] or None
    url = safe_url(form.get('url'))
    if url is False:
        return '링크는 https:// 로 시작하는 주소나 / 로 시작하는 사내 주소만 됩니다.'
    if kind == 'video' and not url:
        return '영상은 링크가 있어야 합니다.'
    try:
        day = int(form.get('day') or 1)
    except ValueError:
        return '날짜(일차)는 숫자로 넣으세요.'
    if day == 0:
        day = 1
    if day < -MAX_BEFORE or day > MAX_AFTER:
        return '날은 입사 %d일 전 ~ %d일차 사이로 넣으세요.' % (MAX_BEFORE, MAX_AFTER)
    target = form.get('target') if kind == 'alert' and form.get('target') in TARGET_LABEL else 'self'
    if kind == 'task' and day < 1:
        return '할 일은 1일차부터 넣을 수 있습니다(입사 전에는 계정이 없을 수 있습니다).'
    if iid:
        c.execute('UPDATE onboarding_plan_items SET kind=?, title=?, body=?, url=?, day=?, target=? WHERE id=? AND plan_id=?',
                  (kind, title, body, url, day, target, iid, pid))
        return None
    if c.execute('SELECT COUNT(*) FROM onboarding_plan_items WHERE plan_id=?', (pid,)).fetchone()[0] >= MAX_ITEMS:
        return '한 계획에 항목은 %d개까지입니다.' % MAX_ITEMS
    n = c.execute('SELECT COALESCE(MAX(sort), 0) + 1 FROM onboarding_plan_items WHERE plan_id=?', (pid,)).fetchone()[0]
    c.execute('INSERT INTO onboarding_plan_items (plan_id, kind, title, body, url, day, target, sort) VALUES (?,?,?,?,?,?,?,?)',
              (pid, kind, title, body, url, day, target, n))
    return None


def delete_item(c, pid, iid):
    """끝낸 사람이 있는 할 일은 지우지 않는다 — 기록이 사라진다. 안 끝낸 줄은 같이 지운다."""
    r = c.execute('SELECT kind FROM onboarding_plan_items WHERE id=? AND plan_id=?', (iid, pid)).fetchone()
    if not r:
        return '없는 항목입니다.'
    key = 'pl-%d-%d' % (pid, iid)
    if r[0] == 'task' and c.execute('SELECT 1 FROM onboarding_progress WHERE task_key=? AND done=1 LIMIT 1', (key,)).fetchone():
        return '이미 끝낸 사람이 있는 할 일이라 지울 수 없습니다. 제목을 고치거나 계획을 끄세요.'
    c.execute('DELETE FROM onboarding_progress WHERE task_key=?', (key,))
    c.execute('DELETE FROM onboarding_plan_sent WHERE item_id=?', (iid,))
    c.execute('DELETE FROM onboarding_plan_items WHERE id=?', (iid,))
    return None


def move_item(c, pid, iid, step):
    rows = [r[0] for r in c.execute('SELECT id FROM onboarding_plan_items WHERE plan_id=? ORDER BY day, sort, id', (pid,))]
    if iid not in rows:
        return
    i = rows.index(iid)
    j = i + step
    if 0 <= j < len(rows):
        rows[i], rows[j] = rows[j], rows[i]
    for n, x in enumerate(rows, 1):
        c.execute('UPDATE onboarding_plan_items SET sort=? WHERE id=?', (n, x))


# ── 대상 ─────────────────────────────────────────────

def _people(c, uid=None, today=None):
    today = today or date.today()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    q = ('SELECT u.id, u.name, u.emp_no, u.hire_date, u.department_id, u.position_id, u.employment_type, '
         'u.manager_id, u.buddy_id, d.name AS dept, p.name AS pos%s FROM users u '
         'LEFT JOIN departments d ON d.id=u.department_id LEFT JOIN positions p ON p.id=u.position_id '
         "WHERE u.status='active' AND u.role != 'guest' AND u.hire_date IS NOT NULL AND u.hire_date >= ?"
         % (', u.job_profile_id' if _has_col(c, 'users', 'job_profile_id') else ', NULL AS job_profile_id'))
    args = [since]
    if uid:
        q += ' AND u.id=?'
        args.append(uid)
    return _rows(c, q + ' ORDER BY u.hire_date, u.name', args)


def _site_of(c, did, cache):
    if did not in cache:
        try:
            f = workplace.dept_floor(c, did)
            cache[did] = f['site_code'] if f else None
        except Exception:
            cache[did] = None
    return cache[did]


def _matches(c, s, u, desc, sites):
    if s['depts']:
        ok = set()
        for d in s['depts']:
            d = int(d)
            if d not in desc:
                desc[d] = {d} | org_admin._descendants(c, d)
            ok |= desc[d]
        if u['department_id'] not in ok:
            return False
    if s['positions'] and u['position_id'] not in {int(x) for x in s['positions']}:
        return False
    if s['types'] and u['employment_type'] not in s['types']:
        return False
    if s['profiles'] and u['job_profile_id'] not in {int(x) for x in s['profiles']}:
        return False
    if s['sites'] and _site_of(c, u['department_id'], sites) not in s['sites']:
        return False
    return True


def plans_for(c, uid, people=None):
    """한 사람에게 붙는 켜진 계획(우선순위 순). 대상 기간 밖이면 빈 목록."""
    people = people if people is not None else _people(c, uid)
    u = next((p for p in people if p['id'] == uid), None)
    if not u:
        return []
    ex = {r[0]: r[1] for r in c.execute('SELECT plan_id, mode FROM onboarding_plan_people WHERE user_id=?', (uid,))}
    desc, sites, out = {}, {}, []
    for p in _rows(c, 'SELECT * FROM onboarding_plans WHERE active=1 ORDER BY priority, id'):
        m = ex.get(p['id'])
        if m == 'out':
            continue
        if m == 'in' or _matches(c, _scope(p['scope']), u, desc, sites):
            p['how'] = 'manual' if m == 'in' else 'auto'
            out.append(p)
    return out


def audience(c, pid):
    """이 계획의 대상(자동·예외 포함) + 제외한 사람."""
    p = plan(c, pid)
    if not p:
        return [], []
    ex = {r['user_id']: r for r in _rows(c, 'SELECT * FROM onboarding_plan_people WHERE plan_id=?', (pid,))}
    desc, sites, inn, out = {}, {}, [], []
    for u in _people(c):
        e = ex.get(u['id'])
        hit = _matches(c, p['scope_obj'], u, desc, sites)
        u['reason'] = e['reason'] if e else None
        if e and e['mode'] == 'out':
            if hit:
                out.append(u)
            else:
                u['how'] = 'out'
                out.append(u)
            continue
        if (e and e['mode'] == 'in') or hit:
            u['how'] = 'manual' if e and e['mode'] == 'in' else 'auto'
            inn.append(u)
    return inn, out


def set_exception(c, pid, uid, mode, reason, actor_id):
    reason = (reason or '').strip()[:200]
    if mode not in ('in', 'out'):
        return '잘못된 요청입니다.'
    if not reason:
        return '사유를 넣으세요.'
    if not c.execute('SELECT 1 FROM onboarding_plans WHERE id=?', (pid,)).fetchone():
        return '없는 계획입니다.'
    if not _people(c, uid):
        return '재직 중이고 입사 %d일 안쪽인 사람만 넣을 수 있습니다.' % WINDOW_DAYS
    c.execute('INSERT INTO onboarding_plan_people (plan_id, user_id, mode, reason, created_by) VALUES (?,?,?,?,?) '
              'ON CONFLICT(plan_id, user_id) DO UPDATE SET mode=excluded.mode, reason=excluded.reason, '
              'created_by=excluded.created_by, created_at=CURRENT_TIMESTAMP', (pid, uid, mode, reason, actor_id))
    return None


def drop_exception(c, pid, uid):
    c.execute('DELETE FROM onboarding_plan_people WHERE plan_id=? AND user_id=?', (pid, uid))


# ── 한 사람의 화면 ────────────────────────────────────

def sync_tasks(c, uid, ps=None):
    """붙은 계획의 할 일을 onboarding_progress 에 맞춘다. 끝낸 줄은 계획이 빠져도 남긴다. commit 은 부르는 쪽."""
    ps = plans_for(c, uid) if ps is None else ps
    want = {}
    for p in ps:
        for i in items(c, p['id']):
            if i['kind'] == 'task':
                want['pl-%d-%d' % (p['id'], i['id'])] = (i['title'], 1000 + p['priority'] * 50 + i['sort'])
    for k, (label, order) in want.items():
        c.execute("INSERT OR IGNORE INTO onboarding_progress (user_id, task_key, task_label, category, sort_order) "
                  "VALUES (?,?,?,'plan',?)", (uid, k, label, order))
        c.execute('UPDATE onboarding_progress SET task_label=?, sort_order=? WHERE user_id=? AND task_key=?',
                  (label, order, uid, k))
    gone = [r[0] for r in c.execute("SELECT task_key FROM onboarding_progress WHERE user_id=? AND task_key LIKE 'pl-%' "
                                    "AND done=0", (uid,)) if r[0] not in want]
    for k in gone:
        c.execute('DELETE FROM onboarding_progress WHERE user_id=? AND task_key=?', (uid, k))


def person_view(c, uid, today=None, values=None, everything=False):
    """'내 온보딩'에 붙일 것: 계획 이름들, 할 일 기한, 열린 안내(공지·카드·영상), 곧 열릴 것."""
    today = today or date.today()
    people = _people(c, uid, today)
    ps = plans_for(c, uid, people)
    if not ps:
        return None
    u = people[0]
    hire = workplace.to_date(u['hire_date'])
    vals = values or {}
    due, shown, later = {}, [], []
    for p in ps:
        for i in items(c, p['id']):
            d = item_date(c, hire, i['day'])
            if i['kind'] == 'task':
                due['pl-%d-%d' % (p['id'], i['id'])] = d
                continue
            if i['kind'] == 'alert':
                continue
            x = dict(kind=i['kind'], kind_label=i['kind_label'], title=workplace.fill(i['title'], vals),
                     body=workplace.fill(i['body'], vals), url=i['url'], plan=p['name'], date=d,
                     date_label=('%s(%s)' % (d.strftime('%m.%d'), workplace.WEEKDAY_KO[d.weekday()])) if d else '',
                     new=bool(d and 0 <= (today - d).days <= 2))
            (shown if everything or not d or d <= today else later).append(x)
    key = lambda x: (x['date'] or today)
    return dict(plans=[dict(name=p['name'], how=p['how']) for p in ps], due=due,
                shown=sorted(shown, key=key), later=sorted(later, key=key)[:5], plan_objs=ps)


# ── 알림 ─────────────────────────────────────────────

def sweep(c, notify, today=None):
    """오늘까지 날이 된 알림 항목을 한 번씩 보낸다. 대상 기간 안의 사람만."""
    today = today or date.today()
    n = 0
    people = _people(c, today=today)
    for u in people:
        ps = plans_for(c, u['id'], people)
        if not ps:
            continue
        hire = workplace.to_date(u['hire_date'])
        for p in ps:
            for i in items(c, p['id']):
                if i['kind'] != 'alert':
                    continue
                d = item_date(c, hire, i['day'])
                if not d or d > today or (today - d).days > 7:
                    continue
                if c.execute('SELECT 1 FROM onboarding_plan_sent WHERE item_id=? AND user_id=?', (i['id'], u['id'])).fetchone():
                    continue
                to = {'self': u['id'], 'manager': u['manager_id'], 'buddy': u['buddy_id']}.get(i['target'])
                if i['target'] == 'manager' and not to and u['department_id']:
                    r = c.execute('SELECT leader_id FROM departments WHERE id=?', (u['department_id'],)).fetchone()
                    to = r[0] if r and r[0] != u['id'] else None
                c.execute('INSERT OR IGNORE INTO onboarding_plan_sent (item_id, user_id) VALUES (?,?)', (i['id'], u['id']))
                if not to:
                    continue
                title = i['title'] if i['target'] == 'self' else '%s · %s' % (u['name'], i['title'])
                notify(to, title, (i['body'] or '')[:300], i['url'] or ('/me/onboarding' if i['target'] == 'self'
                                                                           else '/employees/%d' % u['id']))
                n += 1
    c.commit()
    return n


# ── 화면 ──────────────────────────────────────────────

def init_app(app, h):
    from flask import abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    admin_required = h['admin_required']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')
    need = h.get('need') or (lambda code: admin_required)
    log_audit = h['log_audit']

    def notify(uid, title, content, link):
        get_db().execute('INSERT INTO notifications (user_id, type, category, title, content, link) '
                         'VALUES (?,?,?,?,?,?)', (uid, 'info', 'onboarding', title[:120], content, link))

    swept = {}

    @app.before_request
    def obplan_daily_sweep():
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='obplan_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('obplan_sweep_date', ?)", (today,))
                db.commit()
                sweep(db, notify)
            swept[k] = today
        except Exception as e:
            app.logger.error('onboarding plan sweep failed: %s', e)

    def demo_block():
        if session.get('demo_mode'):
            flash('데모에서는 저장되지 않습니다.', 'warning')
            return True
        return False

    @app.route('/people/onboarding-plans', methods=['GET', 'POST'])
    @need('onboarding_plans')
    def obplan_admin():
        db = get_db()
        ensure_schema(db)
        sel = request.values.get('id', type=int)
        tab = request.values.get('tab') or 'list'
        if request.method == 'POST':
            back = redirect(url_for('obplan_admin', id=sel) if sel else url_for('obplan_admin', tab=tab))
            if demo_block():
                return back
            act = request.form.get('action')
            if act == 'save':
                pid, err = save(db, request.form, session['user_id'], sel)
                if err:
                    flash(err, 'error')
                    return back
                db.commit()
                log_audit('update' if sel else 'create', 'personal_info', None, '온보딩 계획 #%s %s' % (pid, request.form.get('name')))
                flash('계획을 저장했습니다.' if sel else '계획을 만들었습니다. 이제 항목을 넣으세요.', 'success')
                return redirect(url_for('obplan_admin', id=pid))
            if not sel or not plan(db, sel):
                abort(404)
            if act == 'toggle':
                db.execute('UPDATE onboarding_plans SET active=1-active, updated_at=CURRENT_TIMESTAMP WHERE id=?', (sel,))
                db.commit()
                flash('켜고 끔을 바꿨습니다. 끈 계획의 할 일 중 끝낸 것은 남습니다.', 'success')
                return back
            if act == 'delete':
                err = delete_plan(db, sel)
                (db.rollback if err else db.commit)()
                flash(err or '지웠습니다.', 'error' if err else 'success')
                return back if err else redirect(url_for('obplan_admin'))
            if act == 'item_save':
                err = save_item(db, sel, request.form, request.form.get('item_id', type=int))
            elif act == 'item_delete':
                err = delete_item(db, sel, request.form.get('item_id', type=int))
            elif act in ('item_up', 'item_down'):
                move_item(db, sel, request.form.get('item_id', type=int), -1 if act == 'item_up' else 1)
                err = None
            elif act in ('ex_in', 'ex_out', 'ex_drop'):
                no = (request.form.get('emp_no') or '').strip()
                u = db.execute('SELECT id FROM users WHERE emp_no=?', (no,)).fetchone() if no else None
                uid = u['id'] if u else request.form.get('user_id', type=int)
                if not uid:
                    err = '사번을 찾지 못했습니다.'
                elif act == 'ex_drop':
                    drop_exception(db, sel, uid)
                    err = None
                else:
                    err = set_exception(db, sel, uid, act[3:], request.form.get('reason'), session['user_id'])
            else:
                abort(400)
            (db.rollback if err else db.commit)()
            flash(err or '저장했습니다.', 'error' if err else 'success')
            return back

        ch = choices(db)
        ctx = dict(tab=tab, active_page='obplans', kinds=KIND_LABEL, targets=TARGET_LABEL, etypes=ETYPE_LABEL,
                   ch=ch, window=WINDOW_DAYS, max_before=MAX_BEFORE, max_after=MAX_AFTER,
                   onboard_n=len(workplace.onboarding_tasks(db) or []))
        if sel:
            p = plan(db, sel)
            if not p:
                abort(404)
            inn, out = audience(db, sel)
            hire_of = {u['id']: workplace.to_date(u['hire_date']) for u in inn}
            its = items(db, sel)
            ctx.update(tab='detail', p=p, items=its, inn=inn, out=out, edit=request.args.get('edit', type=int),
                       sample=(inn[0] if inn else None),
                       sample_dates={i['id']: item_date(db, hire_of[inn[0]['id']], i['day']) for i in its} if inn else {})
        else:
            ctx.update(items_all=plans(db) if tab == 'list' else [])
        return render_template('onboarding_plans/admin.html', **ctx)
