"""직원 정보 변경 이력(B1).

직원 정보가 바뀌면 누가·언제·무엇을·바뀌기 전→후 를 한 줄씩 남긴다.
  - field_changes: 사람 × 칸 한 줄. 값은 '읽을 수 있는 글'로 저장한다(부서 id 가 아니라 부서 이름).
    나중에 부서 이름이 바뀌어도 그때 이름이 남는다.
  - 고치는 쪽은 snapshot() 으로 바꾸기 전 값을 잡고, 저장한 뒤 record_diff() 를 부른다.
  - 비밀번호는 값을 남기지 않는다('(변경)'만).
  - 이 모듈이 생기기 전의 인사 발령(personnel_actions)은 timeline() 이 함께 보여 준다.
"""

TRACKED = (
    ('name', '이름'), ('email', '이메일'), ('phone', '휴대폰'), ('address', '주소'),
    ('emergency_name', '비상 연락처 이름'), ('emergency_phone', '비상 연락처 번호'),
    ('emergency_relation', '비상 연락처 관계'),
    ('birth_date', '생년월일'), ('hire_date', '입사일'),
    ('department_id', '부서'), ('position_id', '직급'), ('job_family_id', '직군'),
    ('job_profile_id', '세부 직무'), ('manager_id', '상위 관리자'),
    ('role', '권한'), ('employment_type', '고용 형태'), ('work_type', '근무 형태'),
    ('status', '재직 상태'), ('termination_date', '퇴사일'), ('termination_reason', '퇴사 사유'),
)
LABEL = dict(TRACKED)
LABEL['password'] = '비밀번호'
LABEL['salary'] = '기본급'

SOURCE_LABEL = {
    'edit': '직원 수정', 'self': '본인 수정', 'bulk': '엑셀 일괄 변경', 'action': '인사 발령',
    'resign': '퇴사 처리', 'password': '비밀번호', 'system': '시스템',
}

PERSONAL = {'name', 'email', 'phone', 'address', 'emergency_name', 'emergency_phone',
            'emergency_relation', 'birth_date'}


def kind_of(field):
    """화면 거르기용: hr(인사) · personal(개인 정보) · account(계정)."""
    if field == 'password':
        return 'account'
    return 'personal' if field in PERSONAL else 'hr'


_ROLE = {'employee': '직원', 'manager': '매니저', 'admin': '관리자', 'recruiter': '리크루터', 'guest': '게스트'}
_EMP = {'full_time': '정규직', 'contract': '계약직', 'intern': '인턴', 'part_time': '파트타임'}
_WORK = {'standard': '일반', 'flex': '유연근무', 'remote': '재택', 'shift': '교대'}
_STATUS = {'active': '재직', 'resigned': '퇴사'}

# id 를 이름으로 바꿔 적는 칸: (표, 이름 칸)
_REF = {
    'department_id': ('departments', 'name'), 'position_id': ('positions', 'name'),
    'job_family_id': ('job_families', 'name'), 'job_profile_id': ('job_profiles', 'name'),
    'manager_id': ('users', 'name'),
}


def _cols(c, table):
    return {r[1] for r in c.execute('PRAGMA table_info(%s)' % table).fetchall()}


_DDL = (
    '''CREATE TABLE IF NOT EXISTS field_changes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        field       TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        source      TEXT NOT NULL DEFAULT 'edit',
        ref         TEXT,
        actor_id    INTEGER,
        actor_name  TEXT,
        reason      TEXT,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_field_changes_user ON field_changes(user_id, created_at)',
)


def ensure_schema(c):
    # executescript 는 진행 중인 저장을 먼저 commit 해 버리므로 execute 로 하나씩.
    for q in _DDL:
        c.execute(q)


def _row(c, uid):
    cols = _cols(c, 'users')
    keep = [f for f, _ in TRACKED if f in cols]
    r = c.execute('SELECT %s FROM users WHERE id=?' % ', '.join(keep), (uid,)).fetchone()
    if r is None:
        return {}
    return {k: r[i] for i, k in enumerate(keep)}


def snapshot(c, uid):
    """바꾸기 전 값. 저장 뒤 record_diff(…, before=이 값) 으로 넘긴다."""
    return _row(c, uid)


def _norm(v):
    if v is None:
        return ''
    return str(v).strip()


def readable(c, field, value):
    """칸 값을 사람이 읽는 글로."""
    v = _norm(value)
    if not v:
        return ''
    if field in _REF:
        table, col = _REF[field]
        try:
            r = c.execute('SELECT %s FROM %s WHERE id=?' % (col, table), (v,)).fetchone()
        except Exception:
            r = None
        return r[0] if r and r[0] else '#' + v
    if field == 'role':
        return _ROLE.get(v, v)
    if field == 'employment_type':
        return _EMP.get(v, v)
    if field == 'work_type':
        return _WORK.get(v, v)
    if field == 'status':
        return _STATUS.get(v, v)
    return v


def _insert(c, uid, field, old, new, source, actor_id, actor_name, ref, reason):
    ensure_schema(c)
    c.execute(
        'INSERT INTO field_changes (user_id, field, old_value, new_value, source, ref, actor_id, actor_name, reason) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (uid, field, old or None, new or None, source, ref, actor_id, actor_name, reason))


def record_diff(c, uid, before, source, actor_id=None, actor_name=None, ref=None, reason=None):
    """before 와 지금 값을 비교해 바뀐 칸마다 한 줄. 남긴 줄 수를 돌려준다. commit 은 부르는 쪽이."""
    if not before:
        return 0
    after = _row(c, uid)
    n = 0
    for f, _ in TRACKED:
        if f not in before or f not in after:
            continue
        if _norm(before[f]) == _norm(after[f]):
            continue
        _insert(c, uid, f, readable(c, f, before[f]), readable(c, f, after[f]),
                source, actor_id, actor_name, ref, reason)
        n += 1
    return n


def record_change(c, uid, field, old, new, source, actor_id=None, actor_name=None, ref=None, reason=None):
    """한 칸만 직접 남길 때. 비밀번호는 값 없이 '(변경)'."""
    if field == 'password':
        _insert(c, uid, 'password', None, '(변경)', source, actor_id, actor_name, ref, reason)
        return
    _insert(c, uid, field, _norm(old), _norm(new), source, actor_id, actor_name, ref, reason)


_PA_FIELD = {
    'dept_change': 'department_id', 'position_change': 'position_id', 'role_change': 'role',
    'employment_type_change': 'employment_type', 'manager_change': 'manager_id',
}


def timeline(c, uid, limit=300):
    """시간 역순 목록. 각 줄: at, field, label, old, new, source, source_label, actor, reason."""
    ensure_schema(c)
    out = []
    for r in c.execute(
            'SELECT field, old_value, new_value, source, actor_name, reason, created_at, ref '
            'FROM field_changes WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?', (uid, limit)):
        out.append(dict(at=r[6], field=r[0], label=LABEL.get(r[0], r[0]), old=r[1] or '', new=r[2] or '',
                        source=r[3], source_label=SOURCE_LABEL.get(r[3], r[3]),
                        actor=r[4] or '', reason=r[5] or ''))
    # 이 모듈 전에 반영된 인사 발령(급여 발령은 급여 탭에서 본다)
    linked = {r[0] for r in c.execute(
        "SELECT ref FROM field_changes WHERE user_id=? AND ref LIKE 'pa:%'", (uid,))}
    try:
        rows = c.execute(
            "SELECT pa.id, pa.action_type, pa.from_value, pa.to_value, pa.reason, "
            "COALESCE(pa.applied_at, pa.effective_date, pa.created_at), u.name "
            "FROM personnel_actions pa LEFT JOIN users u ON u.id = pa.processed_by "
            "WHERE pa.user_id=? AND pa.status='approved' AND pa.action_type != 'salary_change'",
            (uid,)).fetchall()
    except Exception:
        rows = []
    for r in rows:
        if 'pa:%s' % r[0] in linked:
            continue
        f = _PA_FIELD.get(r[1], r[1])
        new = r[3] or ''
        if '|' in new:
            new = new.split('|')[0]
        old = r[2] or ''
        if f in ('role', 'employment_type'):
            new, old = readable(c, f, new), readable(c, f, old)
        out.append(dict(at=r[5], field=f, label=LABEL.get(f, f), old=old, new=new,
                        source='action', source_label=SOURCE_LABEL['action'],
                        actor=r[6] or '', reason=r[4] or ''))
    for x in out:
        x['kind'] = kind_of(x['field'])
    out.sort(key=lambda x: _norm(x['at']), reverse=True)
    return out[:limit]
