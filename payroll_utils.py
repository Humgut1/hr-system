"""
급여 계산 유틸리티 — 한국 노동법 기준 (2026년)

적용 법령:
  - 근로기준법 제60조  : 연차유급휴가
  - 근로기준법 제48조  : 임금명세서 필수 기재사항
  - 소득세법 시행령 §12: 비과세 소득 (식대 20만원, 교통비 20만원)
  - 국민연금법         : 근로자 부담 4.5%
  - 국민건강보험법     : 근로자 부담 3.545%
  - 노인장기요양보험법 : 건강보험료의 12.95% (2026년)
  - 고용보험법         : 근로자 부담 0.9% (실업급여)
  - 소득세법           : 근로소득세 누진세율 + 지방소득세 10%
"""

from datetime import date


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
