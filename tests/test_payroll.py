# -*- coding: utf-8 -*-
"""급여·근태 계산 회귀 테스트 (정비 로드맵 M4).

기대값은 지금 코드가 내는 값이 아니라 법령에서 손으로 계산한 값이다.
코드를 고쳐서 이 테스트가 깨지면, 법령이 바뀐 게 아닌 한 코드 쪽이 틀린 것이다.

    python -m pytest tests/ -q
"""
import os
import sqlite3
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import payroll_utils as pu  # noqa: E402

# 통상시급 10,000원이 되는 월 기본급 (10,000 × 209h)
BASE_10K = 2_090_000


# ══ 최저임금법 ════════════════════════════════════════════════════

def test_min_wage_2026_hourly():
    # 고용노동부 고시 제2025-47호 — 2026년 시간급 10,320원
    assert pu.min_wage_hourly(2026) == 10_320


def test_min_wage_by_year_and_future_fallback():
    assert pu.min_wage_hourly(2025) == 10_030
    assert pu.min_wage_hourly(2027) == 10_700
    assert pu.min_wage_hourly(2030) == pu.min_wage_hourly(max(pu.MIN_WAGE_BY_YEAR))


def test_check_min_wage_2026_monthly_boundary():
    # 10,320 × 209h = 2,156,880원
    assert pu.check_min_wage(2_156_880, year=2026)['ok'] is True
    r = pu.check_min_wage(2_156_870, year=2026)
    assert r['ok'] is False and r['shortage'] == 10


def test_check_min_wage_part_time_hours():
    # 월 소정 100시간 파트타임 — 1,032,000원이 하한
    assert pu.check_min_wage(1_032_000, monthly_hours=100, year=2026)['ok'] is True
    assert pu.check_min_wage(1_031_999, monthly_hours=100, year=2026)['ok'] is False


# ══ 근로기준법 §60 연차 ═══════════════════════════════════════════

@pytest.mark.parametrize('hire, as_of, expected', [
    # 1년 미만: 1개월 개근마다 1일 — 한 달을 채운 다음 날 발생
    ('2026-01-15', '2026-02-14', 0),
    ('2026-01-15', '2026-02-15', 1),
    ('2026-01-15', '2026-12-15', 11),
    ('2026-01-15', '2027-01-14', 11),   # 최대 11일
    # 1년: 15일 (1년을 채운 다음 날)
    ('2026-01-15', '2027-01-15', 15),
    # 3년 이상: 최초 1년 초과 2년마다 +1
    ('2026-01-15', '2029-01-14', 15),
    ('2026-01-15', '2029-01-15', 16),
    ('2021-03-01', '2026-02-28', 16),   # 5년 하루 전
    ('2021-03-01', '2026-03-01', 17),   # 5년 — 365.25 나눗셈이면 여기서 하루 늦게 16이 나왔다
    ('2005-03-01', '2026-03-01', 25),   # 21년 = 15 + 10
    ('1990-03-01', '2026-03-01', 25),   # 상한 25일
    # 말일·윤일 입사 (민법 §160 — 해당 일이 없으면 그 달 말일 만료)
    ('2026-01-31', '2026-02-28', 0),
    ('2026-01-31', '2026-03-01', 1),
    ('2024-02-29', '2025-02-28', 11),
    ('2024-02-29', '2025-03-01', 15),
])
def test_annual_leave_entitlement(hire, as_of, expected):
    assert pu.annual_leave_entitlement(date.fromisoformat(hire), date.fromisoformat(as_of)) == expected
    assert pu.calc_annual_leave(hire, as_of=date.fromisoformat(as_of)) == expected


def test_unused_leave_pay_uses_same_rule():
    # 3년 근속(16일) − 사용 6일 = 10일 × 평균임금 100,000원
    r = pu.calc_unused_leave_pay('2023-03-02', '2026-03-02', used_days=6, avg_daily_wage=100_000)
    assert r['total_leave'] == 16
    assert r['unused_days'] == 10
    assert r['unused_leave_pay'] == 1_000_000


def _leave_db():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY, hire_date TEXT);
        CREATE TABLE leave_balances (user_id INT, year INT, carry_over_days REAL);
        CREATE TABLE leave_requests (user_id INT, type TEXT, days REAL, status TEXT, start_date TEXT);
        INSERT INTO users VALUES (1, '2000-01-01');                -- 25일
        INSERT INTO leave_balances VALUES (1, 2026, 4);
        INSERT INTO leave_requests VALUES
            (1, 'annual',  3,   'approved', '2026-03-02'),
            (1, 'half_am', 0.5, 'approved', '2026-04-01'),
            (1, 'sick',    2,   'approved', '2026-05-01'),
            (1, 'annual',  1,   'pending',  '2026-06-01'),
            (1, 'annual',  5,   'rejected', '2026-06-10'),
            (1, 'annual',  2,   'approved', '2025-12-01');       -- 작년 사용분
    ''')
    return db


def test_leave_balance_single_formula():
    r = pu.compute_leave_balance(_leave_db(), 1, year=2026, sick_policy='annual')
    assert r['base'] == 25 and r['carryover'] == 4 and r['total'] == 29
    assert r['used'] == 5.5            # 연차 3 + 반차 0.5 + 병가 2 (연차 차감 정책)
    assert r['remaining'] == 23.5


def test_leave_balance_sick_policy_and_pending():
    db = _leave_db()
    assert pu.compute_leave_balance(db, 1, year=2026, sick_policy='separate')['used'] == 3.5
    assert pu.compute_leave_balance(db, 1, year=2026, include_pending=True)['used'] == 6.5


# ══ 근로자퇴직급여 보장법 §8 / 근로기준법 §2 평균임금 ═════════════

def test_severance_formula():
    # 평균임금 = 3개월 임금 9,000,000 ÷ 92일, 퇴직금 = 평균임금 × 30 × 재직일수(1,826) ÷ 365
    slips = [{'gross_pay': 3_000_000, 'year': 2025, 'month': m} for m in (10, 11, 12)]
    r = pu.calc_severance('2021-01-01', '2026-01-01', slips)
    assert r['eligible'] is True
    assert r['basis_days'] == 92
    assert r['tenure_days'] == 1826
    assert r['severance_amount'] == 14_681_953


def test_severance_under_one_year():
    r = pu.calc_severance('2025-06-01', '2026-03-01', [])
    assert r['eligible'] is False and r['severance_amount'] == 0


def test_prorated_salary():
    # 1월 17일~31일 = 15일 / 31일
    r = pu.calc_prorated_salary(3_100_000, '2026-01-17', '2026-12-31', 2026, 1)
    assert r['days_worked'] == 15 and r['prorated_salary'] == 1_500_000


# ══ 근로기준법 §54 휴게 · §50 소정근로 · §56③ 야간 ═══════════════

def test_day_hours_standard_day():
    r = pu.calc_day_hours('2026-03-02', '09:00', '18:00')
    assert (r['raw_min'], r['break_min'], r['total_min']) == (540, 60, 480)
    assert (r['regular_min'], r['overtime_min'], r['night_min']) == (480, 0, 0)


def test_day_hours_overtime():
    r = pu.calc_day_hours('2026-03-02', '09:00', '21:00')
    assert r['total_min'] == 660 and r['overtime_min'] == 180


def test_day_hours_overnight_night_window():
    r = pu.calc_day_hours('2026-03-02', '22:00', '06:00')   # 자정 넘김
    assert r['raw_min'] == 480
    assert r['night_min'] == 480                              # 22~06시 전부 야간


# ══ 근로기준법 §56 연장·야간·휴일 수당 (통상시급 10,000원) ═══════

def test_overtime_pay_is_150_percent():
    # 평일 연장 2시간 = 10,000 × 1.5 × 2
    r = pu.calc_extra_pay(120, 0, BASE_10K)
    assert r['overtime_pay'] == 30_000
    assert r['holiday_pay'] == 0
    assert r['hourly_wage'] == 10_000


def test_night_premium_is_50_percent():
    # 소정근로 안의 야간 2시간 = 가산분 10,000 × 0.5 × 2
    assert pu.calc_extra_pay(0, 120, BASE_10K)['night_pay'] == 10_000


def test_overtime_and_night_stack():
    # 연장이면서 야간인 2시간 = 1.5 + 0.5
    assert pu.calc_extra_pay(120, 120, BASE_10K)['total_extra_pay'] == 40_000


def test_holiday_within_8h_is_150_percent():
    r = pu.calc_extra_pay(0, 0, BASE_10K, is_holiday=True, holiday_regular_min=480)
    assert r['holiday_pay'] == 120_000          # 10,000 × 1.5 × 8
    assert r['overtime_pay'] == 0


def test_holiday_over_8h_is_200_percent_not_250():
    # 8시간 120,000 + 초과 2시간 10,000 × 2.0 × 2 = 40,000 → 160,000 (연장 가산 중복 없음)
    r = pu.calc_extra_pay(120, 0, BASE_10K, is_holiday=True, holiday_regular_min=480)
    assert r['holiday_pay'] == 160_000
    assert r['overtime_pay'] == 0
    assert r['total_extra_pay'] == 160_000


def test_holiday_ot_request_without_checkin_splits_at_8h():
    # 체크인 없이 승인된 휴일 OT 600분 — 앞 8시간 1.5배, 나머지 2배
    assert pu.calc_extra_pay(600, 0, BASE_10K, is_holiday=True)['holiday_pay'] == 160_000


def test_holiday_night_stacks():
    # 휴일 22~24시 2시간 = 1.5 + 0.5
    r = pu.calc_extra_pay(0, 120, BASE_10K, is_holiday=True, holiday_regular_min=120)
    assert r['total_extra_pay'] == 40_000


def test_extra_pay_zero_salary():
    assert pu.calc_extra_pay(120, 120, 0)['total_extra_pay'] == 0


# ══ 근로기준법 §53 주 52시간 ═════════════════════════════════════

def _week_db(daily_minutes):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE checkins (user_id INT, date TEXT, regular_min INT, overtime_min INT)')
    for i, m in enumerate(daily_minutes):
        db.execute('INSERT INTO checkins VALUES (1, ?, ?, ?)',
                   (f'2026-03-{2 + i:02d}', min(m, 480), max(0, m - 480)))   # 3/2(월)부터
    return db


def test_week_bounds_monday_start():
    assert pu.get_week_bounds('2026-03-05') == ('2026-03-02', '2026-03-08')


def test_weekly_52h_ok_warning_violation():
    ok = pu.calc_weekly_hours(_week_db([480] * 5), 1, '2026-03-04')          # 40h
    assert not ok['is_warning'] and not ok['is_violation'] and ok['weekly_ot_min'] == 0
    warn = pu.calc_weekly_hours(_week_db([600] * 5), 1, '2026-03-04')        # 50h
    assert warn['is_warning'] and not warn['is_violation'] and warn['remain_min'] == 120
    full = pu.calc_weekly_hours(_week_db([624] * 5), 1, '2026-03-04')        # 정확히 52h — 위반 아님
    assert not full['is_violation']
    over = pu.calc_weekly_hours(_week_db([600] * 6), 1, '2026-03-04')        # 60h
    assert over['is_violation'] and over['over_min'] == 480


# ══ 4대보험 2026 (근로자 부담분, 10원 미만 절사) ═════════════════

def test_insurance_2026_rates():
    r = pu.calc_insurance(3_000_000, pay_date=(2026, 3))
    assert r['national_pension'] == 142_500        # 4.75%
    assert r['health_insurance'] == 107_850        # 3.595%
    assert r['long_term_care'] == 14_170           # 건강보험료 × 13.14% = 14,171.49
    assert r['employment_insurance'] == 27_000     # 0.9%


def test_insurance_2025_rates_still_apply_to_2025_pay():
    r = pu.calc_insurance(3_000_000, pay_date=(2025, 12))
    assert r['national_pension'] == 135_000        # 4.5%
    assert r['health_insurance'] == 106_350        # 3.545%


def test_pension_base_is_clamped():
    rates = pu.get_insurance_rates((2026, 3))
    hi = pu.calc_insurance(20_000_000, pay_date=(2026, 3))
    lo = pu.calc_insurance(100_000, pay_date=(2026, 3))
    assert hi['pension_base'] == rates['pension_max']
    assert lo['pension_base'] == rates['pension_min']
    # 건강·고용은 상한 없이 보수월액 그대로
    assert hi['employment_insurance'] == 180_000


# ══ 소득세 간이세액표 (2026) ═════════════════════════════════════

def test_withholding_zero_below_table_start():
    assert pu.calc_simple_withholding(700_000)['income_tax'] == 0


def test_withholding_monotonic():
    incomes = range(1_500_000, 10_000_001, 250_000)
    for fam in (1, 2, 4):
        taxes = [pu.calc_simple_withholding(m, fam)['income_tax'] for m in incomes]
        assert taxes == sorted(taxes), f'family={fam}'
    for m in (3_000_000, 6_000_000, 12_000_000):
        by_family = [pu.calc_simple_withholding(m, f)['income_tax'] for f in range(1, 12)]
        assert by_family == sorted(by_family, reverse=True), f'monthly={m}'


def test_withholding_10m_row_matches_table_and_continuous():
    assert pu._table_tax(10_000_000, 1) == pu._WH_TEN[0]
    below = pu._table_tax(9_999_000, 1)
    above = pu._table_tax(10_010_000, 1)
    assert below <= pu._WH_TEN[0] <= above


def test_child_credit_amounts():
    # 8~20세 자녀 차감: 1명 20,830 / 2명 45,830 / 3명부터 1명당 33,330 추가
    assert pu.child_withholding_credit(0) == 0
    assert pu.child_withholding_credit(1) == 20_830
    assert pu.child_withholding_credit(2) == 45_830
    assert pu.child_withholding_credit(3) == 79_160


def test_withholding_rate_choice_and_local_tax():
    base = pu.calc_simple_withholding(5_000_000, 1)
    low = pu.calc_simple_withholding(5_000_000, 1, rate_pct=80)
    high = pu.calc_simple_withholding(5_000_000, 1, rate_pct=120)
    assert low['income_tax'] == base['table_tax'] * 80 // 100 // 10 * 10
    assert high['income_tax'] == base['table_tax'] * 120 // 100 // 10 * 10
    assert base['local_income_tax'] == base['income_tax'] // 10 // 10 * 10   # 지방소득세 10%


def test_bonus_withholding_formula():
    # 시행령 §194: (월급여 + 상여/기간) 세액 × 기간 − 월급여 세액 × 기간
    m, bonus, n = 4_000_000, 6_000_000, 3
    w = pu.calc_simple_withholding(m + bonus // n)['income_tax']
    b = pu.calc_simple_withholding(m)['income_tax']
    assert pu.calc_bonus_withholding(m, bonus, n)['income_tax'] == (w - b) * n // 10 * 10


# ══ 근로소득공제 (소득세법 §47) ═══════════════════════════════════

@pytest.mark.parametrize('gross, expected', [
    (5_000_000, 3_500_000),        # 70%
    (15_000_000, 7_500_000),       # 350만 + 40%
    (45_000_000, 12_000_000),      # 750만 + 15%
    (100_000_000, 14_750_000),     # 1,200만 + 5%
    (200_000_000, 16_750_000),     # 1,475만 + (2억−1억) × 2%
    (500_000_000, 20_000_000),     # 한도 2,000만
])
def test_earned_income_deduction(gross, expected):
    assert pu.calc_earned_income_deduction(gross) == expected
