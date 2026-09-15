"""회사·건물·회의실·입사일 규칙 (회의실·온보딩 V1, 2026-09-15).

주인은 TalentCore 다. 건물·층·방·예약표·입사일 규칙을 여기서 갖고,
Hire 는 API 로 입사 가능일과 빈 면접실을 물어보고 예약만 한다.

사용자 결정
  - 입사일은 월·수만. 그날이 공휴일이면 다음 입사 가능일로 안내한다.
  - 입사일마다 교육장 10:00~11:30 은 오리엔테이션 고정 예약.
    예약표에 행으로 저장하지 않고 그때그때 계산한다(규칙을 바꾸면 곧바로 반영되게).
    입사 예정자가 한 명도 없으면 D-2 에 풀려 다른 사람이 쓸 수 있다.
  - 면접실은 자동 배정하지 않는다. 추천 3개를 보여 주고 담당자가 눌러 예약한다.

flask 에 기대지 않는 순수 함수만 둔다 — conn(sqlite3.Row) 를 받아서 쓴다.
"""
import json
import re
from datetime import date, datetime, timedelta

WEEKDAY_KO = ['월', '화', '수', '목', '금', '토', '일']

ROOM_TYPES = {
    'interview': '면접실', 'meeting': '회의실', 'small': '소회의실', 'large': '대회의실',
    'training': '교육장', 'lounge': '라운지', 'phone': '폰부스',
}
BOOKABLE_LABEL = {'all': '전 직원', 'hr': '인사', 'exec': '경영진'}
TASK_CATEGORIES = {'setup': '계정·장비', 'learning': '교육', 'admin': '서류', 'social': '관계', 'team': '팀 업무'}
PLACEHOLDERS = {'employee_name', 'department', 'position', 'hire_date', 'manager_name',
                'buddy_name', 'company_name', 'company_address'}

SETTING_DEFAULTS = {
    'start_weekdays': '0,2',           # 0=월 … 6=일
    'orientation_room': 'HQ-1F',
    'orientation_start': '10:00',
    'orientation_end': '11:30',
    'orientation_release_days': '2',
}

GRID_START = '08:00'
GRID_END = '20:00'
SLOT_MIN = 30

COMPANY_KEYS = ('name', 'name_en', 'reg_no', 'ceo', 'address', 'tel', 'founded',
                'industry', 'website', 'intro', 'zip')


# ── 스키마 ────────────────────────────────────────────────────────────
def ensure_schema(c):
    c.executescript('''
        CREATE TABLE IF NOT EXISTS workplace_sites (
            code            TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            building        TEXT NOT NULL DEFAULT '',
            address         TEXT NOT NULL DEFAULT '',
            address_detail  TEXT NOT NULL DEFAULT '',
            zip             TEXT NOT NULL DEFAULT '',
            transit         TEXT NOT NULL DEFAULT '[]',
            parking         TEXT NOT NULL DEFAULT '',
            building_hours  TEXT NOT NULL DEFAULT '',
            visitor_access  TEXT NOT NULL DEFAULT '',
            employee_access TEXT NOT NULL DEFAULT '',
            sort_order      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS workplace_floors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            site_code   TEXT NOT NULL,
            floor       INTEGER NOT NULL,
            label       TEXT NOT NULL DEFAULT '',
            departments TEXT NOT NULL DEFAULT '[]',
            seats       INTEGER NOT NULL DEFAULT 0,
            amenities   TEXT NOT NULL DEFAULT '[]',
            grid_w      INTEGER NOT NULL DEFAULT 24,
            grid_h      INTEGER NOT NULL DEFAULT 12,
            UNIQUE(site_code, floor)
        );
        CREATE TABLE IF NOT EXISTS workplace_rooms (
            code        TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            site_code   TEXT NOT NULL,
            floor       INTEGER NOT NULL,
            type        TEXT NOT NULL DEFAULT 'meeting',
            capacity    INTEGER NOT NULL DEFAULT 4,
            equipment   TEXT NOT NULL DEFAULT '[]',
            bookable_by TEXT NOT NULL DEFAULT 'all',
            x INTEGER NOT NULL DEFAULT 0, y INTEGER NOT NULL DEFAULT 0,
            w INTEGER NOT NULL DEFAULT 1, h INTEGER NOT NULL DEFAULT 1,
            note        TEXT NOT NULL DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS room_bookings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code   TEXT NOT NULL,
            title       TEXT NOT NULL,
            start_at    TEXT NOT NULL,
            end_at      TEXT NOT NULL,
            user_id     INTEGER,
            kind        TEXT NOT NULL DEFAULT 'normal',
            ref         TEXT NOT NULL DEFAULT '',
            attendees   INTEGER NOT NULL DEFAULT 0,
            booked_by   TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'active',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cancelled_at TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_room_bookings_room ON room_bookings(room_code, start_at);
        CREATE INDEX IF NOT EXISTS idx_room_bookings_user ON room_bookings(user_id, start_at);
        CREATE TABLE IF NOT EXISTS onboarding_content (
            key        TEXT PRIMARY KEY,
            body       TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER
        );
    ''')
    for k, v in SETTING_DEFAULTS.items():
        c.execute('INSERT OR IGNORE INTO company_settings (key, value) VALUES (?, ?)', (k, v))


def _j(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def settings(conn):
    out = dict(SETTING_DEFAULTS)
    for r in conn.execute('SELECT key, value FROM company_settings WHERE key IN (%s)'
                          % ','.join('?' * len(SETTING_DEFAULTS)), tuple(SETTING_DEFAULTS)):
        if r[1] != '':
            out[r[0]] = r[1]
    out['weekdays'] = sorted({int(x) for x in out['start_weekdays'].split(',') if x.strip().isdigit() and int(x) < 7})
    try:
        out['release_days'] = max(0, int(out['orientation_release_days']))
    except ValueError:
        out['release_days'] = 2
    return out


def weekdays_label(wds):
    return '·'.join(WEEKDAY_KO[w] for w in wds)


# ── 날짜 도구 ─────────────────────────────────────────────────────────
def to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def holidays(conn, d_from, d_to):
    try:
        return {r[0] for r in conn.execute(
            'SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?',
            (d_from.isoformat(), d_to.isoformat()))}
    except Exception:
        return set()


def holiday_name(conn, d):
    try:
        r = conn.execute('SELECT name FROM public_holidays WHERE date=?', (d.isoformat(),)).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def is_start_day(conn, d, st=None):
    d = to_date(d)
    st = st or settings(conn)
    return bool(d) and d.weekday() in st['weekdays'] and d.isoformat() not in holidays(conn, d, d)


def next_start_dates(conn, d_from, n=6, st=None):
    """d_from(포함)부터 입사 가능한 날 n개."""
    st = st or settings(conn)
    d_from = to_date(d_from) or date.today()
    if not st['weekdays']:
        return []
    hol = holidays(conn, d_from, d_from + timedelta(days=7 * n + 60))
    out, d = [], d_from
    while len(out) < n and (d - d_from).days < 7 * n + 60:
        if d.weekday() in st['weekdays'] and d.isoformat() not in hol:
            out.append(d)
        d += timedelta(days=1)
    return out


def start_date_problem(conn, d, st=None):
    """입사일 규칙 위반이면 (안내문, 가까운 입사 가능일 목록), 문제없으면 None."""
    st = st or settings(conn)
    dd = to_date(d)
    if not dd:
        return ('입사일 형식이 올바르지 않습니다', next_start_dates(conn, date.today(), 3, st))
    if dd.weekday() in st['weekdays'] and dd.isoformat() not in holidays(conn, dd, dd):
        return None
    allowed = weekdays_label(st['weekdays'])
    hn = holiday_name(conn, dd) if dd.weekday() in st['weekdays'] else None
    wd = WEEKDAY_KO[dd.weekday()]
    why = ('%s(%s)%s 공휴일(%s)이라 입사일로 쓸 수 없습니다' % (dd.isoformat(), wd, '은' if _jong(wd) else '는', hn)
           if hn else '입사일은 매주 %s요일만 가능합니다 — %s(%s)' % (allowed, dd.isoformat(), WEEKDAY_KO[dd.weekday()]))
    return (why, next_start_dates(conn, dd, 3, st))


def working_day(conn, start, n):
    """start 를 1일차로 보고 주말·공휴일을 건너뛴 n일차 날짜."""
    d = to_date(start)
    hol = holidays(conn, d, d + timedelta(days=n * 2 + 20))
    k = 0
    while True:
        if d.weekday() < 5 and d.isoformat() not in hol:
            k += 1
            if k >= n:
                return d
        d += timedelta(days=1)


def hm(s):
    h, m = str(s).split(':')[:2]
    return int(h) * 60 + int(m)


def fmt_hm(mins):
    return '%02d:%02d' % (mins // 60, mins % 60)


# ── 건물·층·방 ─────────────────────────────────────────────────────────
def sites(conn):
    out = []
    for r in conn.execute('SELECT * FROM workplace_sites ORDER BY sort_order, code'):
        s = dict(r)
        s['transit'] = _j(s['transit'], [])
        out.append(s)
    return out


def floors(conn, site_code=None):
    q = 'SELECT * FROM workplace_floors'
    args = ()
    if site_code:
        q += ' WHERE site_code=?'
        args = (site_code,)
    out = []
    for r in conn.execute(q + ' ORDER BY site_code, floor', args):
        f = dict(r)
        f['departments'] = _j(f['departments'], [])
        f['amenities'] = _j(f['amenities'], [])
        out.append(f)
    return out


def rooms(conn, active_only=True):
    q = 'SELECT * FROM workplace_rooms'
    if active_only:
        q += ' WHERE active=1'
    out = []
    for r in conn.execute(q + ' ORDER BY site_code, floor, code'):
        x = dict(r)
        x['equipment'] = _j(x['equipment'], [])
        x['type_label'] = ROOM_TYPES.get(x['type'], x['type'])
        out.append(x)
    return out


def room(conn, code):
    r = conn.execute('SELECT * FROM workplace_rooms WHERE code=?', (code,)).fetchone()
    if not r:
        return None
    x = dict(r)
    x['equipment'] = _j(x['equipment'], [])
    x['type_label'] = ROOM_TYPES.get(x['type'], x['type'])
    return x


def has_workplace(conn):
    try:
        return conn.execute('SELECT 1 FROM workplace_rooms WHERE active=1 LIMIT 1').fetchone() is not None
    except Exception:
        return False


def _ancestors(conn, dept_id):
    seen, out, cur = set(), [], dept_id
    while cur and cur not in seen:
        seen.add(cur)
        r = conn.execute('SELECT id, name, parent_id, dept_type, leader_id FROM departments WHERE id=?', (cur,)).fetchone()
        if not r:
            break
        out.append(r)
        cur = r['parent_id']
    return out


def dept_floor(conn, dept_id):
    """부서가 앉는 층. 팀은 자기 이름이 적힌 층, 실·본부는 아래 팀이 앉은 층,
    부문(최상위)·경영진은 가장 높은 층(경영진층)."""
    fl = floors(conn)
    if not fl or not dept_id:
        return None
    by_name = {}
    for f in fl:
        for n in f['departments']:
            by_name.setdefault(n, f)
    r = conn.execute('SELECT id, name, dept_type FROM departments WHERE id=?', (dept_id,)).fetchone()
    if not r:
        return None
    if r['name'] in by_name:
        return by_name[r['name']]
    if r['dept_type'] != 'division':
        queue = [dept_id]
        while queue:
            kids = conn.execute('SELECT id, name FROM departments WHERE parent_id IN (%s) ORDER BY id'
                                % ','.join('?' * len(queue)), queue).fetchall()
            for k in kids:
                if k['name'] in by_name:
                    return by_name[k['name']]
            queue = [k['id'] for k in kids]
    return max(fl, key=lambda f: f['floor'])


def team_room(conn, dept_id):
    """팀장·버디 세션을 여는 방 — 팀이 앉은 층의 전 직원용 회의실 중 가장 작은 곳."""
    f = dept_floor(conn, dept_id)
    if not f:
        return None
    cands = [r for r in rooms(conn) if r['site_code'] == f['site_code'] and r['floor'] == f['floor']
             and r['type'] in ('meeting', 'small', 'large')]
    open_ = [r for r in cands if r['bookable_by'] == 'all'] or cands
    if not open_:
        return None
    return sorted(open_, key=lambda r: (r['type'] != 'meeting', r['capacity'], r['code']))[0]


# ── 누가 어떤 방을 잡을 수 있나 ─────────────────────────────────────────
def _role_user_ids(conn, exclude=('hr_desk',)):
    try:
        rows = conn.execute('SELECT key, user_id, delegate_id FROM approval_roles').fetchall()
    except Exception:
        return {}
    return {r['key']: (r['user_id'], r['delegate_id']) for r in rows if r['key'] not in exclude}


def is_hr_member(conn, user_id, role):
    if role in ('admin', 'recruiter'):
        return True
    u = conn.execute('SELECT department_id FROM users WHERE id=?', (user_id,)).fetchone()
    chro = _role_user_ids(conn).get('chro', (None, None))[0]
    if not u or not chro:
        return False
    return any(a['leader_id'] == chro for a in _ancestors(conn, u['department_id']))


def is_exec_member(conn, user_id, role):
    if role == 'admin':
        return True
    ids = set()
    for u, dg in _role_user_ids(conn).values():
        ids.update(x for x in (u, dg) if x)
    if user_id in ids:
        return True
    return conn.execute("SELECT 1 FROM departments WHERE leader_id=? AND dept_type='division'",
                        (user_id,)).fetchone() is not None


def can_book(conn, rm, user_id, role):
    b = rm.get('bookable_by', 'all')
    if b == 'all':
        return True
    if b == 'hr':
        return is_hr_member(conn, user_id, role)
    if b == 'exec':
        return is_exec_member(conn, user_id, role)
    return role == 'admin'


# ── 오리엔테이션 고정 예약(계산) ────────────────────────────────────────
def hires_on(conn, d):
    """그날 입사하는 사람 — 대기 중인 입사 예정자 + 이미 직원으로 전환된 사람."""
    iso = to_date(d).isoformat()
    out = []
    for r in conn.execute("SELECT id, name, department_name AS dept, 'incoming' AS src FROM incoming_hires "
                          "WHERE status='waiting' AND start_date=? ORDER BY name", (iso,)):
        out.append(dict(r))
    for r in conn.execute("SELECT u.id, u.name, d.name AS dept, 'user' AS src FROM users u "
                          "LEFT JOIN departments d ON d.id=u.department_id "
                          "WHERE u.hire_date=? AND u.status='active' ORDER BY u.name", (iso,)):
        out.append(dict(r))
    return out


def orientation_blocks(conn, d_from, d_to, today=None, st=None):
    st = st or settings(conn)
    today = today or date.today()
    d_from, d_to = to_date(d_from), to_date(d_to)
    hol = holidays(conn, d_from, d_to)
    out, d = [], d_from
    while d <= d_to:
        if d.weekday() in st['weekdays'] and d.isoformat() not in hol:
            people = hires_on(conn, d)
            left = (d - today).days
            if people or left > st['release_days']:
                out.append({
                    'id': None, 'room_code': st['orientation_room'], 'kind': 'orientation',
                    'title': '신규 입사자 오리엔테이션' + (' · %d명' % len(people) if people else ''),
                    'start_at': '%s %s' % (d.isoformat(), st['orientation_start']),
                    'end_at': '%s %s' % (d.isoformat(), st['orientation_end']),
                    'date': d.isoformat(), 'people': people, 'user_name': '인사팀',
                    'release_on': (d - timedelta(days=st['release_days'])).isoformat() if not people else None,
                })
        d += timedelta(days=1)
    return out


# ── 예약 ───────────────────────────────────────────────────────────────
def parse_dt(s):
    try:
        return datetime.strptime(str(s).strip().replace('T', ' ')[:16], '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None


def bookings_between(conn, d_from, d_to, room_code=None):
    q = ("SELECT b.*, u.name AS user_name FROM room_bookings b LEFT JOIN users u ON u.id=b.user_id "
         "WHERE b.status='active' AND b.start_at < ? AND b.end_at > ?")
    args = ['%s 24:00' % to_date(d_to).isoformat(), '%s 00:00' % to_date(d_from).isoformat()]
    if room_code:
        q += ' AND b.room_code=?'
        args.append(room_code)
    return [dict(r) for r in conn.execute(q + ' ORDER BY b.start_at', args)]


def find_conflict(conn, room_code, start_at, end_at, exclude_id=None, st=None, today=None):
    s, e = start_at.strftime('%Y-%m-%d %H:%M'), end_at.strftime('%Y-%m-%d %H:%M')
    q = ("SELECT b.*, u.name AS user_name FROM room_bookings b LEFT JOIN users u ON u.id=b.user_id "
         "WHERE b.status='active' AND b.room_code=? AND b.start_at < ? AND b.end_at > ?")
    args = [room_code, e, s]
    if exclude_id:
        q += ' AND b.id<>?'
        args.append(exclude_id)
    r = conn.execute(q, args).fetchone()
    if r:
        return dict(r)
    for blk in orientation_blocks(conn, start_at.date(), end_at.date(), today=today, st=st):
        if blk['room_code'] == room_code and blk['start_at'] < e and blk['end_at'] > s:
            return blk
    return None


def validate_slot(start_at, end_at):
    if not start_at or not end_at:
        return '시작·종료 시간을 확인해 주세요'
    if end_at <= start_at:
        return '종료 시간이 시작 시간보다 늦어야 합니다'
    if start_at.date() != end_at.date():
        return '예약은 하루 안에서만 가능합니다'
    if start_at.minute % SLOT_MIN or end_at.minute % SLOT_MIN:
        return '예약은 30분 단위로만 가능합니다'
    if start_at.strftime('%H:%M') < GRID_START or end_at.strftime('%H:%M') > GRID_END:
        return '예약 가능 시간은 %s~%s 입니다' % (GRID_START, GRID_END)
    return None


def create_booking(conn, room_code, title, start_at, end_at, user_id=None, role='employee',
                   kind='normal', ref='', attendees=0, note='', booked_by='', skip_permission=False,
                   today=None):
    """예약 한 건. 성공하면 (id, None), 실패하면 (None, 안내문, 겹친 예약)."""
    rm = room(conn, room_code)
    if not rm or not rm['active']:
        return None, '없는 회의실입니다', None
    start_at, end_at = parse_dt(start_at) if isinstance(start_at, str) else start_at, \
        parse_dt(end_at) if isinstance(end_at, str) else end_at
    err = validate_slot(start_at, end_at)
    if err:
        return None, err, None
    if (today or date.today()) > start_at.date() or (today is None and end_at <= datetime.now()):
        return None, '지난 시간은 예약할 수 없습니다', None
    if not skip_permission and not can_book(conn, rm, user_id, role):
        return None, '%s은 %s만 예약할 수 있습니다' % (rm['name'], BOOKABLE_LABEL.get(rm['bookable_by'], '관리자')), None
    title = (title or '').strip()[:80]
    if not title:
        return None, '회의 이름을 적어 주세요', None
    hit = find_conflict(conn, room_code, start_at, end_at, today=today)
    if hit:
        who = hit.get('user_name') or hit.get('booked_by') or ''
        return None, '%s %s~%s 에 이미 예약이 있습니다 (%s%s)' % (
            rm['name'], hit['start_at'][11:16], hit['end_at'][11:16], hit['title'],
            ' · ' + who if who else ''), hit
    cur = conn.execute(
        'INSERT INTO room_bookings (room_code, title, start_at, end_at, user_id, kind, ref, attendees, note, booked_by) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (room_code, title, start_at.strftime('%Y-%m-%d %H:%M'), end_at.strftime('%Y-%m-%d %H:%M'),
         user_id, kind, (ref or '')[:120], int(attendees or 0), (note or '')[:300], (booked_by or '')[:60]))
    return cur.lastrowid, None, None


def ics_text(b, rm, site=None):
    def dt(s):
        return s.replace('-', '').replace(':', '').replace(' ', 'T') + '00'
    loc = rm['name'] + ' (' + rm['code'] + ')'
    if site:
        loc += ', %s %s층, %s' % (site.get('building', ''), rm['floor'], site.get('address', ''))

    def esc(s):
        return str(s).replace('\\', '\\\\').replace(';', r'\;').replace(',', r'\,').replace('\n', r'\n')
    stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//TalentCore//Rooms//KO', 'CALSCALE:GREGORIAN',
        'BEGIN:VTIMEZONE', 'TZID:Asia/Seoul', 'BEGIN:STANDARD', 'DTSTART:19700101T000000',
        'TZOFFSETFROM:+0900', 'TZOFFSETTO:+0900', 'TZNAME:KST', 'END:STANDARD', 'END:VTIMEZONE',
        'BEGIN:VEVENT', 'UID:room-booking-%s@talentcore' % b['id'], 'DTSTAMP:' + stamp,
        'DTSTART;TZID=Asia/Seoul:' + dt(b['start_at']), 'DTEND;TZID=Asia/Seoul:' + dt(b['end_at']),
        'SUMMARY:' + esc(b['title']), 'LOCATION:' + esc(loc),
    ]
    if b.get('note'):
        lines.append('DESCRIPTION:' + esc(b['note']))
    lines += ['END:VEVENT', 'END:VCALENDAR']
    return '\r\n'.join(lines) + '\r\n'


# ── 면접실 추천 ────────────────────────────────────────────────────────
def recommend_rooms(conn, start_at, end_at, people=2, mode='onsite', needs=(), limit=3, today=None, exclude_id=None):
    """자동 배정이 아니라 추천 — 인원 맞는 가장 작은 방 · 필요한 장비 · 낮은 층 · 시간 전체가 빈 방."""
    start_at = parse_dt(start_at) if isinstance(start_at, str) else start_at
    end_at = parse_dt(end_at) if isinstance(end_at, str) else end_at
    err = validate_slot(start_at, end_at)
    if err:
        return {'ok': False, 'error': err, 'rooms': []}
    people = max(1, int(people or 1))
    video = mode in ('video', 'remote', '비대면')
    needs = set(needs or ())
    if video:
        needs.add('화상회의')
    fit, busy = [], []
    for rm in rooms(conn):
        if rm['bookable_by'] == 'exec' or rm['type'] in ('training', 'lounge'):
            continue
        if rm['type'] == 'phone' and not (video and people == 1):
            continue
        if rm['capacity'] < people:
            continue
        eq = set(rm['equipment'])
        missing = [n for n in needs if n not in eq and not (n == '화상회의' and rm['type'] == 'phone')]
        if missing:
            continue
        hit = find_conflict(conn, rm['code'], start_at, end_at, exclude_id=exclude_id, today=today)
        if video:
            rank = {'phone': 0, 'interview': 1, 'small': 1, 'meeting': 2, 'large': 3}.get(rm['type'], 3)
        else:
            rank = {'interview': 0, 'small': 1, 'meeting': 1, 'large': 2}.get(rm['type'], 3)
        why = ['%d인실 · %d명' % (rm['capacity'], people)]
        if rm['type'] == 'interview':
            why.append('면접 전용')
        if rm['floor'] == 1 and not video:
            why.append('1층 · 방문객 라운지에서 바로 이동')
        for n in sorted(needs):
            why.append(n + ' 있음')
        item = {'code': rm['code'], 'name': rm['name'], 'floor': rm['floor'], 'site': rm['site_code'],
                'type': rm['type'], 'type_label': rm['type_label'], 'capacity': rm['capacity'],
                'equipment': rm['equipment'], 'reasons': why,
                'key': (rank, rm['capacity'] - people, rm['floor'], rm['code'])}
        if hit:
            item['busy'] = '%s~%s %s' % (hit['start_at'][11:16], hit['end_at'][11:16], hit['title'])
            busy.append(item)
        else:
            fit.append(item)
    fit.sort(key=lambda x: x['key'])
    busy.sort(key=lambda x: x['key'])
    for x in fit + busy:
        x.pop('key', None)
    return {'ok': True, 'rooms': fit[:limit], 'busy': busy[:limit],
            'start': start_at.strftime('%Y-%m-%d %H:%M'), 'end': end_at.strftime('%Y-%m-%d %H:%M'),
            'people': people, 'mode': 'video' if video else 'onsite'}


# ── 한국어 조사 ────────────────────────────────────────────────────────
_DIGIT_JONG = {'0': 21, '1': 8, '2': 0, '3': 16, '4': 0, '5': 0, '6': 1, '7': 8, '8': 8, '9': 0}


def _jong(word):
    """마지막 글자의 받침 번호(없으면 0, ㄹ=8). 한글이 아니면 발음으로 어림."""
    w = re.sub(r'[\s\)\]\}"\'’”.·]+$', '', str(word or ''))
    if not w:
        return 0
    ch = w[-1]
    if '가' <= ch <= '힣':
        return (ord(ch) - 0xAC00) % 28
    if ch in _DIGIT_JONG:
        return _DIGIT_JONG[ch]
    if ch.lower() in 'lr':
        return 8
    if ch.lower() in 'mnk':
        return 1
    return 0


_PAIRS = {'과': ('과', '와'), '와': ('과', '와'), '이': ('이', '가'), '가': ('이', '가'),
          '은': ('은', '는'), '는': ('은', '는'), '을': ('을', '를'), '를': ('을', '를')}


def fill(text, values):
    """{{key}} 를 채우고 바로 뒤 조사를 받침에 맞춘다: {{buddy_name}}과 → 김하늘과 / 이준호와."""
    if not text:
        return ''

    def sub(m):
        key, josa = m.group(1), m.group(2) or ''
        v = str(values.get(key) or '')
        if not josa:
            return v
        j = _jong(v)
        if josa in ('으로', '로'):
            return v + ('으로' if j and j != 8 else '로')
        a, b = _PAIRS[josa]
        # '이'가 다음 글자와 붙어 단어를 이루면(이다·이며…) 조사로 보지 않는다
        return v + (a if j else b)
    return re.sub(r'\{\{\s*(\w+)\s*\}\}(으로|로|과|와|은|는|을|를|이(?![다며고라었에면지])|가(?![다-힣]))?', sub, text)


# ── GPT 자료 가져오기: 읽기·검사·저장 ──────────────────────────────────
def parse_blobs(text):
    """붙여 넣은 글에서 JSON 덩어리를 모두 꺼내 하나로 합친다(PART A, PART B 를 이어 붙여도 됨)."""
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```\w*|```$', '', text, flags=re.M)
    dec, i, merged, n = json.JSONDecoder(), 0, {}, 0
    while True:
        j = text.find('{', i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError as e:
            raise ValueError('JSON 을 읽지 못했습니다 (%d번째 글자 근처): %s' % (j + 1, e.msg))
        if isinstance(obj, dict):
            merged.update(obj)
            n += 1
        i = end
    if not n:
        raise ValueError('JSON 을 찾지 못했습니다')
    return merged


def _overlap(a, b):
    return a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w'] and a['y'] < b['y'] + b['h'] and b['y'] < a['y'] + a['h']


def validate(conn, data):
    """(errors, warnings, summary). errors 가 있으면 저장하지 않는다."""
    E, W = [], []
    st = settings(conn)
    dept_rows = conn.execute('SELECT name, dept_type FROM departments').fetchall()
    dept_names = {r['name'] for r in dept_rows}
    team_names = {r['name'] for r in dept_rows if r['dept_type'] == 'team'}

    has_place = any(k in data for k in ('sites', 'floors', 'rooms'))
    site_codes, floor_map, room_codes = set(), {}, set()
    if has_place:
        for k in ('sites', 'floors', 'rooms'):
            if not isinstance(data.get(k), list) or not data.get(k):
                E.append('%s 목록이 비어 있습니다' % k)
        for s in data.get('sites') or []:
            code = str(s.get('code') or '').strip()
            if not code:
                E.append('건물(site)에 code 가 없습니다')
            elif code in site_codes:
                E.append('건물 코드 %s 가 두 번 나옵니다' % code)
            site_codes.add(code)
            if not s.get('address'):
                W.append('%s 건물 주소가 비어 있습니다 — 첫날 안내에 주소가 안 나옵니다' % code)
        listed = {}
        for f in data.get('floors') or []:
            key = (f.get('site'), f.get('floor'))
            if f.get('site') not in site_codes:
                E.append('%s층: 없는 건물 코드 %s' % (f.get('floor'), f.get('site')))
            if not isinstance(f.get('floor'), int):
                E.append('층 번호는 숫자여야 합니다: %r' % f.get('floor'))
                continue
            if key in floor_map:
                E.append('%s %s층이 두 번 나옵니다' % key)
            g = f.get('grid') or {}
            floor_map[key] = {'w': int(g.get('w') or 24), 'h': int(g.get('h') or 12)}
            for n in f.get('departments') or []:
                if n not in dept_names:
                    W.append('%s층의 "%s" 는 조직도에 없는 부서입니다' % (f['floor'], n))
                if n in listed:
                    W.append('"%s" 가 %s층과 %s층에 모두 적혀 있습니다 — %s층으로 봅니다' % (n, listed[n], f['floor'], listed[n]))
                else:
                    listed[n] = f['floor']
        for t in sorted(team_names - set(listed)):
            W.append('"%s" 은 어느 층에도 없습니다 — 첫날 안내에서 팀 회의실을 못 찾습니다' % t)

        by_floor = {}
        for r in data.get('rooms') or []:
            code = str(r.get('code') or '').strip()
            tag = code or r.get('name') or '?'
            if not code:
                E.append('회의실 %s 에 code 가 없습니다' % tag)
                continue
            if code in room_codes:
                E.append('회의실 코드 %s 가 두 번 나옵니다' % code)
            room_codes.add(code)
            key = (r.get('site'), r.get('floor'))
            if key not in floor_map:
                E.append('%s: %s %s층이 floors 에 없습니다' % (code, key[0], key[1]))
                continue
            if r.get('type') not in ROOM_TYPES:
                W.append('%s: 방 종류 "%s" 를 모릅니다 (%s 중 하나)' % (code, r.get('type'), ', '.join(ROOM_TYPES)))
            if r.get('bookable_by', 'all') not in BOOKABLE_LABEL:
                E.append('%s: bookable_by 는 all·hr·exec 중 하나여야 합니다' % code)
            try:
                cap = int(r.get('capacity'))
                if cap < 1:
                    raise ValueError
            except (TypeError, ValueError):
                E.append('%s: 수용 인원(capacity)이 1 이상의 숫자가 아닙니다' % code)
            try:
                box = {k: int(r.get(k)) for k in ('x', 'y', 'w', 'h')}
            except (TypeError, ValueError):
                E.append('%s: 배치도 좌표(x·y·w·h)가 숫자가 아닙니다' % code)
                continue
            gw, gh = floor_map[key]['w'], floor_map[key]['h']
            if box['w'] < 1 or box['h'] < 1 or box['x'] < 0 or box['y'] < 0 or box['x'] + box['w'] > gw or box['y'] + box['h'] > gh:
                E.append('%s: 배치도 밖으로 나갑니다 (층 크기 %dx%d)' % (code, gw, gh))
            box['code'] = code
            for other in by_floor.get(key, []):
                if _overlap(box, other):
                    E.append('%s 와 %s 가 배치도에서 겹칩니다' % (other['code'], code))
            by_floor.setdefault(key, []).append(box)
        if room_codes and st['orientation_room'] not in room_codes:
            W.append('오리엔테이션 방 %s 가 회의실 목록에 없습니다 — 회사·건물 설정에서 바꿔 주세요' % st['orientation_room'])
    else:
        room_codes = {r['code'] for r in rooms(conn)}

    ob = data.get('onboarding')
    if ob is not None:
        if not isinstance(ob, dict):
            E.append('onboarding 은 객체여야 합니다')
            ob = {}
        known = room_codes | {'TEAM'}
        o_s, o_e = hm(st['orientation_start']), hm(st['orientation_end'])
        fd = ob.get('first_day') or {}
        for it in fd.get('schedule') or []:
            _check_item(it, '첫날', known, E, W)
            try:
                s = hm(it['time'])
                e = s + int(it.get('minutes') or 0)
            except (KeyError, ValueError):
                continue
            if it.get('room') == st['orientation_room'] and s < o_e and o_s < e and (s, e) != (o_s, o_e):
                W.append('첫날 %s "%s" 이 교육장 오리엔테이션(%s~%s)과 겹칩니다' % (it['time'], it.get('title'), st['orientation_start'], st['orientation_end']))
        for it in ob.get('first_week') or []:
            _check_item(it, '%s일차' % it.get('day'), known, E, W)
            if it.get('room') != st['orientation_room']:
                continue
            try:
                s = hm(it['time'])
                e = s + int(it.get('minutes') or 0)
                n = int(it.get('day'))
            except (KeyError, ValueError, TypeError):
                continue
            if not (s < o_e and o_s < e):
                continue
            for wd in st['weekdays']:
                # 주말만 건너뛰어 그 입사 요일의 n일차 요일을 구한다
                k, d = 1, wd
                while k < n:
                    d = (d + 1) % 7
                    if d < 5:
                        k += 1
                if d in st['weekdays']:
                    W.append('%d일차 %s "%s" — %s요일 입사자는 이날이 %s요일이라 오리엔테이션과 교육장이 겹칩니다'
                             % (n, it['time'], it.get('title'), WEEKDAY_KO[wd], WEEKDAY_KO[d]))
        keys = set()
        for t in ob.get('tasks') or []:
            k = str(t.get('key') or '')
            if not k:
                E.append('할 일 "%s" 에 key 가 없습니다' % t.get('label'))
            elif k in keys:
                E.append('할 일 key %s 가 두 번 나옵니다' % k)
            keys.add(k)
            if t.get('category') not in TASK_CATEGORIES:
                W.append('할 일 %s: 분류 "%s" 를 모릅니다 (%s)' % (k, t.get('category'), ', '.join(TASK_CATEGORIES)))
            _check_placeholders(t.get('label'), '할 일 ' + k, W)
        _check_placeholders(ob.get('welcome_letter'), '환영 편지', W)
        for q in ob.get('faq') or []:
            _check_placeholders((q.get('q') or '') + (q.get('a') or ''), 'FAQ', W)
            if 'Google Calendar' in (q.get('a') or '') and '회의실' in (q.get('q') or ''):
                W.append('FAQ "%s" — 회의실 예약은 TalentCore 에서 합니다' % q.get('q'))

    if not has_place and ob is None:
        E.append('company/sites/floors/rooms 또는 onboarding 중 하나는 있어야 합니다')

    summary = {
        'sites': len(data.get('sites') or []), 'floors': len(data.get('floors') or []),
        'rooms': len(data.get('rooms') or []),
        'tasks': len((ob or {}).get('tasks') or []), 'sessions': len((ob or {}).get('sessions') or []),
        'faq': len((ob or {}).get('faq') or []), 'has_company': bool(data.get('company')),
        'has_onboarding': ob is not None,
    }
    return E, W, summary


def _check_item(it, where, known, E, W):
    if not re.match(r'^\d{1,2}:\d{2}$', str(it.get('time') or '')):
        E.append('%s "%s": 시간 형식이 HH:MM 이 아닙니다' % (where, it.get('title')))
    if it.get('room') and it['room'] not in known:
        W.append('%s "%s": 없는 방 코드 %s' % (where, it.get('title'), it['room']))
    _check_placeholders((it.get('title') or '') + (it.get('detail') or ''), where, W)


def _check_placeholders(text, where, W):
    for k in re.findall(r'\{\{\s*(\w+)\s*\}\}', text or ''):
        if k not in PLACEHOLDERS:
            W.append('%s: 모르는 자리표시자 {{%s}}' % (where, k))


def import_data(conn, data, user_id=None):
    """검사를 통과한 자료를 저장. 회의실은 지우지 않고 목록에서 빠진 방만 사용 중지(지난 예약 보존)."""
    co = data.get('company') or {}
    site_list = data.get('sites') or []
    if co:
        vals = {k: str(co.get(k) or '') for k in COMPANY_KEYS if co.get(k)}
        ceo_name = None
        try:
            r = conn.execute("SELECT u.name FROM approval_roles r JOIN users u ON u.id=r.user_id WHERE r.key='ceo'").fetchone()
            ceo_name = r[0] if r else None
        except Exception:
            pass
        vals['ceo'] = ceo_name or co.get('ceo') or co.get('ceo_title') or '대표이사'
        if site_list:
            s0 = site_list[0]
            vals['address'] = (s0.get('address') or '') + (' (%s)' % s0['building'] if s0.get('building') else '')
            vals['zip'] = s0.get('zip') or ''
        for k, v in vals.items():
            conn.execute('INSERT INTO company_settings (key, value) VALUES (?, ?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
    if any(k in data for k in ('sites', 'floors', 'rooms')):
        conn.execute('DELETE FROM workplace_sites')
        for i, s in enumerate(site_list):
            conn.execute('INSERT INTO workplace_sites (code, name, building, address, address_detail, zip, transit, '
                         'parking, building_hours, visitor_access, employee_access, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (s['code'], s.get('name') or s['code'], s.get('building') or '', s.get('address') or '',
                          s.get('address_detail') or '', s.get('zip') or '',
                          json.dumps(s.get('transit') or [], ensure_ascii=False), s.get('parking') or '',
                          s.get('building_hours') or '', s.get('visitor_access') or '', s.get('employee_access') or '', i))
        conn.execute('DELETE FROM workplace_floors')
        for f in data.get('floors') or []:
            g = f.get('grid') or {}
            conn.execute('INSERT INTO workplace_floors (site_code, floor, label, departments, seats, amenities, grid_w, grid_h) '
                         'VALUES (?,?,?,?,?,?,?,?)',
                         (f['site'], f['floor'], f.get('label') or '', json.dumps(f.get('departments') or [], ensure_ascii=False),
                          int(f.get('seats') or 0), json.dumps(f.get('amenities') or [], ensure_ascii=False),
                          int(g.get('w') or 24), int(g.get('h') or 12)))
        codes = []
        for r in data.get('rooms') or []:
            codes.append(r['code'])
            conn.execute(
                'INSERT INTO workplace_rooms (code, name, site_code, floor, type, capacity, equipment, bookable_by, x, y, w, h, note, active) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(code) DO UPDATE SET name=excluded.name, site_code=excluded.site_code, '
                'floor=excluded.floor, type=excluded.type, capacity=excluded.capacity, equipment=excluded.equipment, '
                'bookable_by=excluded.bookable_by, x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h, note=excluded.note, active=1',
                (r['code'], r.get('name') or r['code'], r['site'], r['floor'], r.get('type') or 'meeting', int(r['capacity']),
                 json.dumps(r.get('equipment') or [], ensure_ascii=False), r.get('bookable_by') or 'all',
                 int(r['x']), int(r['y']), int(r['w']), int(r['h']), r.get('note') or ''))
        if codes:
            conn.execute('UPDATE workplace_rooms SET active=0 WHERE code NOT IN (%s)' % ','.join('?' * len(codes)), codes)
    if data.get('onboarding') is not None:
        conn.execute('INSERT INTO onboarding_content (key, body, updated_at, updated_by) VALUES (?,?,CURRENT_TIMESTAMP,?) '
                     'ON CONFLICT(key) DO UPDATE SET body=excluded.body, updated_at=CURRENT_TIMESTAMP, updated_by=excluded.updated_by',
                     ('main', json.dumps(data['onboarding'], ensure_ascii=False), user_id))


def onboarding_content(conn):
    try:
        r = conn.execute("SELECT body, updated_at FROM onboarding_content WHERE key='main'").fetchone()
    except Exception:
        return None
    return _j(r[0], None) if r else None


def export_data(conn):
    """지금 저장된 자료를 가져오기 양식 그대로 — 고쳐서 다시 붙여 넣을 수 있게."""
    co = {r[0]: r[1] for r in conn.execute('SELECT key, value FROM company_settings')}
    out = {
        'company': {k: co.get(k, '') for k in ('name', 'name_en', 'reg_no', 'founded', 'industry', 'tel', 'website', 'intro')},
        'sites': [{k: s[k] for k in ('code', 'name', 'building', 'address', 'address_detail', 'zip', 'transit',
                                     'parking', 'building_hours', 'visitor_access', 'employee_access')} for s in sites(conn)],
        'floors': [{'site': f['site_code'], 'floor': f['floor'], 'label': f['label'], 'departments': f['departments'],
                    'seats': f['seats'], 'amenities': f['amenities'], 'grid': {'w': f['grid_w'], 'h': f['grid_h']}}
                   for f in floors(conn)],
        'rooms': [{'code': r['code'], 'name': r['name'], 'site': r['site_code'], 'floor': r['floor'], 'type': r['type'],
                   'capacity': r['capacity'], 'equipment': r['equipment'], 'bookable_by': r['bookable_by'],
                   'x': r['x'], 'y': r['y'], 'w': r['w'], 'h': r['h'], 'note': r['note']} for r in rooms(conn)],
    }
    ob = onboarding_content(conn)
    if ob is not None:
        out['onboarding'] = ob
    return out


# ── 온보딩: 가져온 자료 + 그 사람의 입사일·부서·팀장·버디 → 첫 주 안내 ──────
def onboarding_tasks(conn):
    """체크리스트 원본. 자료가 없으면 빈 목록(부르는 쪽이 옛 기본 목록으로 대신한다)."""
    ob = onboarding_content(conn) or {}
    return [t for t in (ob.get('tasks') or []) if isinstance(t, dict) and t.get('key') and t.get('label')]


def onboarding_person(conn, user_id):
    """자리표시자에 들어갈 값. 팀장은 보고라인(manager_id), 없으면 부서장."""
    u = conn.execute(
        'SELECT u.id, u.name, u.hire_date, u.department_id, u.manager_id, u.buddy_id, '
        'd.name AS dept, d.leader_id, p.name AS pos FROM users u '
        'LEFT JOIN departments d ON d.id=u.department_id LEFT JOIN positions p ON p.id=u.position_id '
        'WHERE u.id=?', (user_id,)).fetchone()
    if not u:
        return None

    def person(pid):
        if not pid or pid == user_id:
            return None
        r = conn.execute('SELECT u.id, u.name, u.email, d.name AS dept, p.name AS pos FROM users u '
                         'LEFT JOIN departments d ON d.id=u.department_id LEFT JOIN positions p ON p.id=u.position_id '
                         'WHERE u.id=?', (pid,)).fetchone()
        return dict(r) if r else None

    co = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM company_settings WHERE key IN ('name', 'address')")}
    manager = person(u['manager_id']) or person(u['leader_id'])
    buddy = person(u['buddy_id'])
    hd = to_date(u['hire_date'])
    return {
        'user': dict(u), 'manager': manager, 'buddy': buddy, 'hire': hd,
        'values': {
            'employee_name': u['name'], 'department': u['dept'] or '소속 부서', 'position': u['pos'] or '담당 직무',
            'hire_date': '%s(%s)' % (hd.strftime('%Y.%m.%d'), WEEKDAY_KO[hd.weekday()]) if hd else '입사일',
            'manager_name': manager['name'] if manager else '팀장',
            'buddy_name': buddy['name'] if buddy else '버디',
            'company_name': co.get('name') or '회사', 'company_address': co.get('address') or '',
        },
    }


def _where(conn, code, team, cache):
    """일정의 방 코드 → 사람이 읽는 장소. TEAM 은 그 사람 팀 층의 회의실."""
    if code in cache:
        return cache[code]
    r = team if code == 'TEAM' else (room(conn, code) if code else None)
    if r:
        out = {'code': r['code'], 'name': r['name'], 'floor': r['floor'], 'label': '%s층 %s' % (r['floor'], r['name'])}
    elif code == 'TEAM':
        out = {'code': None, 'name': '팀 자리', 'floor': None, 'label': '팀 자리'}
    else:
        out = {'code': code, 'name': code or '', 'floor': None, 'label': code or ''}
    cache[code] = out
    return out


def onboarding_plan(conn, user_id, today=None):
    """한 사람의 첫날·첫 주 안내. 자료(onboarding_content)가 없으면 None."""
    ob = onboarding_content(conn)
    who = onboarding_person(conn, user_id)
    if not ob or not who:
        return None
    today = today or date.today()
    st = settings(conn)
    vals = who['values']
    team = team_room(conn, who['user']['department_id'])
    tfloor = dept_floor(conn, who['user']['department_id'])
    hire = who['hire']
    cache = {}

    def owner(role):
        if role == '팀장' and who['manager']:
            return '팀장 %s' % who['manager']['name']
        if role == '버디' and who['buddy']:
            return '버디 %s' % who['buddy']['name']
        return role or ''

    def item(x):
        t = str(x.get('time') or '')
        try:
            end = fmt_hm(hm(t) + int(x.get('minutes') or 0))
        except (ValueError, TypeError):
            end = ''
        code = x.get('room') or ''
        return {
            'time': t, 'end': end, 'title': fill(x.get('title'), vals), 'detail': fill(x.get('detail'), vals),
            'where': _where(conn, code, team, cache), 'owner': owner(x.get('owner_role')),
            'orientation': code == st['orientation_room'] and t == st['orientation_start'],
        }

    fd = ob.get('first_day') or {}
    by_day = {1: [item(x) for x in (fd.get('schedule') or [])]}
    for x in ob.get('first_week') or []:
        try:
            n = int(x.get('day') or 0)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            by_day.setdefault(n, []).append(item(x))
    days = []
    for n in sorted(by_day):
        d = working_day(conn, hire, n) if hire else None
        days.append({'n': n, 'date': d, 'label': ('%s(%s)' % (d.strftime('%m.%d'), WEEKDAY_KO[d.weekday()])) if d else '',
                     'today': d == today, 'past': bool(d and d < today),
                     'items': sorted(by_day[n], key=lambda i: i['time'])})

    due = {}
    for t in ob.get('tasks') or []:
        try:
            n = int(t.get('due_day') or 0)
        except (TypeError, ValueError):
            n = 0
        if t.get('key') and n >= 1:
            due[t['key']] = working_day(conn, hire, n) if hire else None
    site = (sites(conn) or [None])[0]
    orient = room(conn, st['orientation_room'])
    return {
        'who': who, 'values': vals, 'days': days, 'due': due,
        'dday': (hire - today).days if hire else None,
        'arrive_time': fd.get('arrive_time') or '', 'meet_place': fill(fd.get('meet_place'), vals),
        'bring': [fill(b, vals) for b in (fd.get('bring') or [])],
        'site': site, 'team_room': team, 'team_floor': tfloor,
        'orientation': {'room': orient['name'] if orient else st['orientation_room'], 'floor': orient['floor'] if orient else None,
                        'start': st['orientation_start'], 'end': st['orientation_end']},
        'welcome_letter': fill(ob.get('welcome_letter'), vals),
        'office_guide': [{'title': fill(g.get('title'), vals), 'body': fill(g.get('body'), vals)}
                         for g in (ob.get('office_guide') or []) if isinstance(g, dict)],
        'faq': [{'q': fill(f.get('q'), vals), 'a': fill(f.get('a'), vals)} for f in (ob.get('faq') or []) if isinstance(f, dict)],
        'sessions': [{'title': fill(s.get('title'), vals), 'owner': s.get('owner_role') or '', 'minutes': s.get('minutes'),
                      'slides': [fill(x, vals) for x in (s.get('slides') or [])]}
                     for s in (ob.get('sessions') or []) if isinstance(s, dict)],
    }


def upcoming_start_days(conn, today=None, n=4):
    """인사팀용: 다가오는 입사일마다 누가 오는지와 오리엔테이션 장소."""
    today = today or date.today()
    st = settings(conn)
    orient = room(conn, st['orientation_room'])
    out = []
    for d in next_start_dates(conn, today, n, st):
        people = hires_on(conn, d)
        left = (d - today).days
        out.append({
            'date': d, 'label': '%s(%s)' % (d.strftime('%m.%d'), WEEKDAY_KO[d.weekday()]), 'dday': left,
            'people': people, 'room': orient['name'] if orient else st['orientation_room'],
            'floor': orient['floor'] if orient else None, 'start': st['orientation_start'], 'end': st['orientation_end'],
            'released': not people and left <= st['release_days'],
        })
    return out
