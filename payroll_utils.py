"""
급여 계산 유틸리티 — 한국 노동법 기준 (2026년)

적용 법령:
  - 근로기준법 제34조  : 퇴직급여 (퇴직금)
  - 근로기준법 제56조  : 연장·야간·휴일 근로 가산수당
  - 근로기준법 제60조  : 연차유급휴가 + 미사용연차수당
  - 근로기준법 제48조  : 임금명세서 필수 기재사항
  - 최저임금법         : 2026년 시간급 최저임금
  - 소득세법 §12       : 비과세 소득 (식대 20만원, 교통비 20만원, 육아수당 10만원 등)
  - 소득세법 §47       : 근로소득공제 (총급여 구간별 공제율)
  - 소득세법 §50       : 기본공제 (본인 + 부양가족 1인당 150만원)
  - 소득세법 §51       : 추가공제 (경로우대 100만원, 장애인 200만원, 한부모 100만원, 부녀자 50만원)
  - 소득세법 §59의2    : 자녀세액공제 (만 8세 이상 자녀, 출산·입양공제)
  - 국민연금법         : 근로자 부담 4.5%
  - 국민건강보험법     : 근로자 부담 3.545%
  - 노인장기요양보험법 : 건강보험료의 12.95% (2026년)
  - 고용보험법         : 근로자 부담 0.9% (실업급여)
"""

import calendar as _cal
from datetime import date, datetime, time as dtime, timedelta

# ══ 근로기준법 시간 상수 ══════════════════════════════════════════
# §50  소정근로시간
DAILY_WORK_MAX   = 480   # 1일 8시간 = 480분
WEEKLY_WORK_STD  = 2400  # 1주 40시간 = 2400분
# §53  연장근로 한도
WEEKLY_OT_MAX    = 720   # 1주 12시간 = 720분
WEEKLY_TOTAL_MAX = 3120  # 1주 최대 52시간 = 3120분
WEEKLY_WARNING   = 2880  # 경고선 48시간 = 2880분
# 월 환산 근로시간: (주 40h + 주휴 8h) × 52주 / 12개월 = 208.67 → 209h
MONTHLY_STD_HOURS = 209

# ── 최저임금 (최저임금법, 2026년) ─────────────────────────────
MIN_WAGE_HOURLY  = 10_030                          # 시간급 (원)
MIN_WAGE_MONTHLY = MIN_WAGE_HOURLY * MONTHLY_STD_HOURS   # 월 환산 = 2,096,270원


def check_min_wage(base_salary: int,
                   monthly_hours: int = MONTHLY_STD_HOURS) -> dict:
    """
    기본급이 최저임금 미달 여부 확인.

    Args:
        base_salary   : 월 기본급 (원)
        monthly_hours : 월 소정근로시간 (전일제=209h, 파트타임은 실제 시간)

    Returns:
        ok, shortage, min_monthly, min_hourly, effective_hourly
    """
    min_monthly      = MIN_WAGE_HOURLY * monthly_hours
    shortage         = max(0, min_monthly - base_salary)
    effective_hourly = round(base_salary / monthly_hours) if monthly_hours else 0
    return {
        'ok':              shortage == 0,
        'shortage':        shortage,
        'min_monthly':     min_monthly,
        'min_hourly':      MIN_WAGE_HOURLY,
        'effective_hourly': effective_hourly,
        'monthly_hours':   monthly_hours,
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


def compute_leave_balance(db, user_id, year=None, sick_policy='annual', include_pending=False):
    """연차 잔액 계산 — 시스템 전체의 유일한 공식 (P0-1, improvement_plan.md).

    app.py의 get_leave_balance()와 mcp_server.py가 공유한다.
    db: sqlite3 connection (row_factory 무관 — 인덱스 접근만 사용)

    - base:      근속 기준 법정 발생 연차 (calc_annual_leave — §60)
    - carryover: leave_balances의 해당 연도 이월분
    - used:      해당 연도(start_date 기준) 소진성 휴가 합
                 · 연차/오전반차/오후반차는 항상 소진
                 · 병가는 sick_policy='annual'(연차 차감 정책)일 때만 소진
    - include_pending: 승인 대기(pending/reviewed) 건 포함 — 신청 검증용 (이중 신청 방지)
    """
    if year is None:
        year = date.today().year

    row = db.execute('SELECT hire_date FROM users WHERE id=?', (user_id,)).fetchone()
    hire_date = row[0] if row else None
    base = calc_annual_leave(hire_date) if hire_date else 15.0

    try:
        row = db.execute(
            'SELECT carry_over_days FROM leave_balances WHERE user_id=? AND year=?',
            (user_id, year)
        ).fetchone()
        carryover = float(row[0] or 0) if row else 0.0
    except Exception:   # 테이블 미생성 등
        carryover = 0.0

    deduct_types = ['annual', 'half_am', 'half_pm']
    if sick_policy == 'annual':
        deduct_types.append('sick')

    statuses = "('approved','pending','reviewed')" if include_pending else "('approved')"
    placeholders = ','.join('?' * len(deduct_types))
    used = float(db.execute(
        f"SELECT COALESCE(SUM(days),0) FROM leave_requests "
        f"WHERE user_id=? AND status IN {statuses} AND type IN ({placeholders}) "
        f"AND strftime('%Y', start_date)=?",
        (user_id, *deduct_types, str(year))
    ).fetchone()[0])

    total = base + carryover
    return {
        'year': year, 'base': base, 'carryover': carryover,
        'total': total, 'used': used, 'remaining': total - used,
        'deduct_types': deduct_types,
    }


# ══ 소득세 정확 계산 (§47 / §50 / §51 / §59의2) ══════════════════════

def calc_earned_income_deduction(annual_gross: int) -> int:
    """
    근로소득공제 (소득세법 §47).
    총급여액 구간별 공제율 적용, 한도 2,000만원.
    """
    if annual_gross <= 5_000_000:
        d = int(annual_gross * 0.70)
    elif annual_gross <= 15_000_000:
        d = 3_500_000 + int((annual_gross - 5_000_000) * 0.40)
    elif annual_gross <= 45_000_000:
        d = 7_500_000 + int((annual_gross - 15_000_000) * 0.15)
    elif annual_gross <= 100_000_000:
        d = 12_000_000 + int((annual_gross - 45_000_000) * 0.05)
    else:
        d = 14_750_000
    return min(d, 20_000_000)


def calc_personal_deductions(dependents: list,
                              is_female: bool = False,
                              annual_gross: int = 0) -> dict:
    """
    인적공제 계산 (소득세법 §50 기본공제 / §51 추가공제 / §59의2 자녀세액공제).

    Args:
        dependents   : employee_dependents 행 리스트 (dict or sqlite3.Row)
                       필드: relation, birth_date, is_disabled, annual_income,
                             is_cohabiting, is_adopted, birth_order
        is_female    : 직원이 여성인지 (부녀자공제 판정)
        annual_gross : 직원 연간 총급여 (부녀자공제 소득 요건 3천만원 이하)

    Returns dict:
        basic_deduction          : 기본공제 합계 (본인 포함)
        senior_extra             : 경로우대 추가공제
        disabled_extra           : 장애인 추가공제
        single_parent_extra      : 한부모공제
        widow_extra              : 부녀자공제
        total_personal_deduction : 전체 인적공제 합계
        num_dependents           : 기본공제 인원 (본인 포함)
        child_tax_credit         : 자녀세액공제 (세액에서 직접 차감)
        birth_credit             : 출산·입양공제 (당해연도)
        children_under_8         : 만 8세 미만 자녀 수 (육아수당 자동 적용 판정용)
    """
    from datetime import date
    today = date.today()

    def age_of(birth_date_str):
        if not birth_date_str:
            return None
        try:
            bd = date.fromisoformat(str(birth_date_str)[:10])
            return today.year - bd.year - (
                1 if (today.month, today.day) < (bd.month, bd.day) else 0
            )
        except (ValueError, TypeError):
            return None

    basic_deduction  = 1_500_000   # 본인 기본공제
    senior_extra     = 0
    disabled_extra   = 0
    has_spouse       = False
    has_child        = False       # 기본공제 대상 직계비속

    qualified_count  = 1           # 기본공제 인원 (본인)
    children_tax_credit_list = []  # 만 8세 이상 기본공제 대상 자녀
    children_under_8 = 0           # 만 8세 미만 자녀 (육아수당용)
    birth_credit     = 0

    for dep in (dependents or []):
        rel        = dep['relation'] if hasattr(dep, '__getitem__') else getattr(dep, 'relation', '')
        bd_str     = dep['birth_date'] if hasattr(dep, '__getitem__') else getattr(dep, 'birth_date', None)
        disabled   = bool(dep['is_disabled'] if hasattr(dep, '__getitem__') else getattr(dep, 'is_disabled', 0))
        dep_income = int(dep['annual_income'] if hasattr(dep, '__getitem__') else getattr(dep, 'annual_income', 0) or 0)
        cohabit    = bool(dep['is_cohabiting'] if hasattr(dep, '__getitem__') else getattr(dep, 'is_cohabiting', 1))
        is_adopted = bool(dep['is_adopted'] if hasattr(dep, '__getitem__') else getattr(dep, 'is_adopted', 0))
        birth_ord  = int(dep['birth_order'] if hasattr(dep, '__getitem__') else getattr(dep, 'birth_order', 1) or 1)

        age = age_of(bd_str)

        # ── 소득 요건: 연간 소득금액 100만원 이하 (§50)
        income_ok = dep_income <= 1_000_000

        # ── 나이 요건 (장애인이면 전면 면제)
        if rel == 'spouse':
            age_ok   = True
            cohabit  = True  # 배우자는 동거요건 없음
            has_spouse = True
        elif rel in ('parent', 'grandparent'):
            age_ok = disabled or (age is not None and age >= 60)
        elif rel == 'child':
            age_ok = disabled or (age is not None and age <= 20)
        elif rel == 'sibling':
            age_ok = disabled or (age is not None and (age <= 20 or age >= 60))
        else:
            age_ok = False

        # ── 동거 요건 (형제자매·직계존속만 적용)
        cohabit_ok = cohabit if rel in ('parent', 'grandparent', 'sibling') else True

        if age_ok and income_ok and cohabit_ok:
            basic_deduction += 1_500_000
            qualified_count += 1

            # 경로우대 (만 70세 이상)
            if age is not None and age >= 70:
                senior_extra += 1_000_000

            # 장애인 추가공제
            if disabled:
                disabled_extra += 2_000_000

            # 자녀 관련
            if rel == 'child':
                has_child = True
                if age is not None:
                    if age >= 8:
                        children_tax_credit_list.append(dep)
                    else:
                        children_under_8 += 1
                # 출산·입양공제 (당해연도 출산/입양 여부는 birth_order로 판단)
                if is_adopted or (bd_str and str(bd_str)[:4] == str(today.year)):
                    if birth_ord == 1:
                        birth_credit += 300_000
                    elif birth_ord == 2:
                        birth_credit += 500_000
                    else:
                        birth_credit += 700_000

    # ── 한부모 vs 부녀자 (중복 불가, 한부모 우선)
    single_parent_extra = 0
    widow_extra         = 0
    if not has_spouse and has_child:
        single_parent_extra = 1_000_000
    elif is_female and annual_gross <= 30_000_000:
        if has_spouse or has_child:
            widow_extra = 500_000

    total_personal_deduction = (
        basic_deduction + senior_extra + disabled_extra
        + single_parent_extra + widow_extra
    )

    # ── 자녀세액공제 (§59의2, 만 8세 이상 기본공제 대상 자녀)
    n = len(children_tax_credit_list)
    if n == 0:
        child_tax_credit = 0
    elif n == 1:
        child_tax_credit = 150_000
    elif n == 2:
        child_tax_credit = 300_000
    else:
        child_tax_credit = 300_000 + (n - 2) * 300_000

    return {
        'basic_deduction':          basic_deduction,
        'senior_extra':             senior_extra,
        'disabled_extra':           disabled_extra,
        'single_parent_extra':      single_parent_extra,
        'widow_extra':              widow_extra,
        'total_personal_deduction': total_personal_deduction,
        'num_dependents':           qualified_count,
        'child_tax_credit':         child_tax_credit,
        'birth_credit':             birth_credit,
        'children_under_8':         children_under_8,
        'children_tax_credit_count': n,
    }


def _calc_annual_tax(taxable_base: int) -> int:
    """과세표준 → 산출세액 (소득세법 §55 누진세율)"""
    if taxable_base <= 14_000_000:
        return int(taxable_base * 0.06)
    elif taxable_base <= 50_000_000:
        return int(taxable_base * 0.15) - 1_260_000
    elif taxable_base <= 88_000_000:
        return int(taxable_base * 0.24) - 5_760_000
    elif taxable_base <= 150_000_000:
        return int(taxable_base * 0.35) - 15_440_000
    elif taxable_base <= 300_000_000:
        return int(taxable_base * 0.38) - 19_940_000
    elif taxable_base <= 500_000_000:
        return int(taxable_base * 0.40) - 25_940_000
    else:
        return int(taxable_base * 0.42) - 35_940_000


# ── 4대보험 + 소득세 계산 ─────────────────────────────────
def calc_payslip(
    base_salary: int,
    meal_allowance: int = 0,
    transport_allowance: int = 0,
    overtime_pay: int = 0,
    extra_benefits: list = None,
    dependents: list = None,
    is_female: bool = False,
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

    소득세: §47 근로소득공제 → §50/§51 인적공제 → §55 누진세율 → §59의2 자녀세액공제
    지방소득세: 소득세 × 10 %

    Args:
        extra_benefits: [{'key': str, 'name': str, 'amount': int,
                          'tax_exempt': bool, 'monthly_limit': int|None}]
        dependents    : employee_dependents 쿼리 결과 (list of Row/dict)
        is_female     : 부녀자공제 판정용
    """
    # ── 비과세 처리 (식대·교통비)
    TAX_FREE_MEAL      = 200_000
    TAX_FREE_TRANSPORT = 200_000

    nontax_meal      = min(meal_allowance, TAX_FREE_MEAL)
    nontax_transport = min(transport_allowance, TAX_FREE_TRANSPORT)

    # ── extra_benefits 처리
    extra_benefits = extra_benefits or []
    benefits_gross     = 0
    benefits_nontax    = 0
    benefits_breakdown = []

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

    # ── 과세소득 (총급여)
    taxable_monthly = (
        base_salary
        + overtime_pay
        + max(0, meal_allowance - nontax_meal)
        + max(0, transport_allowance - nontax_transport)
        + (benefits_gross - benefits_nontax)
    )
    annual_gross = taxable_monthly * 12

    # ── 4대보험
    national_pension     = round(taxable_monthly * 0.045)
    health_insurance     = round(taxable_monthly * 0.03545)
    long_term_care       = round(health_insurance * 0.1295)
    employment_insurance = round(taxable_monthly * 0.009)

    # ── 소득세 (§47 → §50/§51 → §55 → §59의2)
    income_deduction     = calc_earned_income_deduction(annual_gross)
    earned_income        = annual_gross - income_deduction  # 근로소득금액

    dep_result           = calc_personal_deductions(dependents, is_female, annual_gross)
    total_personal_ded   = dep_result['total_personal_deduction']
    child_tax_credit     = dep_result['child_tax_credit'] + dep_result['birth_credit']

    taxable_base = max(0, earned_income - total_personal_ded)
    annual_tax   = max(0, _calc_annual_tax(taxable_base) - child_tax_credit)

    income_tax       = max(0, round(annual_tax / 12))
    local_income_tax = round(income_tax * 0.10)

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
        'base_salary':              base_salary,
        'meal_allowance':           meal_allowance,
        'transport_allowance':      transport_allowance,
        'overtime_pay':             overtime_pay,
        'nontax_meal':              nontax_meal,
        'nontax_transport':         nontax_transport,
        'benefits_gross':           benefits_gross,
        'benefits_nontax':          benefits_nontax,
        'benefits_breakdown':       benefits_breakdown,
        'taxable_monthly':          taxable_monthly,
        'annual_gross':             annual_gross,
        'income_deduction':         income_deduction,
        'earned_income':            earned_income,
        'total_personal_deduction': total_personal_ded,
        'num_dependents':           dep_result['num_dependents'],
        'child_tax_credit_amount':  child_tax_credit,
        'children_under_8':         dep_result['children_under_8'],
        'national_pension':         national_pension,
        'health_insurance':         health_insurance,
        'long_term_care':           long_term_care,
        'employment_insurance':     employment_insurance,
        'income_tax':               income_tax,
        'local_income_tax':         local_income_tax,
        'total_deduction':          total_deduction,
        'gross_pay':                gross_pay,
        'net_pay':                  net_pay,
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


# ══ 근태 시간 계산 (근로기준법 §50 / §53 / §54 / §56) ════════════


def _calc_break_min(raw_min: int) -> int:
    """
    근로기준법 §54 기준 법정 휴게시간 공제.

    - 4시간 이상 ~ 8시간 미만 : 30분 이상 (본 함수는 정확히 30분 적용)
    - 8시간 이상              : 1시간 이상 (본 함수는 정확히 60분 적용)
    - 4시간 미만              : 휴게 의무 없음 (0분)

    실무 주의: 실제 사업장에서 취업규칙으로 더 긴 휴게를 부여할 수 있으나,
    법정 최소값을 기준으로 적용함.
    """
    if raw_min >= 480:
        return 60
    elif raw_min >= 240:
        return 30
    return 0


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

    근로기준법 적용:
      §54  휴게시간 — 4h 이상 30분, 8h 이상 60분 자동 공제
      §50  소정근로 — 하루 8시간(480분) 초과분을 overtime_min으로 분리
      §56  야간근로 — 22:00~06:00 구간 별도 집계 (수당 계산용)

    Args:
        date_str      : 'YYYY-MM-DD'
        check_in_str  : 'HH:MM'
        check_out_str : 'HH:MM'

    Returns dict:
        raw_min      : 체크인~체크아웃 실 경과 분 (휴게 공제 전)
        break_min    : §54 공제 휴게시간 (분)
        total_min    : 실 근로시간 (raw_min - break_min)
        regular_min  : 소정근로 (≤ 480분)
        overtime_min : 연장근로 (> 480분)
        night_min    : 야간근로 22:00~06:00 (수당 계산용, total_min에 이미 포함)
    """
    try:
        ci = datetime.strptime(f'{date_str} {check_in_str}',  '%Y-%m-%d %H:%M')
        co = datetime.strptime(f'{date_str} {check_out_str}', '%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return {
            'raw_min': 0, 'break_min': 0, 'total_min': 0,
            'regular_min': 0, 'overtime_min': 0, 'night_min': 0,
        }

    # 퇴근이 출근보다 이르면 자정을 넘긴 것으로 처리
    if co <= ci:
        co += timedelta(days=1)

    raw_min   = int((co - ci).total_seconds() / 60)
    break_min = _calc_break_min(raw_min)      # §54 법정 휴게시간 공제
    total_min = max(0, raw_min - break_min)   # 실 근로시간

    regular_min  = min(total_min, DAILY_WORK_MAX)        # §50 소정근로
    overtime_min = max(0, total_min - DAILY_WORK_MAX)    # §53 연장근로

    # 야간 계산은 휴게 공제 전 원 구간 기준 (수당 계산 시 실제 체류 시간이 기준)
    night_min = _calc_night_overlap(ci, co)

    return {
        'raw_min':      raw_min,
        'break_min':    break_min,
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
        'hourly_wage':     round(base_salary / MONTHLY_STD_HOURS),
    }


# ══ 주간 근로시간 집계 (근로기준법 §53) ═══════════════════════════


def get_week_bounds(date_str: str) -> tuple:
    """
    해당 날짜가 속하는 주(週) 월요일~일요일 날짜 반환.

    근로기준법에서 "1주"의 기산점은 취업규칙으로 정하되 미정 시 관행으로 결정.
    본 시스템은 월요일 기산을 기본값으로 적용.

    Returns: (monday_str, sunday_str)  'YYYY-MM-DD' 형식
    """
    d      = date.fromisoformat(date_str)
    monday = d - timedelta(days=d.weekday())   # weekday(): 월=0, 일=6
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def calc_weekly_hours(db, user_id: int, date_str: str) -> dict:
    """
    해당 날짜가 속하는 주(월~일)의 누적 근로시간 집계 및 §53 위반 여부 판정.

    근로기준법 §53:
      - 연장근로 = 소정근로(40h) 초과분
      - 휴일근로도 연장근로 한도(12h)에 포함 (2018년 개정)
      - 1주 최대 52시간 (5인 이상 사업장 전면 적용, 2021.7.1~)

    Args:
        db       : SQLite connection (get_db() 반환값)
        user_id  : 직원 ID
        date_str : 'YYYY-MM-DD' — 집계할 날짜가 속한 주를 특정

    Returns dict:
        week_start    : 해당 주 월요일 (YYYY-MM-DD)
        week_end      : 해당 주 일요일 (YYYY-MM-DD)
        total_min     : 주 실 근로시간 합계 (분)
        total_h       : 주 실 근로시간 합계 (시간, 소수점 1자리)
        weekly_ot_min : 주 40시간 초과분 (분)
        remain_min    : 52시간까지 남은 분 (0 이하면 위반)
        remain_h      : 52시간까지 남은 시간
        is_warning    : 48시간 이상 52시간 이하 (경고)
        is_violation  : 52시간 초과 (위반)
        over_min      : 초과 분 (위반 시)
        over_h        : 초과 시간 (위반 시)
        days_worked   : 해당 주 체크인 일수
    """
    monday, sunday = get_week_bounds(date_str)

    rows = db.execute(
        'SELECT regular_min, overtime_min FROM checkins '
        'WHERE user_id = ? AND date BETWEEN ? AND ?',
        (user_id, monday, sunday)
    ).fetchall()

    total_min    = sum(r['regular_min'] + r['overtime_min'] for r in rows)
    weekly_ot    = max(0, total_min - WEEKLY_WORK_STD)
    remain_min   = max(0, WEEKLY_TOTAL_MAX - total_min)
    over_min     = max(0, total_min - WEEKLY_TOTAL_MAX)

    return {
        'week_start':    monday,
        'week_end':      sunday,
        'total_min':     total_min,
        'total_h':       round(total_min / 60, 1),
        'weekly_ot_min': weekly_ot,
        'remain_min':    remain_min,
        'remain_h':      round(remain_min / 60, 1),
        'is_warning':    WEEKLY_WARNING <= total_min <= WEEKLY_TOTAL_MAX,
        'is_violation':  total_min > WEEKLY_TOTAL_MAX,
        'over_min':      over_min,
        'over_h':        round(over_min / 60, 1),
        'days_worked':   len(rows),
    }


# ── v0.51: Compa-Ratio ──────────────────────────────────────────────────────

def calc_compa_ratio(base_salary: int, mid_salary: int) -> float | None:
    """
    Compa-Ratio = 현재 기본급 / 밴드 중간값
    - 1.0 = 밴드 정중앙
    - <1.0 = 밴드 하단 (Red-circle 위험)
    - >1.0 = 밴드 상단 (Over-market)
    """
    if not mid_salary or mid_salary <= 0:
        return None
    # base_salary는 월급, mid_salary는 연봉 기준 → 월급 × 12로 연봉 환산 후 비교
    return round((base_salary * 12) / mid_salary, 3)


def compa_band(ratio: float | None) -> str:
    """Compa-Ratio → merit_matrix compa_band 키 변환"""
    if ratio is None:
        return 'at'
    if ratio < 0.9:
        return 'below'
    if ratio > 1.1:
        return 'above'
    return 'at'


def merit_from_matrix(db, grade: str, ratio: float | None) -> float:
    """merit_matrix 테이블에서 성과등급 × compa_band → 인상률(%) 조회"""
    band = compa_band(ratio)
    row  = db.execute(
        'SELECT increase_pct FROM merit_matrix WHERE performance_grade=? AND compa_band=?',
        (grade, band)
    ).fetchone()
    return row['increase_pct'] if row else 0.0
