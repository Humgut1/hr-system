"""
급여 계산 유틸리티 — 한국 노동법 기준 (2026년)

적용 법령:
  - 근로기준법 제34조  : 퇴직급여 (퇴직금)
  - 근로기준법 제56조  : 연장·야간·휴일 근로 가산수당
  - 근로기준법 제60조  : 연차유급휴가 + 미사용연차수당
  - 근로기준법 제48조  : 임금명세서 필수 기재사항
  - 최저임금법         : 2026년 시간급 최저임금
  - 소득세법 §12       : 비과세 소득 (식대 20만원, 교통비 20만원, 육아수당 10만원 등)
  - 국민연금법         : 근로자 부담 4.5%
  - 국민건강보험법     : 근로자 부담 3.545%
  - 노인장기요양보험법 : 건강보험료의 12.95% (2026년)
  - 고용보험법         : 근로자 부담 0.9% (실업급여)
  - 소득세법           : 근로소득세 누진세율 + 지방소득세 10%
"""

import calendar as _cal
from datetime import date, datetime, time as dtime, timedelta

# ── 최저임금 (최저임금법, 2026년) ─────────────────────────────
MIN_WAGE_HOURLY  = 10_030          # 시간급 (원) — 2025년 고시, 2026년 예상치
MIN_WAGE_MONTHLY = 10_030 * 209    # 월 환산 (주 40h 기준 월 209h) = 2,096,270원


def check_min_wage(base_salary: int) -> dict:
    """
    기본급이 최저임금 월 환산액 미달 여부 확인.
    Returns: {'ok': bool, 'shortage': int, 'min_monthly': int}
    """
    shortage = max(0, MIN_WAGE_MONTHLY - base_salary)
    return {
        'ok':          shortage == 0,
        'shortage':    shortage,
        'min_monthly': MIN_WAGE_MONTHLY,
        'min_hourly':  MIN_WAGE_HOURLY,
    }


# ── 퇴직금 계산 (근로기준법 §34) ─────────────────────────────
def calc_severance(hire_date_str: str, termination_date_str: str,
                   recent_payslips: list) -> dict:
    """
    퇴직금 계산.

    산식:
      평균임금 = 퇴직 전 3개월 총임금 합계 / 퇴직 전 3개월 총일수
      퇴직금   = 평균임금 × 30일 × (근속일수 / 365)

    Args:
        hire_date_str        : 입사일 (YYYY-MM-DD)
        termination_date_str : 퇴직일 (YYYY-MM-DD)
        recent_payslips      : 최근 3개월 payslip dict 리스트
                               각 항목: {'gross_pay': int, 'year': int, 'month': int}

    Returns dict:
        tenure_days      : 근속일수
        tenure_years     : 근속연수 (소수 포함)
        avg_daily_wage   : 평균임금 (일)
        severance_amount : 퇴직금 (원, 원 미만 절사)
        basis_total_pay  : 3개월 총임금
        basis_days       : 3개월 총일수
        eligible         : 퇴직금 지급 대상 여부 (1년 이상)
    """
    try:
        hire = date.fromisoformat(hire_date_str)
        term = date.fromisoformat(termination_date_str)
    except (ValueError, TypeError):
        return {'eligible': False, 'severance_amount': 0}

    tenure_days  = (term - hire).days
    tenure_years = tenure_days / 365.0

    # 1년 미만 근속 시 퇴직금 미발생
    if tenure_days < 365:
        return {
            'eligible':        False,
            'tenure_days':     tenure_days,
            'tenure_years':    round(tenure_years, 2),
            'severance_amount': 0,
            'reason':          f'근속 {tenure_days}일 — 1년 미만으로 퇴직금 미발생',
        }

    # 평균임금 계산 (퇴직 전 3개월)
    if recent_payslips:
        # 3개월 총임금
        basis_total_pay = sum(p.get('gross_pay', 0) for p in recent_payslips)
        # 3개월 총일수 (해당 월의 실제 달력 일수 합산)
        import calendar
        basis_days = sum(
            calendar.monthrange(p['year'], p['month'])[1]
            for p in recent_payslips
        )
    else:
        # payslip 없으면 퇴직일 기준 역산 불가 → 기본급 기반 추정
        basis_total_pay = 0
        basis_days      = 92  # 3개월 평균

    if basis_days > 0 and basis_total_pay > 0:
        avg_daily_wage = basis_total_pay / basis_days
    else:
        avg_daily_wage = 0

    severance_amount = int(avg_daily_wage * 30 * (tenure_days / 365))

    return {
        'eligible':         True,
        'tenure_days':      tenure_days,
        'tenure_years':     round(tenure_years, 2),
        'avg_daily_wage':   round(avg_daily_wage),
        'basis_total_pay':  basis_total_pay,
        'basis_days':       basis_days,
        'severance_amount': severance_amount,
        'payslip_months':   len(recent_payslips),
    }


# ── 연차 계산 (근로기준법 제60조) ─────────────────────────
def calc_annual_leave(hire_date_str: str) -> float:
    """
    입사일 기준 연차 일수 계산.
    - 1년 미만  : 1개월 개근 시 1일, 최대 11일
    - 1년 이상  : 15일 기본
    - 3년 이상  : 2년마다 1일 가산, 최대 25일
    """
    try:
        hire = date.fromisoformat(hire_date_str)
    except (ValueError, TypeError):
        return 15.0

    today      = date.today()
    days_total = (today - hire).days

    if days_total < 365:
        months = int(days_total / 30.4375)  # 평균 월일수
        return float(min(months, 11))

    full_years = int(days_total / 365.25)
    extra      = (full_years - 1) // 2      # 3년차부터 2년마다 +1일
    return float(min(15 + extra, 25))


# ── 4대보험 + 소득세 계산 ─────────────────────────────────
def calc_payslip(
    base_salary: int,
    meal_allowance: int = 0,
    transport_allowance: int = 0,
    overtime_pay: int = 0,
    extra_benefits: list = None,
) -> dict:
    """
    월 급여에서 공제액을 계산해 명세서 dict 반환.

    비과세 한도 (소득세법 시행령 §12):
      - 식대            : 20만원/월
      - 교통비          : 20만원/월
      - extra_benefits  : BENEFIT_CATALOG 기준 각 항목별 비과세 한도 자동 적용

    4대보험 근로자 부담률 (2026년):
      - 국민연금        : 4.5 %
      - 건강보험        : 3.545 %
      - 장기요양보험    : 건강보험료 × 12.95 %
      - 고용보험        : 0.9 %

    소득세: 연간 과세표준 기준 누진세율 → 월 환산
    지방소득세: 소득세 × 10 %

    Args:
        extra_benefits: [{'key': str, 'name': str, 'amount': int,
                          'tax_exempt': bool, 'monthly_limit': int|None}]
                        BENEFIT_CATALOG 항목을 그대로 전달.
    """
    # ── 비과세 처리 (식대·교통비)
    TAX_FREE_MEAL      = 200_000   # 소득세법 시행령 §12①3
    TAX_FREE_TRANSPORT = 200_000   # 소득세법 시행령 §12①1

    nontax_meal      = min(meal_allowance, TAX_FREE_MEAL)
    nontax_transport = min(transport_allowance, TAX_FREE_TRANSPORT)

    # ── extra_benefits 처리
    extra_benefits = extra_benefits or []
    benefits_gross     = 0   # 추가 지급 합계 (gross_pay에 포함)
    benefits_nontax    = 0   # 비과세 합계
    benefits_breakdown = []  # 명세서 표시용

    for b in extra_benefits:
        amount = int(b.get('amount', 0))
        if amount <= 0:
            continue
        tax_exempt = b.get('tax_exempt', False)
        limit      = b.get('monthly_limit')
        if tax_exempt and limit is not None:
            exempt_part  = min(amount, limit)
            taxable_part = amount - exempt_part
        elif tax_exempt:
            exempt_part  = amount
            taxable_part = 0
        else:
            exempt_part  = 0
            taxable_part = amount

        benefits_gross  += amount
        benefits_nontax += exempt_part
        benefits_breakdown.append({
            'key':          b.get('key', ''),
            'name':         b.get('name', ''),
            'amount':       amount,
            'exempt_part':  exempt_part,
            'taxable_part': taxable_part,
        })

    # 과세소득 = 기본급 + 연장수당 + 한도초과 수당 + 과세 복리후생
    taxable_monthly = (
        base_salary
        + overtime_pay
        + max(0, meal_allowance - nontax_meal)
        + max(0, transport_allowance - nontax_transport)
        + (benefits_gross - benefits_nontax)
    )

    # ── 4대보험
    national_pension     = round(taxable_monthly * 0.045)
    health_insurance     = round(taxable_monthly * 0.03545)
    long_term_care       = round(health_insurance * 0.1295)   # 12.95 %
    employment_insurance = round(taxable_monthly * 0.009)

    # ── 소득세 (근로소득 간이세액표 기준 누진세율)
    annual_taxable = taxable_monthly * 12
    if annual_taxable <= 14_000_000:
        annual_tax = annual_taxable * 0.06
    elif annual_taxable <= 50_000_000:
        annual_tax = annual_taxable * 0.15 - 1_260_000
    elif annual_taxable <= 88_000_000:
        annual_tax = annual_taxable * 0.24 - 5_760_000
    elif annual_taxable <= 150_000_000:
        annual_tax = annual_taxable * 0.35 - 15_440_000
    elif annual_taxable <= 300_000_000:
        annual_tax = annual_taxable * 0.38 - 19_940_000
    elif annual_taxable <= 500_000_000:
        annual_tax = annual_taxable * 0.40 - 25_940_000
    else:
        annual_tax = annual_taxable * 0.42 - 35_940_000

    income_tax       = max(0, round(annual_tax / 12))
    local_income_tax = round(income_tax * 0.10)   # 지방소득세 10 %

    # ── 집계
    gross_pay = (
        base_salary + meal_allowance + transport_allowance
        + overtime_pay + benefits_gross
    )
    total_deduction = (
        national_pension + health_insurance + long_term_care
        + employment_insurance + income_tax + local_income_tax
    )
    net_pay = gross_pay - total_deduction

    return {
        'base_salary':           base_salary,
        'meal_allowance':        meal_allowance,
        'transport_allowance':   transport_allowance,
        'overtime_pay':          overtime_pay,
        'nontax_meal':           nontax_meal,
        'nontax_transport':      nontax_transport,
        'benefits_gross':        benefits_gross,
        'benefits_nontax':       benefits_nontax,
        'benefits_breakdown':    benefits_breakdown,
        'taxable_monthly':       taxable_monthly,
        'national_pension':      national_pension,
        'health_insurance':      health_insurance,
        'long_term_care':        long_term_care,
        'employment_insurance':  employment_insurance,
        'income_tax':            income_tax,
        'local_income_tax':      local_income_tax,
        'total_deduction':       total_deduction,
        'gross_pay':             gross_pay,
        'net_pay':               net_pay,
    }


def fmt_krw(amount: int) -> str:
    """정수를 한국 원화 형식으로 포매팅"""
    return f"{amount:,}"


# ── 복리후생·비과세 카탈로그 (소득세법 §12) ─────────────────────
BENEFIT_CATALOG = {
    # ── ① 매월 급여명세서 반영 (monthly_fixed) ────────────────────
    'meal': {
        'name': '식대',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 200_000,
        'legal_basis': '소득세법 시행령 §12①3',
        'description': '회사가 직접 식사를 제공하지 않는 경우 월 20만원까지 비과세',
        'default_amount': 200_000,
        'conditions': None,
        'icon': 'fa-utensils',
        'sort': 1,
    },
    'transport': {
        'name': '교통비',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 200_000,
        'legal_basis': '소득세법 시행령 §12①1',
        'description': '대중교통·주차비 등 월 20만원까지 비과세',
        'default_amount': 100_000,
        'conditions': None,
        'icon': 'fa-bus',
        'sort': 2,
    },
    'car_allowance': {
        'name': '자가운전보조금',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 200_000,
        'legal_basis': '소득세법 시행령 §12①2',
        'description': '본인 소유 차량을 업무에 사용하는 경우 월 20만원까지 비과세',
        'default_amount': 200_000,
        'conditions': '본인 명의 차량 보유자만 적용',
        'icon': 'fa-car',
        'sort': 3,
    },
    'childcare': {
        'name': '육아수당',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 100_000,
        'legal_basis': '소득세법 시행령 §12①9',
        'description': '만 8세 이하 자녀가 있는 근로자 월 10만원까지 비과세',
        'default_amount': 100_000,
        'conditions': '만 8세 이하 자녀 보유자만 적용',
        'icon': 'fa-baby',
        'sort': 4,
    },
    'tuition': {
        'name': '학자금 지원',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 100_000,
        'legal_basis': '소득세법 시행령 §12①10',
        'description': '자녀 교육비 지원 월 10만원까지 비과세',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-graduation-cap',
        'sort': 5,
    },
    'research_allowance': {
        'name': '연구보조비',
        'category': 'nontax',
        'payment_type': 'monthly_fixed',
        'tax_exempt': True,
        'monthly_limit': 200_000,
        'legal_basis': '소득세법 시행령 §12①4',
        'description': '연구직·기술직 근로자 월 20만원까지 비과세',
        'default_amount': 0,
        'conditions': '연구직·기술직 해당자만 적용',
        'icon': 'fa-flask',
        'sort': 6,
    },
    # ── ② 복지포인트 / 연간 예산형 (annual_budget) ───────────────
    'welfare_point': {
        'name': '복지포인트',
        'category': 'welfare',
        'payment_type': 'annual_budget',
        'tax_exempt': False,
        'monthly_limit': None,
        'legal_basis': '소득세법 §20 (근로소득 — 과세, 2024 대법원)',
        'description': '베네피아·이지웰 등 외부 플랫폼 위탁 또는 자체 운영. 급여명세서 별도 관리, 연말정산 시 합산.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-gift',
        'sort': 10,
        'platform_options': ['베네피아', '이지웰(현대)', '베네핏허브', '자체 운영'],
    },
    # ── ③ 영수증 환급형 (reimbursement) ──────────────────────────
    'health_support': {
        'name': '건강검진 지원',
        'category': 'welfare',
        'payment_type': 'reimbursement',
        'tax_exempt': False,
        'monthly_limit': None,
        'annual_limit': None,
        'legal_basis': '법정 의무검진 범위 내 비과세, 추가항목 과세',
        'description': '직원이 영수증 제출 → HR 승인 → 환급. 법정 건강검진은 비과세, 프리미엄 항목 초과분 과세.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-heartbeat',
        'sort': 11,
    },
    'gym_support': {
        'name': '피트니스·운동 지원',
        'category': 'welfare',
        'payment_type': 'reimbursement',
        'tax_exempt': False,
        'monthly_limit': None,
        'annual_limit': None,
        'legal_basis': '소득세법 §20 (복리후생비 — 과세)',
        'description': '헬스장·운동 관련 영수증 제출 환급. 전액 과세.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-dumbbell',
        'sort': 12,
    },
    'self_dev': {
        'name': '자기계발비',
        'category': 'welfare',
        'payment_type': 'reimbursement',
        'tax_exempt': False,
        'monthly_limit': None,
        'annual_limit': None,
        'legal_basis': '소득세법 §20 (복리후생비 — 과세)',
        'description': '도서구입·강의·자격증 취득 비용 영수증 환급.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-book',
        'sort': 13,
    },
    # ── ④ 상여·성과급 별도 지급 (separate_bonus) ─────────────────
    'holiday_bonus_lunar': {
        'name': '설 상여금',
        'category': 'bonus',
        'payment_type': 'separate_bonus',
        'tax_exempt': False,
        'monthly_limit': None,
        'legal_basis': '소득세법 §20 (상여 — 과세)',
        'description': '설 명절 별도 지급. 과세. 2024 대법원: 이익배분형은 퇴직금 평균임금 제외.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-moon',
        'sort': 20,
        'calc_type': 'pct_of_base',
        'default_pct': 100,
    },
    'holiday_bonus_chuseok': {
        'name': '추석 상여금',
        'category': 'bonus',
        'payment_type': 'separate_bonus',
        'tax_exempt': False,
        'monthly_limit': None,
        'legal_basis': '소득세법 §20 (상여 — 과세)',
        'description': '추석 명절 별도 지급. 과세. 퇴직금 평균임금 제외.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-star',
        'sort': 21,
        'calc_type': 'pct_of_base',
        'default_pct': 100,
    },
    'pi_bonus': {
        'name': '성과급 PI (개인)',
        'category': 'bonus',
        'payment_type': 'separate_bonus',
        'tax_exempt': False,
        'monthly_limit': None,
        'legal_basis': '소득세법 §20 (상여 — 과세)',
        'description': '개인 성과등급 기반. 연 1~2회 별도 지급. 성과등급(S/A/B/C/D)별 연봉 대비 % 설정.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-chart-line',
        'sort': 22,
        'calc_type': 'grade_pct',
        'grade_pct': {'S': 20, 'A': 15, 'B': 10, 'C': 5, 'D': 0},
    },
    'ci_bonus': {
        'name': '성과급 CI (회사)',
        'category': 'bonus',
        'payment_type': 'separate_bonus',
        'tax_exempt': False,
        'monthly_limit': None,
        'legal_basis': '소득세법 §20 (상여 — 과세)',
        'description': '회사 목표 달성률 기반. 연 1회 별도 지급. 기본급 대비 % × 달성률로 계산.',
        'default_amount': 0,
        'conditions': None,
        'icon': 'fa-building',
        'sort': 23,
        'calc_type': 'company_pct',
        'default_pct': 10,
    },
}

# 지급 방식 레이블
PAYMENT_TYPE_LABELS = {
    'monthly_fixed':  ('매월 급여명세서 반영',    '매월 고정 금액으로 급여명세서에 자동 포함됩니다.'),
    'annual_budget':  ('복지포인트 / 연간 예산',  '연간 1인당 예산을 설정합니다. 급여명세서와 별도로 관리되며 연말정산 시 합산됩니다.'),
    'reimbursement':  ('영수증 환급',             '직원이 영수증을 제출하면 HR이 승인 후 환급합니다. 연간 한도를 설정하세요.'),
    'separate_bonus': ('상여·성과급 별도 지급',   '급여 생성과 분리된 별도 지급 버튼으로 실행합니다. 퇴직금 평균임금에서 제외됩니다.'),
}

# 카테고리 한글 레이블 (하위 호환)
BENEFIT_CATEGORY_LABELS = {
    'nontax':  '비과세 수당',
    'welfare': '복리후생',
    'bonus':   '상여·성과급',
}


# ── 중도 입사/퇴사 일할계산 ─────────────────────────────────────
def calc_prorated_salary(base_salary: int, start_date_str: str,
                         end_date_str: str, year: int, month: int) -> dict:
    """
    해당 월에 부분 근무한 경우 일할계산 급여 반환.

    Args:
        base_salary    : 월 기본급
        start_date_str : 입사일 or 해당 월 근무 시작일 (YYYY-MM-DD)
        end_date_str   : 퇴직일 or 해당 월 근무 종료일 (YYYY-MM-DD)
        year / month   : 계산 대상 연월

    Returns dict:
        days_in_month, days_worked, prorated_salary, is_full_month
    """
    try:
        start = date.fromisoformat(start_date_str)
        end   = date.fromisoformat(end_date_str)
    except (ValueError, TypeError):
        return {
            'days_in_month': 30, 'days_worked': 0,
            'prorated_salary': 0, 'is_full_month': False,
        }

    days_in_month = _cal.monthrange(year, month)[1]
    month_start   = date(year, month, 1)
    month_end     = date(year, month, days_in_month)

    work_start = max(start, month_start)
    work_end   = min(end,   month_end)

    if work_end < work_start:
        return {
            'days_in_month': days_in_month, 'days_worked': 0,
            'prorated_salary': 0, 'is_full_month': False,
        }

    days_worked      = (work_end - work_start).days + 1
    prorated_salary  = int(base_salary * days_worked / days_in_month)
    is_full_month    = (days_worked == days_in_month)

    return {
        'days_in_month':    days_in_month,
        'days_worked':      days_worked,
        'prorated_salary':  prorated_salary,
        'is_full_month':    is_full_month,
        'base_salary':      base_salary,
    }


# ── 미사용 연차수당 (근로기준법 §60⑦) ─────────────────────────
def calc_unused_leave_pay(hire_date_str: str, termination_date_str: str,
                          used_days: float, avg_daily_wage: float) -> dict:
    """
    퇴직 시 미사용 연차에 대한 수당 계산.

    산식: 미사용 연차수당 = 평균임금(일) × 미사용 연차일수

    Args:
        hire_date_str        : 입사일 (YYYY-MM-DD)
        termination_date_str : 퇴직일 (YYYY-MM-DD)
        used_days            : 해당 회계연도 사용 연차일수
        avg_daily_wage       : 평균임금 (일, 퇴직금 계산과 동일 기준)

    Returns dict:
        total_leave, used_days, unused_days, avg_daily_wage, unused_leave_pay
    """
    try:
        hire = date.fromisoformat(hire_date_str)
        term = date.fromisoformat(termination_date_str)
    except (ValueError, TypeError):
        return {'total_leave': 0, 'used_days': used_days,
                'unused_days': 0, 'unused_leave_pay': 0}

    tenure_days = (term - hire).days

    if tenure_days < 30:
        total_leave = 0.0
    elif tenure_days < 365:
        months      = int(tenure_days / 30.4375)
        total_leave = float(min(months, 11))
    else:
        full_years  = int(tenure_days / 365.25)
        extra       = (full_years - 1) // 2      # 3년차부터 2년마다 +1
        total_leave = float(min(15 + extra, 25))

    unused_days      = max(0.0, total_leave - used_days)
    unused_leave_pay = int(avg_daily_wage * unused_days)

    return {
        'total_leave':      total_leave,
        'used_days':        used_days,
        'unused_days':      unused_days,
        'avg_daily_wage':   round(avg_daily_wage),
        'unused_leave_pay': unused_leave_pay,
    }


# ── 종합 퇴직 정산 ─────────────────────────────────────────────
def calc_separation_settlement(
    hire_date_str: str,
    termination_date_str: str,
    recent_payslips: list,
    used_leave_days: float = 0,
    final_month_base_salary: int = 0,
    final_month_days_worked: int = 0,
    final_month_days_total: int = 0,
) -> dict:
    """
    퇴직 시 법정 지급 금액 전체 자동계산.

    구성:
      1. 퇴직금       (근로기준법 §34)  — 1년 이상 근속 시
      2. 미사용 연차수당 (근로기준법 §60⑦)
      3. 마지막 월 일할급여 (중도 퇴직 시)

    Args:
        hire_date_str          : 입사일
        termination_date_str   : 퇴직일
        recent_payslips        : 최근 3개월 payslip list [{'gross_pay':int,'year':int,'month':int}]
        used_leave_days        : 해당 회계연도 사용 연차일수
        final_month_*          : 마지막 월 일할계산 파라미터 (0이면 full month 가정)

    Returns dict:
        severance, unused_leave, prorated, avg_daily_wage, total_settlement
    """
    # 1. 퇴직금
    severance = calc_severance(hire_date_str, termination_date_str, recent_payslips)

    # 평균임금 (일) — 퇴직금 계산에서 이미 구해진 값 재활용
    if severance.get('avg_daily_wage', 0) > 0:
        avg_daily = float(severance['avg_daily_wage'])
    elif recent_payslips:
        import calendar as _c
        total_pay  = sum(p.get('gross_pay', 0) for p in recent_payslips)
        basis_days = sum(_c.monthrange(p['year'], p['month'])[1] for p in recent_payslips)
        avg_daily  = total_pay / basis_days if basis_days else 0.0
    else:
        avg_daily = 0.0

    # 2. 미사용 연차수당
    unused_leave = calc_unused_leave_pay(
        hire_date_str, termination_date_str, used_leave_days, avg_daily
    )

    # 3. 마지막 월 일할급여
    prorated = None
    if final_month_base_salary > 0 and final_month_days_total > 0:
        days_worked = final_month_days_worked if final_month_days_worked > 0 else final_month_days_total
        prorated = {
            'base_salary':      final_month_base_salary,
            'days_worked':      days_worked,
            'days_total':       final_month_days_total,
            'prorated_salary':  int(final_month_base_salary * days_worked / final_month_days_total),
            'is_full_month':    days_worked == final_month_days_total,
        }

    total = (
        severance.get('severance_amount', 0)
        + unused_leave.get('unused_leave_pay', 0)
        + (prorated['prorated_salary'] if prorated else 0)
    )

    return {
        'severance':        severance,
        'unused_leave':     unused_leave,
        'prorated':         prorated,
        'avg_daily_wage':   round(avg_daily),
        'total_settlement': total,
    }


# ── 연장·야간 근로 계산 (근로기준법 §56) ─────────────────────
def _calc_night_overlap(start_dt: datetime, end_dt: datetime) -> int:
    """
    주어진 구간 중 야간(22:00~06:00) 해당 분 계산.
    날짜를 넘기는 경우도 처리.
    """
    night_min = 0
    day = start_dt.date()
    end_day = end_dt.date()

    while day <= end_day:
        night_windows = [
            (datetime.combine(day, dtime(0, 0)),
             datetime.combine(day, dtime(6, 0))),
            (datetime.combine(day, dtime(22, 0)),
             datetime.combine(day + timedelta(days=1), dtime(0, 0))),
        ]
        for ws, we in night_windows:
            s = max(start_dt, ws)
            e = min(end_dt, we)
            if e > s:
                night_min += int((e - s).total_seconds() / 60)
        day += timedelta(days=1)

    return night_min


def calc_day_hours(date_str: str, check_in_str: str, check_out_str: str) -> dict:
    """
    하루 체크인/아웃에서 정규·연장·야간 근무 분 계산.

    Args:
        date_str      : 'YYYY-MM-DD'
        check_in_str  : 'HH:MM'
        check_out_str : 'HH:MM'

    Returns dict:
        total_min, regular_min (≤480), overtime_min (>480), night_min
    """
    try:
        ci = datetime.strptime(f'{date_str} {check_in_str}',  '%Y-%m-%d %H:%M')
        co = datetime.strptime(f'{date_str} {check_out_str}', '%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return {'total_min': 0, 'regular_min': 0, 'overtime_min': 0, 'night_min': 0}

    # 퇴근이 출근보다 이르면 자정을 넘긴 것으로 처리
    if co <= ci:
        co += timedelta(days=1)

    total_min   = int((co - ci).total_seconds() / 60)
    night_min   = _calc_night_overlap(ci, co)
    DAILY_MAX   = 480  # 8시간 = 480분
    regular_min = min(total_min, DAILY_MAX)
    overtime_min = max(0, total_min - DAILY_MAX)

    return {
        'total_min':    total_min,
        'regular_min':  regular_min,
        'overtime_min': overtime_min,
        'night_min':    night_min,
    }


def calc_extra_pay(overtime_min: int, night_min: int, base_salary: int, 
                   is_holiday: bool = False, holiday_regular_min: int = 0) -> dict:
    """
    연장·야간·휴일 수당 금액 계산 (근로기준법 §56).

    가산율:
      연장근로  : 통상임금의 50% 가산 (×1.5 중 0.5 가산분)
      야간근로  : 통상임금의 50% 가산 (×1.5 중 0.5 가산분)
      휴일근로 (§56②):
        - 8시간 이내: 50% 가산 (총 1.5배)
        - 8시간 초과: 100% 가산 (총 2.0배)
      
      → 모든 가산은 중복 적용됨 (예: 휴일 8시간 초과이면서 야간이면 휴일가산 100% + 야간가산 50% = 150% 가산)

    시급 산정: 월 기본급 ÷ 209시간 (주 40h 기준 월 환산)
    """
    if base_salary <= 0:
        return {'overtime_pay': 0, 'night_pay': 0, 'holiday_pay': 0, 'total_extra_pay': 0, 'hourly_wage': 0}
    
    overtime_min = max(0, overtime_min)
    night_min    = max(0, night_min)
    minute_wage  = base_salary / 209 / 60
    
    # 1. 연장 가산분 (50%)
    # 휴일이 아닌 날의 8시간 초과분 혹은 휴일의 8시간 초과분(연장) 처리
    overtime_pay = int(minute_wage * overtime_min * 0.5)
    
    # 2. 야간 가산분 (50%)
    night_pay    = int(minute_wage * night_min * 0.5)
    
    # 3. 휴일 가산분 (50% or 100%)
    holiday_pay = 0
    if is_holiday:
        # 휴일근로 전체 시간에 대해 일단 50% 가산 (8시간 이내분 포함)
        total_holiday_min = holiday_regular_min + overtime_min
        holiday_pay += int(minute_wage * total_holiday_min * 0.5)
        
        # 8시간 초과분(overtime_min)에 대해서는 추가로 50% 더 가산 (총 100% 가산분)
        if overtime_min > 0:
            holiday_pay += int(minute_wage * overtime_min * 0.5)

    return {
        'overtime_pay':    overtime_pay,
        'night_pay':       night_pay,
        'holiday_pay':     holiday_pay,
        'total_extra_pay': overtime_pay + night_pay + holiday_pay,
        'hourly_wage':     round(base_salary / 209),
    }
