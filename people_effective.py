"""효력일 기록(E1).

부서·직급·상위 관리자·고용 형태·기본급은 '언제부터 효력인지'로 한 줄씩 남는다.
  - effective_values: 사람 × 칸 × 효력일 한 줄. value = id·코드·금액 원값, label = 그때 읽는 글.
    그날 기준 값 = 효력일이 그날 이하인 줄 중 가장 늦은 것(같은 날이면 나중에 적은 것).
  - users / employee_salary 는 여전히 '오늘 값'이다. 이 표는 그 위에 날짜를 붙인 기록.
  - 승인된 인사 발령(personnel_actions)은 발령일로 들어온다(sync_actions). 미래 발령도 미리 들어와서
    미래 날짜로 보면 '예정' 값이 보인다.
  - 어디서 고쳤는지 모르는 변경(엑셀·연봉 조정 등)은 sync_current() 가 오늘 날짜로 붙인다.
  - 처음 쓸 때 seed() 가 지금까지의 인사 발령·변경 이력·급여 이력으로 채운다(회사마다 한 번).
  - 지난 효력일 급여 변경은 기록만 한다 — 급여 계산(소급 차액)은 건드리지 않는다.
"""
from datetime import date

FIELDS = (
    ('department_id', '부서'), ('position_id', '직급'), ('manager_id', '상위 관리자'),
    ('employment_type', '고용 형태'), ('salary', '기본급'),
)
LABEL = dict(FIELDS)
USER_FIELDS = ('department_id', 'position_id', 'manager_id', 'employment_type')

SOURCE_LABEL = {
    'seed': '처음 기록', 'edit': '직원 수정', 'action': '인사 발령', 'sync': '변경 확인',
    'hire': '입사', 'salary': '급여 이력',
}

_PA_FIELD = {
    'dept_change': 'department_id', 'position_change': 'position_id', 'manager_change': 'manager_id',
    'employment_type_change': 'employment_type', 'salary_change': 'salary',
}
PA_TYPE = {v: k for k, v in _PA_FIELD.items()}

_EMP = {'full_time': '정규직', 'contract': '계약직', 'intern': '인턴', 'part_time': '파트타임'}
_EMP_BACK = {v: k for k, v in _EMP.items()}
_EMP_BACK.update({'시간제': 'part_time'})
_REF = {'department_id': 'departments', 'position_id': 'positions', 'manager_id': 'users'}

FAR_PAST = '2000-01-01'

_DDL = (
    '''CREATE TABLE IF NOT EXISTS effective_values (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        INTEGER NOT NULL REFERENCES users(id),
        field          TEXT NOT NULL,
        value          TEXT,
        label          TEXT,
        effective_date TEXT NOT NULL,
        source         TEXT NOT NULL DEFAULT 'edit',
        ref            TEXT,
        actor_id       INTEGER,
        actor_name     TEXT,
        reason         TEXT,
        voided_at      TEXT,
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_eff_user ON effective_values(user_id, field, effective_date)',
    'CREATE INDEX IF NOT EXISTS idx_eff_ref ON effective_values(ref)',
)

_ready = set()


def _key(c):
    try:
        return c.execute('PRAGMA database_list').fetchone()[2]
    except Exception:
        return id(c)


def ensure_schema(c):
    """표를 만들고, 처음이면 지금까지 기록으로 채운다. 회사(DB)마다 한 번."""
    k = _key(c)
    if k in _ready:
        return
    for q in _DDL:
        c.execute(q)
    _ready.add(k)
    if not c.execute('SELECT 1 FROM effective_values LIMIT 1').fetchone():
        seed(c)


def forget(c=None):
    """연습 회사처럼 DB 파일을 새로 만들 때."""
    if c is None:
        _ready.clear()
    else:
        _ready.discard(_key(c))


def today():
    return date.today().isoformat()


def _norm(v):
    return '' if v is None else str(v).strip()


def label_of(c, field, value):
    v = _norm(value)
    if not v:
        return ''
    if field in _REF:
        r = c.execute('SELECT name FROM %s WHERE id=?' % _REF[field], (v,)).fetchone()
        return r[0] if r and r[0] else '#' + v
    if field == 'employment_type':
        return _EMP.get(v, v)
    if field == 'salary':
        try:
            return '{:,}원'.format(int(float(v)))
        except ValueError:
            return v
    return v


def _resolve(c, field, text):
    """옛 기록의 '읽는 글'(부서 이름 등)을 원값으로. 못 찾으면 None."""
    t = _norm(text)
    if not t or t in ('—', '-', '없음'):
        return None
    if '|' in t:
        return t.split('|')[-1] or None
    if field in _REF:
        if t.startswith('#') and t[1:].isdigit():
            return t[1:]
        r = c.execute('SELECT id FROM %s WHERE name=? ORDER BY id LIMIT 1' % _REF[field], (t,)).fetchone()
        return str(r[0]) if r else None
    if field == 'employment_type':
        return _EMP_BACK.get(t, t if t in _EMP else None)
    if field == 'salary':
        digits = ''.join(ch for ch in t if ch.isdigit())
        return digits or None
    return t


def current_values(c, uid):
    """오늘 users / employee_salary 에 들어 있는 값."""
    r = c.execute('SELECT department_id, position_id, manager_id, employment_type FROM users WHERE id=?',
                  (uid,)).fetchone()
    if r is None:
        return {}
    out = {f: _norm(r[i]) or None for i, f in enumerate(USER_FIELDS)}
    s = c.execute('SELECT base_salary FROM employee_salary WHERE user_id=?', (uid,)).fetchone()
    out['salary'] = _norm(s[0]) or None if s and s[0] else None
    return out


def add(c, uid, field, value, eff_date, source, ref=None, actor_id=None, actor_name=None, reason=None):
    """한 줄 남긴다. commit 은 부르는 쪽이."""
    v = _norm(value) or None
    c.execute(
        'INSERT INTO effective_values (user_id, field, value, label, effective_date, source, ref, actor_id, '
        'actor_name, reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uid, field, v, label_of(c, field, v) or None, eff_date, source, ref, actor_id, actor_name, reason))


def value_at(c, uid, field, on):
    """그날 기준 (value, 줄). 기록이 없으면 (None, None)."""
    ensure_schema(c)
    r = c.execute(
        'SELECT * FROM effective_values WHERE user_id=? AND field=? AND effective_date<=? AND voided_at IS NULL '
        'ORDER BY effective_date DESC, id DESC LIMIT 1', (uid, field, on)).fetchone()
    return (r['value'] if r else None), r


def last_date(c, uid, field):
    r = c.execute('SELECT MAX(effective_date) FROM effective_values WHERE user_id=? AND field=? '
                  'AND voided_at IS NULL AND effective_date<=?', (uid, field, today())).fetchone()
    return r[0] if r else None


def sync_actions(c):
    """승인된 인사 발령 중 아직 안 들어온 것을 발령일로 넣는다(미래 발령 포함)."""
    try:
        rows = c.execute(
            "SELECT pa.* FROM personnel_actions pa WHERE pa.status='approved' AND pa.action_type IN (%s) "
            "AND NOT EXISTS (SELECT 1 FROM effective_values e WHERE e.ref = 'pa:' || pa.id)"
            % ','.join("'%s'" % t for t in _PA_FIELD)).fetchall()
    except Exception:
        return 0
    for pa in rows:
        f = _PA_FIELD[pa['action_type']]
        v = _resolve(c, f, pa['to_value']) if f != 'employment_type' else (_norm(pa['to_value']) or None)
        actor = c.execute('SELECT name FROM users WHERE id=?', (pa['processed_by'],)).fetchone() \
            if pa['processed_by'] else None
        add(c, pa['user_id'], f, v, _norm(pa['effective_date'])[:10] or today(), 'action',
            ref='pa:%s' % pa['id'], actor_id=pa['processed_by'], actor_name=actor[0] if actor else None,
            reason=pa['reason'])
    return len(rows)


def sync_current(c, uid, actor_id=None, actor_name=None):
    """오늘 값이 기록과 다르면(어디선가 바로 고침) 오늘 날짜로 한 줄. 남긴 줄 수."""
    cur = current_values(c, uid)
    if not cur:
        return 0
    n = 0
    for f in LABEL:
        v, _ = value_at(c, uid, f, today())
        if _norm(v) != _norm(cur.get(f)):
            add(c, uid, f, cur.get(f), today(), 'sync', actor_id=actor_id, actor_name=actor_name)
            n += 1
    return n


def sync_all(c):
    """매일 한 번: 발령 넣기 + 전원 오늘 값 맞추기. commit 은 부르는 쪽이."""
    ensure_schema(c)
    n = sync_actions(c)
    for (uid,) in c.execute('SELECT id FROM users').fetchall():
        n += sync_current(c, uid)
    return n


def seed(c):
    """처음 한 번: 인사 발령 · 변경 이력 · 급여 이력으로 날짜별 줄을 만든다."""
    users = c.execute('SELECT id, hire_date, created_at FROM users').fetchall()
    try:
        has_fc = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_changes'").fetchone())
    except Exception:
        has_fc = False
    for u in users:
        uid = u[0]
        start = _norm(u[1])[:10] or _norm(u[2])[:10] or FAR_PAST
        cur = current_values(c, uid)
        for f in LABEL:
            ev = []   # (날짜, 옛 값, 새 값, 출처, ref, 사유)
            pt = PA_TYPE[f]
            for pa in c.execute(
                    "SELECT id, from_value, to_value, effective_date, reason FROM personnel_actions "
                    "WHERE user_id=? AND action_type=? AND status='approved' AND (applied_at IS NOT NULL "
                    "OR effective_date<=?) ORDER BY effective_date, id", (uid, pt, today())):
                new = _resolve(c, f, pa[2]) if f != 'employment_type' else (_norm(pa[2]) or None)
                ev.append((_norm(pa[3])[:10], _resolve(c, f, pa[1]), new, 'action', 'pa:%s' % pa[0], pa[4]))
            if has_fc and f in USER_FIELDS:
                for fc in c.execute(
                        "SELECT old_value, new_value, created_at, source, ref, reason FROM field_changes "
                        "WHERE user_id=? AND field=? ORDER BY created_at, id", (uid, f)):
                    if fc[4] and str(fc[4]).startswith('pa:'):
                        continue   # 인사 발령 줄로 이미 들어감
                    ev.append((_norm(fc[2])[:10], _resolve(c, f, fc[0]), _resolve(c, f, fc[1]),
                               'edit', None, fc[5]))
            if f == 'salary':
                for sh in c.execute('SELECT changed_at, old_base_salary, new_base_salary, reason FROM salary_history '
                                    'WHERE user_id=? ORDER BY changed_at, id', (uid,)):
                    ev.append((_norm(sh[0])[:10], _norm(sh[1]) or None, _norm(sh[2]) or None, 'salary', None, sh[3]))
            ev = [e for e in ev if e[0]]
            ev.sort(key=lambda e: e[0])
            if ev:
                first_old = ev[0][1]
                d0 = min(start, ev[0][0])
                if first_old and d0 < ev[0][0]:
                    add(c, uid, f, first_old, d0, 'seed')
                for e in ev:
                    add(c, uid, f, e[2], e[0], e[3], ref=e[4], reason=e[5])
            elif cur.get(f):
                add(c, uid, f, cur.get(f), start, 'seed')
    sync_actions(c)
    for u in users:
        sync_current(c, u[0])


def timeline(c, uid):
    """직원 상세용: 효력일 역순. 미래 줄은 upcoming=True."""
    ensure_schema(c)
    t = today()
    out = []
    for r in c.execute('SELECT * FROM effective_values WHERE user_id=? AND voided_at IS NULL '
                       'ORDER BY effective_date DESC, id DESC', (uid,)):
        out.append(dict(date=r['effective_date'], field=r['field'], label=LABEL.get(r['field'], r['field']),
                        value=r['label'] or '없음', source=SOURCE_LABEL.get(r['source'], r['source']),
                        actor=r['actor_name'] or '', reason=r['reason'] or '',
                        upcoming=r['effective_date'] > t, salary=r['field'] == 'salary'))
    return out


def as_of(c, on, uids=None):
    """그날 기준 값들. {uid: {field: value}}."""
    ensure_schema(c)
    q = ('SELECT e.user_id, e.field, e.value FROM effective_values e '
         'JOIN (SELECT user_id, field, MAX(effective_date) AS d FROM effective_values '
         '      WHERE effective_date<=? AND voided_at IS NULL GROUP BY user_id, field) m '
         '  ON m.user_id=e.user_id AND m.field=e.field AND m.d=e.effective_date '
         'WHERE e.voided_at IS NULL ORDER BY e.id')
    out = {}
    for uid, f, v in c.execute(q, (on,)):
        if uids is not None and uid not in uids:
            continue
        out.setdefault(uid, {})[f] = v   # 같은 날 여러 줄이면 id 순서로 덮여 마지막 것이 남는다
    return out


def schedule_edit(c, uid, changes, eff_date, actor_id, reason=None):
    """직원 수정에서 미래 효력일로 고친 칸 → 승인된 예약 발령. changes = {field: 새 원값}. 만든 발령 수."""
    n = 0
    for f, v in changes.items():
        cur = current_values(c, uid).get(f)
        old_label = label_of(c, f, cur) or '—'
        new_label = label_of(c, f, v)
        if f in _REF:
            to_value = '%s|%s' % (new_label, _norm(v)) if v else '없음|'
        else:
            to_value = _norm(v)
        c.execute(
            "INSERT INTO personnel_actions (user_id, action_type, from_value, to_value, effective_date, reason, "
            "status, processed_by) VALUES (?,?,?,?,?,?,'approved',?)",
            (uid, PA_TYPE[f], old_label, to_value, eff_date, reason or '직원 수정 (효력일 예약)', actor_id))
        n += 1
    sync_actions(c)
    return n




def parse_on(raw):
    """주소의 ?on= → 'YYYY-MM-DD'. 비었거나 틀리거나 오늘이면 None(= 지금 값 그대로)."""
    t = _norm(raw)
    try:
        d = date.fromisoformat(t).isoformat()
    except ValueError:
        return None
    return None if d == today() else d


def employed_on(c, on):
    """그날 재직한 사람 id. 입사일 ≤ 그날 ≤ 퇴사일(없으면 재직 중인 사람만)."""
    rows = c.execute(
        "SELECT id FROM users WHERE COALESCE(hire_date,'') <= ? AND ("
        " (COALESCE(termination_date,'')='' AND status='active') OR COALESCE(termination_date,'') >= ?)",
        (on, on)).fetchall()
    return {r[0] for r in rows}


def overlay(c, on, rows):
    """직원 줄(dict 가능한 것)에 그날 기준 부서·직급·매니저·고용형태를 덮는다.
    바뀐 칸은 _diff 에 {field: 지금 읽는 글}. 이름 칸(dept_name·pos_name)도 다시 붙인다."""
    rows = [dict(r) for r in rows]
    vals = as_of(c, on, {r['id'] for r in rows})
    names = {}
    for f, t in _REF.items():
        names[f] = {str(i): n for i, n in c.execute('SELECT id, name FROM %s' % t)}
    for r in rows:
        r['_diff'] = {}
        got = vals.get(r['id'], {})
        for f in USER_FIELDS:
            if f not in got:
                continue
            v = got[f]
            if _norm(v) != _norm(r.get(f)):
                r['_diff'][f] = label_of(c, f, r.get(f)) or '없음'
            r[f] = int(v) if f in _REF and _norm(v).isdigit() else (v or None)
        r['dept_name'] = names['department_id'].get(_norm(r.get('department_id')))
        r['pos_name'] = names['position_id'].get(_norm(r.get('position_id')))
        r['manager_name'] = names['manager_id'].get(_norm(r.get('manager_id')))
    return rows
