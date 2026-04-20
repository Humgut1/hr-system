#!/usr/bin/env python3
"""
한 번만 실행하는 DB 마이그레이션 스크립트.
- 부서 계층 재구성 (부문 → 본부 → 실 → 팀)
- 직급 체계 재구성 (CL1~CL9, 아마존/쿠팡 참고)
- 직군 테이블 생성 (Workday Job Architecture 참고)
- 직군 × 직급 연봉 기준표 생성
- users 테이블에 birth_date, job_family_id 컬럼 추가
- 직원 100명 시드
"""

import sqlite3
import random
from datetime import date, timedelta
from werkzeug.security import generate_password_hash
from database import init_db

DB  = 'hr_system.db'
PW  = 'changeme!'
random.seed(42)

# ── 이름 풀 ─────────────────────────────────────────────────
SURNAMES = [
    '김','이','박','최','정','강','조','윤','장','임',
    '한','오','서','신','권','황','안','송','류','전',
    '홍','고','문','양','손','배','백','허','유','남',
]
GIVEN = [
    '민준','서준','도윤','예준','시우','하준','주원','지후','준서','준우',
    '현우','도현','지훈','건우','우진','선우','서진','민재','현준','연우',
    '유준','정우','승우','승현','시현','준혁','진우','지원','재원','한결',
    '지유','서연','서윤','지민','수아','수빈','하은','채원','윤서','지아',
    '서현','민서','하린','예원','시은','나은','다은','소율','예은','채은',
    '태양','정환','경민','성호','태민','재현','민혁','동현','상민','규현',
    '재민','성민','태준','재준','민성','재영','성준','동준','재혁','민규',
    '은서','지은','수연','혜원','보미','소미','지연','미래','예린','수진',
    '민정','지현','혜진','유진','세은','아름','가은','다인','소진','혜윤',
    '철수','영희','준기','승호','태현','재성','윤호','성환','지성','보현',
]

def gen_names(n: int) -> list[str]:
    seen, result = set(), []
    attempts = 0
    while len(result) < n and attempts < 10000:
        name = random.choice(SURNAMES) + random.choice(GIVEN)
        if name not in seen:
            seen.add(name); result.append(name)
        attempts += 1
    return result

def rand_date(start_year: int, end_year: int) -> date:
    s = date(start_year, 1, 1)
    e = date(min(end_year, date.today().year), 12, 31)
    if s > e: s = e
    return s + timedelta(days=random.randint(0, max(0, (e - s).days)))

# ── 레벨별 분배 ──────────────────────────────────────────────
def make_dist(total: int, levels_weights: list[tuple]) -> list[int]:
    """[(cl_level, weight), ...] → 정확히 total개의 CL 레벨 리스트"""
    total_w = sum(w for _, w in levels_weights)
    dist, allocated = [], 0
    for i, (cl, w) in enumerate(levels_weights):
        if i == len(levels_weights) - 1:
            n = total - allocated
        else:
            n = round(total * w / total_w)
            n = max(0, min(n, total - allocated - (len(levels_weights) - i - 1)))
        dist.extend([cl] * n)
        allocated += n
    random.shuffle(dist)
    return dist

def ic_dist(total: int) -> list[int]:
    """일반 IC 팀 분포"""
    if total >= 8:
        return make_dist(total, [(2,25),(3,40),(4,20),(5,10),(6,5)])
    elif total >= 5:
        return make_dist(total, [(2,20),(3,40),(4,30),(5,10)])
    else:
        return make_dist(total, [(2,30),(3,40),(4,30)])

def small_dist(total: int) -> list[int]:
    return make_dist(total, [(3,40),(4,40),(5,20)])


# ── 연봉 기준표 (단위: 만원, annual) ─────────────────────────
# Amazon L3~L10 / Coupang CL1~CL7 / Workday Grade 참고
# 한국 테크 스타트업 시장 시세 기준
SALARY_TABLE: dict[str, list[int]] = {
    #          CL1   CL2   CL3   CL4    CL5    CL6    CL7    CL8    CL9
    'SWE':   [4000, 5500, 7000, 9000, 11500, 14000, 17000, 22000, 30000],
    'DATA':  [4000, 5800, 7500, 9500, 12000, 15000, 18000, 24000, 30000],
    'PM':    [4000, 5500, 7000, 9000, 11000, 14000, 17000, 22000, 28000],
    'INFRA': [4000, 5500, 7000, 9000, 11500, 14000, 17000, 22000, 28000],
    'DESIGN':[3800, 5000, 6500, 8000, 10000, 13000, 16000, 20000,     0],
    'MKT':   [3500, 4800, 6000, 7500,  9500, 12000, 15000, 19000,     0],
    'SALES': [3500, 5000, 6500, 8000, 10000, 13000, 16000, 20000,     0],
    'OPS':   [3000, 4000, 5200, 6500,  8000, 10000, 13000, 17000,     0],
    'FIN':   [3500, 4800, 6200, 7800,  9800, 12500, 15500, 19500,     0],
    'HR':    [3500, 4600, 6000, 7500,  9500, 12000, 15000,     0,     0],
    'LEGAL': [4500, 6000, 8000,10500, 13500, 17000, 21000,     0,     0],
    'STRAT': [4000, 5500, 7000, 8800, 11000, 14000, 17000, 22000,     0],
}

def monthly_base(jf_code: str, cl: int) -> int:
    salaries = SALARY_TABLE.get(jf_code, [3600]*9)
    ann = salaries[cl - 1] if cl <= len(salaries) else 0
    if ann == 0:
        for lv in range(cl - 2, -1, -1):
            if salaries[lv] > 0:
                ann = salaries[lv]; break
    if ann == 0:
        ann = 3600
    return (ann * 10000) // 12


def run():
    init_db()   # 테이블이 없을 때도 안전하게 실행되도록
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # 이미 실행된 경우 스킵 (idempotent)
    if c.execute('SELECT COUNT(*) FROM job_families').fetchone()[0] > 0:
        conn.close()
        return
    c.execute('PRAGMA foreign_keys = OFF')

    # ── 새 테이블 생성 ─────────────────────────────────────────
    c.executescript('''
        CREATE TABLE IF NOT EXISTS job_families (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS salary_grades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_family_id INTEGER NOT NULL REFERENCES job_families(id),
            position_id   INTEGER NOT NULL REFERENCES positions(id),
            annual_salary INTEGER NOT NULL,
            UNIQUE(job_family_id, position_id)
        );
    ''')

    existing_cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    if 'birth_date' not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN birth_date DATE")
    if 'job_family_id' not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN job_family_id INTEGER REFERENCES job_families(id)")

    # ── 기존 데이터 초기화 ─────────────────────────────────────
    for tbl in [
        'employee_salary','payslips',
        'performance_reviews','performance_goals','performance_cycles',
        'leave_requests','applicant_logs','applicants','job_postings',
        'announcements','users',
        'departments','positions',
        'salary_grades','job_families',
    ]:
        c.execute(f"DELETE FROM {tbl}")
        c.execute(f"DELETE FROM sqlite_sequence WHERE name='{tbl}'")

    # ── 부서 삽입 (부문 → 본부 → 실 → 팀) ─────────────────────
    def dept(name, parent=None):
        c.execute("INSERT INTO departments (name, parent_id) VALUES (?,?)", (name, parent))
        return c.lastrowid

    # 부문
    D_TECH  = dept('테크 부문')
    D_BIZ   = dept('비즈니스 부문')
    D_OPS   = dept('운영 부문')
    D_CORP  = dept('경영지원 부문')

    # 본부
    D_PROD_HQ    = dept('프로덕트 본부',    D_TECH)
    D_ENG_HQ     = dept('엔지니어링 본부',  D_TECH)
    D_SALES_HQ   = dept('영업 본부',        D_BIZ)
    D_MKT_HQ     = dept('마케팅 본부',      D_BIZ)
    D_CX_HQ      = dept('고객경험 본부',    D_OPS)
    D_SVCOPS_HQ  = dept('서비스운영 본부',  D_OPS)
    D_FIN_HQ     = dept('재무 본부',        D_CORP)
    D_HR_HQ      = dept('인사 본부',        D_CORP)

    # 실
    D_PM_DIV     = dept('프로덕트기획실',       D_PROD_HQ)
    D_PLAT_DIV   = dept('플랫폼개발실',         D_ENG_HQ)
    D_DATA_DIV   = dept('데이터개발실',         D_ENG_HQ)
    D_INFRA_DIV  = dept('인프라실',             D_ENG_HQ)
    D_SALES_DIV  = dept('국내영업실',           D_SALES_HQ)
    D_GLOBAL_DIV = dept('해외사업실',           D_SALES_HQ)
    D_MKT_DIV    = dept('마케팅실',             D_MKT_HQ)
    D_CS_DIV     = dept('CS실',                 D_CX_HQ)
    D_SVCOPS_DIV = dept('운영실',               D_SVCOPS_HQ)
    D_FIN_DIV    = dept('재무실',               D_FIN_HQ)
    D_HR_DIV     = dept('HR실',                 D_HR_HQ)
    D_LEGAL_DIV  = dept('법무·컴플라이언스실',  D_CORP)
    D_STRAT_DIV  = dept('경영전략실',           D_CORP)

    # 팀
    D_PM_TEAM      = dept('서비스기획팀',       D_PM_DIV)
    D_UX_TEAM      = dept('UX팀',               D_PM_DIV)
    D_BE_TEAM      = dept('백엔드팀',           D_PLAT_DIV)
    D_FE_TEAM      = dept('프론트엔드팀',       D_PLAT_DIV)
    D_DE_TEAM      = dept('데이터엔지니어링팀', D_DATA_DIV)
    D_ML_TEAM      = dept('ML/AI팀',            D_DATA_DIV)
    D_DEVOPS_TEAM  = dept('DevOps팀',           D_INFRA_DIV)
    D_SEC_TEAM     = dept('보안팀',             D_INFRA_DIV)
    D_B2B_TEAM     = dept('B2B영업팀',          D_SALES_DIV)
    D_PARTNER_TEAM = dept('파트너십팀',         D_SALES_DIV)
    D_GLOBAL_TEAM  = dept('글로벌영업팀',       D_GLOBAL_DIV)
    D_BRAND_TEAM   = dept('브랜드마케팅팀',     D_MKT_DIV)
    D_PERF_TEAM    = dept('퍼포먼스마케팅팀',   D_MKT_DIV)
    D_CONTENT_TEAM = dept('콘텐츠팀',           D_MKT_DIV)
    D_CS_TEAM      = dept('고객지원팀',         D_CS_DIV)
    D_QA_TEAM      = dept('품질관리팀',         D_CS_DIV)
    D_SVCOPS_TEAM  = dept('서비스운영팀',       D_SVCOPS_DIV)
    D_FINPLAN_TEAM = dept('재무기획팀',         D_FIN_DIV)
    D_ACCT_TEAM    = dept('회계팀',             D_FIN_DIV)
    D_HR_TEAM      = dept('인사팀',             D_HR_DIV)
    D_RECRUIT_TEAM = dept('채용팀',             D_HR_DIV)
    D_LEGAL_TEAM   = dept('법무팀',             D_LEGAL_DIV)
    D_STRAT_TEAM   = dept('전략기획팀',         D_STRAT_DIV)

    # ── 직급 삽입 (CL1~CL9) ────────────────────────────────────
    def pos(name, level):
        c.execute("INSERT INTO positions (name, level) VALUES (?,?)", (name, level))
        return c.lastrowid

    P = {
        1: pos('CL1 · 어소시에이트', 1),
        2: pos('CL2 · 주니어',       2),
        3: pos('CL3 · 미드레벨',     3),
        4: pos('CL4 · 시니어',       4),
        5: pos('CL5 · 스태프',       5),
        6: pos('CL6 · 매니저',       6),
        7: pos('CL7 · 시니어 매니저',7),
        8: pos('CL8 · 디렉터',       8),
        9: pos('CL9 · VP/임원',      9),
    }

    # ── 직군 삽입 ──────────────────────────────────────────────
    jf_list = [
        ('SWE',    '소프트웨어 엔지니어링'),
        ('DATA',   '데이터/ML 엔지니어링'),
        ('PM',     '프로덕트 매니지먼트'),
        ('INFRA',  '인프라/DevOps'),
        ('DESIGN', '디자인/UX'),
        ('MKT',    '마케팅'),
        ('SALES',  '영업/사업개발'),
        ('OPS',    '운영/CS'),
        ('FIN',    '재무/회계'),
        ('HR',     '인사/HR'),
        ('LEGAL',  '법무'),
        ('STRAT',  '경영전략'),
    ]
    JF = {}
    for code, name in jf_list:
        c.execute("INSERT INTO job_families (code, name) VALUES (?,?)", (code, name))
        JF[code] = c.lastrowid

    # ── 연봉 기준표 삽입 ──────────────────────────────────────
    for code, salaries in SALARY_TABLE.items():
        fid = JF[code]
        for i, ann_만원 in enumerate(salaries):
            if ann_만원 > 0:
                c.execute(
                    "INSERT INTO salary_grades (job_family_id, position_id, annual_salary) VALUES (?,?,?)",
                    (fid, P[i + 1], ann_만원 * 10000)
                )

    # ── 직원 100명 생성 ────────────────────────────────────────
    # 팀 설정: (dept_id, jf_code, count, dist_fn, base_role)
    TEAMS = [
        (D_BE_TEAM,      'SWE',   11, ic_dist,   'employee'),
        (D_FE_TEAM,      'SWE',    8, ic_dist,   'employee'),
        (D_DE_TEAM,      'DATA',   6, ic_dist,   'employee'),
        (D_ML_TEAM,      'DATA',   4, small_dist,'employee'),
        (D_DEVOPS_TEAM,  'INFRA',  4, ic_dist,   'employee'),
        (D_SEC_TEAM,     'INFRA',  2, small_dist,'employee'),
        (D_PM_TEAM,      'PM',     6, ic_dist,   'employee'),
        (D_UX_TEAM,      'DESIGN', 4, ic_dist,   'employee'),
        (D_B2B_TEAM,     'SALES',  8, ic_dist,   'employee'),
        (D_PARTNER_TEAM, 'SALES',  4, small_dist,'employee'),
        (D_GLOBAL_TEAM,  'SALES',  4, small_dist,'employee'),
        (D_BRAND_TEAM,   'MKT',    4, ic_dist,   'employee'),
        (D_PERF_TEAM,    'MKT',    4, ic_dist,   'employee'),
        (D_CONTENT_TEAM, 'MKT',    2, small_dist,'employee'),
        (D_CS_TEAM,      'OPS',    6, ic_dist,   'employee'),
        (D_QA_TEAM,      'OPS',    3, small_dist,'employee'),
        (D_SVCOPS_TEAM,  'OPS',    4, ic_dist,   'employee'),
        (D_FINPLAN_TEAM, 'FIN',    3, small_dist,'employee'),
        (D_ACCT_TEAM,    'FIN',    3, small_dist,'employee'),
        (D_HR_TEAM,      'HR',     4, ic_dist,   'employee'),   # i=0 → admin
        (D_RECRUIT_TEAM, 'HR',     2, small_dist,'recruiter'),  # i=0 → recruiter
        (D_LEGAL_TEAM,   'LEGAL',  2, small_dist,'employee'),
        (D_STRAT_TEAM,   'STRAT',  2, small_dist,'employee'),
    ]
    # 합계 확인: 11+8+6+4+4+2+6+4+8+4+4+4+4+2+6+3+4+3+3+4+2+2+2 = 100

    ALL_FEATURES = ('attendance,payroll,performance,peer_review,calibration,'
                    'recruiting,announcements,org_chart,certificates')

    total_count = sum(t[2] for t in TEAMS)
    all_names   = gen_names(total_count)
    name_idx    = 0
    pw          = generate_password_hash(PW)

    inserted_ids      = []
    salary_rows       = []

    for dept_id, jf_code, count, dist_fn, base_role in TEAMS:
        cl_levels = dist_fn(count)
        jf_id     = JF[jf_code]

        for i in range(count):
            cl   = cl_levels[i]
            name = all_names[name_idx]; name_idx += 1
            role = base_role

            # 특수 계정 처리
            if dept_id == D_HR_TEAM and i == 0:
                email = 'admin@company.com';     name = 'HR 관리자'; role = 'admin'
            elif dept_id == D_HR_TEAM and i == count - 1:
                email = 'manager@company.com';   name = '김팀장';    role = 'manager'
            elif dept_id == D_BE_TEAM and i == 0:
                email = 'employee@company.com';  name = '이직원'
            elif dept_id == D_RECRUIT_TEAM and i == 0:
                email = 'recruiter@company.com'; name = '박채용'
            else:
                email = f"emp{name_idx:03d}@company.com"

            # 생년월일 (레벨이 높을수록 나이 많음)
            min_age = 22 + cl * 2
            max_age = min(54, min_age + 10)
            birth   = rand_date(date.today().year - max_age, date.today().year - min_age)

            # 입사일 (레벨이 높을수록 입사 일찍)
            max_yrs = min(12, cl * 2)
            min_yrs = max(0, cl - 3)
            hire_s  = date.today() - timedelta(days=max_yrs * 365)
            hire_e  = date.today() - timedelta(days=min_yrs * 365)
            hire    = hire_s + timedelta(
                days=random.randint(0, max(0, (hire_e - hire_s).days))
            )

            phone = f"010-{random.randint(1000,9999)}-{random.randint(1000,9999)}"

            c.execute(
                "INSERT INTO users "
                "(email, password_hash, name, role, department_id, position_id, "
                " phone, hire_date, birth_date, job_family_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (email, pw, name, role, dept_id, P[cl],
                 phone, hire.isoformat(), birth.isoformat(), jf_id)
            )
            uid = c.lastrowid
            inserted_ids.append(uid)

            base = monthly_base(jf_code, cl)
            salary_rows.append((uid, base, 200000, 100000))

    # ── 사번 일괄 생성 ────────────────────────────────────────
    c.execute("UPDATE users SET emp_no = 'TC-' || printf('%05d', id) WHERE emp_no IS NULL")

    # ── 급여 정보 삽입 ─────────────────────────────────────────
    for uid, base, meal, trans in salary_rows:
        c.execute(
            "INSERT INTO employee_salary "
            "(user_id, base_salary, meal_allowance, transport_allowance) "
            "VALUES (?,?,?,?)",
            (uid, base, meal, trans)
        )

    # ── 공지사항 (admin 계정으로) ──────────────────────────────
    admin_row = c.execute(
        "SELECT id FROM users WHERE email='admin@company.com'"
    ).fetchone()
    if admin_row:
        admin_id = admin_row[0]
        announcements = [
            ('2026년 상반기 성과평가 일정 안내',
             '2026년 상반기 성과평가가 5월 1일부터 시작됩니다.\n\n'
             '평가 기간: 2026.05.01 ~ 05.31\n'
             '평가 방법: TalentCore 시스템 내 성과 평가 메뉴에서 진행\n\n'
             '모든 팀장은 기간 내 팀원 평가를 완료해 주세요.', 1),
            ('재택근무 정책 업데이트',
             '2026년 4월부터 재택근무 정책이 아래와 같이 변경됩니다.\n\n'
             '- 주 2회 재택 가능 (화, 목 권장)\n'
             '- 재택 신청은 전주 금요일까지 시스템에서 신청\n'
             '- 팀장 승인 후 확정\n\n'
             '자세한 사항은 HR에 문의 바랍니다.', 0),
            ('사내 복지 포인트 지급 안내',
             '2026년 1분기 복지 포인트가 지급되었습니다.\n\n'
             '- 지급 금액: 인당 200,000원\n'
             '- 사용 기한: 2026.06.30\n'
             '- 사용처: 제휴 가맹점 및 온라인몰\n\n'
             '복지 포인트 관련 문의는 경영지원팀으로 연락 주세요.', 0),
        ]
        c.executemany(
            "INSERT INTO announcements (title, content, pinned, author_id) VALUES (?,?,?,?)",
            [(t, body, pin, admin_id) for t, body, pin in announcements]
        )

    conn.commit()
    c.execute('PRAGMA foreign_keys = ON')
    conn.close()

    print(f"✅ 마이그레이션 완료!")
    print(f"   직원: {len(inserted_ids)}명")
    print(f"   부서: 부문 4 / 본부 8 / 실 13 / 팀 23")
    print(f"   직급: CL1~CL9 (9단계)")
    print(f"   직군: {len(jf_list)}개")
    print(f"   연봉 기준: {sum(1 for s in SALARY_TABLE.values() for v in s if v>0)}개 등급")
    print(f"\n  로그인: admin@company.com / changeme!")


if __name__ == '__main__':
    run()
