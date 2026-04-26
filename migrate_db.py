#!/usr/bin/env python3
"""
한 번만 실행하는 DB 마이그레이션 스크립트.
- 부서 계층 재구성 (부문 → 본부 → 실 → 팀)
- 직급 체계 재구성 (CL1~CL9, 아마존/쿠팡 참고)
- 직군 테이블 생성 (Workday Job Architecture 참고)
- 직군 × 직급 연봉 기준표 생성
- users 테이블에 birth_date, job_family_id 컬럼 추가
- 직원 100명 시드
- 트랜잭셔널 시드 데이터 (급여명세/휴가/근태/성과/계약)
"""

import sqlite3
import random
import os
from datetime import date, timedelta, datetime
from werkzeug.security import generate_password_hash
from database import init_db
from payroll_utils import calc_payslip

_db_dir = os.environ.get('DB_DIR', '')
DB = os.path.join(_db_dir, 'hr_system.db') if _db_dir else 'hr_system.db'
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


def _seed_transactional(c, all_uids: list, admin_id: int):
    """
    직원 시드 완료 후 현실적인 트랜잭셔널 데이터를 삽입.
    - 급여명세서: 최근 6개월 (전체 직원)
    - 휴가신청: 40건 (다양한 상태)
    - 출퇴근 기록: 최근 45일 (무작위 직원 30명)
    - 공지사항: 7건 추가
    - 성과 주기 + 목표 + 리뷰
    - 전자계약: 5건
    """
    today = date.today()
    rng = random.Random(99)  # 별도 시드 — 기존 시드와 분리

    # ── 1. 급여명세서 (최근 6개월, 전체 직원) ────────────────────
    salary_map = {}
    for row in c.execute("SELECT user_id, base_salary, meal_allowance, transport_allowance FROM employee_salary"):
        salary_map[row[0]] = (row[1], row[2], row[3])

    months = []
    for delta in range(6, 0, -1):
        d = today.replace(day=1) - timedelta(days=delta * 28)
        months.append((d.year, d.month))

    for uid in all_uids:
        if uid not in salary_map:
            continue
        base, meal, transport = salary_map[uid]
        for yr, mo in months:
            ot = rng.choice([0, 0, 0, rng.randint(50000, 300000)])
            p = calc_payslip(base, meal, transport, ot)
            c.execute(
                """INSERT OR IGNORE INTO payslips
                   (user_id, year, month, base_salary, meal_allowance, transport_allowance,
                    overtime_pay, national_pension, health_insurance, long_term_care,
                    employment_insurance, income_tax, local_income_tax,
                    gross_pay, total_deduction, net_pay)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, yr, mo,
                 p['base_salary'], p['meal_allowance'], p['transport_allowance'],
                 p['overtime_pay'], p['national_pension'], p['health_insurance'],
                 p['long_term_care'], p['employment_insurance'],
                 p['income_tax'], p['local_income_tax'],
                 p['gross_pay'], p['total_deduction'], p['net_pay'])
            )

    # ── 2. 공지사항 7건 추가 ───────────────────────────────────
    extra_notices = [
        ('2026년 건강검진 대상자 안내',
         '2026년 직장 건강검진 대상자를 안내드립니다.\n\n'
         '- 대상: 짝수년도 출생 전 직원 전원\n'
         '- 기간: 2026.03.01 ~ 11.30\n'
         '- 방법: 가까운 검진 지정 의료기관 방문\n\n'
         '건강검진 미수검 시 과태료가 부과될 수 있으니 반드시 기간 내 수검 바랍니다.', 0),
        ('사무실 이전 안내 (4월 28일)',
         '안녕하세요. 당사 사무실이 아래와 같이 이전될 예정입니다.\n\n'
         '- 이전일: 2026년 4월 28일 (월)\n'
         '- 신주소: 서울시 강남구 테헤란로 123, 12층\n\n'
         '이전 당일은 오전 10시 이후 출근해 주시기 바랍니다.', 1),
        ('스톡옵션 베스팅 일정 공지',
         '2023년 부여된 스톡옵션 1차 베스팅이 도래하였습니다.\n\n'
         '- 베스팅 비율: 25% (1년 클리프)\n'
         '- 행사 가능 기간: 2026.05.01 ~ 2029.04.30\n\n'
         '세부 사항은 재무팀 담당자에게 문의하시기 바랍니다.', 0),
        ('5월 연휴 출퇴근 안내',
         '5월 황금연휴 기간 정상 근무 여부를 안내드립니다.\n\n'
         '- 5/1 (근로자의날): 유급휴일\n'
         '- 5/5 (어린이날): 법정공휴일\n'
         '- 5/6 (대체공휴일): 법정공휴일\n\n'
         '연휴 기간 시스템 긴급 대응 담당자는 별도 공지 예정입니다.', 1),
        ('2026 상반기 OKR 킥오프 워크샵',
         '상반기 OKR 수립을 위한 전사 워크샵을 아래와 같이 진행합니다.\n\n'
         '- 일시: 2026년 4월 30일 (목) 14:00~18:00\n'
         '- 장소: 3층 대강당\n'
         '- 대상: 팀장급 이상 전원 + 팀별 대표 1인\n\n'
         '참석 여부를 4월 25일까지 인사팀에 회신 부탁드립니다.', 0),
        ('사내 추천 채용 인센티브 프로그램 안내',
         '우수 인재 확보를 위한 사내 추천 채용 프로그램을 운영합니다.\n\n'
         '- 추천 성공 시 인센티브: 100만원 (입사 후 3개월 근속 조건)\n'
         '- 추천 방법: TalentCore > 채용 > 지원자 추천\n\n'
         '현재 채용 중인 포지션은 채용공고 페이지에서 확인하세요.', 0),
        ('정보보안 교육 이수 안내 (필수)',
         '연간 정보보안 의무 교육 이수를 완료해 주세요.\n\n'
         '- 대상: 전 임직원\n'
         '- 마감: 2026.04.30\n'
         '- 방법: 사내 LMS 시스템 (lms.company.com) 접속 후 "정보보안 기초" 과정 수강\n\n'
         '미이수 시 보안 규정에 따라 시스템 접근이 제한될 수 있습니다.', 1),
    ]
    c.executemany(
        "INSERT INTO announcements (title, content, pinned, author_id) VALUES (?,?,?,?)",
        [(t, body, pin, admin_id) for t, body, pin in extra_notices]
    )

    # ── 3. 휴가 신청 40건 ─────────────────────────────────────
    leave_types_common = ['annual', 'annual', 'annual', 'half_am', 'half_pm',
                          'sick', 'remote', 'remote', 'outing', 'bereavement']
    statuses = ['approved', 'approved', 'approved', 'pending', 'rejected']
    sample_uids = rng.sample(all_uids, min(40, len(all_uids)))

    for i, uid in enumerate(sample_uids):
        ltype = rng.choice(leave_types_common)
        # 신청일: 최근 90일 내
        days_ago = rng.randint(1, 90)
        req_date = today - timedelta(days=days_ago)
        if ltype in ('half_am', 'half_pm', 'outing', 'sick'):
            start = req_date
            end   = req_date
            days  = 1
        else:
            start = req_date
            end   = req_date + timedelta(days=rng.randint(0, 3))
            days  = (end - start).days + 1
        status = rng.choice(statuses)
        reason_map = {
            'annual': rng.choice(['개인 사유', '가족 행사', '여행', '병원 방문', '개인 용무']),
            'half_am': '오전 외출',
            'half_pm': '오후 개인 용무',
            'sick': '몸이 좋지 않아 요양',
            'remote': '재택근무 신청',
            'outing': '외근 업무',
            'bereavement': '가족 애도',
        }
        reason = reason_map.get(ltype, '개인 사유')
        c.execute(
            """INSERT INTO leave_requests
               (user_id, type, start_date, end_date, days, reason, status)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, ltype, start.isoformat(), end.isoformat(), days, reason, status)
        )

    # ── 4. 출퇴근 기록 (최근 45일, 30명) ─────────────────────
    checkin_uids = rng.sample(all_uids, min(30, len(all_uids)))
    for uid in checkin_uids:
        for day_offset in range(45, 0, -1):
            d = today - timedelta(days=day_offset)
            if d.weekday() >= 5:  # 주말 제외
                continue
            if rng.random() < 0.05:  # 5% 결근
                continue
            # 출근: 8:30~9:30 사이, 퇴근: 18:00~20:30 사이
            in_h = rng.randint(8, 9)
            in_m = rng.randint(0, 59) if in_h == 9 else rng.randint(30, 59)
            out_h = rng.randint(18, 20)
            out_m = rng.randint(0, 59)
            check_in  = f"{in_h:02d}:{in_m:02d}:00"
            check_out = f"{out_h:02d}:{out_m:02d}:00"
            # 근무 시간 계산 (분)
            total_min = (out_h * 60 + out_m) - (in_h * 60 + in_m) - 60  # 점심 1h 제외
            regular_min = min(total_min, 480)
            overtime_min = max(0, total_min - 480)
            c.execute(
                """INSERT OR IGNORE INTO checkins
                   (user_id, date, check_in, check_out, regular_min, overtime_min)
                   VALUES (?,?,?,?,?,?)""",
                (uid, d.isoformat(), check_in, check_out, regular_min, overtime_min)
            )

    # ── 5. 성과 주기 + 목표 + 리뷰 ──────────────────────────
    # 2025 하반기 (완료), 2026 상반기 (진행중)
    c.execute(
        "INSERT OR IGNORE INTO performance_cycles (name, start_date, end_date, status) VALUES (?,?,?,?)",
        ('2025 하반기', '2025-07-01', '2025-12-31', 'closed')
    )
    cycle_2025h2 = c.lastrowid or c.execute(
        "SELECT id FROM performance_cycles WHERE name='2025 하반기'"
    ).fetchone()[0]

    c.execute(
        "INSERT OR IGNORE INTO performance_cycles (name, start_date, end_date, status) VALUES (?,?,?,?)",
        ('2026 상반기', '2026-01-01', '2026-06-30', 'active')
    )
    cycle_2026h1 = c.lastrowid or c.execute(
        "SELECT id FROM performance_cycles WHERE name='2026 상반기'"
    ).fetchone()[0]

    goal_templates = [
        ('KPI', '핵심 지표 달성',     '분기 KPI 목표를 달성하여 팀 성과에 기여한다.', 40),
        ('KPI', '프로젝트 일정 준수', '할당된 프로젝트를 계획 대비 지연 없이 완료한다.', 30),
        ('OKR', '역량 개발 및 성장',  '연간 학습 목표를 이수하고 스킬을 강화한다.', 20),
        ('OKR', '협업 및 팀 기여',    '팀 내 지식 공유와 협업 문화에 적극 기여한다.', 10),
    ]
    scores_closed = [3, 3, 4, 4, 4, 5, 5, 2, 3, 4]
    goal_uids = rng.sample(all_uids, min(50, len(all_uids)))

    manager_id = c.execute(
        "SELECT id FROM users WHERE email='manager@company.com'"
    ).fetchone()
    manager_id = manager_id[0] if manager_id else admin_id

    for uid in goal_uids:
        for cat, title, desc, weight in goal_templates:
            # 2025 H2 (완료된 주기)
            progress_25 = rng.randint(70, 100)
            score_25    = rng.choice(scores_closed)
            self_score  = max(1, min(5, score_25 + rng.randint(-1, 1)))
            c.execute(
                """INSERT OR IGNORE INTO performance_goals
                   (cycle_id, user_id, category, title, description, weight,
                    progress, self_score, self_comment, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (cycle_2025h2, uid, cat, title, desc, weight,
                 progress_25, self_score, '목표 달성을 위해 최선을 다했습니다.', 'completed')
            )
            goal_id = c.lastrowid
            if goal_id:
                c.execute(
                    "INSERT OR IGNORE INTO performance_reviews (goal_id, reviewer_id, score, comment) VALUES (?,?,?,?)",
                    (goal_id, manager_id,
                     score_25, rng.choice(['우수한 성과를 보였습니다.', '목표를 충실히 이행했습니다.',
                                           '기대 이상의 결과물을 제출했습니다.', '개선의 여지가 있습니다.']))
                )

            # 2026 H1 (현재 진행중)
            progress_26 = rng.randint(0, 80)
            c.execute(
                """INSERT OR IGNORE INTO performance_goals
                   (cycle_id, user_id, category, title, description, weight,
                    progress, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (cycle_2026h1, uid, cat, title, desc, weight, progress_26, 'active')
            )

    # ── 6. 전자계약 5건 (계약서 + 서명 완료) ─────────────────
    sample_employees = rng.sample(
        [uid for uid in all_uids if uid != admin_id],
        min(5, len(all_uids) - 1)
    )
    for emp_id in sample_employees:
        emp_row = c.execute(
            "SELECT u.name, u.hire_date, es.base_salary, d.name "
            "FROM users u "
            "LEFT JOIN departments d ON d.id=u.department_id "
            "LEFT JOIN employee_salary es ON es.user_id=u.id "
            "WHERE u.id=?", (emp_id,)
        ).fetchone()
        if not emp_row:
            continue
        emp_name, hire_date, base_sal, dept_name = emp_row
        salary_fmt = f"{base_sal:,}원" if base_sal else "협의"
        content = (
            f"<div style='font-family:serif;padding:40px'>"
            f"<h2 style='text-align:center'>근로계약서</h2>"
            f"<p><strong>회사:</strong> (주)탤런트코어 (이하 '갑')</p>"
            f"<p><strong>근로자:</strong> {emp_name} (이하 '을')</p>"
            f"<p><strong>부서:</strong> {dept_name or '미정'}</p>"
            f"<p><strong>입사일:</strong> {hire_date}</p>"
            f"<p><strong>월 기본급:</strong> {salary_fmt}</p>"
            f"<p>갑과 을은 근로기준법 등 관련 법령을 준수하여 아래와 같이 근로계약을 체결한다.</p>"
            f"<p>제1조 (근로계약기간) 계약기간은 입사일로부터 정함이 없는 근로계약으로 한다.</p>"
            f"<p>제2조 (근무장소) 회사 지정 사업장 및 원격근무.</p>"
            f"<p>제3조 (업무내용) 담당 직무 및 회사가 지시하는 업무.</p>"
            f"<p>제4조 (근무시간) 1일 8시간, 주 40시간을 원칙으로 한다.</p>"
            f"<p>제5조 (임금) 월 기본급 {salary_fmt} (4대보험 및 세금 별도 공제).</p>"
            f"</div>"
        )
        issued_date = (today - timedelta(days=rng.randint(30, 180))).isoformat()
        c.execute(
            """INSERT INTO contracts
               (employee_id, issued_by, title, content_html, status, signed_at, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (emp_id, admin_id,
             f"{emp_name} 근로계약서",
             content, 'signed',
             issued_date + ' 10:00:00',
             issued_date + ' 09:00:00')
        )

    print(f"   급여명세서: {len(all_uids) * len(months)}건 (직원 {len(all_uids)}명 × {len(months)}개월)")
    print(f"   공지사항: +{len(extra_notices)}건")
    print(f"   휴가신청: {len(sample_uids)}건")
    print(f"   출퇴근: {len(checkin_uids)}명 × ~38일")
    print(f"   성과목표: {len(goal_uids)}명 × {len(goal_templates)}개 × 2주기")
    print(f"   전자계약: {len(sample_employees)}건")

    # ── 7. 채용 공고 + 지원자 + 파이프라인 ──────────────────────
    recruiter_row = c.execute(
        "SELECT id FROM users WHERE email='recruiter@company.com'"
    ).fetchone()
    recruiter_id = recruiter_row[0] if recruiter_row else admin_id

    # 공고 정의: (title, dept_name, position_level, desc_summary, status, deadline_offset_days)
    POSTINGS = [
        ('백엔드 엔지니어 (Python/Django)',
         '백엔드팀', 3,
         'Python/Django 기반 서버 개발 경력 3년 이상. MSA 경험 우대.\n'
         '주요 업무: API 설계·개발, DB 최적화, 코드 리뷰.',
         'open', 30),
        ('프론트엔드 엔지니어 (React)',
         '프론트엔드팀', 3,
         'React/TypeScript 3년 이상. 디자인 시스템 구축 경험 우대.\n'
         '주요 업무: 웹 서비스 UI 개발, 성능 최적화.',
         'open', 25),
        ('ML 엔지니어 (추천/검색)',
         'ML/AI팀', 4,
         '추천 시스템 또는 검색 엔진 개발 경험 4년 이상. PyTorch 필수.\n'
         '주요 업무: 추천 모델 개발 및 A/B 테스트 운영.',
         'open', 45),
        ('데이터 엔지니어',
         '데이터엔지니어링팀', 3,
         'Airflow, Spark, dbt 중 2개 이상 실무 경험.\n'
         '주요 업무: 데이터 파이프라인 구축 및 데이터 마트 관리.',
         'open', 20),
        ('시니어 PM (B2B SaaS)',
         '서비스기획팀', 4,
         'B2B SaaS 프로덕트 기획 5년 이상. 사용자 인터뷰, 데이터 기반 의사결정 경험.\n'
         '주요 업무: 로드맵 수립, 고객 문제 정의, 스쿼드 리딩.',
         'open', 35),
        ('DevOps / SRE',
         'DevOps팀', 3,
         'Kubernetes, Terraform, AWS 실무 경험 3년 이상.\n'
         '주요 업무: 클라우드 인프라 운영, CI/CD 파이프라인 관리.',
         'open', 30),
        ('브랜드 마케터',
         '브랜드마케팅팀', 2,
         'SNS 채널 운영 및 콘텐츠 기획 2년 이상.\n'
         '주요 업무: 브랜드 캠페인 기획, 인플루언서 협업.',
         'open', 15),
        ('B2B 영업 (Enterprise)',
         'B2B영업팀', 3,
         '엔터프라이즈 SaaS 영업 경력 3년 이상.\n'
         '주요 업무: 신규 고객 발굴, 계약 협상, 고객 관계 관리.',
         'closed', -10),
        ('UX 디자이너',
         'UX팀', 3,
         'Figma 능숙, 사용자 리서치 및 프로토타이핑 경험 3년 이상.\n'
         '주요 업무: 서비스 UX 설계, 디자인 시스템 운영.',
         'closed', -5),
        ('채용 담당자 (IT/테크 전문)',
         '채용팀', 2,
         'IT/테크 직군 채용 경력 2년 이상. 헤드헌팅 경험 우대.\n'
         '주요 업무: JD 작성, 소싱, 인터뷰 조율.',
         'open', 60),
    ]

    posting_ids = []
    for title, dept_name, pos_level, desc, status, deadline_offset in POSTINGS:
        dept_row = c.execute(
            "SELECT id FROM departments WHERE name=?", (dept_name,)
        ).fetchone()
        pos_row = c.execute(
            "SELECT id FROM positions WHERE level=?", (pos_level,)
        ).fetchone()
        dept_id_p = dept_row[0] if dept_row else None
        pos_id_p  = pos_row[0] if pos_row else None
        deadline  = (today + timedelta(days=deadline_offset)).isoformat() if deadline_offset > 0 else (today + timedelta(days=deadline_offset)).isoformat()
        c.execute(
            """INSERT INTO job_postings
               (title, department_id, position_id, description, requirements, status, deadline, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (title, dept_id_p, pos_id_p, desc,
             '이력서 + 포트폴리오(해당자) 제출. 서류 검토 후 개별 연락.',
             status, deadline, recruiter_id)
        )
        posting_ids.append(c.lastrowid)

    # 지원자 이름 풀
    APPLICANT_NAMES = [
        '강민호','윤지수','정다은','이승준','박소연','김재현','최예림','조민수',
        '한지영','신동현','임채원','오지훈','서유진','권민정','황성호','안예진',
        '송재원','류민수','전지은','홍성준','고은서','문준혁','양미래','손동우',
        '배지현','백승현','허민준','유채린','남정호','박현수','이다인','김성민',
        '정유진','최재훈','윤수빈','조현준','한민지','신예원','임도현','오서연',
        '강준혁','이민채','박재영','김하은','정성준','최도윤','윤예진','조성현',
        '한재민','신수진',
    ]
    SOURCES = ['direct', 'direct', 'linkedin', 'linkedin', 'referral', 'wanted', 'jumpit']
    RESUME_NOTES = [
        '경력기술서 충실, GitHub 포트폴리오 있음',
        '전 직장 대기업 경력. 이직 사유 명확',
        '스타트업 경험 다수. 주도적 업무 스타일',
        '기술 스택 일치율 높음. 코딩테스트 통과',
        '포트폴리오 퀄리티 우수. 팀 추천 지원자',
        '경력 3년, 기대 연봉 협의 가능',
        '오픈소스 기여 이력. 자기소개서 성의 있음',
        '전 직장 동료 추천. 인성 검증됨',
    ]

    # 스테이지별 분포: open 공고는 파이프라인 전반에 걸쳐, closed는 hired/rejected 위주
    STAGE_DIST_OPEN = [
        'applied', 'applied', 'applied',
        'screening', 'screening',
        'interview1', 'interview1',
        'interview2',
        'final',
        'offered', 'rejected',
    ]
    STAGE_DIST_CLOSED = [
        'hired', 'hired', 'rejected', 'rejected', 'rejected',
    ]

    name_pool = list(APPLICANT_NAMES)
    rng.shuffle(name_pool)
    name_cursor = 0

    total_applicants = 0
    for i, (posting_id, (_, _, _, _, status, _)) in enumerate(zip(posting_ids, POSTINGS)):
        count = rng.randint(4, 8) if status == 'open' else rng.randint(3, 5)
        stage_dist = STAGE_DIST_OPEN if status == 'open' else STAGE_DIST_CLOSED

        for _ in range(count):
            if name_cursor >= len(name_pool):
                name_cursor = 0
            app_name = name_pool[name_cursor]; name_cursor += 1
            app_email = f"{app_name.lower().replace(' ', '')}_{rng.randint(10,99)}@email.com"
            phone = f"010-{rng.randint(1000,9999)}-{rng.randint(1000,9999)}"
            stage = rng.choice(stage_dist)
            source = rng.choice(SOURCES)
            resume = rng.choice(RESUME_NOTES)
            days_ago = rng.randint(3, 60)
            created = (today - timedelta(days=days_ago)).isoformat() + ' 10:00:00'

            c.execute(
                """INSERT INTO applicants
                   (posting_id, name, email, phone, source, resume_note, stage, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (posting_id, app_name, app_email, phone, source, resume, stage, created)
            )
            app_id = c.lastrowid
            total_applicants += 1

            # 파이프라인 이동 로그 (스테이지마다 로그 생성)
            stage_order = ['applied','screening','interview1','interview2','final','offered','hired','rejected']
            current_idx = stage_order.index(stage)
            log_date = today - timedelta(days=days_ago)
            for s_idx in range(current_idx + 1):
                log_date = log_date + timedelta(days=rng.randint(1, 5))
                note_map = {
                    'applied': '지원서 접수',
                    'screening': '서류 통과',
                    'interview1': '1차 면접 통과',
                    'interview2': '2차 면접 통과',
                    'final': '최종 면접 완료',
                    'offered': '처우 협의 중',
                    'hired': '입사 확정',
                    'rejected': '불합격 처리',
                }
                c.execute(
                    """INSERT INTO applicant_logs
                       (applicant_id, stage, note, changed_by, created_at)
                       VALUES (?,?,?,?,?)""",
                    (app_id, stage_order[s_idx],
                     note_map.get(stage_order[s_idx], ''),
                     recruiter_id,
                     log_date.isoformat() + ' 09:00:00')
                )

    print(f"   채용공고: {len(posting_ids)}건 (오픈 {sum(1 for _,(_,_,_,_,s,_) in zip(posting_ids,POSTINGS) if s=='open')}건 / 마감 {sum(1 for _,(_,_,_,_,s,_) in zip(posting_ids,POSTINGS) if s=='closed')}건)")
    print(f"   지원자: {total_applicants}명")


def _seed_master_db(c_all_users=None):
    """
    master.db에 데모 테넌트(id=1) 등록.
    이미 존재하면 스킵 (idempotent).
    """
    from master_db import init_master_db, get_master_db, get_tenant_db_path
    init_master_db()

    mdb = get_master_db()
    # 이미 테넌트 1이 있으면 스킵
    if mdb.execute('SELECT id FROM tenants WHERE id=1').fetchone():
        mdb.close()
        return

    from datetime import date, timedelta
    trial_ends = (date.today() + timedelta(days=36500)).isoformat()  # 데모는 100년 트라이얼

    mc = mdb.cursor()
    mc.execute(
        '''INSERT INTO tenants (id, slug, company_name, admin_email, status, trial_ends_at)
           VALUES (1, 'demo', '(주)탤런트코어 데모', 'admin@company.com', 'trial', ?)''',
        (trial_ends,)
    )
    mc.execute(
        '''INSERT INTO subscriptions (tenant_id, status, peak_headcount)
           VALUES (1, 'trialing', 100)'''
    )

    # hr_system.db 모든 사용자 이메일을 tenant_users에 등록
    t1_path = get_tenant_db_path(1)
    t1_conn = sqlite3.connect(t1_path)
    emails  = [r[0] for r in t1_conn.execute(
        "SELECT email FROM users WHERE status='active'"
    ).fetchall()]
    t1_conn.close()

    mc.executemany(
        'INSERT OR IGNORE INTO tenant_users (email, tenant_id) VALUES (?,1)',
        [(e,) for e in emails]
    )
    mdb.commit()
    mdb.close()
    print(f"   master.db: 데모 테넌트 등록 ({len(emails)}명)")


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

    # ── guest 계정 보장 (DELETE FROM users 이후 재생성) ─────────
    if c.execute("SELECT COUNT(*) FROM users WHERE role='guest'").fetchone()[0] == 0:
        import os
        from werkzeug.security import generate_password_hash as gph
        gpw = gph(os.environ.get('HR_GUEST_PASSWORD', 'guest1234!'))
        ALL_F = ('attendance,payroll,performance,peer_review,calibration,'
                 'recruiting,announcements,org_chart,certificates')
        # guest는 HR팀 소속, CL3 직위
        hr_dept = c.execute("SELECT id FROM departments WHERE name='인사팀'").fetchone()
        cl3_pos = c.execute("SELECT id FROM positions WHERE name LIKE '%CL3%'").fetchone()
        dept_id = hr_dept[0] if hr_dept else 1
        pos_id  = cl3_pos[0] if cl3_pos else 3
        c.execute(
            "INSERT INTO users (email, password_hash, name, role, department_id, "
            " position_id, hire_date, onboarded, features_enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ('guest@talentcore.com', gpw, 'Guest', 'guest',
             dept_id, pos_id, '2024-01-01', 1, ALL_F)
        )
        c.execute("UPDATE users SET emp_no='TC-GUEST' WHERE email='guest@talentcore.com'")

    # ── 트랜잭셔널 시드 데이터 ───────────────────────────────────
    _seed_transactional(c, inserted_ids, admin_id if admin_row else inserted_ids[0])

    conn.commit()
    c.execute('PRAGMA foreign_keys = ON')
    conn.close()

    # ── master.db: 데모 테넌트(1) 등록 ───────────────────────
    _seed_master_db(c_all_users=inserted_ids)

    print("Migration complete!")
    print(f"   직원: {len(inserted_ids)}명")
    print(f"   부서: 부문 4 / 본부 8 / 실 13 / 팀 23")
    print(f"   직급: CL1~CL9 (9단계)")
    print(f"   직군: {len(jf_list)}개")
    print(f"   연봉 기준: {sum(1 for s in SALARY_TABLE.values() for v in s if v>0)}개 등급")
    print(f"\n  로그인: admin@company.com / changeme!")


if __name__ == '__main__':
    run()
