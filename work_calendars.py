"""B10 근무 달력·교대 (Workday 대조 로드맵, 2026-10-22).

근무 달력
  - 기본 달력 = 기존 public_holidays 그대로(/admin/holidays 에서 고친다).
    출퇴근·주 52시간·입사일 월·수 규칙은 workplace.holidays() 를 그대로 쓰므로 결과가 바뀌지 않는다.
  - 사업장 달력 = 기본 달력 위에 '쉬는 날 더하기(off)'·'일하는 날로 바꾸기(work)' 만 얹는다.
    holidays_for() = 기본 ∪ off − work.
  - 근무지(workplace_sites)마다 달력을 하나 고를 수 있다(site_calendars).

교대 패턴
  - 주간·야간·교대. 근무 코드별 시간(shifts) + 순환(cycle "D,D,N,N,O,O") + 기준일(anchor).
    그날 근무 = cycle[(날짜 − 기준일) % 길이], 공휴일 쉼(observe_holidays)이면 쉬는 날은 O.
  - 배정은 사번 단위. 새로 배정하면 지금 줄은 전날로 닫고 새 줄을 연다 = 이력이 남는다.
  - 교대 수당 계산은 V1 밖.
"""
import csv
import io
import json
import re
from datetime import date, datetime, timedelta

import workplace

KIND_LABEL = {'day': '주간', 'night': '야간', 'rotating': '교대'}
DAY_KIND_LABEL = {'off': '쉬는 날', 'work': '일하는 날'}
OFF = 'O'
CODE_RE = re.compile(r'^[A-Z0-9_-]{2,20}$')
SHIFT_RE = re.compile(r'^([A-Z])\s+(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})$')

_DDL = (
    '''CREATE TABLE IF NOT EXISTS work_calendars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS calendar_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        calendar_id INTEGER NOT NULL REFERENCES work_calendars(id),
        date DATE NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('off','work')),
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (calendar_id, date))''',
    '''CREATE TABLE IF NOT EXISTS site_calendars (
        site_code TEXT PRIMARY KEY,
        calendar_id INTEGER NOT NULL REFERENCES work_calendars(id))''',
    '''CREATE TABLE IF NOT EXISTS shift_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('day','night','rotating')),
        shifts TEXT NOT NULL,
        cycle TEXT NOT NULL,
        anchor DATE NOT NULL,
        observe_holidays INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS shift_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        pattern_id INTEGER NOT NULL REFERENCES shift_patterns(id),
        calendar_id INTEGER REFERENCES work_calendars(id),
        start_date DATE NOT NULL,
        end_date DATE,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    'CREATE INDEX IF NOT EXISTS idx_shift_asg_user ON shift_assignments(user_id, start_date)',
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
    c.commit()
    if k:
        _done.add(k)


def forget(c=None):
    _done.clear()


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _d(s):
    try:
        return date.fromisoformat((s or '').strip()[:10])
    except ValueError:
        return None


# ── 달력 ──────────────────────────────────────────────

def calendars(c):
    rows = _rows(c, 'SELECT * FROM work_calendars ORDER BY code')
    for r in rows:
        r['n_off'] = c.execute("SELECT COUNT(*) FROM calendar_days WHERE calendar_id=? AND kind='off'", (r['id'],)).fetchone()[0]
        r['n_work'] = c.execute("SELECT COUNT(*) FROM calendar_days WHERE calendar_id=? AND kind='work'", (r['id'],)).fetchone()[0]
        r['sites'] = [x[0] for x in c.execute('SELECT site_code FROM site_calendars WHERE calendar_id=? ORDER BY site_code', (r['id'],))]
        r['n_people'] = c.execute('SELECT COUNT(*) FROM shift_assignments WHERE calendar_id=? AND end_date IS NULL', (r['id'],)).fetchone()[0]
    return rows


def calendar(c, cal_id):
    r = c.execute('SELECT * FROM work_calendars WHERE id=?', (cal_id,)).fetchone()
    return dict(r) if r else None


def calendar_days(c, cal_id, year=None):
    q, args = 'SELECT * FROM calendar_days WHERE calendar_id=?', [cal_id]
    if year:
        q += ' AND date LIKE ?'
        args.append('%d-%%' % year)
    return _rows(c, q + ' ORDER BY date', args)


def holidays_for(c, cal_id, d_from, d_to):
    """그 달력에서 쉬는 날(ISO 문자열 집합). cal_id 가 없으면 기본 달력 = workplace.holidays."""
    base = workplace.holidays(c, d_from, d_to)
    if not cal_id:
        return base
    off, work = set(), set()
    for r in c.execute('SELECT date, kind FROM calendar_days WHERE calendar_id=? AND date BETWEEN ? AND ?',
                       (cal_id, d_from.isoformat(), d_to.isoformat())):
        (off if r[1] == 'off' else work).add(r[0])
    return (base | off) - work


def calendar_for_site(c, site_code):
    r = c.execute('SELECT calendar_id FROM site_calendars WHERE site_code=?', (site_code,)).fetchone()
    return r[0] if r else None


def assignment_on(c, uid, d):
    r = c.execute('SELECT * FROM shift_assignments WHERE user_id=? AND start_date<=? '
                  'AND (end_date IS NULL OR end_date>=?) ORDER BY start_date DESC LIMIT 1',
                  (uid, d.isoformat(), d.isoformat())).fetchone()
    return dict(r) if r else None


def calendar_for_user(c, uid, d):
    a = assignment_on(c, uid, d)
    return a['calendar_id'] if a else None


def create_calendar(c, code, name, note=''):
    code = (code or '').strip().upper()
    name = (name or '').strip()
    if not CODE_RE.match(code):
        return None, '코드는 영문 대문자·숫자·_- 2~20자입니다.'
    if not name:
        return None, '달력 이름을 넣어 주세요.'
    if c.execute('SELECT 1 FROM work_calendars WHERE code=?', (code,)).fetchone():
        return None, '같은 코드의 달력이 있습니다.'
    cur = c.execute('INSERT INTO work_calendars (code, name, note) VALUES (?,?,?)', (code, name[:60], (note or '').strip()[:200]))
    return cur.lastrowid, None


def add_day(c, cal_id, d, kind, name, by):
    if not calendar(c, cal_id):
        return '달력을 찾지 못했습니다.'
    d = _d(d) if isinstance(d, str) else d
    if not d:
        return '날짜를 넣어 주세요.'
    if kind not in DAY_KIND_LABEL:
        return '쉬는 날·일하는 날 중에 고르세요.'
    name = (name or '').strip()
    if not name:
        return '이름을 넣어 주세요(예: 창립기념일).'
    base = workplace.holiday_name(c, d)
    if kind == 'work' and not base and d.weekday() < 5:
        return '이미 일하는 날입니다(기본 달력 공휴일이나 주말만 일하는 날로 바꿀 수 있습니다).'
    if kind == 'off' and base:
        return '기본 달력에서 이미 공휴일입니다(%s).' % base
    c.execute('INSERT INTO calendar_days (calendar_id, date, name, kind, created_by) VALUES (?,?,?,?,?) '
              'ON CONFLICT(calendar_id, date) DO UPDATE SET name=excluded.name, kind=excluded.kind',
              (cal_id, d.isoformat(), name[:60], kind, by))
    return None


def delete_day(c, day_id):
    c.execute('DELETE FROM calendar_days WHERE id=?', (day_id,))


def set_site(c, site_code, cal_id):
    if not c.execute('SELECT 1 FROM workplace_sites WHERE code=?', (site_code,)).fetchone():
        return '근무지를 찾지 못했습니다.'
    if not cal_id:
        c.execute('DELETE FROM site_calendars WHERE site_code=?', (site_code,))
        return None
    if not calendar(c, cal_id):
        return '달력을 찾지 못했습니다.'
    c.execute('INSERT OR REPLACE INTO site_calendars (site_code, calendar_id) VALUES (?,?)', (site_code, cal_id))
    return None


def delete_calendar(c, cal_id):
    if c.execute('SELECT 1 FROM shift_assignments WHERE calendar_id=?', (cal_id,)).fetchone():
        return '배정 기록에 쓰인 달력은 지울 수 없습니다.'
    c.execute('DELETE FROM calendar_days WHERE calendar_id=?', (cal_id,))
    c.execute('DELETE FROM site_calendars WHERE calendar_id=?', (cal_id,))
    c.execute('DELETE FROM work_calendars WHERE id=?', (cal_id,))
    return None


# ── 교대 패턴 ─────────────────────────────────────────

def _hm(s):
    h, m = s.split(':')
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return '%02d:%02d' % (h, m)


def parse_shifts(text):
    """'D 09:00-18:00' 줄들 → {'D': ['09:00','18:00']}"""
    out = {}
    for line in (text or '').splitlines():
        line = line.strip().upper()
        if not line:
            continue
        m = SHIFT_RE.match(line)
        if not m or m.group(1) == OFF:
            return None, '근무 시간은 "D 09:00-18:00" 처럼 한 줄에 하나씩 적어 주세요(O 는 쉬는 날이라 못 씁니다).'
        try:
            out[m.group(1)] = [_hm(m.group(2)), _hm(m.group(3))]
        except ValueError:
            return None, '시각이 잘못됐습니다: %s' % line
    if not out:
        return None, '근무 시간을 한 줄 이상 넣어 주세요.'
    return out, None


def parse_cycle(text, shifts):
    toks = [t.strip().upper() for t in re.split(r'[,\s]+', text or '') if t.strip()]
    if not toks:
        return None, '순환을 넣어 주세요(예: D,D,N,N,O,O).'
    if len(toks) > 56:
        return None, '순환은 56일까지입니다.'
    bad = [t for t in toks if t != OFF and t not in shifts]
    if bad:
        return None, '순환에 시간이 없는 근무 코드가 있습니다: %s' % ', '.join(sorted(set(bad)))
    if all(t == OFF for t in toks):
        return None, '순환에 일하는 날이 하루는 있어야 합니다.'
    return toks, None


def create_pattern(c, f):
    code = (f.get('code') or '').strip().upper()
    name = (f.get('name') or '').strip()
    kind = f.get('kind')
    if not CODE_RE.match(code):
        return None, '코드는 영문 대문자·숫자·_- 2~20자입니다.'
    if not name:
        return None, '패턴 이름을 넣어 주세요.'
    if kind not in KIND_LABEL:
        return None, '종류를 고르세요.'
    if c.execute('SELECT 1 FROM shift_patterns WHERE code=?', (code,)).fetchone():
        return None, '같은 코드의 패턴이 있습니다.'
    shifts, err = parse_shifts(f.get('shifts'))
    if err:
        return None, err
    cyc, err = parse_cycle(f.get('cycle'), shifts)
    if err:
        return None, err
    anchor = _d(f.get('anchor'))
    if not anchor:
        return None, '순환 시작일을 넣어 주세요.'
    cur = c.execute('INSERT INTO shift_patterns (code, name, kind, shifts, cycle, anchor, observe_holidays) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (code, name[:60], kind, json.dumps(shifts), ','.join(cyc), anchor.isoformat(),
                     1 if f.get('observe_holidays') else 0))
    return cur.lastrowid, None


def _pat(r):
    p = dict(r)
    p['shifts_map'] = json.loads(p['shifts'] or '{}')
    p['cycle_list'] = p['cycle'].split(',')
    p['kind_label'] = KIND_LABEL.get(p['kind'], p['kind'])
    p['shifts_text'] = ' · '.join('%s %s~%s' % (k, v[0], v[1]) for k, v in p['shifts_map'].items())
    return p


def patterns(c, active_only=False):
    q = 'SELECT * FROM shift_patterns' + (' WHERE active=1' if active_only else '') + ' ORDER BY active DESC, code'
    out = []
    for r in c.execute(q):
        p = _pat(r)
        p['n_people'] = c.execute('SELECT COUNT(*) FROM shift_assignments WHERE pattern_id=? AND end_date IS NULL',
                                  (p['id'],)).fetchone()[0]
        out.append(p)
    return out


def pattern(c, pid):
    r = c.execute('SELECT * FROM shift_patterns WHERE id=?', (pid,)).fetchone()
    return _pat(r) if r else None


def day_for(p, d, hol=()):
    """그날 근무: {'date','code','start','end','night','holiday'} (code 'O' = 쉼)."""
    anchor = date.fromisoformat(p['anchor'])
    cyc = p['cycle_list']
    code = cyc[(d - anchor).days % len(cyc)]
    is_hol = d.isoformat() in hol
    if is_hol and p['observe_holidays']:
        code = OFF
    out = {'date': d.isoformat(), 'code': code, 'start': None, 'end': None, 'night': False, 'holiday': is_hol}
    if code != OFF:
        s, e = p['shifts_map'][code]
        out.update(start=s, end=e, night=e <= s)
    return out


def preview(c, p, d_from, days=14, cal_id=None):
    d_to = d_from + timedelta(days=days - 1)
    hol = holidays_for(c, cal_id, d_from, d_to)
    return [day_for(p, d_from + timedelta(days=i), hol) for i in range(days)]


def schedule(c, uid, d_from, days=14):
    """그 사람의 날짜별 근무(배정이 없는 날은 None)."""
    out = []
    for i in range(days):
        d = d_from + timedelta(days=i)
        a = assignment_on(c, uid, d)
        if not a:
            out.append({'date': d.isoformat(), 'code': None})
            continue
        p = pattern(c, a['pattern_id'])
        out.append(day_for(p, d, holidays_for(c, a['calendar_id'], d, d)))
    return out


# ── 배정 ──────────────────────────────────────────────

def assign(c, emp_nos, pattern_id, cal_id, start, by):
    """사번 여러 개에 패턴 배정. 지금 줄은 전날로 닫는다. → (배정 수, 건너뜀 목록, 오류)"""
    p = pattern(c, pattern_id)
    if not p or not p['active']:
        return 0, [], '패턴을 고르세요.'
    if cal_id and not calendar(c, cal_id):
        return 0, [], '달력을 찾지 못했습니다.'
    start = _d(start) if isinstance(start, str) else start
    if not start:
        return 0, [], '시작일을 넣어 주세요.'
    nos = [x for x in re.split(r'[\s,]+', emp_nos or '') if x]
    if not nos:
        return 0, [], '사번을 넣어 주세요.'
    n, skipped = 0, []
    for no in dict.fromkeys(nos):
        u = c.execute("SELECT id, status FROM users WHERE emp_no=?", (no,)).fetchone()
        if not u:
            skipped.append('%s 없는 사번' % no)
            continue
        if u['status'] != 'active':
            skipped.append('%s 재직자 아님' % no)
            continue
        if c.execute('SELECT 1 FROM shift_assignments WHERE user_id=? AND start_date>=?',
                     (u['id'], start.isoformat())).fetchone():
            skipped.append('%s 그날 이후 배정 있음' % no)
            continue
        c.execute('UPDATE shift_assignments SET end_date=? WHERE user_id=? AND (end_date IS NULL OR end_date>=?)',
                  ((start - timedelta(days=1)).isoformat(), u['id'], start.isoformat()))
        c.execute('INSERT INTO shift_assignments (user_id, pattern_id, calendar_id, start_date, created_by) '
                  'VALUES (?,?,?,?,?)', (u['id'], p['id'], cal_id or None, start.isoformat(), by))
        n += 1
    return n, skipped, None


def end_assignment(c, aid, end):
    r = c.execute('SELECT * FROM shift_assignments WHERE id=?', (aid,)).fetchone()
    if not r:
        return '배정을 찾지 못했습니다.'
    if r['end_date']:
        return '이미 끝난 배정입니다.'
    end = _d(end) if isinstance(end, str) else end
    if not end or end < date.fromisoformat(r['start_date']):
        return '끝나는 날은 시작일 이후여야 합니다.'
    c.execute('UPDATE shift_assignments SET end_date=? WHERE id=?', (end.isoformat(), aid))
    return None


def current_assignments(c, q=''):
    today = date.today().isoformat()
    rows = _rows(c, 'SELECT a.*, u.name, u.emp_no, d.name AS dept, p.code AS pcode, p.name AS pname, p.kind, '
                    'w.name AS cal_name FROM shift_assignments a JOIN users u ON u.id=a.user_id '
                    'JOIN shift_patterns p ON p.id=a.pattern_id LEFT JOIN departments d ON d.id=u.department_id '
                    'LEFT JOIN work_calendars w ON w.id=a.calendar_id '
                    'WHERE a.end_date IS NULL OR a.end_date>=? ORDER BY u.emp_no, a.start_date', (today,))
    if q:
        rows = [r for r in rows if q in (r['name'] or '') or q in (r['emp_no'] or '') or q in (r['dept'] or '')]
    for r in rows:
        r['kind_label'] = KIND_LABEL.get(r['kind'], r['kind'])
        r['upcoming'] = r['start_date'] > today
    return rows


def history(c, uid):
    return _rows(c, 'SELECT a.*, p.code AS pcode, p.name AS pname, w.name AS cal_name FROM shift_assignments a '
                    'JOIN shift_patterns p ON p.id=a.pattern_id LEFT JOIN work_calendars w ON w.id=a.calendar_id '
                    'WHERE a.user_id=? ORDER BY a.start_date DESC', (uid,))


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서', '패턴 코드', '패턴', '종류', '달력', '시작일', '끝나는 날'])
    for r in rows:
        w.writerow([r['emp_no'] or '', r['name'], r['dept'] or '', r['pcode'], r['pname'], r['kind_label'],
                    r['cal_name'] or '기본 달력', r['start_date'], r['end_date'] or ''])
    return '﻿' + buf.getvalue()


# ── 화면 ──────────────────────────────────────────────

def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')

    app.jinja_env.globals['calendars_can'] = lambda: can('calendars')

    @app.route('/work-calendars', methods=['GET', 'POST'])
    @login_required
    def work_calendars():
        if not can('calendars'):
            abort(403)
        db = get_db()
        ensure_schema(db)
        tab = request.values.get('tab') or 'cal'
        if tab not in ('cal', 'shift', 'assign'):
            tab = 'cal'
        uid = session['user_id']
        if request.method == 'POST':
            back = dict(tab=tab)
            if session.get('demo_mode'):
                flash('데모에서는 저장되지 않습니다.', 'warning')
                return redirect(url_for('work_calendars', **back))
            a = request.form.get('action')
            f = request.form
            err, msg = None, None
            if a == 'cal_new':
                cid, err = create_calendar(db, f.get('code'), f.get('name'), f.get('note'))
                back['id'], msg = cid, '달력을 만들었습니다.'
            elif a == 'day_add':
                back['id'] = f.get('cal_id', type=int)
                err = add_day(db, back['id'], f.get('date'), f.get('kind'), f.get('name'), uid)
                msg = '날을 넣었습니다.'
            elif a == 'day_del':
                back['id'] = f.get('cal_id', type=int)
                delete_day(db, f.get('day_id', type=int))
                msg = '날을 뺐습니다.'
            elif a == 'site':
                err = set_site(db, f.get('site_code'), f.get('cal_id', type=int))
                msg = '근무지 달력을 바꿨습니다.'
            elif a == 'cal_del':
                err = delete_calendar(db, f.get('cal_id', type=int))
                msg = '달력을 지웠습니다.'
            elif a == 'pat_new':
                pid, err = create_pattern(db, f)
                back['id'], msg = pid, '패턴을 만들었습니다.'
            elif a == 'pat_toggle':
                pid = f.get('pattern_id', type=int)
                db.execute('UPDATE shift_patterns SET active=1-active WHERE id=?', (pid,))
                back['id'], msg = pid, '패턴 사용 여부를 바꿨습니다.'
            elif a == 'assign':
                n, skipped, err = assign(db, f.get('emp_nos'), f.get('pattern_id', type=int),
                                         f.get('cal_id', type=int), f.get('start_date'), uid)
                if not err:
                    msg = '%d명 배정했습니다.' % n
                    if skipped:
                        msg += ' 건너뜀: %s' % ' · '.join(skipped[:10])
            elif a == 'end':
                err = end_assignment(db, f.get('assignment_id', type=int), f.get('end_date'))
                msg = '배정을 끝냈습니다.'
            else:
                abort(400)
            if err:
                db.rollback()
                flash(err, 'error')
            else:
                db.commit()
                flash(msg, 'success')
            back = {k: v for k, v in back.items() if v}
            return redirect(url_for('work_calendars', **back))

        ctx = dict(tab=tab, kinds=KIND_LABEL, day_kinds=DAY_KIND_LABEL, active_page='work_calendars',
                   cals=calendars(db), today=date.today().isoformat())
        sel = request.args.get('id', type=int)
        if tab == 'cal':
            year = request.args.get('year', type=int) or date.today().year
            ctx.update(year=year, sites=workplace.sites(db),
                       site_map={r[0]: r[1] for r in db.execute('SELECT site_code, calendar_id FROM site_calendars')})
            if sel:
                cal = calendar(db, sel)
                if cal:
                    y0, y1 = date(year, 1, 1), date(year, 12, 31)
                    ctx.update(cal=cal, days=calendar_days(db, sel, year),
                               n_base=len(workplace.holidays(db, y0, y1)),
                               n_total=len(holidays_for(db, sel, y0, y1)))
        elif tab == 'shift':
            ctx['pats'] = patterns(db)
            if sel:
                p = pattern(db, sel)
                if p:
                    ctx.update(pat=p, prev=preview(db, p, date.today()))
        else:
            q = (request.args.get('q') or '').strip()
            rows = current_assignments(db, q)
            if request.args.get('csv'):
                return Response(to_csv(rows), mimetype='text/csv; charset=utf-8',
                                headers={'Content-Disposition': 'attachment; filename=shift_assignments.csv'})
            ctx.update(q=q, rows=rows, pats=patterns(db, active_only=True))
            emp = (request.args.get('emp') or '').strip()
            if emp:
                u = db.execute('SELECT id, name, emp_no FROM users WHERE emp_no=?', (emp,)).fetchone()
                if u:
                    ctx.update(person=dict(u), sched=schedule(db, u['id'], date.today()), hist=history(db, u['id']))
        return render_template('work_calendars/index.html', **ctx)
