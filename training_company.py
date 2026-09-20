"""
training_company.py — 연습(교육) 회사

Grow 교육 모드가 쓰는 별도 테넌트를 만들고, 언제든 처음 상태로 되돌린다.
연습 데이터는 실제 회사 데이터와 완전히 다른 DB 파일에 들어간다(tenant_N.db).

규칙
  - 비밀값 없음. 연습 계정은 비밀번호로 로그인할 수 없다(입장표로만 들어간다).
  - is_training=1 인 테넌트만 건드린다. 테넌트 1(데모)은 어떤 경우에도 건드리지 않는다.

쓰는 법
  python training_company.py --status    연습 회사 상태 보기
  python training_company.py --reset     연습 회사를 처음 상태로 되돌리기(없으면 새로 만든다)
"""

import os
import sqlite3
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from database import init_db
from master_db import (
    create_tenant, get_master_db, get_tenant_db_path, get_training_tenant,
    mark_training_tenant, get_or_create_api_token, register_tenant_user,
    migrate_subscriptions,
)

COMPANY_NAME = '(주)새봄테크'
ADMIN_EMAIL  = 'hana@saebom.example'
ADMIN_NAME   = '김하나'

# ── 조직 ────────────────────────────────────────────────────
# (id, 이름, 상위, 종류)
DEPARTMENTS = [
    (1, '경영지원실',   None, 'dept'),
    (2, '개발실',       None, 'dept'),
    (3, '사업실',       None, 'dept'),
    (4, '인사팀',          1, 'team'),
    (5, '재무팀',          1, 'team'),
    (6, '백엔드팀',        2, 'team'),
    (7, '프론트엔드팀',    2, 'team'),
    (8, '영업팀',          3, 'team'),
    (9, '고객성공팀',      3, 'team'),
]

# 헤드카운트 대장 — 상위 조직 정원에는 하위 합계가 들어 있다(Core 규칙).
#   경영지원실 5 = 인사팀 2 + 재무팀 2 + 실 자기 몫 1(대표)
#   개발실 8 = 백엔드팀 5 + 프론트엔드팀 3
#   사업실 4 = 영업팀 2 + 고객성공팀 2
HEADCOUNT = {1: 5, 2: 8, 3: 4, 4: 2, 5: 2, 6: 5, 7: 3, 8: 2, 9: 2}

# 조직장
LEADERS = {1: 1, 4: 2, 5: 4, 6: 6, 7: 11, 8: 14, 9: 16}

# ── 직급 ────────────────────────────────────────────────────
POSITIONS = [
    (1, 'L1 — Associate', 1), (2, 'L2 — Junior', 2), (3, 'L3 — Mid-Level', 3),
    (4, 'L4 — Senior', 4),    (5, 'L5 — Staff', 5),  (6, 'L6 — Manager', 6),
    (7, 'L7 — Senior Manager', 7), (8, 'L8 — Director', 8), (9, 'L9 — VP / Executive', 9),
]

# ── 직군 (코드는 Core 기본 직군 체계와 같게 — 연봉 밴드가 코드로 붙는다) ──
JOB_FAMILIES = [
    (1, 'SWE',   'Software Engineering', 'TECH',    1),
    (2, 'FE',    'Frontend Engineering', 'TECH',    2),
    (3, 'QA',    'Quality Assurance',    'TECH',    7),
    (4, 'PM',    'Product Management',   'PRODUCT', 1),
    (5, 'SALES', 'Sales',                'GTM',     1),
    (6, 'CS',    'Customer Success',     'GTM',     3),
    (7, 'FIN',   'Finance & Accounting', 'CORP',    1),
    (8, 'HR',    'Human Resources',      'PEOPLE',  1),
    (9, 'TA',    'Talent Acquisition',   'PEOPLE',  2),
]

# ── 사람 ────────────────────────────────────────────────────
# (id, 이름, 아이디, 부서, 직급level, 직군, 역할, 관리자, 입사일)
PEOPLE = [
    (1,  '한지우', 'jiwoo',  1, 9, None, 'manager',  None, '2019-03-04'),
    (2,  '정수민', 'sumin',  4, 6, 9,    'manager',     1, '2020-06-01'),
    (3,  ADMIN_NAME, 'hana', 4, 3, 8,    'admin',       2, '2025-03-03'),
    (4,  '오세영', 'seyoung', 5, 6, 7,   'manager',     1, '2020-09-01'),
    (5,  '윤도현', 'dohyun', 5, 3, 7,    'employee',    4, '2023-04-03'),
    (6,  '박지민', 'jimin',  6, 6, 1,    'manager',     1, '2019-11-01'),
    (7,  '강태오', 'taeo',   6, 4, 1,    'employee',    6, '2021-07-01'),
    (8,  '서지안', 'jian',   6, 3, 1,    'employee',    6, '2023-01-02'),
    (9,  '류하준', 'hajun',  6, 2, 1,    'employee',    6, '2025-01-02'),
    (10, '임세라', 'sera',   6, 4, 1,    'employee',    6, '2021-03-02'),   # 퇴사자
    (11, '최윤',   'yun',    7, 6, 2,    'manager',     1, '2020-02-03'),
    (12, '노아름', 'areum',  7, 3, 2,    'employee',   11, '2022-08-01'),
    (13, '배준영', 'junyng', 7, 2, 2,    'employee',   11, '2024-09-02'),
    (14, '김세진', 'sejin',  8, 6, 5,    'manager',     1, '2020-04-01'),
    (15, '문가온', 'gaon',   8, 3, 5,    'employee',   14, '2023-06-01'),
    (16, '홍유진', 'yujin',  9, 6, 6,    'manager',     1, '2021-02-01'),
    (17, '장태희', 'taehee', 9, 3, 6,    'employee',   16, '2024-03-04'),
]

LEAVER_ID     = 10          # 임세라 — 미션 1의 결원 충원 대상
LEAVER_BEFORE = 21          # 며칠 전에 퇴사했는가

FEATURES = ('attendance,payroll,performance,peer_review,calibration,'
            'recruiting,announcements,org_chart,certificates')


def _unusable_password():
    """비밀번호로는 못 들어오게 — 아무도 모르는 값으로 잠근다."""
    import secrets
    return generate_password_hash(secrets.token_urlsafe(32))


def seed_training_company(db_path: str):
    """연습 회사 데이터를 처음 상태로 채운다. 이미 있던 내용은 지운다."""
    init_db(db_path=db_path)          # 스키마 + 기본 시드

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    c = db.cursor()

    # 기본 시드로 들어온 회사(김철수·이영희…)를 치우고 연습 회사로 갈아 끼운다
    for t in ('announcements', 'users', 'departments', 'positions', 'job_families'):
        c.execute(f'DELETE FROM {t}')
    c.execute("DELETE FROM sqlite_sequence WHERE name IN "
              "('announcements','users','departments','positions','job_families')")

    for pid, name, level in POSITIONS:
        c.execute('INSERT INTO positions (id, name, level) VALUES (?,?,?)', (pid, name, level))

    for jid, code, name, gcode, sort in JOB_FAMILIES:
        grp = c.execute('SELECT id FROM job_family_groups WHERE code=?', (gcode,)).fetchone()
        c.execute('INSERT INTO job_families (id, code, name, group_id, sort_order) VALUES (?,?,?,?,?)',
                  (jid, code, name, grp['id'] if grp else None, sort))

    for did, name, parent, dtype in DEPARTMENTS:
        c.execute('INSERT INTO departments (id, name, parent_id, dept_type) VALUES (?,?,?,?)',
                  (did, name, parent, dtype))

    pw = _unusable_password()
    for uid, name, handle, dept, level, jf, role, mgr, hired in PEOPLE:
        c.execute(
            'INSERT INTO users (id, email, password_hash, name, role, department_id, '
            ' position_id, job_family_id, hire_date, status, emp_no, manager_id, '
            ' employment_type, onboarded, features_enabled, tour_completed) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uid, f'{handle}@saebom.example', pw, name, role, dept, level, jf, hired,
             'active', 'SB-%04d' % uid, mgr, 'full_time', 1, FEATURES, 1))

    # 퇴사자 — 미션 1에서 "이 사람 자리를 다시 채우는" 결원 충원 대상이 된다
    left_on = (date.today() - timedelta(days=LEAVER_BEFORE)).isoformat()
    c.execute("UPDATE users SET status='resigned', termination_date=?, termination_reason=? "
              'WHERE id=?', (left_on, '개인 사정', LEAVER_ID))

    for did, leader in LEADERS.items():
        c.execute('UPDATE departments SET leader_id=? WHERE id=?', (leader, did))

    year = date.today().year
    c.execute('DELETE FROM department_headcount')
    for did, target in HEADCOUNT.items():
        c.execute('INSERT INTO department_headcount (department_id, target_count, fiscal_year) '
                  'VALUES (?,?,?)', (did, target, year))

    for key, value in [
        ('name', COMPANY_NAME), ('name_en', 'Saebom Tech'), ('ceo', '한지우'),
        ('industry', '소프트웨어 개발'), ('founded', '2019-03-04'),
        ('employee_count', str(len(PEOPLE) - 1)),
        ('intro', '연습용 회사입니다. 여기서 한 일은 실제 회사 데이터에 영향을 주지 않습니다.'),
    ]:
        c.execute('INSERT INTO company_settings (key, value) VALUES (?,?) '
                  'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    c.execute('UPDATE company_config SET setup_completed=1 WHERE id=1')

    db.commit()
    db.close()

    # 연봉 밴드는 직군이 있어야 깔린다 — 직군을 넣은 뒤 한 번 더 돌린다
    init_db(db_path=db_path)

    db = sqlite3.connect(db_path)
    db.execute("DELETE FROM users WHERE role='guest'")   # 기본 시드가 되살린 손님 계정
    db.commit()
    db.close()


def ensure_training_tenant() -> int:
    """연습 테넌트를 찾거나 만든다. tenant_id 반환."""
    migrate_subscriptions()          # is_training 칸·입장표 표 보장
    row = get_training_tenant()
    if row:
        tenant_id = row['id']
    else:
        tenant_id = create_tenant(COMPANY_NAME + ' (연습)', ADMIN_EMAIL)
    mark_training_tenant(tenant_id)
    get_or_create_api_token(tenant_id)
    register_tenant_user(ADMIN_EMAIL, tenant_id)
    return tenant_id


def reset_training_company(expect_tenant_id: int = None) -> int:
    """연습 회사를 처음 상태로. DB 파일을 지우고 다시 만든다."""
    tenant_id = ensure_training_tenant()
    if expect_tenant_id and expect_tenant_id != tenant_id:
        raise RuntimeError('연습 테넌트가 아닙니다.')
    if tenant_id == 1:
        raise RuntimeError('연습 테넌트가 데모 테넌트(1)일 수 없습니다.')
    path = get_tenant_db_path(tenant_id)
    if os.path.exists(path):
        os.remove(path)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    seed_training_company(path)
    return tenant_id


def training_admin(db_path: str):
    """연습 회사의 관리자(학습자가 될 사람) 한 줄."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    row = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id  =p.id '
        "WHERE u.role='admin' AND u.status='active' ORDER BY u.id LIMIT 1").fetchone()
    db.close()
    return row


def _status():
    row = get_training_tenant()
    if not row:
        print('연습 회사 없음 — python training_company.py --reset 로 만듭니다.')
        return
    path = get_tenant_db_path(row['id'])
    print(f"연습 테넌트 {row['id']} · {row['company_name']} · DB {path}")
    if not os.path.exists(path):
        print('  DB 파일이 아직 없습니다.')
        return
    db = sqlite3.connect(path)
    n = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    left = db.execute("SELECT COUNT(*) FROM users WHERE status='resigned'").fetchone()[0]
    reqs = db.execute('SELECT COUNT(*) FROM job_requisitions').fetchone()[0]
    db.close()
    print(f'  재직 {n}명 · 퇴사 {left}명 · 채용 요청서 {reqs}건')


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else '--status'
    if arg == '--reset':
        tid = reset_training_company()
        print(f'연습 회사를 처음 상태로 되돌렸습니다 — 테넌트 {tid}')
        _status()
    else:
        _status()
