"""
급여 계산 유틸리티 — 한국 노동법 기준 (2026년)

적용 법령:
  - 근로기준법 제34조  : 퇴직급여 (퇴직금)
  - 근로기준법 제60조  : 연차유급휴가
  - 근로기준법 제48조  : 임금명세서 필수 기재사항
  - 최저임금법         : 2026년 시간급 최저임금
  - 소득세법 시행령 §12: 비과세 소득 (식대 20만원, 교통비 20만원)
  - 국민연금법         : 근로자 부담 4.5%
  - 국민건강보험법     : 근로자 부담 3.545%
  - 노인장기요양보험법 : 건강보험료의 12.95% (2026년)
  - 고용보험법         : 근로자 부담 0.9% (실업급여)
  - 소득세법           : 근로소득세 누진세율 + 지방소득세 10%
"""

from datetime import date

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
) -> dict:
    """
    월 급여에서 공제액을 계산해 명세서 dict 반환.

    비과세 한도 (소득세법 시행령 §12):
      - 식대      : 20만원/월
      - 교통비    : 20만원/월

    4대보험 근로자 부담률 (2026년):
      - 국민연금        : 4.5 %
      - 건강보험        : 3.545 %
      - 장기요양보험    : 건강보험료 × 12.95 %
      - 고용보험        : 0.9 %

    소득세: 연간 과세표준 기준 누진세율 → 월 환산
    지방소득세: 소득세 × 10 %
    """
    # ── 비과세 처리
    TAX_FREE_MEAL      = 200_000   # 소득세법 시행령 §12①3
    TAX_FREE_TRANSPORT = 200_000   # 소득세법 시행령 §12①1

    nontax_meal      = min(meal_allowance, TAX_FREE_MEAL)
    nontax_transport = min(transport_allowance, TAX_FREE_TRANSPORT)

    # 과세소득 = 기본급 + 연장수당 + 한도초과 수당
    taxable_monthly = (
        base_salary
        + overtime_pay
        + max(0, meal_allowance - nontax_meal)
        + max(0, transport_allowance - nontax_transport)
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

    income_tax      = max(0, round(annual_tax / 12))
    local_income_tax = round(income_tax * 0.10)   # 지방소득세 10 %

    # ── 집계
    gross_pay = base_salary + meal_allowance + transport_allowance + overtime_pay
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
