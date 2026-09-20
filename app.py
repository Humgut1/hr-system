import os
import sqlite3
import uuid
import json
import base64
import hmac
import hashlib
import time
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for, jsonify, Response)
from werkzeug.security import check_password_hash, generate_password_hash
from payroll_utils import (calc_payslip, calc_annual_leave, compute_leave_balance, fmt_krw,
                           calc_severance, check_min_wage, MIN_WAGE_MONTHLY,
                           calc_day_hours, calc_extra_pay,
                           get_week_bounds, calc_weekly_hours,
                           WEEKLY_TOTAL_MAX, WEEKLY_WARNING,
                           BENEFIT_CATALOG, BENEFIT_CATEGORY_LABELS, PAYMENT_TYPE_LABELS,
                           calc_prorated_salary, calc_unused_leave_pay,
                           calc_separation_settlement, calc_insurance,
                           calc_simple_withholding, calc_personal_deductions,
                           calc_bonus_withholding, WITHHOLDING_RATES)
from master_db import (
    init_master_db, migrate_subscriptions, get_master_db, get_tenant_db_path,
    get_tenant_by_email, get_tenant, create_tenant,
    register_tenant_user, update_tenant_user_email, remove_tenant_user,
    update_peak_headcount, reset_peak_headcount,
    save_billing_key, log_billing, update_billing_log,
    compute_sub_state, start_grace_period, lock_tenant,
    PRICE_PER_SEAT, TRIAL_DAYS,
    PLAN_PRICES, PLAN_LABELS, DEFAULT_PLAN, get_plan_price,
    get_tenant_plan, set_tenant_plan,
    seed_default_superadmin, get_superadmin_by_username,
    list_tenants_with_state, set_tenant_status,
    get_or_create_api_token, regenerate_api_token, get_tenant_by_api_token,
    issue_training_ticket, use_training_ticket, TRAINING_TICKET_SECONDS,
)

app = Flask(__name__)
# HR_SECRET_KEY 우선, 구버전 .env 호환으로 SECRET_KEY도 인식 (v1.4.3 보안 수리)
app.secret_key = (os.environ.get('HR_SECRET_KEY')
                  or os.environ.get('SECRET_KEY')
                  or 'dev-only-change-in-prod')
if app.secret_key == 'dev-only-change-in-prod':
    import logging as _logging
    _logging.getLogger(__name__).warning(
        '경고: 세션 시크릿이 개발용 기본값입니다. 운영 환경에서는 HR_SECRET_KEY 환경변수를 반드시 설정하세요.')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ── 지원자 서류 업로드 설정 ────────────────────────────────────
UPLOAD_FOLDER    = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'applicant_docs')
ALLOWED_EXTS     = {'pdf', 'doc', 'docx', 'hwp', 'pptx', 'png', 'jpg', 'jpeg'}
MAX_FILE_SIZE_MB = 20
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DOC_TYPE_LABEL = {
    'resume':       '이력서',
    'cover_letter': '자기소개서',
    'portfolio':    '포트폴리오',
    'certificate':  '자격증',
    'other':        '기타',
}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTS

# ── 직원 문서함 업로드 설정 ────────────────────────────────────
EMP_DOC_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'employee_docs')
os.makedirs(EMP_DOC_UPLOAD_FOLDER, exist_ok=True)

EMP_DOC_TYPE_LABEL = {
    'id_card':    '신분증 사본',
    'bankbook':   '통장 사본',
    'diploma':    '졸업·자격증명서',
    'contract':   '계약 관련 서류',
    'other':      '기타',
}

# ── 토스페이먼츠 키 ─────────────────────────────────────────
# 무료 파트너 모드 (launch_plan P0-4, v1.5.1) — 기본 결제 비활성.
# 유료 전환 시 .env에 BILLING_ENABLED=1 + 토스 라이브 키 설정.
BILLING_ENABLED = os.environ.get('BILLING_ENABLED', '0') == '1'

TOSS_CLIENT_KEY = os.environ.get(
    'TOSS_CLIENT_KEY', 'test_ck_D5GePWvyJnrK0W0k6q8gLzN97Eoq'
)
TOSS_SECRET_KEY = os.environ.get(
    'TOSS_SECRET_KEY', 'test_sk_zXLkKEypNArWmo50nX3lmeaxYG5pqkgs4EBbA'
)

# ── DB 초기화 ────────────────────────────────────────────────
from database import init_db, DEFAULT_REVIEW_FORM, review_form_snapshot
import copilot
import workplace
init_master_db()        # master.db
migrate_subscriptions() # grace_until 등 신규 컬럼 추가
seed_default_superadmin() # SaaS 운영자 기본 계정 시드
init_db()               # hr_system.db (테넌트 1 기본 스키마)

# 체크인=체크아웃 동일 시각 오염 데이터 정리 (check_in == check_out → 분=0)
def _fix_checkin_data():
    import os
    for fname in os.listdir('.'):
        if not fname.endswith('.db') or fname == 'master.db':
            continue
        try:
            _c = sqlite3.connect(fname)
            cols = {r[1] for r in _c.execute('PRAGMA table_info(checkins)')}
            if 'check_in' not in cols:
                _c.close(); continue
            _c.execute(
                "UPDATE checkins SET regular_min=0, overtime_min=0, "
                "night_min=0, holiday_min=0, break_min=0 "
                "WHERE check_in IS NOT NULL AND check_out IS NOT NULL "
                "AND check_in = check_out"
            )
            _c.commit()
            _c.close()
        except Exception:
            pass
_fix_checkin_data()

# 연봉 기준표·부서 확장 시드 (job_families가 비어 있을 때만 자동 실행)
def _ensure_extended_seed():
    t1_path = get_tenant_db_path(1)
    _c = sqlite3.connect(t1_path)
    try:
        empty = _c.execute('SELECT COUNT(*) FROM job_families').fetchone()[0] == 0
    except Exception:
        empty = True
    finally:
        _c.close()
    if empty:
        from migrate_db import run as _run
        _run()
_ensure_extended_seed()

# ── 회사 정보 기본값 (환경변수 → DB 순으로 오버라이드) ────────
_COMPANY_DEFAULTS = {
    'name':    os.environ.get('COMPANY_NAME',    '주식회사 탤런트코어'),
    'reg_no':  os.environ.get('COMPANY_REG_NO',  '000-00-00000'),
    'ceo':     os.environ.get('COMPANY_CEO',     '대표이사'),
    'address': os.environ.get('COMPANY_ADDRESS', '서울특별시 강남구 테헤란로 000'),
    'tel':     os.environ.get('COMPANY_TEL',     '02-0000-0000'),
    # 회사·건물 가져오기(workplace)로 채워지는 값
    'name_en': '', 'founded': '', 'industry': '', 'website': '', 'intro': '', 'zip': '',
}

def get_company_info():
    """DB에 저장된 회사 정보 우선, 없으면 환경변수 기본값 사용"""
    db = get_db()
    rows = db.execute('SELECT key, value FROM company_settings').fetchall()
    info = dict(_COMPANY_DEFAULTS)
    for row in rows:
        if row['key'] in info and row['value']:
            info[row['key']] = row['value']
    return info


def get_company_config():
    """company_config 테이블에서 정책 설정을 dict로 반환"""
    db  = get_db()
    row = db.execute('SELECT * FROM company_config WHERE id=1').fetchone()
    if row:
        return dict(row)
    # 테이블이 비어있으면 기본값 dict 반환
    return {
        'work_system': 'standard', 'work_start': '09:00', 'work_end': '18:00',
        'lunch_start': '12:00',    'lunch_end':  '13:00',
        'core_start':  '10:00',    'core_end':   '16:00',
        'flex_settle_months': 1,   'elastic_unit': '2weeks',
        'remote_allowed': 1,       'remote_max_days_week': 3,
        'leave_policy': 'legal',   'leave_extra_days': 0,
        'allow_half_day': 1,       'allow_quarter_day': 0,
        'sick_policy': 'annual',   'sick_days_year': 0,
        'pay_day': 25,
        'default_meal_allowance': 200000, 'default_transport_allowance': 100000,
        'perf_cycle': 'semiannual','use_peer_review': 1,
        'use_self_review': 1,      'grade_system': 'SABCD',
        'grade_dist_mode': 'recommended', 'grade_dist_scope': 'company',
        'grade_dist_s': 10, 'grade_dist_a': 20, 'grade_dist_b': 40,
        'grade_dist_c': 20, 'grade_dist_d': 10,
        'one_on_one_cadence_days': 14, 'feedback_public_praise': 1,
        'setup_completed': 0,      'setup_step': 0,
    }


GRADE_DIST_DEFAULT = {'S': 10, 'A': 20, 'B': 40, 'C': 20, 'D': 10}
GRADE_DIST_MIN_DEPT = 10   # 부서 단위 배분은 10명 이상 부서만 개별 적용, 나머지는 합산


def get_grade_dist(cfg=None):
    """등급 배분 정책 — mode(forced|recommended), scope(company|dept), pct(등급별 %)."""
    cfg = cfg or get_company_config()
    pct = {}
    for g in 'SABCD':
        try:
            pct[g] = int(cfg.get('grade_dist_' + g.lower()) if cfg.get('grade_dist_' + g.lower()) is not None else GRADE_DIST_DEFAULT[g])
        except (TypeError, ValueError):
            pct[g] = GRADE_DIST_DEFAULT[g]
    if sum(pct.values()) != 100:
        pct = dict(GRADE_DIST_DEFAULT)
    mode = cfg.get('grade_dist_mode') if cfg.get('grade_dist_mode') in ('forced', 'recommended') else 'recommended'
    scope = cfg.get('grade_dist_scope') if cfg.get('grade_dist_scope') in ('company', 'dept') else 'company'
    return {'mode': mode, 'scope': scope, 'pct': pct,
            'label': '강제 배분' if mode == 'forced' else '권장 배분'}


def save_grade_dist(db, f):
    """초기 설정·회사 설정 공통 저장. 합계가 100이 아니면 비율은 저장하지 않고 안내 문구 반환."""
    if 'grade_dist_mode' not in f:
        return None
    mode = f.get('grade_dist_mode') if f.get('grade_dist_mode') in ('forced', 'recommended') else 'recommended'
    scope = f.get('grade_dist_scope') if f.get('grade_dist_scope') in ('company', 'dept') else 'company'
    db.execute('UPDATE company_config SET grade_dist_mode=?, grade_dist_scope=? WHERE id=1', (mode, scope))
    try:
        pct = {g: max(0, min(100, int(f.get('grade_dist_' + g.lower(), '') or 0))) for g in 'SABCD'}
    except ValueError:
        return '등급 배분 비율은 숫자로 입력'
    total = sum(pct.values())
    if total != 100:
        return f'등급 배분 비율 합계 {total}% (100%가 되어야 저장)'
    db.execute('UPDATE company_config SET grade_dist_s=?, grade_dist_a=?, grade_dist_b=?, '
               'grade_dist_c=?, grade_dist_d=? WHERE id=1',
               (pct['S'], pct['A'], pct['B'], pct['C'], pct['D']))
    return None


def grade_dist_groups(db, cycle_id, gd, override=None):
    """배분 적용 단위별 인원·확정 등급 수.
    override=(user_id, grade) 또는 {user_id: grade} 는 확정 직전 검사용.
    평가 명단(P1)이 있는 주기는 명단 대상자만 센다."""
    has_roster = db.execute('SELECT 1 FROM cycle_participants WHERE cycle_id=? LIMIT 1', (cycle_id,)).fetchone()
    if has_roster:
        emps = db.execute(
            "SELECT u.id, u.department_id, d.name dn, cr.final_grade FROM cycle_participants cp "
            "JOIN users u ON u.id=cp.user_id "
            "LEFT JOIN departments d ON u.department_id=d.id "
            "LEFT JOIN calibration_results cr ON cr.user_id=u.id AND cr.cycle_id=cp.cycle_id "
            "WHERE cp.cycle_id=? AND cp.included=1", (cycle_id,)
        ).fetchall()
    else:
        emps = db.execute(
            "SELECT u.id, u.department_id, d.name dn, cr.final_grade FROM users u "
            "LEFT JOIN departments d ON u.department_id=d.id "
            "LEFT JOIN calibration_results cr ON cr.user_id=u.id AND cr.cycle_id=? "
            "WHERE u.status='active' AND u.role NOT IN ('admin','guest')", (cycle_id,)
        ).fetchall()
    if isinstance(override, tuple):
        override = {override[0]: override[1]}
    override = override or {}
    size = {}
    for e in emps:
        size[e['department_id'] or 0] = size.get(e['department_id'] or 0, 0) + 1
    groups, member = {}, {}
    for e in emps:
        dk = e['department_id'] or 0
        if gd['scope'] == 'dept' and size[dk] >= GRADE_DIST_MIN_DEPT:
            key, label = dk, (e['dn'] or '부서 미지정')
        elif gd['scope'] == 'dept':
            key, label = 'small', f'{GRADE_DIST_MIN_DEPT}명 미만 부서 합산'
        else:
            key, label = 'all', '전사'
        grp = groups.setdefault(key, {'key': key, 'label': label, 'total': 0, 'confirmed': 0,
                                      'dist': {g: 0 for g in 'SABCD'}})
        grp['total'] += 1
        grade = e['final_grade']
        if e['id'] in override:
            grade = override[e['id']]
        if grade in grp['dist']:
            grp['dist'][grade] += 1
            grp['confirmed'] += 1
        member[e['id']] = key
    import math
    for grp in groups.values():
        n = grp['total']
        grp['cap'] = {g: math.ceil(n * gd['pct'][g] / 100) for g in 'SA'}
        grp['min'] = {g: math.floor(n * gd['pct'][g] / 100) for g in 'CD'}
        over = [f'{g} {grp["dist"][g]}명 > 상한 {grp["cap"][g]}명' for g in 'SA' if grp['dist'][g] > grp['cap'][g]]
        under = [f'{g} {grp["dist"][g]}명 < 하한 {grp["min"][g]}명' for g in 'CD' if grp['dist'][g] < grp['min'][g]]
        grp['over'], grp['under'] = over, under
        grp['done'] = grp['confirmed'] >= n
    ordered = sorted(groups.values(), key=lambda x: (x['key'] == 'small', x['label']))
    return ordered, member


@app.context_processor
def inject_company_config():
    """모든 템플릿에 company_config 주입"""
    try:
        return {'company_config': get_company_config()}
    except Exception:
        return {'company_config': {}}


# ── DB ──────────────────────────────────────────────────────
def get_db():
    """테넌트 격리 DB 연결. session['tenant_id'] → tenant_N.db"""
    db = getattr(g, '_database', None)
    if db is None:
        tenant_id = session.get('tenant_id', 1)  # 기본값 1 = 데모 테넌트
        db_path = get_tenant_db_path(tenant_id)
        db = g._database = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# ══════════════════════════════════════════════════════════════
#  연차 잔액 단일 소스 (P0-1, improvement_plan.md)
#  발생·이월·사용을 이 함수 한 곳에서만 계산한다.
#  화면 표시 / 신청 검증 / 연차촉진(§61) / 퇴직 정산(미사용수당) 전부 이 함수 사용.
# ══════════════════════════════════════════════════════════════
def get_leave_balance(db, user_id, year=None, include_pending=False):
    """연차 잔액 계산 — payroll_utils.compute_leave_balance의 얇은 래퍼.

    회사 정책(sick_policy)만 여기서 조회해 넘긴다. 화면 표시 / 신청 검증 /
    연차촉진(§61) / 퇴직 정산(미사용수당) / Slack / MCP 전부 같은 공식을 쓴다.
    """
    try:
        sick_policy = (get_company_config().get('sick_policy') or 'annual')
    except Exception:
        sick_policy = 'annual'
    return compute_leave_balance(db, user_id, year=year,
                                 sick_policy=sick_policy,
                                 include_pending=include_pending)


# ══════════════════════════════════════════════════════════════
#  CSRF 방어 (Phase A-2 보안 기준선)
#  - 세션별 토큰 발급 → 템플릿 meta 태그 → static/js/csrf.js가
#    모든 폼/fetch에 자동 주입 → 아래 before_request가 전역 검증
# ══════════════════════════════════════════════════════════════

# 외부 서비스가 직접 호출하는 엔드포인트 (자체 서명 검증으로 보호됨)
CSRF_EXEMPT_ENDPOINTS = {'billing_webhook', 'slack_command', 'slack_interactive', 'hires_webhook',
                         'workplace_rooms_book_api', 'workplace_rooms_cancel_api', 'sso_hire_verify',
                         'offer_approval_create_api', 'offer_approval_cancel_api',
                         'training_ticket_api', 'training_reset_api'}


def _get_csrf_token():
    """세션에 CSRF 토큰이 없으면 생성 후 반환."""
    if 'csrf_token' not in session:
        import secrets
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': _get_csrf_token}


# ══════════════════════════════════════════════════════════════
#  요금제 3계층 기능 게이팅 (Phase B-7, saas_plan.md §2)
#  Core   — 인사·근태·급여·증명서·전자결재·문서함·입사 예정자 (기본, 게이트 없음)
#  Growth — + 성과·온보딩·복지포인트·다면평가
#  Enterprise — + 채용 ATS·승계·Talent Card·급여 구조(밴드/Merit/ACR)·데이터 마법사
#  ※ 채용 ATS는 2026-07-15 승헌씨 지시로 Growth→Enterprise 격리 (입사 예정자는 Core 유지)
# ══════════════════════════════════════════════════════════════

_GROWTH_FEATURES     = {'performance', 'onboarding', 'welfare', 'peer_review'}
_ENTERPRISE_FEATURES = _GROWTH_FEATURES | {'recruiting', 'succession', 'talent_advanced', 'comp_advanced', 'data_wizard'}

PLAN_FEATURES = {
    'core':       set(),
    'growth':     _GROWTH_FEATURES,
    'enterprise': _ENTERPRISE_FEATURES,
}


def _current_plan():
    """요청 단위 캐시된 테넌트 요금제."""
    if not hasattr(g, '_tenant_plan'):
        g._tenant_plan = get_tenant_plan(session.get('tenant_id', 1))
    return g._tenant_plan


# 구 내장 ATS(/recruit/* 공고·파이프라인·지원자·오퍼)를 메뉴에 보일지.
# 2026-09-02 은퇴 — 채용은 Hire 한 곳에서만 한다. 코드는 지우지 않고 잠가 둔다.
LEGACY_ATS_NAV = False


@app.context_processor
def inject_legacy_ats():
    return {'legacy_ats_nav': LEGACY_ATS_NAV}


@app.context_processor
def inject_plan():
    if 'user_id' not in session:
        return {'tenant_plan': None, 'plan_label': '', 'plan_features': _ENTERPRISE_FEATURES}
    plan = _current_plan()
    return {
        'tenant_plan':   plan,
        'plan_label':    PLAN_LABELS.get(plan, plan),
        'plan_features': PLAN_FEATURES.get(plan, _ENTERPRISE_FEATURES),
    }


@app.before_request
def csrf_protect():
    if request.method != 'POST':
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    sent = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
    saved = session.get('csrf_token', '')
    if not saved or not sent or not hmac.compare_digest(saved, sent):
        app.logger.warning(f'CSRF 검증 실패 — {request.method} {request.path} (endpoint={request.endpoint})')
        # 로그인 화면은 오래 열어두거나 뒤로 가서 다시 누르는 일이 잦다.
        # 그때 '접근 권한이 없습니다'(403) 를 보여줄 이유가 없다 — 이미 들어와 있으면
        # 대시보드로, 아니면 새 표를 받은 로그인 화면으로 돌려보낸다. POST 는 처리하지 않는다.
        if request.endpoint == 'login':
            if 'user_id' in session and not session.get('demo_mode'):
                return redirect(url_for('dashboard'))
            flash('로그인 화면이 오래돼 다시 입력해야 합니다.', 'info')
            return redirect(url_for('login'))
        abort(403, description='CSRF 토큰이 유효하지 않습니다. 페이지를 새로고침한 뒤 다시 시도해주세요.')


@app.before_request
def audit_exports():
    """Excel 내보내기 전수 감사 기록 (export_hub 화면 제외, 실제 다운로드만)."""
    ep = request.endpoint or ''
    if ep.startswith('export_') and ep != 'export_hub' and session.get('user_id'):
        log_audit('download', 'export', None, f'데이터 내보내기 ({request.path})')


# ── 미래발령 자동 적용 (서버 기동 후 첫 요청 시 1회 실행) ──────────
_scheduled_check_done = False

@app.before_request
def apply_scheduled_once():
    global _scheduled_check_done
    if _scheduled_check_done:
        return
    _scheduled_check_done = True
    try:
        today = date.today().isoformat()
        db = get_db()
        pending = db.execute(
            "SELECT * FROM personnel_actions WHERE status='approved' AND applied_at IS NULL AND effective_date <= ?",
            (today,)
        ).fetchall()
        for pa in pending:
            _do_apply_action(db, pa)
            db.execute(
                "UPDATE personnel_actions SET applied_at=CURRENT_TIMESTAMP WHERE id=?",
                (pa['id'],)
            )
            add_notification(
                pa['user_id'], 'info', 'action',
                '인사발령 자동 반영',
                f'발령일({pa["effective_date"]})이 도래하여 인사발령이 자동 반영되었습니다.',
                url_for('employee_detail', emp_id=pa['user_id'])
            )
        if pending:
            db.commit()
    except Exception:
        pass


def _do_apply_action(db, pa):
    """personnel_action 행을 실제 users/salary 테이블에 반영."""
    emp_id = pa['user_id']
    a_type = pa['action_type']
    to_val = pa['to_value']
    if a_type == 'dept_change':
        db.execute('UPDATE users SET department_id=? WHERE id=?', (to_val.split('|')[-1], emp_id))
    elif a_type == 'position_change':
        db.execute('UPDATE users SET position_id=? WHERE id=?', (to_val.split('|')[-1], emp_id))
    elif a_type == 'role_change':
        db.execute('UPDATE users SET role=? WHERE id=?', (to_val, emp_id))
    elif a_type == 'employment_type_change':
        db.execute('UPDATE users SET employment_type=? WHERE id=?', (to_val, emp_id))
    elif a_type == 'manager_change':
        new_id = to_val.split('|')[-1] or None
        db.execute('UPDATE users SET manager_id=? WHERE id=?', (new_id, emp_id))
    elif a_type == 'salary_change':
        new_sal = int(to_val)
        old = db.execute('SELECT 1 FROM employee_salary WHERE user_id=?', (emp_id,)).fetchone()
        if old:
            db.execute('UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?', (new_sal, emp_id))
        else:
            db.execute('INSERT INTO employee_salary (user_id, base_salary) VALUES (?,?)', (emp_id, new_sal))



def validate_password(pw):
    """
    비밀번호 정책 (Phase A-5, KISA 가이드 기준):
    8자 이상 + 영문/숫자/특수문자 중 2종 이상 조합.
    통과하면 None, 실패하면 에러 메시지 반환.
    """
    if len(pw) < 8:
        return '비밀번호는 8자 이상이어야 합니다.'
    kinds = sum([
        any(c.isalpha() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    ])
    if kinds < 2:
        return '비밀번호는 영문/숫자/특수문자 중 2종 이상을 조합해야 합니다.'
    return None


def log_audit(action, category, target_user_id=None, detail=''):
    """
    감사 로그 기록 (Phase A-3).
    민감 데이터(급여/성과/개인정보/문서) 열람·변경, 내보내기, 인증 이벤트를 남긴다.
    실패해도 본 요청을 막지 않는다 (best-effort).
    """
    try:
        db = get_db()
        db.execute(
            'INSERT INTO audit_logs (actor_id, actor_name, actor_role, action, category, target_user_id, detail, ip) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (session.get('user_id'), session.get('user_name'), session.get('user_role'),
             action, category, target_user_id, detail,
             request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip())
        )
        db.commit()
    except Exception as e:
        app.logger.error(f'audit log failed: {e}')


def add_notification(user_id, n_type, category, title, content, link=None):
    db = get_db()
    db.execute(
        'INSERT INTO notifications (user_id, type, category, title, content, link) '
        'VALUES (?,?,?,?,?,?)',
        (user_id, n_type, category, title, content, link)
    )
    db.commit()


@app.context_processor
def inject_user_features():
    uid = session.get('user_id')
    if not uid:
        return {'unread_notifications': 0, 'today': date.today().isoformat()}

    db = get_db()
    unread_row = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0', (uid,)
    ).fetchone()
    unread_notifications = unread_row[0] if unread_row else 0

    return {
        'unread_notifications': unread_notifications,
        'today': date.today().isoformat()
    }


@app.context_processor
def inject_sub_state():
    return {'sub_state': None}


@app.context_processor
def inject_peer_enabled():
    """활성 평가 주기의 다면평가 포함 여부 — 사이드바 메뉴 게이팅용 (v1.1.0)"""
    if not session.get('user_id'):
        return {'peer_enabled': True}
    try:
        row = get_db().execute(
            "SELECT include_peer FROM performance_cycles WHERE status='active' "
            "ORDER BY start_date DESC LIMIT 1"
        ).fetchone()
        # 활성 주기가 없으면 과거 데이터 열람을 위해 메뉴 유지
        return {'peer_enabled': (row is None) or bool(row['include_peer'])}
    except Exception:
        return {'peer_enabled': True}


# ── 성과 부가 기능 스위치 ─────────────────────────────────────────
# 성과의 중심은 대상자·다면평가 배정·캘리브레이션 세팅. 아래 기능은 회사 설정에서 켤 때만 보인다.
PERF_ADDONS = {
    'one_on_one': ('use_one_on_one', '1:1 면담', '매니저·팀원 정기 면담 일정·안건·액션 기록',
                   {'one_on_ones', 'one_on_one'}),
    'feedback': ('use_feedback', '피드백', '수시 칭찬·개선 제안·동료 의견 요청',
                 {'feedback_home', 'feedback_give', 'feedback_request_new', 'feedback_request_respond'}),
    'goal_alignment': ('use_goal_alignment', '목표 정렬', '전사·부서 목표를 만들고 개인 목표를 연결',
                       {'goal_alignment', 'org_goal_new', 'org_goal_edit', 'performance_goal_align'}),
    'succession': ('use_succession', '승계 계획', '핵심 포지션별 후계자·준비도 관리',
                   {'succession', 'export_succession'}),
}
_ADDON_BY_ENDPOINT = {ep: k for k, v in PERF_ADDONS.items() for ep in v[3]}

# 평가 명단에서 신입을 빼는 기준 (주기 종료일 기준 근속 개월)
PERF_MIN_MONTHS_CHOICES = [0, 1, 3, 6]

# 다면평가에서 한 사람이 받을 평가자 수 (최소 / 최대)
PEER_MIN_CHOICES = [2, 3, 4, 5]
PEER_MAX_CHOICES = [3, 4, 5, 6, 8]


def perf_addons(db=None):
    """켜진 부가 기능 {key: bool} — 컬럼이 없던 옛 DB는 끔으로 본다. 요청당 1회 조회."""
    if 'perf_addons' in g:
        return g.perf_addons
    try:
        row = (db or get_db()).execute('SELECT * FROM company_config WHERE id=1').fetchone()
        cfg = dict(row) if row else {}
    except Exception:
        cfg = {}
    out = {k: bool(cfg.get(v[0])) for k, v in PERF_ADDONS.items()}
    cp = cfg.get('copilot_enabled')
    out['copilot'] = True if cp is None else bool(cp)
    g.perf_addons = out
    return out


def save_perf_addons(db, f):
    """초기 설정·회사 설정 공통 — 스위치 섹션이 함께 제출될 때만 저장."""
    if 'addon_section' not in f:
        return
    for col, *_ in PERF_ADDONS.values():
        db.execute(f'UPDATE company_config SET {col}=? WHERE id=1', (1 if f.get(col) else 0,))
    try:
        mm = int(f.get('perf_min_months', 3))
    except (TypeError, ValueError):
        mm = 3
    if mm not in PERF_MIN_MONTHS_CHOICES:
        mm = 3
    db.execute('UPDATE company_config SET perf_min_months=? WHERE id=1', (mm,))
    try:
        lo = int(f.get('peer_min', 3))
        hi = int(f.get('peer_max', 5))
    except (TypeError, ValueError):
        lo, hi = 3, 5
    if lo not in PEER_MIN_CHOICES:
        lo = 3
    if hi not in PEER_MAX_CHOICES:
        hi = 5
    if hi < lo:
        hi = lo
    db.execute('UPDATE company_config SET peer_min=?, peer_max=? WHERE id=1', (lo, hi))
    g.pop('perf_addons', None)


@app.context_processor
def inject_perf_addons():
    if not session.get('user_id'):
        return {'addon': {k: False for k in list(PERF_ADDONS) + ['copilot']}, 'perf_addon_defs': PERF_ADDONS}
    return {'addon': perf_addons(), 'perf_addon_defs': PERF_ADDONS}


@app.before_request
def perf_addon_gate():
    key = _ADDON_BY_ENDPOINT.get(request.endpoint or '')
    if not key or not session.get('user_id') or perf_addons().get(key):
        return
    flash(f"{PERF_ADDONS[key][1]} 기능이 꺼져 있습니다 · 회사 설정 > 성과 부가 기능에서 켤 수 있습니다", 'warning')
    return redirect(url_for('performance'))


def _demo_write_blocked():
    """데모 모드(체험하기)에서는 role과 무관하게 모든 쓰기 요청을 차단한다."""
    if session.get('demo_mode') and request.method == 'POST':
        flash('데모 체험 모드에서는 저장·수정·삭제가 제한됩니다.', 'error')
        return True
    return False

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if _demo_write_blocked():
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') != 'admin':
            abort(403)
        if _demo_write_blocked():
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def manager_or_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') not in ('admin', 'manager'):
            abort(403)
        if _demo_write_blocked():
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def recruiter_or_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') not in ('admin', 'recruiter'):
            abort(403)
        if _demo_write_blocked():
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

# 옛 채용 요청서 주소(/recruit/requisitions/*)로 들어오면 승격된 새 주소로 넘긴다.
# 북마크와 예전 메일 링크가 깨지지 않게 하려는 것뿐, 화면은 하나다.
@app.route('/recruit/requisitions')
def _legacy_req_list():
    return redirect(url_for('requisition_list'), code=301)


@app.route('/recruit/requisitions/new')
def _legacy_req_new():
    return redirect(url_for('requisition_new'), code=301)


@app.route('/recruit/requisitions/<int:req_id>')
def _legacy_req_detail(req_id):
    return redirect(url_for('requisition_detail', req_id=req_id), code=301)


def retired_ats(f):
    """옛 내장 ATS(/recruit/* 공고·파이프라인·지원자·오퍼) 잠금.

    2026-09-04 T6 — 채용은 Hire 한 곳에서만 한다. 화면은 열리지 않고(410),
    대신 지금 그 일을 하는 곳(요청서·자리 대장·입사 예정자·Hire)으로 안내한다.
    코드와 데이터는 지우지 않는다 — 되살릴 일이 생기면 이 데코레이터만 떼면 된다.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        try:
            hire_url = _hire_config()['url']
        except Exception:
            hire_url = ''
        return render_template('errors/retired_ats.html', hire_url=hire_url), 410
    return decorated


def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'superadmin_id' not in session:
            return redirect(url_for('saas_login'))
        return f(*args, **kwargs)
    return decorated


# ── Notifications ──────────────────────────────────────────
@app.route('/notifications')
@login_required
def notifications():
    db  = get_db()
    uid = session['user_id']
    
    # 읽음 처리 (페이지 접속 시 모든 알림 읽음 처리 혹은 개별 처리 선택 가능)
    db.execute('UPDATE notifications SET is_read=1 WHERE user_id=?', (uid,))
    db.commit()
    
    notifs = db.execute(
        'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
        (uid,)
    ).fetchall()
    
    return render_template('notifications.html', notifications=notifs, active_page='notifications')


# ── Helper ──────────────────────────────────────────────────
def build_dept_tree(depts, parent_id=None):
    nodes = []
    for d in depts:
        if d['parent_id'] == parent_id:
            node = dict(d)
            node['children'] = build_dept_tree(depts, d['id'])
            # 하위 전체 인원 합산 (팀 단위 인원을 상위 부서에 집계)
            node['total_count'] = node['member_count'] + sum(
                c['total_count'] for c in node['children']
            )
            nodes.append(node)
    return nodes


def build_reporting_tree(users, manager_id=None, seen=None):
    if seen is None:
        seen = set()
    nodes = []
    for user in users:
        if user['manager_id'] == manager_id and user['id'] not in seen:
            node = dict(user)
            branch_seen = seen | {user['id']}
            node['children'] = build_reporting_tree(users, user['id'], branch_seen)
            node['report_count'] = len(node['children'])
            node['total_reports'] = node['report_count'] + sum(
                c['total_reports'] for c in node['children']
            )
            nodes.append(node)
    return nodes


# ── Auth Routes ──────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        if session.get('demo_mode'):
            session.clear()
        else:
            return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not email or not password:
            error = '이메일과 비밀번호를 입력해주세요.'
        else:
            # ── master.db에서 테넌트 조회 ────────────────────────
            tenant_row = get_tenant_by_email(email)
            if tenant_row:
                tenant_id = tenant_row['id']
            else:
                # master.db에 없으면 데모 테넌트(1)에서 시도
                tenant_id = 1

            # ── 해당 테넌트 DB에서 인증 ──────────────────────────
            db_path = get_tenant_db_path(tenant_id)
            _db = sqlite3.connect(db_path)
            _db.row_factory = sqlite3.Row
            user = _db.execute(
                'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
                'FROM users u '
                'LEFT JOIN departments d ON u.department_id = d.id '
                'LEFT JOIN positions   p ON u.position_id   = p.id '
                'WHERE u.email = ? AND u.status = ?',
                (email, 'active')
            ).fetchone()
            _db.close()

            if user and check_password_hash(user['password_hash'], password):
                sso_next = session.get('sso_next') or ''
                session.clear()
                session['tenant_id']  = tenant_id
                session['user_id']    = user['id']
                session['user_name']  = user['name']
                session['user_role']  = user['role']
                session['user_email'] = user['email']
                session['dept_name']  = user['dept_name'] or ''
                session['pos_name']   = user['pos_name']  or ''
                session['dept_id']    = user['department_id'] or 0
                session['onboarded']  = 1 if user['onboarded'] else 0
                session['show_tour']  = not bool(user['tour_completed'])

                # ── 구독 만료 체크 (guest/demo 제외) ────────────
                if user['role'] != 'guest' and tenant_row:
                    sub_status   = tenant_row['sub_status']
                    trial_ends   = tenant_row['trial_ends_at']
                    period_end   = tenant_row['current_period_end']
                    today_str    = date.today().isoformat()
                    if sub_status == 'trialing' and trial_ends and today_str > trial_ends:
                        session['subscription_expired'] = True
                    elif sub_status in ('past_due', 'cancelled'):
                        session['subscription_expired'] = True
                    else:
                        session.pop('subscription_expired', None)

                log_audit('login', 'auth', user['id'], f'로그인 성공 ({email})')
                if sso_next.startswith('/sso/hire?'):
                    return redirect(sso_next)
                return redirect(url_for('dashboard'))
            log_audit('login_failed', 'auth', None, f'로그인 실패 ({email})')
            error = '이메일 또는 비밀번호가 올바르지 않습니다.'
    return render_template('login.html', error=error)

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/demo')
def demo_login():
    """랜딩 '체험하기' 버튼 — 데모 테넌트 admin 계정으로 즉시 로그인 (전체 기능 열람 가능, 저장/수정/삭제는 차단)"""
    if 'user_id' in session:
        session.clear()

    tenant_id = 1  # 데모 테넌트
    db_path = get_tenant_db_path(tenant_id)
    _db = sqlite3.connect(db_path)
    _db.row_factory = sqlite3.Row
    user = _db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions   p ON u.position_id   = p.id '
        "WHERE u.role = 'admin' AND u.status = 'active' "
        'ORDER BY u.id LIMIT 1',
        ()
    ).fetchone()
    _db.close()

    if not user:
        flash('데모 계정을 찾을 수 없습니다. 관리자에게 문의하세요.', 'error')
        return redirect(url_for('login'))

    session.clear()
    session['tenant_id']  = tenant_id
    session['user_id']    = user['id']
    session['user_name']  = user['name']
    session['user_role']  = user['role']
    session['user_email'] = user['email']
    session['dept_name']  = user['dept_name'] or ''
    session['pos_name']   = user['pos_name']  or ''
    session['dept_id']    = user['department_id'] or 0
    session['onboarded']  = 1
    session['demo_mode']  = True
    session['show_tour']  = True
    return redirect(url_for('dashboard'))


@app.route('/tour/complete', methods=['POST'])
def tour_complete():
    """온보딩 투어 완료/스킵 — 데모 세션은 세션 플래그만, 실사용자는 DB에도 영구 저장"""
    session['show_tour'] = False
    if 'user_id' in session and not session.get('demo_mode'):
        db = get_db()
        db.execute('UPDATE users SET tour_completed = 1 WHERE id = ?', (session['user_id'],))
        db.commit()
    return ('', 204)


# ── SaaS 관리 (운영자 전용, 테넌트와 무관한 별도 로그인) ─────────────
@app.route('/saas/login', methods=['GET', 'POST'])
def saas_login():
    if 'superadmin_id' in session:
        return redirect(url_for('saas_dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        admin = get_superadmin_by_username(username)
        if admin and check_password_hash(admin['password_hash'], password):
            session.clear()
            session['superadmin_id'] = admin['id']
            session['superadmin_name'] = admin['username']
            return redirect(url_for('saas_dashboard'))
        error = '아이디 또는 비밀번호가 올바르지 않습니다.'
    return render_template('saas/login.html', error=error)


@app.route('/saas/logout', methods=['GET', 'POST'])
def saas_logout():
    session.pop('superadmin_id', None)
    session.pop('superadmin_name', None)
    return redirect(url_for('saas_login'))


@app.route('/saas')
@superadmin_required
def saas_dashboard():
    tenants = list_tenants_with_state()
    today = date.today().isoformat()
    return render_template('saas/dashboard.html', tenants=tenants, today=today)


@app.route('/saas/tenants/<int:tenant_id>')
@superadmin_required
def saas_tenant_detail(tenant_id):
    tenant = get_tenant(tenant_id)
    if not tenant:
        abort(404)
    db_path = get_tenant_db_path(tenant_id)
    _db = sqlite3.connect(db_path)
    _db.row_factory = sqlite3.Row
    headcount = _db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND role NOT IN ('guest')"
    ).fetchone()[0]
    users = _db.execute(
        "SELECT id, name, email, role, status FROM users WHERE role NOT IN ('guest') ORDER BY id LIMIT 200"
    ).fetchall()
    _db.close()
    billing_conn = get_master_db()
    billing_logs = billing_conn.execute(
        'SELECT * FROM billing_logs WHERE tenant_id=? ORDER BY created_at DESC LIMIT 24',
        (tenant_id,)
    ).fetchall()
    billing_conn.close()
    return render_template(
        'saas/tenant_detail.html',
        tenant=tenant, headcount=headcount, users=users, billing_logs=billing_logs,
        plan_prices=PLAN_PRICES, plan_labels=PLAN_LABELS,
        tenant_plan=get_tenant_plan(tenant_id),
    )


@app.route('/saas/tenants/<int:tenant_id>/plan', methods=['POST'])
@superadmin_required
def saas_tenant_plan(tenant_id):
    new_plan = request.form.get('plan')
    if new_plan not in PLAN_PRICES:
        flash('올바르지 않은 요금제입니다.', 'error')
        return redirect(url_for('saas_tenant_detail', tenant_id=tenant_id))
    set_tenant_plan(tenant_id, new_plan)
    flash(f'요금제가 "{PLAN_LABELS[new_plan]}"(으)로 변경되었습니다.', 'success')
    return redirect(url_for('saas_tenant_detail', tenant_id=tenant_id))


@app.route('/saas/tenants/<int:tenant_id>/status', methods=['POST'])
@superadmin_required
def saas_tenant_status(tenant_id):
    new_status = request.form.get('status')
    if new_status not in ('trial', 'active', 'suspended', 'cancelled'):
        flash('올바르지 않은 상태값입니다.', 'error')
        return redirect(url_for('saas_tenant_detail', tenant_id=tenant_id))
    set_tenant_status(tenant_id, new_status)
    flash(f'테넌트 상태가 "{new_status}"로 변경되었습니다.', 'success')
    return redirect(url_for('saas_tenant_detail', tenant_id=tenant_id))


@app.route('/admin/setup', methods=['GET', 'POST'])
@login_required
def admin_setup():
    from datetime import datetime as dt
    db = get_db()

    if request.method == 'POST':
        if session.get('user_role') != 'admin':
            flash('설정 저장은 관리자만 가능합니다.', 'error')
            return redirect(url_for('admin_setup'))
        s = request.form
        was_done = bool(get_company_config().get("setup_completed"))

        # ── Step 1: 회사 기본정보 ─────────────────────────────
        for key in ['name', 'reg_no', 'ceo', 'address', 'tel', 'founded', 'industry', 'employee_count']:
            val = s.get(key, '').strip()
            db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, val))

        # ── Step 2: 근무 제도 ─────────────────────────────────
        work_system         = s.get('work_system', 'standard')
        work_start          = s.get('work_start', '09:00')
        work_end            = s.get('work_end',   '18:00')
        lunch_start         = s.get('lunch_start','12:00')
        lunch_end           = s.get('lunch_end',  '13:00')
        core_start          = s.get('core_start', '10:00')
        core_end            = s.get('core_end',   '16:00')
        flex_settle_months  = int(s.get('flex_settle_months', 1))
        elastic_unit        = s.get('elastic_unit', '2weeks')
        remote_allowed      = 1 if s.get('remote_allowed') else 0
        remote_max_days_week = int(s.get('remote_max_days_week', 3))

        # ── Step 3: 휴가 정책 ─────────────────────────────────
        leave_policy        = s.get('leave_policy', 'legal')
        leave_extra_days    = int(s.get('leave_extra_days', 0) or 0)
        allow_half_day      = 1 if s.get('allow_half_day') else 0
        allow_quarter_day   = 1 if s.get('allow_quarter_day') else 0
        sick_policy         = s.get('sick_policy', 'annual')
        sick_days_year      = int(s.get('sick_days_year', 0) or 0)

        # ── Step 4: 급여 기본 설정 ───────────────────────────
        pay_day                     = int(s.get('pay_day', 25) or 25)
        default_meal_allowance      = int(s.get('default_meal_allowance', 200000) or 0)
        default_transport_allowance = int(s.get('default_transport_allowance', 100000) or 0)

        # ── Step 5: 복리후생 활성화 ─────────────────────────
        selected_benefits = set(s.getlist('benefits'))
        for key, meta in BENEFIT_CATALOG.items():
            enabled = 1 if key in selected_benefits else 0
            db.execute('''
                INSERT INTO benefit_configs
                    (key, enabled, amount, payment_type, annual_limit, platform)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    enabled=excluded.enabled
            ''', (
                key, enabled,
                meta.get('default_amount', 0),
                meta.get('payment_type', 'monthly_fixed'),
                meta.get('annual_limit'),
                None,
            ))

        # ── Step 6: 성과관리 ─────────────────────────────────
        perf_cycle       = s.get('perf_cycle', 'semiannual')
        use_peer_review  = 1 if s.get('use_peer_review') else 0
        use_self_review  = 1 if s.get('use_self_review') else 0
        grade_system     = s.get('grade_system', 'SABCD')

        db.execute('''
            INSERT INTO company_config (
                id, work_system, work_start, work_end, lunch_start, lunch_end,
                core_start, core_end, flex_settle_months, elastic_unit,
                remote_allowed, remote_max_days_week,
                leave_policy, leave_extra_days, allow_half_day, allow_quarter_day,
                sick_policy, sick_days_year,
                pay_day, default_meal_allowance, default_transport_allowance,
                perf_cycle, use_peer_review, use_self_review, grade_system,
                setup_completed, setup_step, updated_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,9,?)
            ON CONFLICT(id) DO UPDATE SET
                work_system=excluded.work_system,
                work_start=excluded.work_start, work_end=excluded.work_end,
                lunch_start=excluded.lunch_start, lunch_end=excluded.lunch_end,
                core_start=excluded.core_start, core_end=excluded.core_end,
                flex_settle_months=excluded.flex_settle_months,
                elastic_unit=excluded.elastic_unit,
                remote_allowed=excluded.remote_allowed,
                remote_max_days_week=excluded.remote_max_days_week,
                leave_policy=excluded.leave_policy,
                leave_extra_days=excluded.leave_extra_days,
                allow_half_day=excluded.allow_half_day,
                allow_quarter_day=excluded.allow_quarter_day,
                sick_policy=excluded.sick_policy, sick_days_year=excluded.sick_days_year,
                pay_day=excluded.pay_day,
                default_meal_allowance=excluded.default_meal_allowance,
                default_transport_allowance=excluded.default_transport_allowance,
                perf_cycle=excluded.perf_cycle,
                use_peer_review=excluded.use_peer_review,
                use_self_review=excluded.use_self_review,
                grade_system=excluded.grade_system,
                setup_completed=1, setup_step=9,
                updated_at=excluded.updated_at
        ''', (
            work_system, work_start, work_end, lunch_start, lunch_end,
            core_start, core_end, flex_settle_months, elastic_unit,
            remote_allowed, remote_max_days_week,
            leave_policy, leave_extra_days, allow_half_day, allow_quarter_day,
            sick_policy, sick_days_year,
            pay_day, default_meal_allowance, default_transport_allowance,
            perf_cycle, use_peer_review, use_self_review, grade_system,
            dt.now().isoformat()
        ))
        # ── 기본 Work Schedule 자동 생성 ───────────────────────────
        stype_map = {
            'standard':    ('fixed',         '기본 고정근무',   '09:00', '18:00', '10:00', '16:00', 480),
            'flex':        ('flex',          '선택근로제',      '08:00', '20:00', core_start, core_end, 480),
            'elastic':     ('fixed',         '탄력근로제',      work_start, work_end, '10:00', '16:00', 480),
            'autonomous':  ('discretionary', '재량근로제',      None,    None,    None,    None,    480),
        }
        ws_vals = stype_map.get(work_system, stype_map['standard'])
        existing_default = db.execute('SELECT id FROM work_schedules WHERE is_default=1').fetchone()
        if not existing_default:
            db.execute(
                'INSERT INTO work_schedules '
                '(name, schedule_type, work_start, work_end, core_start, core_end, daily_hours_min, is_default) '
                'VALUES (?,?,?,?,?,?,?,1)',
                (ws_vals[1], ws_vals[0], ws_vals[2], ws_vals[3], ws_vals[4], ws_vals[5], ws_vals[6])
            )
        else:
            db.execute(
                'UPDATE work_schedules SET name=?, schedule_type=?, work_start=?, work_end=?, '
                'core_start=?, core_end=?, daily_hours_min=? WHERE is_default=1',
                (ws_vals[1], ws_vals[0], ws_vals[2], ws_vals[3], ws_vals[4], ws_vals[5], ws_vals[6])
            )
        notes = _setup_save_org(db, s)
        gd_note = save_grade_dist(db, s)
        save_perf_culture(db, s)
        save_default_form_weights(db, s)
        save_perf_addons(db, s)
        if gd_note:
            notes.insert(0, gd_note)

        db.commit()
        session['onboarded'] = 1
        if notes:
            flash('저장 완료 · 확인 필요: ' + ' / '.join(notes[:5]), 'warning')
        else:
            flash('초기 설정 저장 완료' if was_done else '초기 설정 완료', 'success')
        return redirect(url_for('admin_setup') if was_done else url_for('dashboard'))

    return render_template('admin/setup.html', **_setup_context(db))


def _setup_context(db):
    """초기 설정 화면 — 회사 정책 + 조직·정원·직급·결재선·Hire 현재값."""
    from datetime import datetime as dt
    fy = dt.now().year
    config  = get_company_config()
    company = get_company_info()

    depts = [dict(r) for r in db.execute(
        'SELECT d.id, d.name, d.parent_id, d.dept_type, u.name AS leader_name '
        'FROM departments d LEFT JOIN users u ON d.leader_id=u.id ORDER BY d.id').fetchall()]
    active = {r[0]: r[1] for r in db.execute(
        "SELECT department_id, COUNT(*) FROM users WHERE status='active' GROUP BY department_id").fetchall()}
    target = {r[0]: r[1] for r in db.execute(
        'SELECT department_id, target_count FROM department_headcount WHERE fiscal_year=?', (fy,)).fetchall()}
    by_id = {d['id']: d for d in depts}
    kids = {}
    for d in depts:
        kids.setdefault(d['parent_id'] if d['parent_id'] in by_id else None, []).append(d)
    type_rank = {k: i for i, (k, *_r) in enumerate(DEPT_TYPES)}
    dept_rows = []

    def walk(pid, depth):
        for d in sorted(kids.get(pid, []), key=lambda x: (type_rank.get(x['dept_type'], 9), x['id'])):
            d.update(depth=depth, active=active.get(d['id'], 0), target=target.get(d['id']),
                     parent_name=by_id[d['parent_id']]['name'] if d['parent_id'] in by_id else None)
            dept_rows.append(d)
            walk(d['id'], depth + 1)
    walk(None, 0)

    positions = db.execute(
        "SELECT p.id, p.name, p.level, (SELECT COUNT(*) FROM users u WHERE u.position_id=p.id "
        "AND u.status='active') AS cnt FROM positions p ORDER BY p.level, p.id").fetchall()

    flows = {ht: _flow_template(db, ht) for ht in REQUISITION_HIRE_TYPE_LABEL}
    people_map = {r['id']: r['name'] for r in db.execute("SELECT id, name FROM users").fetchall()}
    hire = _hire_config()
    benefit_enabled = {r['key']: bool(r['enabled']) for r in db.execute(
        'SELECT key, enabled FROM benefit_configs').fetchall()}
    active_total = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]

    readiness = {
        'company': bool(company.get('name') and company.get('reg_no')),
        'org':     bool(dept_rows) and bool(target),
        'grade':   bool(positions),
        'hiring':  bool(hire.get('url') and hire.get('token')),
        'work':    bool(config.get('setup_completed')),
        'leave':   bool(config.get('setup_completed')),
        'pay':     bool(config.get('setup_completed')),
        'benefit': bool(benefit_enabled),
        'perf':    bool(config.get('setup_completed')),
    }
    return dict(
        config=config, company=company, benefit_catalog=BENEFIT_CATALOG,
        benefit_enabled=benefit_enabled, fiscal_year=fy,
        dept_rows=dept_rows, dept_types=DEPT_TYPES, dept_type_label=DEPT_TYPE_LABEL,
        hc_total=sum(v for k, v in target.items() if k in by_id),
        active_in_depts=sum(v for k, v in active.items() if k in by_id),
        positions=positions, flows=flows, people_map=people_map,
        hire_type_labels=REQUISITION_HIRE_TYPE_LABEL, hire_type_hints=REQUISITION_HIRE_TYPE_HINT,
        role_labels=FLOW_ROLE_LABEL,
        hire_url=hire.get('url') or '', hire_token_set=bool(hire.get('token')),
        hire_connected=bool(hire.get('url') and hire.get('token')),
        active_total=active_total, readiness=readiness,
    )


def _setup_save_org(db, f):
    """초기 설정 저장 — 조직·정원·직급·결재선·Hire. 이미 있는 항목은 중복 생성하지 않는다."""
    from datetime import datetime as dt
    fy = dt.now().year
    notes = []

    def upsert_hc(dept_id, raw):
        raw = (raw or '').strip()
        if raw == '':
            return
        try:
            n = max(0, int(raw))
        except ValueError:
            return
        db.execute('INSERT INTO department_headcount (department_id, target_count, fiscal_year) VALUES (?,?,?) '
                   'ON CONFLICT(department_id, fiscal_year) DO UPDATE SET target_count=excluded.target_count',
                   (dept_id, n, fy))

    # 기존 조직 정원
    for key in f.keys():
        if key.startswith('hc_') and key[3:].isdigit():
            upsert_hc(int(key[3:]), f.get(key))

    # 조직 추가 — 입력 순서대로, 상위 조직은 이름으로 연결
    names, types, parents, hcs = (f.getlist('nd_name'), f.getlist('nd_type'),
                                  f.getlist('nd_parent'), f.getlist('nd_hc'))
    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        dtype = types[i] if i < len(types) and types[i] in DEPT_TYPE_LABEL else 'team'
        pname = (parents[i] if i < len(parents) else '').strip()
        parent = None
        if pname:
            parent = db.execute('SELECT id, dept_type FROM departments WHERE name=? ORDER BY id DESC LIMIT 1',
                                (pname,)).fetchone()
            if not parent:
                notes.append('%s — 상위 조직 "%s" 없음' % (name, pname))
                continue
            if parent['dept_type'] not in DEPT_TYPE_PARENT_ALLOWED.get(dtype, []):
                notes.append('%s(%s) — %s(%s) 하위 불가' % (
                    name, DEPT_TYPE_LABEL[dtype], pname, DEPT_TYPE_LABEL.get(parent['dept_type'], '')))
                continue
        pid = parent['id'] if parent else None
        row = db.execute('SELECT id FROM departments WHERE name=? AND parent_id IS ?', (name, pid)).fetchone()
        if row:
            did = row['id']
        else:
            did = db.execute('INSERT INTO departments (name, parent_id, dept_type) VALUES (?,?,?)',
                             (name, pid, dtype)).lastrowid
        upsert_hc(did, hcs[i] if i < len(hcs) else '')

    # 직급 추가
    for name, lv in zip(f.getlist('np_name'), f.getlist('np_level')):
        name = name.strip()
        if not name or db.execute('SELECT 1 FROM positions WHERE name=?', (name,)).fetchone():
            continue
        try:
            level = max(1, int(lv))
        except (TypeError, ValueError):
            level = (db.execute('SELECT COALESCE(MAX(level),0) FROM positions').fetchone()[0] or 0) + 1
        db.execute('INSERT INTO positions (name, level) VALUES (?,?)', (name, level))

    # 결재선 — 단계 구성은 유지, 단계명·처리 기한만 반영
    for ht in REQUISITION_HIRE_TYPE_LABEL:
        steps = _flow_template(db, ht)
        stored = db.execute('SELECT 1 FROM requisition_flow_steps WHERE hire_type=? LIMIT 1', (ht,)).fetchone()
        for s in steps:
            label = (f.get('fl_%s_%d_label' % (ht, s['step_no'])) or '').strip() or s['label']
            try:
                sla = max(1, int(f.get('fl_%s_%d_sla' % (ht, s['step_no'])) or s['sla_days']))
            except ValueError:
                sla = s['sla_days']
            if stored:
                db.execute('UPDATE requisition_flow_steps SET label=?, sla_days=? WHERE hire_type=? AND step_no=?',
                           (label, sla, ht, s['step_no']))
            else:
                db.execute('INSERT INTO requisition_flow_steps (hire_type, step_no, role_kind, exec_key, user_id, label, sla_days) '
                           'VALUES (?,?,?,?,?,?,?)', (ht, s['step_no'], s['role_kind'], s.get('exec_key'),
                                                      s.get('user_id'), label, sla))

    # Hire 연동 — 토큰은 입력했을 때만 교체
    if 'hire_url' in f:
        url = f.get('hire_url', '').strip().rstrip('/')
        if url and not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('hire_url', url))
    token = f.get('hire_token', '').strip()
    if token:
        db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('hire_token', token))
    return notes


@app.route('/admin/integrations', methods=['GET', 'POST'])
@admin_required
def admin_integrations():
    db = get_db()
    if request.method == 'POST':
        for svc in ('slack', 'jira', 'confluence'):
            enabled = 1 if request.form.get(f'enable_{svc}') else 0
            db.execute(
                "UPDATE integration_configs SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE service=?",
                (enabled, svc)
            )
        db.commit()
        flash('연동 설정이 저장되었습니다.', 'success')
        return redirect(url_for('admin_integrations'))

    configs = {r['service']: dict(r) for r in db.execute("SELECT * FROM integration_configs").fetchall()}
    logs    = db.execute(
        "SELECT * FROM integration_logs ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    # 환경변수 상태 확인
    import os as _os
    env_status = {
        'slack_bot':    bool(_os.environ.get('SLACK_BOT_TOKEN')),
        'slack_admin':  bool(_os.environ.get('SLACK_ADMIN_TOKEN')),
        'jira_token':   bool(_os.environ.get('JIRA_API_TOKEN')),
        'jira_url':     bool(_os.environ.get('JIRA_BASE_URL')),
        'confluence':   bool(_os.environ.get('CONFLUENCE_BASE_URL')),
    }
    return render_template('admin/integrations.html',
                           configs=configs, logs=logs,
                           env_status=env_status, active_page='integrations')


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    from datetime import datetime as dt
    db = get_db()

    if request.method == 'POST':
        s = request.form
        for key in ['name', 'reg_no', 'ceo', 'address', 'tel', 'founded', 'industry', 'employee_count']:
            val = s.get(key, '').strip()
            db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, val))

        db.execute('''
            UPDATE company_config SET
                work_system=?, work_start=?, work_end=?, lunch_start=?, lunch_end=?,
                core_start=?, core_end=?, flex_settle_months=?, elastic_unit=?,
                remote_allowed=?, remote_max_days_week=?,
                leave_policy=?, leave_extra_days=?, allow_half_day=?, allow_quarter_day=?,
                sick_policy=?, sick_days_year=?,
                pay_day=?, default_meal_allowance=?, default_transport_allowance=?,
                perf_cycle=?, use_peer_review=?, use_self_review=?, grade_system=?,
                updated_at=?
            WHERE id=1
        ''', (
            s.get('work_system','standard'),
            s.get('work_start','09:00'), s.get('work_end','18:00'),
            s.get('lunch_start','12:00'), s.get('lunch_end','13:00'),
            s.get('core_start','10:00'), s.get('core_end','16:00'),
            int(s.get('flex_settle_months',1) or 1),
            s.get('elastic_unit','2weeks'),
            1 if s.get('remote_allowed') else 0,
            int(s.get('remote_max_days_week',3) or 3),
            s.get('leave_policy','legal'),
            int(s.get('leave_extra_days',0) or 0),
            1 if s.get('allow_half_day') else 0,
            1 if s.get('allow_quarter_day') else 0,
            s.get('sick_policy','annual'),
            int(s.get('sick_days_year',0) or 0),
            int(s.get('pay_day',25) or 25),
            int(s.get('default_meal_allowance',200000) or 0),
            int(s.get('default_transport_allowance',100000) or 0),
            s.get('perf_cycle','semiannual'),
            1 if s.get('use_peer_review') else 0,
            1 if s.get('use_self_review') else 0,
            s.get('grade_system','SABCD'),
            dt.now().isoformat()
        ))
        gd_note = save_grade_dist(db, s)
        save_perf_culture(db, s)
        save_default_form_weights(db, s)
        if 'copilot_section' in s:
            db.execute('UPDATE company_config SET copilot_enabled=? WHERE id=1', (1 if s.get('copilot_enabled') else 0,))
        save_perf_addons(db, s)
        db.commit()
        if gd_note:
            flash('설정 저장 · ' + gd_note, 'warning')
        else:
            flash('설정이 저장되었습니다.', 'success')
        return redirect(url_for('admin_settings'))

    config  = get_company_config()
    company = get_company_info()
    positions = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    approval_chains = {wf: get_approval_chain(db, wf) for wf in APPROVAL_WORKFLOWS}
    return render_template('admin/settings.html', config=config, company=company,
                           positions=positions,
                           position_presets=POSITION_PRESETS,
                           approval_workflows=APPROVAL_WORKFLOWS,
                           approval_chains=approval_chains,
                           copilot=copilot_view(), copilot_usage=_copilot_usage(db),
                           active_page='settings')


# ── 승인 체인 설정화 (Phase C-13 — 결재선 화면 편집) ────────────
APPROVAL_WORKFLOWS = {
    'leave': {
        'label': '휴가·근태 신청',
        'options': {
            'meta_default': '휴가 유형별 기본값 (연차=매니저 전결, 법정휴가=2단계)',
            'manager_only': '매니저 전결 — 모든 유형 1단계 승인',
            'manager_hr':   '매니저 검토 → HR 최종 승인 — 모든 유형 2단계',
        },
        'default': 'meta_default',
    },
    'certificate': {
        'label': '증명서 발급',
        'options': {
            'hr':   'HR 승인 후 발급',
            'auto': '신청 즉시 자동 발급 (승인 생략)',
        },
        'default': 'hr',
    },
    'personnel_action': {
        'label': '인사발령',
        'options': {
            'hr':   '기안 → HR 승인 시 반영',
            'auto': '관리자 전결 — 관리자가 기안하면 즉시 승인·반영',
        },
        'default': 'hr',
    },
    'overtime': {
        'label': '연장근로(OT) 승인',
        'options': {
            'manager_only': '매니저 전결 — 1단계 승인',
            'manager_hr':   '매니저 검토 → HR 최종 승인 — 2단계',
        },
        'default': 'manager_only',
    },
}


def get_approval_chain(db, workflow):
    """결재선 설정 조회 — 미설정 시 기본값."""
    try:
        row = db.execute('SELECT chain FROM approval_chains WHERE workflow=?', (workflow,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    default = APPROVAL_WORKFLOWS.get(workflow, {}).get('default', '')
    chain = row['chain'] if row else default
    if chain not in APPROVAL_WORKFLOWS.get(workflow, {}).get('options', {}):
        chain = default
    return chain


@app.route('/admin/approval-chains', methods=['POST'])
@admin_required
def admin_approval_chains():
    """결재선 저장 — 워크플로우별 승인 단계 설정."""
    db = get_db()
    changed = []
    for wf, meta in APPROVAL_WORKFLOWS.items():
        val = request.form.get(wf, '')
        if val in meta['options']:
            db.execute(
                'INSERT INTO approval_chains (workflow, chain, updated_at) VALUES (?,?,CURRENT_TIMESTAMP) '
                'ON CONFLICT(workflow) DO UPDATE SET chain=excluded.chain, updated_at=CURRENT_TIMESTAMP',
                (wf, val)
            )
            changed.append(f'{meta["label"]}={meta["options"][val]}')
    db.commit()
    log_audit('update', 'personal_info', None, '결재선 설정 변경 — ' + ' / '.join(changed))
    flash('결재선 설정이 저장되었습니다.', 'success')
    return redirect(url_for('admin_settings') + '?tab=approvals')


# ── 직급 체계 프리셋 (Phase C-12, saas_plan.md §3) ─────────────
POSITION_PRESETS = {
    'l_level': {
        'label': 'L-레벨형 (테크 스타트업)',
        'names': {1: 'L1 — Associate', 2: 'L2 — Junior', 3: 'L3 — Mid-Level',
                  4: 'L4 — Senior', 5: 'L5 — Staff', 6: 'L6 — Manager',
                  7: 'L7 — Senior Manager', 8: 'L8 — Director', 9: 'L9 — VP / Executive'},
    },
    'kr_title': {
        'label': '호칭형 (사원-대리-과장-차장-부장)',
        'names': {1: '사원', 2: '주임', 3: '대리', 4: '과장', 5: '차장',
                  6: '부장', 7: '이사', 8: '상무', 9: '부사장'},
    },
}


@app.route('/admin/positions/preset', methods=['POST'])
@admin_required
def admin_positions_preset():
    """직급 라벨 프리셋 일괄 적용 — 내부 레벨 체계는 유지, 이름만 변경."""
    db     = get_db()
    preset = request.form.get('preset', '')
    if preset not in POSITION_PRESETS:
        flash('올바른 프리셋을 선택하세요.', 'error')
        return redirect(url_for('admin_settings'))
    names = POSITION_PRESETS[preset]['names']
    updated = 0
    for row in db.execute('SELECT id, level FROM positions').fetchall():
        new_name = names.get(row['level'])
        if new_name:
            db.execute('UPDATE positions SET name=? WHERE id=?', (new_name, row['id']))
            updated += 1
    db.commit()
    log_audit('update', 'personal_info', None,
              f'직급 체계 프리셋 적용 — {POSITION_PRESETS[preset]["label"]} ({updated}개 직급)')
    flash(f'직급 이름이 "{POSITION_PRESETS[preset]["label"]}" 기준으로 변경되었습니다 ({updated}개). '
          '직원 데이터·급여 밴드는 그대로 유지됩니다.', 'success')
    return redirect(url_for('admin_settings'))


# ── Dashboard helpers ─────────────────────────────────────────
def _greeting():
    h = datetime.now().hour
    if h < 12:  return '좋은 아침이에요'
    if h < 17:  return '좋은 오후예요'
    return '수고 많으셨어요'

_WEEKDAY_KO = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']

def _today_label():
    d = date.today()
    return f"{d.month}월 {d.day}일 {_WEEKDAY_KO[d.weekday()]}"

def _tenure(hire_date_str):
    if not hire_date_str:
        return None
    try:
        hd = datetime.strptime(hire_date_str, '%Y-%m-%d').date()
        td = date.today()
        years  = td.year - hd.year - ((td.month, td.day) < (hd.month, hd.day))
        months = (td.month - hd.month) % 12
        return f'{years}y {months}m' if years else f'{months}m'
    except Exception:
        return None

# ── Dashboard ────────────────────────────────────────────────
def _admin_month_dues(db, cfg, payslip_count):
    """홈 '이번 달 기한': 급여일·법정 신고일·성과 사이클 마감을 날짜순으로."""
    import calendar as _cal
    t = date.today()
    last = _cal.monthrange(t.year, t.month)[1]
    def d(day):
        return date(t.year, t.month, max(1, min(day, last)))
    pay_day = int(cfg.get('pay_day', 25) or 25)
    prev_first = (t.replace(day=1) - timedelta(days=1)).replace(day=1)
    prev_hires = db.execute(
        "SELECT COUNT(*) FROM users WHERE hire_date>=? AND hire_date<?",
        (prev_first.isoformat(), t.replace(day=1).isoformat())).fetchone()[0]
    rows = [
        {'label': '원천세 신고·납부', 'sub': '전월 지급분', 'date': d(10), 'done': None},
        {'label': '4대보험 취득신고', 'sub': f'전월 입사 {prev_hires}명', 'date': d(15), 'done': None},
        {'label': '근태 마감', 'sub': '', 'date': d(pay_day - 7), 'done': None},
        {'label': '급여 확정', 'sub': '', 'date': d(pay_day - 3), 'done': payslip_count > 0},
        {'label': '급여 지급', 'sub': '', 'date': d(pay_day), 'done': None},
    ]
    month_start, month_end = t.replace(day=1).isoformat(), d(last).isoformat()
    for c in db.execute("SELECT name, goal_deadline, review_deadline FROM performance_cycles "
                        "WHERE status='active'").fetchall():
        for col, lab in (('goal_deadline', '목표 수립 마감'), ('review_deadline', '평가 마감')):
            v = c[col]
            if v and month_start <= v[:10] <= month_end:
                rows.append({'label': lab, 'sub': c['name'], 'date': date.fromisoformat(v[:10]), 'done': None})
    for r in rows:
        n = (r['date'] - t).days
        r['iso'] = r['date'].isoformat()
        if r['done']:
            r['state'], r['due_label'] = 'done', '완료'
        elif n < 0:
            r['state'], r['due_label'] = 'past', '경과'
        elif n == 0:
            r['state'], r['due_label'] = 'late', '오늘'
        else:
            r['state'], r['due_label'] = ('wait' if n <= 3 else ''), f'D-{n}'
    rows.sort(key=lambda r: r['date'])
    return rows


@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('user_role')
    uid  = session.get('user_id')
    db   = get_db()
    today      = date.today().isoformat()
    greet      = _greeting()
    today_str  = _today_label()
    first_name = (session.get('user_name') or '').split()[0]
    cfg = get_company_config()
    if not cfg.get('setup_completed') and role == 'admin':
        return redirect(url_for('admin_setup'))

    if role == 'admin':
        total_employees   = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        total_departments = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
        pending_leave     = db.execute("SELECT COUNT(*) FROM leave_requests WHERE status='pending'").fetchone()[0]
        # T6 — 옛 ATS(job_postings/applicants) 대신 자리 카드가 채용 지표의 근거다.
        open_seats        = db.execute(
            "SELECT COUNT(*) FROM job_openings WHERE status IN ('approved','open')").fetchone()[0]
        hires_waiting_count = db.execute("SELECT COUNT(*) FROM incoming_hires WHERE status='waiting'").fetchone()[0]
        recent_employees  = db.execute(
            'SELECT u.name, d.name AS dept, p.name AS pos, u.hire_date '
            'FROM users u LEFT JOIN departments d ON u.department_id=d.id '
            'LEFT JOIN positions p ON u.position_id=p.id '
            "WHERE u.status='active' ORDER BY u.created_at DESC LIMIT 6"
        ).fetchall()
        recent_posts = db.execute(
            'SELECT id, title, pinned, created_at FROM announcements '
            'ORDER BY pinned DESC, created_at DESC LIMIT 5'
        ).fetchall()
        who_out = db.execute(
            'SELECT u.name, d.name AS dept, lr.type, lr.end_date '
            'FROM leave_requests lr JOIN users u ON lr.user_id=u.id '
            'LEFT JOIN departments d ON u.department_id=d.id '
            "WHERE lr.status='approved' AND lr.start_date<=? AND lr.end_date>=? "
            'ORDER BY u.name LIMIT 8', (today, today)
        ).fetchall()
        # ── 미결 문서: 결재 대기함과 같은 목록(기한·상태 포함) ──
        inbox_items = collect_approval_rows(db, uid, role, session.get('dept_id'))
        inbox_count = len(inbox_items)
        inbox_late  = sum(1 for r in inbox_items if r['state'] == 'late')
        out_today   = db.execute(
            "SELECT COUNT(DISTINCT lr.user_id) FROM leave_requests lr "
            "WHERE lr.status='approved' AND lr.start_date<=? AND lr.end_date>=?", (today, today)).fetchone()[0]
        # ── 신규 위젯 데이터 ─────────────────────────────────
        this_year   = date.today().year
        this_month_n = date.today().month
        payroll_row = db.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(net_pay),0) as total "
            "FROM payslips WHERE year=? AND month=?", (this_year, this_month_n)
        ).fetchone()
        payroll_summary = {'count': payroll_row['cnt'], 'total': payroll_row['total']}
        # 열려 있는 자리를 요청서 단위로 묶어 보여준다 (T6)
        open_jobs = db.execute(
            "SELECT r.id AS req_id, r.title, "
            "  SUM(CASE WHEN o.status IN ('approved','open') THEN 1 ELSE 0 END) AS open_count, "
            "  COUNT(o.id) AS total_count "
            "FROM job_openings o JOIN job_requisitions r ON o.requisition_id=r.id "
            "GROUP BY r.id HAVING open_count > 0 ORDER BY r.id DESC LIMIT 5"
        ).fetchall()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        ot_violations = db.execute(
            "SELECT u.name, u.id as uid, SUM(c.overtime_min) as ot_min "
            "FROM checkins c JOIN users u ON c.user_id=u.id "
            "WHERE c.check_in >= ? AND u.status='active' "
            "GROUP BY c.user_id HAVING SUM(c.overtime_min) > 720 "
            "ORDER BY ot_min DESC LIMIT 5", (week_ago,)
        ).fetchall()
        month_dues = _admin_month_dues(db, cfg, payroll_summary['count'])
        enabled_widgets = get_widget_prefs(uid, 'admin')
        widget_catalog  = WIDGET_CATALOG['admin']
        return render_template('dashboard/admin.html',
            month_dues=month_dues,
            greet=greet, today_str=today_str, first_name=first_name,
            total_employees=total_employees, total_departments=total_departments,
            pending_leave=pending_leave, open_seats=open_seats,
            hires_waiting_count=hires_waiting_count,
            recent_employees=recent_employees,
            recent_posts=recent_posts, who_out=who_out,
            inbox_items=inbox_items, inbox_count=inbox_count,
            inbox_late=inbox_late, out_today=out_today, today_iso=today,
            payroll_summary=payroll_summary, open_jobs=open_jobs,
            ot_violations=ot_violations,
            labels=LEAVE_LABELS, active_page='home',
            enabled_widgets=enabled_widgets, widget_catalog=widget_catalog)

    if role == 'manager':
        dept_id = session.get('dept_id')
        team_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE department_id=? AND status='active'", (dept_id,)
        ).fetchone()[0]
        pending_count = db.execute(
            "SELECT COUNT(*) FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE u.department_id=? AND lr.status='pending'", (dept_id,)
        ).fetchone()[0]
        today_leave = db.execute(
            "SELECT COUNT(*) FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE u.department_id=? AND lr.status='approved' "
            "AND lr.start_date<=? AND lr.end_date>=?", (dept_id, today, today)
        ).fetchone()[0]
        # Inbox: 팀 대기 휴가 신청
        inbox_rows = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, u.name as user_name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE u.department_id=? AND lr.status='pending' "
            "ORDER BY lr.created_at ASC LIMIT 5", (dept_id,)
        ).fetchall()
        inbox_items = [
            {'id': r['id'], 'category': 'leave',
             'title': r['user_name'] + ' — ' + LEAVE_LABELS.get(r['type'], r['type']),
             'sub': r['start_date'] + (' ~ ' + r['end_date'] if r['start_date'] != r['end_date'] else '')}
            for r in inbox_rows
        ]
        # ── 승인 허브 확장 (P1-1): 목표 승인·이의신청·OT ──
        goal_rows = db.execute(
            "SELECT u.id AS uid, u.name, COUNT(*) AS cnt, c.id AS cid, c.name AS cycle_name "
            "FROM performance_goals g JOIN users u ON g.user_id=u.id "
            "JOIN performance_cycles c ON g.cycle_id=c.id "
            "WHERE g.approval_status='submitted' AND c.status='active' "
            "AND (u.manager_id=? OR u.department_id=?) "
            "GROUP BY u.id, c.id ORDER BY MIN(g.created_at) ASC LIMIT 3",
            (uid, dept_id)
        ).fetchall()
        for r in goal_rows:
            inbox_items.append({
                'id': r['uid'], 'category': 'goal',
                'title': f"{r['name']} — 목표 승인 요청 ({r['cnt']}개)",
                'sub': r['cycle_name'],
                'link': url_for('performance', cycle=r['cid'])
            })
        appeal_rows = db.execute(
            "SELECT ga.id, u.name, ga.old_grade, ga.cycle_id "
            "FROM grade_appeals ga JOIN users u ON ga.user_id=u.id "
            "WHERE ga.status='pending' AND u.manager_id=? ORDER BY ga.created_at ASC LIMIT 3",
            (uid,)
        ).fetchall()
        for r in appeal_rows:
            inbox_items.append({
                'id': r['id'], 'category': 'appeal',
                'title': f"{r['name']} — 등급 이의신청 (현재 {r['old_grade']})",
                'sub': '재검토 의견 필요',
                'link': url_for('performance_appeals', cycle=r['cycle_id'])
            })
        ot_rows = db.execute(
            "SELECT o.id, u.name, o.date, o.ot_minutes FROM overtime_requests o "
            "JOIN users u ON o.user_id=u.id "
            "WHERE o.status='pending' AND u.department_id=? ORDER BY o.created_at ASC LIMIT 3",
            (dept_id,)
        ).fetchall()
        for r in ot_rows:
            inbox_items.append({
                'id': r['id'], 'category': 'overtime',
                'title': f"{r['name']} — 연장근로 승인 요청",
                'sub': f"{r['date']} · {r['ot_minutes'] // 60}시간 {r['ot_minutes'] % 60}분",
                'link': url_for('attendance_home', tab='ot')
            })
        inbox_items.extend(_c2_todo(db, uid, role, session.get('dept_id'), get_perf_culture()))
        inbox_count = len(inbox_items)
        team_goals = db.execute(
            "SELECT pg.title, pg.self_score, u.name as user_name, AVG(pr.score) as avg_score "
            "FROM performance_goals pg JOIN users u ON pg.user_id=u.id "
            "LEFT JOIN performance_reviews pr ON pg.id=pr.goal_id "
            "WHERE u.department_id=? GROUP BY pg.id ORDER BY u.name LIMIT 8", (dept_id,)
        ).fetchall()
        recent_posts = db.execute(
            'SELECT id, title, pinned, created_at FROM announcements '
            'ORDER BY pinned DESC, created_at DESC LIMIT 4'
        ).fetchall()
        who_out = db.execute(
            "SELECT u.name, lr.type, lr.end_date FROM leave_requests lr "
            "JOIN users u ON lr.user_id=u.id WHERE u.department_id=? "
            "AND lr.status='approved' AND lr.start_date<=? AND lr.end_date>=? "
            "ORDER BY u.name", (dept_id, today, today)
        ).fetchall()
        upcoming_reviews = db.execute(
            "SELECT pc.name as cycle_name, pc.end_date AS review_end, "
            "COUNT(DISTINCT pg.user_id) as member_count, "
            "COUNT(DISTINCT CASE WHEN pr.id IS NOT NULL THEN pg.user_id END) as reviewed_count "
            "FROM performance_cycles pc "
            "JOIN performance_goals pg ON pc.id=pg.cycle_id "
            "JOIN users u ON pg.user_id=u.id "
            "LEFT JOIN performance_reviews pr ON pg.id=pr.goal_id "
            "WHERE u.department_id=? AND pc.end_date >= ? "
            "GROUP BY pc.id ORDER BY pc.end_date ASC LIMIT 3",
            (dept_id, today)
        ).fetchall()
        enabled_widgets = get_widget_prefs(uid, 'manager')
        widget_catalog  = WIDGET_CATALOG['manager']
        return render_template('dashboard/manager.html',
            greet=greet, today_str=today_str, first_name=first_name,
            team_count=team_count, pending_count=pending_count,
            today_leave=today_leave, inbox_items=inbox_items, inbox_count=inbox_count,
            team_goals=team_goals, recent_posts=recent_posts,
            who_out=who_out, upcoming_reviews=upcoming_reviews,
            labels=LEAVE_LABELS, active_page='home',
            enabled_widgets=enabled_widgets, widget_catalog=widget_catalog)

    if role == 'recruiter':
        # T6 — 리크루터가 TalentCore에서 하는 일은 '요청서·자리·입사 예정자' 셋이다.
        # 후보자·면접·오퍼는 Hire 소관이라 여기서 세지 않는다.
        pending_reqs_n = db.execute(
            "SELECT COUNT(*) FROM job_requisitions WHERE status IN ('pending_dept','pending_hr')"
        ).fetchone()[0]
        open_seats = db.execute(
            "SELECT COUNT(*) FROM job_openings WHERE status IN ('approved','open')").fetchone()[0]
        ready_to_push = db.execute(
            "SELECT COUNT(*) FROM job_openings WHERE status='approved'").fetchone()[0]
        hires_waiting_count = db.execute(
            "SELECT COUNT(*) FROM incoming_hires WHERE status='waiting'").fetchone()[0]
        recent_reqs = db.execute(
            'SELECT r.id, r.title, r.status, r.created_at, d.name AS dept_name, '
            "  (SELECT COUNT(*) FROM job_openings o WHERE o.requisition_id=r.id) AS seats, "
            "  (SELECT COUNT(*) FROM job_openings o WHERE o.requisition_id=r.id AND o.status='filled') AS filled "
            'FROM job_requisitions r LEFT JOIN departments d ON r.department_id=d.id '
            'ORDER BY r.id DESC LIMIT 6'
        ).fetchall()
        upcoming_hires = db.execute(
            'SELECT id, name, start_date, department_name, job_title '
            "FROM incoming_hires WHERE status='waiting' "
            "ORDER BY COALESCE(start_date, '9999-12-31') LIMIT 5"
        ).fetchall()
        recent_posts = db.execute(
            'SELECT id, title, pinned, created_at FROM announcements '
            'ORDER BY pinned DESC, created_at DESC LIMIT 5'
        ).fetchall()
        REQ_STATUS_MAP = {
            'draft':'작성 중', 'pending_dept':'부서장 결재', 'pending_hr':'HR 결재',
            'approved':'승인됨', 'rejected':'반려', 'posted':'공고 중',
        }
        return render_template('dashboard/recruiter.html',
            greet=greet, today_str=today_str, first_name=first_name,
            pending_reqs_n=pending_reqs_n, open_seats=open_seats,
            ready_to_push=ready_to_push, hires_waiting_count=hires_waiting_count,
            recent_reqs=recent_reqs, upcoming_hires=upcoming_hires,
            recent_posts=recent_posts, req_status_map=REQ_STATUS_MAP,
            hire_url=_hire_config()['url'], active_page='home')


    # employee
    hire_row = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    hire_date_str = hire_row['hire_date'] if hire_row else None
    _bal = get_leave_balance(db, uid)
    total_leave  = _bal['total']
    used_leave   = _bal['used']
    remain_leave = _bal['remaining']
    recent_reqs   = db.execute(
        'SELECT type, start_date, end_date, days, status FROM leave_requests '
        'WHERE user_id=? ORDER BY created_at DESC LIMIT 5', (uid,)
    ).fetchall()
    upcoming_leave = db.execute(
        "SELECT type, start_date, end_date, days FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND start_date>? "
        "ORDER BY start_date ASC LIMIT 3", (uid, today)
    ).fetchall()
    recent_posts = db.execute(
        'SELECT id, title, pinned, created_at FROM announcements '
        'ORDER BY pinned DESC, created_at DESC LIMIT 4'
    ).fetchall()
    tenure_str = _tenure(hire_date_str)
    pct_used   = int(float(used_leave) / total_leave * 100) if total_leave else 0
    my_goals = db.execute(
        "SELECT pg.id, pg.title, pg.weight, pg.progress, pg.self_score, pc.name as cycle_name "
        "FROM performance_goals pg "
        "JOIN performance_cycles pc ON pg.cycle_id=pc.id "
        "WHERE pg.user_id=? AND pc.status IN ('active','closed') "
        "ORDER BY pc.start_date DESC, pg.weight DESC LIMIT 5", (uid,)
    ).fetchall()
    enabled_widgets = get_widget_prefs(uid, 'employee')
    widget_catalog  = WIDGET_CATALOG['employee']
    return render_template('dashboard/employee.html',
        greet=greet, today_str=today_str, first_name=first_name,
        total_leave=total_leave, used_leave=used_leave,
        remain_leave=remain_leave, pct_used=pct_used,
        recent_reqs=recent_reqs, upcoming_leave=upcoming_leave,
        recent_posts=recent_posts, tenure_str=tenure_str,
        my_goals=my_goals,
        perf_todo=_c2_todo(db, uid, 'employee', None, get_perf_culture(), with_team=False),
        labels=LEAVE_LABELS, active_page='home',
        enabled_widgets=enabled_widgets, widget_catalog=widget_catalog)


@app.route('/dashboard/widgets', methods=['POST'])
@login_required
def dashboard_widgets_save():
    uid  = session['user_id']
    role = session.get('user_role', 'employee')
    catalog = WIDGET_CATALOG.get(role, [])
    db = get_db()
    for w in catalog:
        key = w['key']
        enabled = 1 if request.form.get(f'w_{key}') else 0
        db.execute(
            'INSERT INTO dashboard_widgets (user_id, widget_key, enabled) VALUES (?,?,?) '
            'ON CONFLICT(user_id, widget_key) DO UPDATE SET enabled=excluded.enabled',
            (uid, key, enabled)
        )
    db.commit()
    flash('대시보드 설정이 저장되었습니다.', 'success')
    return redirect(url_for('dashboard'))


# ── Announcements ────────────────────────────────────────────
@app.route('/announcements')
@login_required
def announcements():
    db    = get_db()
    posts = db.execute(
        'SELECT a.*, u.name AS author_name '
        'FROM announcements a JOIN users u ON a.author_id = u.id '
        'ORDER BY a.pinned DESC, a.created_at DESC'
    ).fetchall()
    return render_template('announcements/list.html', posts=posts,
                           active_page='announcements')

@app.route('/announcements/<int:post_id>')
@login_required
def announcement_detail(post_id):
    db   = get_db()
    post = db.execute(
        'SELECT a.*, u.name AS author_name '
        'FROM announcements a JOIN users u ON a.author_id = u.id '
        'WHERE a.id = ?', (post_id,)
    ).fetchone()
    if not post:
        abort(404)
    return render_template('announcements/detail.html', post=post,
                           active_page='announcements')

@app.route('/announcements/new', methods=['GET', 'POST'])
@admin_required
def announcement_new():
    error = None
    if request.method == 'POST':
        title   = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        pinned  = 1 if request.form.get('pinned') else 0
        if not title or not content:
            error = '제목과 내용을 모두 입력해주세요.'
        else:
            db = get_db()
            db.execute(
                'INSERT INTO announcements (title, content, pinned, author_id) VALUES (?, ?, ?, ?)',
                (title, content, pinned, session['user_id'])
            )
            db.commit()
            return redirect(url_for('announcements'))
    return render_template('announcements/form.html', post=None, error=error,
                           active_page='announcements')

@app.route('/announcements/<int:post_id>/edit', methods=['GET', 'POST'])
@admin_required
def announcement_edit(post_id):
    db   = get_db()
    post = db.execute('SELECT * FROM announcements WHERE id=?', (post_id,)).fetchone()
    if not post:
        abort(404)
    error = None
    if request.method == 'POST':
        title   = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        pinned  = 1 if request.form.get('pinned') else 0
        if not title or not content:
            error = '제목과 내용을 모두 입력해주세요.'
        else:
            db.execute(
                'UPDATE announcements SET title=?, content=?, pinned=?, '
                'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                (title, content, pinned, post_id)
            )
            db.commit()
            return redirect(url_for('announcement_detail', post_id=post_id))
    return render_template('announcements/form.html', post=post, error=error,
                           active_page='announcements')


# ── Global Search ────────────────────────────────────────────
@app.route('/search')
@login_required
def global_search():
    from flask import jsonify
    q = request.args.get('q', '').strip()
    if not q or len(q) < 1:
        return jsonify([])
    like = f'%{q}%'
    db = get_db()
    rows = db.execute(
        '''SELECT u.id, u.name, u.emp_no, u.email,
                  d.name dept_name, p.name pos_name
             FROM users u
             LEFT JOIN departments d ON u.department_id = d.id
             LEFT JOIN positions p ON u.position_id = p.id
            WHERE u.status = 'active'
              AND (u.name LIKE ? OR u.email LIKE ? OR u.emp_no LIKE ?
                   OR d.name LIKE ? OR p.name LIKE ?)
            ORDER BY u.name LIMIT 8''',
        (like, like, like, like, like)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Org Chart ────────────────────────────────────────────────
@app.route('/org')
@login_required
def org_chart():
    import json as _json
    db = get_db()
    rows = db.execute(
        '''SELECT u.id, u.name, u.email, u.phone, u.hire_date, u.manager_id,
                  u.employment_type,
                  d.name dept_name, p.name pos_name, jf.name jf_name,
                  SUBSTR(p.name, 1, 3) cl_label
           FROM users u
           LEFT JOIN departments d  ON u.department_id = d.id
           LEFT JOIN positions   p  ON u.position_id   = p.id
           LEFT JOIN job_families jf ON u.job_family_id = jf.id
           WHERE u.status="active"
           ORDER BY u.name'''
    ).fetchall()

    active_ids = {r['id'] for r in rows}
    people_map = {}
    for r in rows:
        mid = r['manager_id'] if r['manager_id'] in active_ids else None
        people_map[r['id']] = {
            'id': r['id'], 'name': r['name'], 'email': r['email'] or '',
            'phone': r['phone'] or '', 'hire': r['hire_date'] or '',
            'dept': r['dept_name'] or '', 'title': r['pos_name'] or '',
            'jf': r['jf_name'] or '', 'employment_type': r['employment_type'] or '',
            'mid': mid, 'reps': []
        }

    for pid, p in people_map.items():
        if p['mid'] and p['mid'] in people_map:
            people_map[p['mid']]['reps'].append(pid)

    total = len(people_map)
    employees_json = _json.dumps(people_map)
    return render_template('org/index.html',
                           employees_json=employees_json,
                           me=session['user_id'],
                           total=total,
                           active_page='org')


@app.route('/org/person/<int:uid>')
@login_required
def org_person(uid):
    from flask import jsonify
    db  = get_db()
    row = db.execute(
        '''SELECT u.id, u.name, u.email, u.phone, u.hire_date, u.employment_type,
                  d.name dept_name, p.name pos_name, jf.name jf_name,
                  m.name manager_name
           FROM users u
           LEFT JOIN departments d  ON u.department_id = d.id
           LEFT JOIN positions   p  ON u.position_id   = p.id
           LEFT JOIN job_families jf ON u.job_family_id = jf.id
           LEFT JOIN users        m  ON u.manager_id    = m.id
           WHERE u.id=? AND u.status="active"''', (uid,)
    ).fetchone()
    if not row:
        return jsonify({}), 404
    return jsonify(dict(row))


# ── Employees ────────────────────────────────────────────────
@app.route('/employees')
@manager_or_admin
def employees():
    db      = get_db()
    q            = request.args.get('q', '').strip()
    dept_id      = request.args.get('dept', '')
    jf_id        = request.args.get('jf', '')
    pos_id       = request.args.get('pos', '')
    emp_type     = request.args.get('emp_type', '')
    perf_grade   = request.args.get('grade', '')
    status       = request.args.get('status', 'active')   # active | resigned (v1.2.7 탭)
    if status not in ('active', 'resigned'):
        status = 'active'

    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    jfs   = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()

    sql = (
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name, jf.name AS jf_name, '
        '       cr.final_grade AS perf_grade '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions   p ON u.position_id   = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id = jf.id '
        'LEFT JOIN (SELECT user_id, final_grade FROM calibration_results '
        '           WHERE id IN (SELECT MAX(id) FROM calibration_results GROUP BY user_id)) cr '
        '           ON u.id = cr.user_id '
        'WHERE u.status = ?'
    )
    params = [status]
    if q:
        sql    += ' AND (u.name LIKE ? OR u.email LIKE ? OR u.emp_no LIKE ?)'
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if dept_id:
        sql += ' AND u.department_id = ?'; params.append(dept_id)
    if jf_id:
        sql += ' AND u.job_family_id = ?'; params.append(jf_id)
    if pos_id:
        sql += ' AND u.position_id = ?'; params.append(pos_id)
    if emp_type:
        sql += ' AND u.employment_type = ?'; params.append(emp_type)
    if perf_grade:
        sql += ' AND cr.final_grade = ?'; params.append(perf_grade)
    sql += ' ORDER BY u.name'

    emp_list = db.execute(sql, params).fetchall()

    # 상단 탭 카운트 (v1.2.7 — 입사 예정자를 직원 관리로 통합)
    try:
        hires_waiting = db.execute(
            "SELECT COUNT(*) FROM incoming_hires WHERE status='waiting'").fetchone()[0]
    except sqlite3.OperationalError:
        hires_waiting = 0
    resigned_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='resigned'").fetchone()[0]

    return render_template('employees/list.html',
                           employees=emp_list, depts=depts, jfs=jfs, poses=poses,
                           q=q, dept_id=dept_id, jf_id=jf_id, pos_id=pos_id,
                           emp_type=emp_type, perf_grade=perf_grade,
                           status=status, hires_waiting=hires_waiting,
                           resigned_count=resigned_count,
                           active_page='employees')

@app.route('/employees/<int:emp_id>')
@login_required
def employee_detail(emp_id):
    # 모든 로그인 사용자 프로필 조회 가능
    # 민감 탭(급여/근태/성과/복리후생) 권한: admin=전체, manager=직속팀원+본인, 그 외=본인만
    role = session['user_role']
    uid  = session['user_id']
    if role == 'admin':
        can_see_sensitive = True
    elif role == 'manager':
        can_see_sensitive = (emp_id == uid)  # 직속팀원 여부는 emp 조회 후 판단
    else:
        can_see_sensitive = (emp_id == uid)
    db  = get_db()
    emp = db.execute(
        'SELECT u.*, d.name dept_name, p.name pos_name, '
        '       jf.name jf_name, m.name manager_name, '
        '       b.id buddy_id, b.name buddy_name, bd.name buddy_dept, bp.name buddy_pos '
        'FROM users u '
        'LEFT JOIN departments d  ON u.department_id = d.id '
        'LEFT JOIN positions   p  ON u.position_id   = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id = jf.id '
        'LEFT JOIN users       m  ON u.manager_id    = m.id '
        'LEFT JOIN users       b  ON u.buddy_id      = b.id '
        'LEFT JOIN departments bd ON b.department_id = bd.id '
        'LEFT JOIN positions   bp ON b.position_id   = bp.id '
        'WHERE u.id=?', (emp_id,)
    ).fetchone()
    if not emp:
        abort(404)

    # 매니저: 직속 팀원(manager_id == 본인)이면 민감 정보 허용
    if role == 'manager' and emp['manager_id'] == uid:
        can_see_sensitive = True

    # 타인의 민감정보(급여/성과/개인정보) 열람 감사 기록 (본인 조회는 제외)
    if can_see_sensitive and emp_id != uid:
        log_audit('view', 'personal_info', emp_id, f'직원 프로필 민감정보 열람 ({emp["name"]})')

    payslips = db.execute(
        'SELECT year, month, gross_pay, net_pay, base_salary '
        "FROM payslips WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 6",
        (emp_id,)
    ).fetchall()

    leaves = db.execute(
        'SELECT * FROM leave_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 8',
        (emp_id,)
    ).fetchall()

    cycle = db.execute(
        "SELECT * FROM performance_cycles WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    goals = []
    if cycle:
        goals = db.execute(
            'SELECT * FROM performance_goals WHERE user_id=? AND cycle_id=? ORDER BY id',
            (emp_id, cycle['id'])
        ).fetchall()

    _bal = get_leave_balance(db, emp_id)
    annual_leave = _bal['total']
    used_leave   = _bal['used']

    salary_history = db.execute(
        'SELECT sh.*, u.name AS changed_by_name '
        'FROM salary_history sh '
        'LEFT JOIN users u ON sh.changed_by = u.id '
        'WHERE sh.user_id=? ORDER BY sh.changed_at DESC LIMIT 20',
        (emp_id,)
    ).fetchall()

    severance = db.execute(
        'SELECT * FROM severance_payments WHERE user_id=? ORDER BY processed_at DESC LIMIT 1',
        (emp_id,)
    ).fetchone()

    actions = db.execute(
        'SELECT pa.*, u.name AS processed_by_name '
        'FROM personnel_actions pa '
        'LEFT JOIN users u ON pa.processed_by = u.id '
        'WHERE pa.user_id=? ORDER BY pa.effective_date DESC',
        (emp_id,)
    ).fetchall()

    # 리포팅 라인: 상위 관리자 체인
    reporting_chain = []
    mgr_id = emp['manager_id']
    seen   = {emp_id}
    while mgr_id and mgr_id not in seen:
        seen.add(mgr_id)
        mgr = db.execute(
            'SELECT u.id, u.name, d.name dept_name, p.name pos_name '
            'FROM users u '
            'LEFT JOIN departments d ON u.department_id=d.id '
            'LEFT JOIN positions   p ON u.position_id=p.id '
            'WHERE u.id=?', (mgr_id,)
        ).fetchone()
        if not mgr:
            break
        reporting_chain.append(mgr)
        mgr_row = db.execute('SELECT manager_id FROM users WHERE id=?', (mgr_id,)).fetchone()
        mgr_id  = mgr_row['manager_id'] if mgr_row else None

    # 직속 부하직원
    direct_reports = db.execute(
        'SELECT u.id, u.name, d.name dept_name, p.name pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id=p.id '
        "WHERE u.manager_id=? AND u.status='active'",
        (emp_id,)
    ).fetchall()
    action_departments = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall()
    action_positions = db.execute('SELECT id, name FROM positions ORDER BY level').fetchall()
    action_managers = db.execute(
        "SELECT id, name FROM users WHERE role IN ('admin','manager') AND status='active' AND id!=? ORDER BY name",
        (emp_id,)
    ).fetchall()

    # 급여 밴드 + Compa-Ratio
    from payroll_utils import calc_compa_ratio, compa_band as _compa_band
    emp_salary = db.execute(
        'SELECT base_salary FROM employee_salary WHERE user_id=?', (emp_id,)
    ).fetchone()
    band_data = db.execute(
        'SELECT min_salary, mid_salary, max_salary FROM salary_grades '
        'WHERE position_id=? AND job_family_id=?',
        (emp['position_id'], emp['job_family_id'])
    ).fetchone() if emp['position_id'] and emp['job_family_id'] else None
    base_salary  = emp_salary['base_salary'] if emp_salary else 0
    mid_salary   = band_data['mid_salary']   if band_data else None
    compa_ratio  = calc_compa_ratio(base_salary, mid_salary) if band_data else None
    # emp를 dict로 변환하고 밴드 데이터 추가
    emp = dict(emp)
    emp['base_salary']  = base_salary
    emp['compa_ratio']  = compa_ratio
    emp['compa_band']   = _compa_band(compa_ratio)
    if band_data:
        emp['min_salary'] = band_data['min_salary']
        emp['mid_salary'] = band_data['mid_salary']
        emp['max_salary'] = band_data['max_salary']

    # 복리후생 탭 데이터
    company_benefit_cfgs = {
        r['key']: dict(r)
        for r in db.execute("SELECT * FROM benefit_configs WHERE enabled=1").fetchall()
    }
    emp_benefit_overrides = {
        r['benefit_key']: dict(r)
        for r in db.execute(
            "SELECT * FROM employee_benefit_overrides WHERE user_id=?", (emp_id,)
        ).fetchall()
    }
    # 전사 활성 항목 + 직원 오버라이드 병합
    benefit_rows = []
    for key, meta in sorted(BENEFIT_CATALOG.items(), key=lambda x: x[1].get('sort', 99)):
        cfg = company_benefit_cfgs.get(key)
        if not cfg:
            continue   # 전사에서 비활성화된 항목은 표시 안함
        ovr = emp_benefit_overrides.get(key)
        benefit_rows.append({
            'key':           key,
            'name':          meta['name'],
            'icon':          meta.get('icon', 'fa-circle'),
            'payment_type':  meta.get('payment_type'),
            'tax_exempt':    meta.get('tax_exempt', False),
            'monthly_limit': meta.get('monthly_limit'),
            'legal_basis':   meta.get('legal_basis', ''),
            'description':   meta.get('description', ''),
            'conditions':    meta.get('conditions'),
            # 전사 기본값
            'company_amount': cfg.get('amount', 0),
            'company_pct':    cfg.get('pct'),
            # 오버라이드 여부 및 값
            'has_override':   ovr is not None,
            'ovr_enabled':    ovr['enabled'] if ovr else True,
            'ovr_amount':     ovr['amount']  if ovr else None,
            'ovr_note':       ovr['note']    if ovr else '',
        })

    # 부양가족 + 생애사건
    dependents  = db.execute(
        'SELECT * FROM employee_dependents WHERE user_id=? ORDER BY relation, birth_date',
        (emp_id,)
    ).fetchall()
    life_events = db.execute(
        'SELECT le.*, u.name created_by_name '
        'FROM life_events le '
        'LEFT JOIN users u ON le.created_by = u.id '
        'WHERE le.user_id=? ORDER BY le.event_date DESC',
        (emp_id,)
    ).fetchall()

    RELATION_LABEL = {
        'spouse': '배우자', 'child': '자녀', 'parent': '부모',
        'grandparent': '조부모', 'sibling': '형제자매',
    }
    LIFE_EVENT_LABEL = {
        'marriage': '혼인', 'divorce': '이혼', 'birth': '출산',
        'adoption': '입양', 'death_of_dependent': '부양가족 사망',
        'disability_onset': '장애 발생', 'child_school_entry': '자녀 취학',
        'child_age_out': '자녀 공제 제외',
    }

    return render_template('employees/detail.html',
                           emp=emp, payslips=payslips, leaves=leaves,
                           goals=goals, cycle=cycle,
                           annual_leave=annual_leave, used_leave=used_leave,
                           severance=severance, actions=actions,
                           reporting_chain=reporting_chain,
                           direct_reports=direct_reports,
                           action_departments=action_departments,
                           action_positions=action_positions,
                           action_managers=action_managers,
                           benefit_rows=benefit_rows,
                           salary_history=salary_history,
                           skills=db.execute('SELECT * FROM employee_skills WHERE user_id=? ORDER BY level DESC, skill_name', (emp_id,)).fetchall(),
                           certs=db.execute('SELECT * FROM employee_certs WHERE user_id=? ORDER BY expiry_date ASC', (emp_id,)).fetchall(),
                           emp_documents=db.execute(
                               'SELECT ed.*, u.name uploaded_by_name FROM employee_documents ed '
                               'LEFT JOIN users u ON ed.uploaded_by = u.id '
                               'WHERE ed.user_id=? ORDER BY ed.uploaded_at DESC', (emp_id,)
                           ).fetchall(),
                           emp_doc_type_label=EMP_DOC_TYPE_LABEL,
                           skill_levels=SKILL_LEVELS,
                           today=date.today().isoformat(),
                           leave_labels=LEAVE_LABELS,
                           can_see_sensitive=can_see_sensitive,
                           dependents=dependents,
                           life_events=life_events,
                           relation_label=RELATION_LABEL,
                           life_event_label=LIFE_EVENT_LABEL,
                           buddy_candidates=db.execute(
                               """SELECT u.id, u.name,
                                         p.name AS pos_name,
                                         d.name AS dept_name
                                  FROM users u
                                  LEFT JOIN positions p ON u.position_id = p.id
                                  LEFT JOIN departments d ON u.department_id = d.id
                                  WHERE u.status='active'
                                    AND u.id != ?
                                    AND u.department_id = (SELECT department_id FROM users WHERE id=?)
                                  ORDER BY u.name""",
                               (emp_id, emp_id)
                           ).fetchall(),
                           active_page='employees')


@app.route('/employees/<int:emp_id>/dependents/add', methods=['POST'])
@login_required
def dependent_add(emp_id):
    """부양가족 추가 (본인·admin·직속 매니저만)."""
    role = session['user_role']
    uid  = session['user_id']
    db   = get_db()
    if role != 'admin' and uid != emp_id:
        abort(403)
    name       = request.form.get('name', '').strip()
    relation   = request.form.get('relation')
    birth_date = request.form.get('birth_date') or None
    gender     = request.form.get('gender') or None
    is_disabled  = 1 if request.form.get('is_disabled') else 0
    annual_income = int(request.form.get('annual_income', 0) or 0)
    is_cohabiting = 1 if request.form.get('is_cohabiting') else 0
    is_adopted    = 1 if request.form.get('is_adopted') else 0
    birth_order   = request.form.get('birth_order') or None
    note          = request.form.get('note', '').strip() or None
    if not name or not relation:
        flash('이름과 관계는 필수입니다.', 'error')
        return redirect(url_for('employee_detail', emp_id=emp_id))
    db.execute(
        'INSERT INTO employee_dependents '
        '(user_id, name, relation, birth_date, gender, is_disabled, annual_income, '
        'is_cohabiting, is_adopted, birth_order, note) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (emp_id, name, relation, birth_date, gender, is_disabled, annual_income,
         is_cohabiting, is_adopted, birth_order, note)
    )
    db.commit()
    flash('부양가족이 추가되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-family')


@app.route('/employees/<int:emp_id>/dependents/<int:dep_id>/delete', methods=['POST'])
@login_required
def dependent_delete(emp_id, dep_id):
    role = session['user_role']
    uid  = session['user_id']
    db   = get_db()
    if role != 'admin' and uid != emp_id:
        abort(403)
    db.execute('DELETE FROM employee_dependents WHERE id=? AND user_id=?', (dep_id, emp_id))
    db.commit()
    flash('부양가족이 삭제되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-family')


@app.route('/employees/<int:emp_id>/life-events/add', methods=['POST'])
@login_required
def life_event_add(emp_id):
    role = session['user_role']
    uid  = session['user_id']
    db   = get_db()
    if role != 'admin' and uid != emp_id:
        abort(403)
    event_type  = request.form.get('event_type')
    event_date  = request.form.get('event_date')
    description = request.form.get('description', '').strip() or None
    if not event_type or not event_date:
        flash('사건 유형과 날짜는 필수입니다.', 'error')
        return redirect(url_for('employee_detail', emp_id=emp_id))
    db.execute(
        'INSERT INTO life_events (user_id, event_type, event_date, description, created_by) '
        'VALUES (?,?,?,?,?)',
        (emp_id, event_type, event_date, description, uid)
    )
    db.commit()

    # 결혼·출산 시 복리후생 enrollment event 자동 생성
    LIFE_BENEFIT_MAP = {
        'marriage': ('marriage', '혼인 복리후생 선택', 30),
        'birth':    ('birth',    '출산 복리후생 선택', 60),
        'adoption': ('birth',    '입양 복리후생 선택', 60),
    }
    if event_type in LIFE_BENEFIT_MAP:
        ev_key, ev_label, days = LIFE_BENEFIT_MAP[event_type]
        existing = db.execute(
            "SELECT 1 FROM benefit_enrollment_events WHERE user_id=? AND event_type=? AND status='pending'",
            (emp_id, ev_key)
        ).fetchone()
        if not existing:
            from datetime import date as _date, timedelta as _td
            due = (_date.today() + _td(days=days)).isoformat()
            db.execute(
                'INSERT INTO benefit_enrollment_events (user_id, event_type, event_label, due_date) VALUES (?,?,?,?)',
                (emp_id, ev_key, ev_label, due)
            )
            db.commit()
            add_notification(
                emp_id, 'action', 'benefit',
                f'생애사건({ev_label}) 복리후생 선택 안내',
                f'{due}까지 복리후생 항목을 선택해 주세요.',
                url_for('me_benefits')
            )

    flash('생애사건이 기록되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-family')


@app.route('/employees/<int:emp_id>/benefits', methods=['POST'])
@admin_required
def employee_benefits_save(emp_id):
    """직원별 복리후생 오버라이드 저장."""
    db = get_db()
    if not db.execute('SELECT 1 FROM users WHERE id=?', (emp_id,)).fetchone():
        abort(404)

    for key in BENEFIT_CATALOG:
        # 폼에 해당 key가 존재하는 경우만 처리 (오버라이드 ON 체크박스)
        has_override = request.form.get(f'override_{key}') == '1'
        if not has_override:
            # 오버라이드 제거 (전사 기본값으로 복귀)
            db.execute(
                'DELETE FROM employee_benefit_overrides WHERE user_id=? AND benefit_key=?',
                (emp_id, key)
            )
            continue

        enabled = 0 if request.form.get(f'disabled_{key}') == '1' else 1
        raw_amt = request.form.get(f'amount_{key}', '').strip()
        amount  = int(raw_amt) if raw_amt.isdigit() else 0
        note    = request.form.get(f'note_{key}', '').strip() or None

        db.execute(
            'INSERT INTO employee_benefit_overrides (user_id, benefit_key, amount, enabled, note) '
            'VALUES (?,?,?,?,?) '
            'ON CONFLICT(user_id, benefit_key) DO UPDATE SET '
            'amount=excluded.amount, enabled=excluded.enabled, '
            'note=excluded.note, updated_at=CURRENT_TIMESTAMP',
            (emp_id, key, amount, enabled, note)
        )

    db.commit()
    flash('복리후생 오버라이드가 저장되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-benefits')


# ── 스킬 & 자격증 CRUD ───────────────────────────────────────────────

SKILL_LEVELS = {'beginner':'초급', 'intermediate':'중급', 'advanced':'고급', 'expert':'전문가'}

# ── Work Schedule 상수 ──────────────────────────────────────────
SCHEDULE_TYPES = [
    ('fixed',         '고정근무',   '#6366f1', '근로기준법 §50',  '출퇴근 시간 고정'),
    ('flex',          '선택근로',   '#0891b2', '근로기준법 §52',  '코어타임 내 자유 출퇴근'),
    ('discretionary', '재량근로',   '#059669', '근로기준법 §58③', '업무 방식·시간 재량'),
    ('short',         '단축근로',   '#d97706', '근로기준법 §74',  '임산부·육아 단축근무'),
    ('remote',        '재택/원격',  '#7c3aed', '텔레워크 가이드', '위치 무관 근무'),
]
SCHEDULE_TYPE_LABEL = {k: v for k, v, *_ in SCHEDULE_TYPES}
SCHEDULE_TYPE_COLOR = {k: c for k, _, c, *_ in SCHEDULE_TYPES}
ATTENDANCE_STATUS_LABEL = {
    'present':     '정상',
    'late':        '지각',
    'early_leave': '조퇴',
    'absent':      '결근',
    'on_leave':    '휴가',
    'holiday':     '공휴일',
    'remote':      '재택',
}
ATTENDANCE_STATUS_COLOR = {
    'present':     '#059669',
    'late':        '#d97706',
    'early_leave': '#f59e0b',
    'absent':      '#dc2626',
    'on_leave':    '#6366f1',
    'holiday':     '#0891b2',
    'remote':      '#7c3aed',
}


# ── HR 커스텀 리포트 빌더 ────────────────────────────────────────────────────
REPORT_SOURCES = [
    {
        'key': 'employee', 'label': '직원 정보',
        'icon': 'fa-user', 'color': '#dbeafe', 'icon_color': '#1d4ed8',
        'fields': [
            {'key': 'name',            'label': '이름',      'sql': 'u.name',            'agg': False, 'needs': []},
            {'key': 'dept',            'label': '부서',      'sql': 'd.name',            'agg': False, 'needs': ['dept_join']},
            {'key': 'position',        'label': '직급',      'sql': 'p.name',            'agg': False, 'needs': ['pos_join']},
            {'key': 'job_family',      'label': '직군',      'sql': 'jf.name',           'agg': False, 'needs': ['jf_join']},
            {'key': 'employment_type', 'label': '고용형태',  'sql': 'u.employment_type', 'agg': False, 'needs': []},
            {'key': 'hire_date',       'label': '입사일',    'sql': 'u.hire_date',       'agg': False, 'needs': []},
            {'key': 'base_salary',     'label': '기본급',    'sql': 'es.base_salary',    'agg': False, 'needs': ['sal_join']},
            {'key': 'emp_status',      'label': '재직상태',  'sql': 'u.status',          'agg': False, 'needs': []},
        ]
    },
    {
        'key': 'checkin', 'label': '출퇴근',
        'icon': 'fa-business-time', 'color': '#fef3c7', 'icon_color': '#b45309',
        'fields': [
            {'key': 'work_days',    'label': '출근일수',   'sql': 'COUNT(DISTINCT c.date)',                                                   'agg': True, 'needs': ['checkin_join']},
            {'key': 'regular_h',   'label': '정규(시간)', 'sql': 'ROUND(COALESCE(SUM(c.regular_min),0)/60.0,1)',                             'agg': True, 'needs': ['checkin_join']},
            {'key': 'overtime_h',  'label': '연장(시간)', 'sql': 'ROUND(COALESCE(SUM(c.overtime_min),0)/60.0,1)',                            'agg': True, 'needs': ['checkin_join']},
            {'key': 'night_h',     'label': '야간(시간)', 'sql': 'ROUND(COALESCE(SUM(c.night_min),0)/60.0,1)',                               'agg': True, 'needs': ['checkin_join']},
            {'key': 'late_count',  'label': '지각횟수',   'sql': "SUM(CASE WHEN c.attendance_status='late' THEN 1 ELSE 0 END)",              'agg': True, 'needs': ['checkin_join']},
            {'key': 'early_leave', 'label': '조퇴횟수',   'sql': "SUM(CASE WHEN c.attendance_status='early_leave' THEN 1 ELSE 0 END)",       'agg': True, 'needs': ['checkin_join']},
        ]
    },
    {
        'key': 'payroll', 'label': '급여',
        'icon': 'fa-won-sign', 'color': '#dcfce7', 'icon_color': '#16a34a',
        'fields': [
            {'key': 'total_gross',  'label': '총지급액',      'sql': 'COALESCE(SUM(ps.gross_pay),0)',           'agg': True, 'needs': ['payroll_join']},
            {'key': 'total_net',    'label': '총실수령액',    'sql': 'COALESCE(SUM(ps.net_pay),0)',             'agg': True, 'needs': ['payroll_join']},
            {'key': 'avg_gross',    'label': '월평균지급액',  'sql': 'ROUND(COALESCE(AVG(ps.gross_pay),0),0)',  'agg': True, 'needs': ['payroll_join']},
            {'key': 'total_ot_pay', 'label': '연장수당합계',  'sql': 'COALESCE(SUM(ps.overtime_pay),0)',        'agg': True, 'needs': ['payroll_join']},
        ]
    },
    {
        'key': 'leave', 'label': '근태',
        'icon': 'fa-clock', 'color': '#ffedd5', 'icon_color': '#c2410c',
        'fields': [
            {'key': 'leave_days',   'label': '휴가사용일수', 'sql': "COALESCE(SUM(CASE WHEN lr.status='approved' THEN lr.days ELSE 0 END),0)", 'agg': True, 'needs': ['leave_join']},
            {'key': 'leave_count',  'label': '휴가신청건수', 'sql': 'COUNT(DISTINCT lr.id)',                                                    'agg': True, 'needs': ['leave_join']},
            {'key': 'annual_used',  'label': '연차사용일수', 'sql': "COALESCE(SUM(CASE WHEN lr.type='annual' AND lr.status='approved' THEN lr.days ELSE 0 END),0)", 'agg': True, 'needs': ['leave_join']},
        ]
    },
    {
        'key': 'performance', 'label': '성과',
        'icon': 'fa-chart-line', 'color': '#f3e8ff', 'icon_color': '#7c3aed',
        'fields': [
            {'key': 'perf_grade',    'label': '성과등급',     'sql': 'cr.final_grade',           'agg': False, 'needs': ['perf_join']},
            {'key': 'self_avg',      'label': '자기평가평균', 'sql': 'ROUND(cr.self_avg,2)',      'agg': False, 'needs': ['perf_join']},
            {'key': 'mgr_avg',       'label': '매니저평가',   'sql': 'ROUND(cr.mgr_avg,2)',       'agg': False, 'needs': ['perf_join']},
            {'key': 'peer_avg',      'label': '다면평가평균', 'sql': 'ROUND(cr.peer_avg,2)',      'agg': False, 'needs': ['perf_join']},
            {'key': 'goal_progress', 'label': '목표진행률',   'sql': 'ROUND(AVG(pg.progress),1)', 'agg': True,  'needs': ['goal_join']},
        ]
    },
    {
        'key': 'talent', 'label': 'Talent 평가',
        'icon': 'fa-id-card', 'color': '#ede9fe', 'icon_color': '#5b21b6',
        'fields': [
            {'key': 'potential_score',  'label': '잠재력',       'sql': 'cr.potential_score',  'agg': False, 'needs': ['perf_join']},
            {'key': 'retention_risk',   'label': '이탈위험',     'sql': 'cr.retention_risk',   'agg': False, 'needs': ['perf_join']},
            {'key': 'loss_impact',      'label': '이탈임팩트',   'sql': 'cr.loss_impact',      'agg': False, 'needs': ['perf_join']},
            {'key': 'achievable_level', 'label': '달성가능레벨', 'sql': 'cr.achievable_level', 'agg': False, 'needs': ['perf_join']},
            {'key': 'downgrade_reason', 'label': '하향조정사유', 'sql': 'cr.downgrade_reason', 'agg': False, 'needs': ['perf_join']},
        ]
    },
    {
        'key': 'salary_hist', 'label': '급여 변경이력',
        'icon': 'fa-history', 'color': '#dcfce7', 'icon_color': '#15803d',
        'fields': [
            {'key': 'sal_change_count', 'label': '급여변경횟수', 'sql': 'COALESCE(sh_agg.change_count,0)', 'agg': False, 'needs': ['sal_hist_join']},
            {'key': 'sal_last_change',  'label': '최근변경일',   'sql': 'sh_agg.last_change_at',          'agg': False, 'needs': ['sal_hist_join']},
            {'key': 'sal_total_raise',  'label': '누적인상액',   'sql': 'COALESCE(sh_agg.total_raise,0)',  'agg': False, 'needs': ['sal_hist_join']},
        ]
    },
    {
        'key': 'peer_review', 'label': '다면평가',
        'icon': 'fa-comments', 'color': '#fce7f3', 'icon_color': '#be185d',
        'fields': [
            {'key': 'peer_score_avg', 'label': '다면평가 평균점수', 'sql': 'COALESCE(pr_agg.peer_score_avg,0)', 'agg': False, 'needs': ['peer_review_join']},
            {'key': 'peer_count',     'label': '피어리뷰 수신건수', 'sql': 'COALESCE(pr_agg.peer_count,0)',     'agg': False, 'needs': ['peer_review_join']},
        ]
    },
    {
        'key': 'welfare', 'label': '복지포인트',
        'icon': 'fa-gift', 'color': '#ffedd5', 'icon_color': '#c2410c',
        'fields': [
            {'key': 'welfare_balance',     'label': '복지포인트 잔액',   'sql': 'COALESCE(wp_agg.balance,0)',       'agg': False, 'needs': ['welfare_join']},
            {'key': 'welfare_total_grant', 'label': '누적 지급 포인트', 'sql': 'COALESCE(wp_agg.total_granted,0)', 'agg': False, 'needs': ['welfare_join']},
        ]
    },
    {
        'key': 'skills', 'label': '스킬 & 자격증',
        'icon': 'fa-certificate', 'color': '#dbeafe', 'icon_color': '#1d4ed8',
        'fields': [
            {'key': 'skill_names', 'label': '보유 스킬 목록',  'sql': 'sk_agg.skill_names',  'agg': False, 'needs': ['skill_join']},
            {'key': 'cert_names',  'label': '보유 자격증 목록', 'sql': 'ec_agg.cert_names',   'agg': False, 'needs': ['cert_join']},
        ]
    },
]
_REPORT_FIELD_MAP = {f['key']: f for src in REPORT_SOURCES for f in src['fields']}


def build_report_query(field_keys, filters, limit=200):
    """화이트리스트 기반 동적 SQL 생성. (SQL injection 없음 — 모든 식별자는 상수에서만 옴)"""
    import re

    # 날짜 포맷 검증
    def safe_date(s):
        return s if s and re.match(r'^\d{4}-\d{2}-\d{2}$', s) else None

    date_from = safe_date(filters.get('date_from'))
    date_to   = safe_date(filters.get('date_to'))

    selected  = [_REPORT_FIELD_MAP[k] for k in field_keys if k in _REPORT_FIELD_MAP]
    needs     = set()
    for f in selected:
        needs.update(f['needs'])
    if filters.get('dept_id'):
        needs.add('dept_join')

    # SELECT 절 — 사번만 고정, 이름은 선택
    select_parts  = ['u.emp_no AS "사번"']
    col_labels    = ['사번']
    group_non_agg = ['u.id', 'u.emp_no']

    for f in selected:
        select_parts.append(f'{f["sql"]} AS "{f["label"]}"')
        col_labels.append(f['label'])
        if not f['agg']:
            group_non_agg.append(f['sql'])

    # JOIN 절
    joins = []
    if 'dept_join' in needs:
        joins.append('LEFT JOIN departments d ON u.department_id = d.id')
    if 'pos_join' in needs:
        joins.append('LEFT JOIN positions p ON u.position_id = p.id')
    if 'jf_join' in needs:
        joins.append('LEFT JOIN job_families jf ON u.job_family_id = jf.id')
    if 'sal_join' in needs:
        joins.append('LEFT JOIN employee_salary es ON es.user_id = u.id')
    if 'checkin_join' in needs:
        date_cond = f" AND c.date BETWEEN '{date_from}' AND '{date_to}'" if date_from and date_to else ''
        joins.append(f'LEFT JOIN checkins c ON c.user_id = u.id{date_cond}')
    if 'payroll_join' in needs:
        date_cond = (f" AND (ps.year || '-' || printf('%02d',ps.month)) BETWEEN "
                     f"'{date_from[:7]}' AND '{date_to[:7]}'") if date_from and date_to else ''
        joins.append(f'LEFT JOIN payslips ps ON ps.user_id = u.id{date_cond}')
    if 'leave_join' in needs:
        date_cond = f" AND lr.start_date BETWEEN '{date_from}' AND '{date_to}'" if date_from and date_to else ''
        joins.append(f'LEFT JOIN leave_requests lr ON lr.user_id = u.id{date_cond}')
    if 'perf_join' in needs:
        joins.append(
            'LEFT JOIN (SELECT user_id, final_grade, self_avg, peer_avg, mgr_avg, '
            'potential_score, retention_risk, loss_impact, achievable_level, downgrade_reason '
            'FROM calibration_results WHERE id IN '
            '(SELECT MAX(id) FROM calibration_results GROUP BY user_id)) cr ON cr.user_id = u.id'
        )
    if 'goal_join' in needs:
        joins.append('LEFT JOIN performance_goals pg ON pg.user_id = u.id')
    if 'sal_hist_join' in needs:
        joins.append(
            'LEFT JOIN (SELECT user_id, COUNT(*) change_count, MAX(changed_at) last_change_at, '
            'SUM(new_base_salary - old_base_salary) total_raise '
            'FROM salary_history GROUP BY user_id) sh_agg ON sh_agg.user_id = u.id'
        )
    if 'peer_review_join' in needs:
        joins.append(
            'LEFT JOIN (SELECT reviewee_id, ROUND(AVG(score),2) peer_score_avg, COUNT(*) peer_count '
            'FROM peer_reviews GROUP BY reviewee_id) pr_agg ON pr_agg.reviewee_id = u.id'
        )
    if 'welfare_join' in needs:
        joins.append(
            'LEFT JOIN (SELECT user_id, '
            'SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END) total_granted, '
            '(SELECT wl2.balance_after FROM welfare_point_ledger wl2 '
            ' WHERE wl2.user_id = wpl.user_id ORDER BY wl2.created_at DESC LIMIT 1) balance '
            'FROM welfare_point_ledger wpl GROUP BY user_id) wp_agg ON wp_agg.user_id = u.id'
        )
    if 'skill_join' in needs:
        joins.append(
            "LEFT JOIN (SELECT user_id, "
            "GROUP_CONCAT(skill_name || '(' || level || ')', ', ') skill_names "
            "FROM employee_skills GROUP BY user_id) sk_agg ON sk_agg.user_id = u.id"
        )
    if 'cert_join' in needs:
        joins.append(
            "LEFT JOIN (SELECT user_id, "
            "GROUP_CONCAT(cert_name, ', ') cert_names "
            "FROM employee_certs GROUP BY user_id) ec_agg ON ec_agg.user_id = u.id"
        )

    # WHERE 절
    where_parts = ["u.status = 'active'"]
    params = []
    if filters.get('dept_id'):
        where_parts.append('u.department_id = ?')
        params.append(int(filters['dept_id']))
    if filters.get('employment_type'):
        where_parts.append('u.employment_type = ?')
        params.append(filters['employment_type'])

    sql = (
        f"SELECT {', '.join(select_parts)}\n"
        f"FROM users u\n"
        + ('\n'.join(joins) + '\n' if joins else '')
        + f"WHERE {' AND '.join(where_parts)}\n"
        f"GROUP BY {', '.join(group_non_agg)}\n"
        f"ORDER BY u.emp_no\n"
        + (f'LIMIT {int(limit)}' if limit else '')
    )
    return sql, params, col_labels


def get_user_schedule(db, user_id, date_str):
    """직원의 해당 날짜 활성 스케줄 반환. 없으면 회사 기본 스케줄."""
    row = db.execute('''
        SELECT ws.* FROM user_schedule_assignments usa
        JOIN work_schedules ws ON usa.schedule_id = ws.id
        WHERE usa.user_id = ?
          AND usa.effective_from <= ?
          AND (usa.effective_to IS NULL OR usa.effective_to >= ?)
        ORDER BY usa.effective_from DESC LIMIT 1
    ''', (user_id, date_str, date_str)).fetchone()
    if row:
        return dict(row)
    row = db.execute('SELECT * FROM work_schedules WHERE is_default=1 LIMIT 1').fetchone()
    return dict(row) if row else None


def judge_attendance(check_in_time, schedule):
    """체크인 시각 기준 출결 상태 판정 → present / late"""
    if not schedule or not check_in_time:
        return 'present'
    stype = schedule.get('schedule_type', 'fixed')
    try:
        ci_h, ci_m = map(int, check_in_time.split(':'))
        checkin_min = ci_h * 60 + ci_m
        grace = int(schedule.get('grace_minutes') or 10)
        if stype in ('fixed', 'short'):
            ws = schedule.get('work_start', '09:00')
            ws_h, ws_m = map(int, ws.split(':'))
            if checkin_min > ws_h * 60 + ws_m + grace:
                return 'late'
        elif stype == 'flex':
            cs = schedule.get('core_start', '10:00')
            cs_h, cs_m = map(int, cs.split(':'))
            if checkin_min > cs_h * 60 + cs_m + grace:
                return 'late'
    except Exception:
        pass
    return 'present'


def judge_early_leave(check_out_time, schedule):
    """퇴근 시각 기준 조퇴 여부 판정"""
    if not schedule or not check_out_time:
        return False
    stype = schedule.get('schedule_type', 'fixed')
    try:
        co_h, co_m = map(int, check_out_time.split(':'))
        checkout_min = co_h * 60 + co_m
        grace = int(schedule.get('grace_minutes') or 10)
        if stype in ('fixed', 'short'):
            we = schedule.get('work_end', '18:00')
            we_h, we_m = map(int, we.split(':'))
            return checkout_min < we_h * 60 + we_m - grace
    except Exception:
        pass
    return False

DEPT_TYPES = [
    ('division', '부문',  '#6366f1', '사업부·부문 단위'),
    ('hq',       '본부',  '#0891b2', '본부·사업본부 단위'),
    ('dept',     '실/처', '#059669', '실·처·센터 단위'),
    ('team',     '팀',    '#d97706', '팀·그룹 단위'),
]
DEPT_TYPE_LABEL = {k: v for k, v, *_ in DEPT_TYPES}
DEPT_TYPE_COLOR = {k: c for k, _, c, *_ in DEPT_TYPES}
# 상위 타입 규칙: division > hq > dept > team
DEPT_TYPE_PARENT_ALLOWED = {
    'division': [],                          # 최상위, 부모 없음
    'hq':       ['division'],
    'dept':     ['division', 'hq'],
    'team':     ['division', 'hq', 'dept'],
}

@app.route('/employees/<int:emp_id>/skills/add', methods=['POST'])
@login_required
def skill_add(emp_id):
    if session['user_role'] not in ('admin', 'manager') and session['user_id'] != emp_id:
        abort(403)
    skill_name = request.form.get('skill_name', '').strip()
    level      = request.form.get('level', 'intermediate')
    if skill_name and level in SKILL_LEVELS:
        db = get_db()
        db.execute('INSERT INTO employee_skills (user_id, skill_name, level) VALUES (?,?,?)',
                   (emp_id, skill_name, level))
        db.commit()
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-skills')


@app.route('/employees/<int:emp_id>/skills/<int:skill_id>/delete', methods=['POST'])
@login_required
def skill_delete(emp_id, skill_id):
    if session['user_role'] not in ('admin', 'manager') and session['user_id'] != emp_id:
        abort(403)
    db = get_db()
    db.execute('DELETE FROM employee_skills WHERE id=? AND user_id=?', (skill_id, emp_id))
    db.commit()
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-skills')


@app.route('/employees/<int:emp_id>/certs/add', methods=['POST'])
@login_required
def cert_add(emp_id):
    if session['user_role'] not in ('admin', 'manager') and session['user_id'] != emp_id:
        abort(403)
    cert_name   = request.form.get('cert_name', '').strip()
    issued_by   = request.form.get('issued_by', '').strip() or None
    issued_date = request.form.get('issued_date', '').strip() or None
    expiry_date = request.form.get('expiry_date', '').strip() or None
    if cert_name:
        db = get_db()
        db.execute(
            'INSERT INTO employee_certs (user_id, cert_name, issued_by, issued_date, expiry_date) VALUES (?,?,?,?,?)',
            (emp_id, cert_name, issued_by, issued_date, expiry_date)
        )
        db.commit()
        # 만료 30일 이내면 즉시 알림
        if expiry_date:
            from datetime import timedelta
            days_left = (date.fromisoformat(expiry_date) - date.today()).days
            if 0 <= days_left <= 30:
                add_notification(
                    emp_id, 'warning', 'cert',
                    '자격증 만료 임박',
                    f'"{cert_name}" 자격증이 {days_left}일 후 만료됩니다.',
                    url_for('employee_detail', emp_id=emp_id) + '#tab-skills'
                )
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-skills')


@app.route('/employees/<int:emp_id>/certs/<int:cert_id>/delete', methods=['POST'])
@login_required
def cert_delete(emp_id, cert_id):
    if session['user_role'] not in ('admin', 'manager') and session['user_id'] != emp_id:
        abort(403)
    db = get_db()
    db.execute('DELETE FROM employee_certs WHERE id=? AND user_id=?', (cert_id, emp_id))
    db.commit()
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-skills')


def _can_access_emp_docs(emp_id):
    role = session['user_role']
    uid  = session['user_id']
    if role == 'admin' or uid == emp_id:
        return True
    if role == 'manager':
        db = get_db()
        row = db.execute('SELECT manager_id FROM users WHERE id=?', (emp_id,)).fetchone()
        return bool(row and row['manager_id'] == uid)
    return False


@app.route('/employees/<int:emp_id>/documents/upload', methods=['POST'])
@login_required
def employee_doc_upload(emp_id):
    if not _can_access_emp_docs(emp_id):
        abort(403)
    db = get_db()
    f = request.files.get('file')
    doc_type = request.form.get('doc_type', 'other')
    if doc_type not in EMP_DOC_TYPE_LABEL:
        doc_type = 'other'
    if not f or not f.filename:
        flash('파일을 선택해주세요.', 'warning')
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-documents')
    if not allowed_file(f.filename):
        flash('허용되지 않는 파일 형식입니다.', 'danger')
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-documents')
    content = f.read()
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        flash(f'파일 크기는 {MAX_FILE_SIZE_MB}MB 이하여야 합니다.', 'danger')
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-documents')
    ext = f.filename.rsplit('.', 1)[1].lower()
    stored_name = f'{uuid.uuid4().hex}.{ext}'
    save_path = os.path.join(EMP_DOC_UPLOAD_FOLDER, stored_name)
    with open(save_path, 'wb') as out:
        out.write(content)
    db.execute(
        'INSERT INTO employee_documents (user_id, doc_type, original_name, stored_name, file_size, uploaded_by) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (emp_id, doc_type, f.filename, stored_name, len(content), session['user_id'])
    )
    db.commit()
    log_audit('create', 'document', emp_id, f'서류 업로드 ({EMP_DOC_TYPE_LABEL.get(doc_type, doc_type)}: {f.filename})')
    flash('서류가 업로드됐습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-documents')


@app.route('/employees/documents/<int:doc_id>/file')
@login_required
def employee_doc_file(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM employee_documents WHERE id=?', (doc_id,)).fetchone()
    if not doc:
        abort(404)
    if not _can_access_emp_docs(doc['user_id']):
        abort(403)
    log_audit('download', 'document', doc['user_id'], f'서류 다운로드 ({doc["original_name"]})')
    from flask import send_from_directory
    return send_from_directory(EMP_DOC_UPLOAD_FOLDER, doc['stored_name'],
                               download_name=doc['original_name'])


@app.route('/employees/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
def employee_doc_delete(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM employee_documents WHERE id=?', (doc_id,)).fetchone()
    if not doc:
        abort(404)
    if not _can_access_emp_docs(doc['user_id']):
        abort(403)
    emp_id = doc['user_id']
    try:
        os.remove(os.path.join(EMP_DOC_UPLOAD_FOLDER, doc['stored_name']))
    except OSError:
        pass
    db.execute('DELETE FROM employee_documents WHERE id=?', (doc_id,))
    db.commit()
    log_audit('delete', 'document', emp_id, f'서류 삭제 ({doc["original_name"]})')
    flash('서류가 삭제됐습니다.', 'info')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#tab-documents')


@app.route('/employees/<int:emp_id>/assign-buddy', methods=['POST'])
@login_required
def assign_buddy(emp_id):
    if session['user_role'] not in ('admin', 'manager'):
        abort(403)
    buddy_id = request.form.get('buddy_id', type=int)
    db = get_db()
    db.execute('UPDATE users SET buddy_id=? WHERE id=?', (buddy_id or None, emp_id))
    db.commit()
    if buddy_id:
        try:
            from integrations.dispatcher import on_buddy_assigned
            emp  = dict(db.execute(
                "SELECT u.name, u.email, d.name AS dept, p.name AS pos, u.hire_date "
                "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
                "LEFT JOIN positions p ON u.position_id=p.id WHERE u.id=?", (emp_id,)
            ).fetchone() or {})
            bud  = dict(db.execute(
                "SELECT u.name, u.email, d.name AS dept, p.name AS pos "
                "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
                "LEFT JOIN positions p ON u.position_id=p.id WHERE u.id=?", (buddy_id,)
            ).fetchone() or {})
            on_buddy_assigned(emp, bud, db_path=get_tenant_db_path(session.get('tenant_id', 1)))
        except Exception as e:
            app.logger.warning(f'assign_buddy integration error: {e}')
        flash('버디가 배정되었습니다.', 'success')
    else:
        flash('버디 배정이 해제되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id))


@app.route('/employees/new', methods=['GET', 'POST'])
@admin_required
def employee_new():
    db      = get_db()
    depts   = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses   = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs     = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    managers = db.execute(
        "SELECT id, name FROM users WHERE role IN ('admin','manager') AND status='active' ORDER BY name"
    ).fetchall()
    error = None

    if request.method == 'POST':
        name            = request.form.get('name', '').strip()
        email           = request.form.get('email', '').strip()
        password        = request.form.get('password', '').strip()
        role            = request.form.get('role', 'employee')
        dept_id         = request.form.get('department_id') or None
        pos_id          = request.form.get('position_id') or None
        jf_id           = request.form.get('job_family_id') or None
        phone           = request.form.get('phone', '').strip() or None
        hire_date       = request.form.get('hire_date') or None
        birth_date      = request.form.get('birth_date') or None
        employment_type = request.form.get('employment_type', 'full_time')
        manager_id      = request.form.get('manager_id') or None

        if not name or not email or not password:
            error = '이름, 이메일, 비밀번호는 필수입니다.'
        elif validate_password(password):
            error = validate_password(password)
        elif db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            error = '이미 사용 중인 이메일입니다.'
        else:
            cur = db.execute(
                'INSERT INTO users (name, email, password_hash, role, department_id, position_id, '
                '  job_family_id, phone, hire_date, birth_date, employment_type, manager_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (name, email, generate_password_hash(password), role,
                 dept_id, pos_id, jf_id, phone, hire_date, birth_date,
                 employment_type, manager_id)
            )
            new_id = cur.lastrowid
            db.execute("UPDATE users SET emp_no = 'TC-' || printf('%05d', id) WHERE id=?", (new_id,))
            # 지원자→직원 전환: applicant에 hired_employee_id 연결
            from_applicant_id = request.form.get('from_applicant', type=int)
            if from_applicant_id:
                db.execute(
                    'UPDATE applicants SET hired_employee_id=? WHERE id=?',
                    (new_id, from_applicant_id)
                )
                db.execute(
                    'UPDATE offers SET hired_employee_id=?, status="accepted", responded_at=CURRENT_TIMESTAMP '
                    'WHERE applicant_id=? AND status IN ("sent","negotiating","draft")',
                    (new_id, from_applicant_id)
                )
            # 입사 예정자→직원 전환 (Phase C-11)
            from_hire_id = request.form.get('from_hire', type=int)
            if from_hire_id:
                db.execute(
                    "UPDATE incoming_hires SET status='converted', converted_user_id=?, "
                    "converted_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting'",
                    (new_id, from_hire_id)
                )
                # 연봉 정보가 있으면 employee_salary에 반영 (월 기본급 = 연봉/12)
                hire_row = db.execute('SELECT salary, opening_id FROM incoming_hires WHERE id=?',
                                      (from_hire_id,)).fetchone()
                if hire_row and hire_row['salary']:
                    db.execute(
                        'INSERT OR REPLACE INTO employee_salary (user_id, base_salary) VALUES (?, ?)',
                        (new_id, int(hire_row['salary'] / 12))
                    )
                # 예약해 둔 자리 카드에 도장을 찍는다 — 여기서 정원이 실제로 채워진다 (T5)
                if hire_row and hire_row['opening_id']:
                    db.execute(
                        "UPDATE job_openings SET status='filled', hired_user_id=?, "
                        "filled_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND status IN ('approved','open')",
                        (new_id, hire_row['opening_id'])
                    )
            # Enrollment Event 자동 생성
            from datetime import date, timedelta
            due = (date.today() + timedelta(days=30)).isoformat()
            db.execute(
                "INSERT INTO benefit_enrollment_events (user_id, event_type, event_label, due_date) VALUES (?,?,?,?)",
                (new_id, 'onboarding', '입사 복리후생 선택', due)
            )
            add_notification(new_id, 'action', 'action',
                '복리후생 등록 안내',
                '입사를 축하합니다! 복리후생 항목을 확인하고 등록을 완료해 주세요.',
                url_for('me_benefits'))
            db.commit()
            # ── master.db 동기화: 이메일 매핑 + peak headcount ──
            tid = session.get('tenant_id', 1)
            register_tenant_user(email, tid)
            active_count = db.execute(
                "SELECT COUNT(*) FROM users WHERE status='active'"
            ).fetchone()[0]
            update_peak_headcount(tid, active_count)
            flash(f'직원 {name}(TC-{new_id:05d})이 추가되었습니다.', 'success')
            # ── 외부 서비스 연동 트리거 ───────────────────────
            try:
                from integrations.dispatcher import on_employee_created
                _d = db.execute("SELECT name FROM departments WHERE id=?", (dept_id,)).fetchone() if dept_id else None
                _p = db.execute("SELECT name FROM positions WHERE id=?", (pos_id,)).fetchone() if pos_id else None
                dept_name = _d['name'] if _d else ''
                pos_name  = _p['name'] if _p else ''
                on_employee_created({
                    'id': new_id,
                    'name': name, 'email': email,
                    'dept': dept_name, 'pos': pos_name,
                    'hire_date': hire_date or date.today().isoformat(),
                }, db_path=get_tenant_db_path(session.get('tenant_id', 1)))
            except Exception as _ie:
                app.logger.warning(f'Integration error on employee_created: {_ie}')
            # ── T5: 채용에서 넘어온 사람이면 계약서·온보딩·알림을 여기서 켠다 ──
            if from_hire_id:
                _op_code = None
                _op = db.execute(
                    'SELECT o.code FROM incoming_hires h '
                    'JOIN job_openings o ON o.id = h.opening_id WHERE h.id=?',
                    (from_hire_id,)).fetchone()
                if _op:
                    _op_code = _op['code']
                try:
                    _fired = ignite_after_hire(db, new_id, session['user_id'], _op_code)
                    if _fired:
                        flash('입사 확정 — ' + ' · '.join(_fired) + '까지 자동으로 처리했습니다.',
                              'success')
                    if '근로계약서 발송' not in _fired:
                        flash('근로계약서는 아직 나가지 않았습니다. 계약서 화면에서 발행해 주세요.',
                              'info')
                except Exception as _te:
                    app.logger.warning(f'T5 ignite failed for {new_id}: {_te}')
                    flash('직원은 등록됐지만 계약서·온보딩 자동 처리가 실패했습니다. '
                          '계약서 화면에서 직접 발행해 주세요.', 'error')
            return redirect(url_for('employees'))

    # 지원자→직원 전환 프리필 (기획서 P0: 오퍼 수락 시 /employees/new 프리필)
    prefill = {}
    from_applicant_id = request.args.get('from_applicant', type=int)
    if from_applicant_id:
        ap = get_db().execute(
            'SELECT a.*, jp.department_id AS jp_dept FROM applicants a '
            'JOIN job_postings jp ON a.posting_id = jp.id WHERE a.id=?', (from_applicant_id,)
        ).fetchone()
        if ap:
            prefill = {
                'name':          request.args.get('name', ap['name']),
                'email':         request.args.get('email', ap['email'] or ''),
                'phone':         request.args.get('phone', ap['phone'] or ''),
                'department_id': request.args.get('dept', ap['jp_dept'] or ''),
                'from_applicant': from_applicant_id,
            }

    # 입사 예정자→직원 전환 프리필 (Phase C-11)
    from_hire_id = request.args.get('from_hire', type=int)
    if from_hire_id:
        h = db.execute("SELECT * FROM incoming_hires WHERE id=? AND status='waiting'", (from_hire_id,)).fetchone()
        if h:
            # 부서/직급 이름 → id 매칭 (일치하는 것만 프리필)
            dept_id = None
            if h['department_name']:
                d = db.execute('SELECT id FROM departments WHERE name=?', (h['department_name'],)).fetchone()
                dept_id = d['id'] if d else None
            pos_id = None
            if h['position_name']:
                p = db.execute('SELECT id FROM positions WHERE name=?', (h['position_name'],)).fetchone()
                pos_id = p['id'] if p else None
            prefill = {
                'name':          h['name'],
                'email':         h['email'] or '',
                'phone':         h['phone'] or '',
                'department_id': dept_id or '',
                'position_id':   pos_id or '',
                'hire_date':     h['start_date'] or '',
                'from_hire':     from_hire_id,
            }

    return render_template('employees/form.html',
                           mode='new', depts=depts, poses=poses, jfs=jfs,
                           managers=managers, error=error, emp=None,
                           prefill=prefill,
                           active_page='employees')

# ══════════════════════════════════════════════════════════════
#  CSV 직원 일괄 임포트 (Phase B-6 — 실고객 진입로)
# ══════════════════════════════════════════════════════════════

IMPORT_TMP_DIR = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'imports')
os.makedirs(IMPORT_TMP_DIR, exist_ok=True)

IMPORT_COLUMNS = ['이름', '이메일', '부서', '직급', '직군', '고용형태',
                  '입사일', '생년월일', '전화', '기본급(월)', '매니저이메일']

EMP_TYPE_MAP = {
    '정규직': 'full_time', '계약직': 'contract', '인턴': 'intern', '파트타임': 'part_time',
    'full_time': 'full_time', 'contract': 'contract', 'intern': 'intern', 'part_time': 'part_time',
}
EMP_TYPE_KO = {'full_time': '정규직', 'contract': '계약직', 'intern': '인턴', 'part_time': '파트타임'}


def _read_csv_rows(raw_bytes):
    """CSV 파싱 — UTF-8(BOM 포함)과 한국 Excel 기본 인코딩(CP949) 모두 지원."""
    import csv, io
    for enc in ('utf-8-sig', 'cp949'):
        try:
            text = raw_bytes.decode(enc)
            return list(csv.DictReader(io.StringIO(text)))
        except (UnicodeDecodeError, csv.Error):
            continue
    return None


def _norm_date(s):
    """YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD → ISO. 빈값은 None, 오류는 False."""
    s = (s or '').strip()
    if not s:
        return None
    for sep in ('-', '.', '/'):
        parts = s.split(sep)
        if len(parts) == 3:
            try:
                return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
            except ValueError:
                return False
    return False


@app.route('/employees/import/template')
@admin_required
def employee_import_template():
    """샘플 CSV 다운로드 (Excel 호환 UTF-8 BOM) — 부서/직급/직군 예시는 실제 등록된 이름 사용."""
    import io
    from flask import Response
    db = get_db()
    dept = (db.execute('SELECT name FROM departments ORDER BY id LIMIT 1').fetchone() or {'name': ''})['name']
    pos  = (db.execute('SELECT name FROM positions ORDER BY id LIMIT 1').fetchone() or {'name': ''})['name']
    jf   = (db.execute('SELECT name FROM job_families ORDER BY id LIMIT 1').fetchone() or {'name': ''})['name']
    out = io.StringIO()
    out.write(','.join(IMPORT_COLUMNS) + '\n')
    out.write(f'홍길동,hong@example.com,{dept},{pos},{jf},정규직,2026-01-02,1995-03-15,010-1234-5678,3200000,kim@example.com\n')
    out.write(f'김철수,kim@example.com,{dept},{pos},{jf},계약직,2026-02-01,,,,\n')
    return Response('﻿' + out.getvalue(),
                    mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=employee_import_template.csv'})


@app.route('/employees/import', methods=['GET', 'POST'])
@admin_required
def employee_import():
    """1단계: CSV 업로드 → 검증 → 미리보기."""
    db = get_db()
    if request.method == 'GET':
        return render_template('employees/import.html', step='upload',
                               columns=IMPORT_COLUMNS, active_page='employees')

    f = request.files.get('file')
    if not f or not f.filename:
        flash('CSV 파일을 선택해주세요.', 'warning')
        return redirect(url_for('employee_import'))

    rows = _read_csv_rows(f.read())
    if rows is None:
        flash('CSV를 읽을 수 없습니다. UTF-8 또는 Excel(CP949)로 저장했는지 확인해주세요.', 'danger')
        return redirect(url_for('employee_import'))
    if not rows:
        flash('데이터 행이 없습니다. 템플릿을 참고해 작성해주세요.', 'warning')
        return redirect(url_for('employee_import'))

    # 이름 매칭용 사전
    depts = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM departments').fetchall()}
    poses = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM positions').fetchall()}
    jfs   = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM job_families').fetchall()}
    existing_emails = {r['email'] for r in db.execute('SELECT email FROM users').fetchall()}

    seen_emails = set()
    results = []
    for i, row in enumerate(rows, start=2):  # CSV 2행부터 (1행 = 헤더)
        r = {k: (row.get(k) or '').strip() for k in IMPORT_COLUMNS}
        errors = []

        if not r['이름']:
            errors.append('이름 누락')
        email = r['이메일'].lower()
        if not email:
            errors.append('이메일 누락')
        elif '@' not in email or '.' not in email.split('@')[-1]:
            errors.append('이메일 형식 오류')
        elif email in existing_emails:
            errors.append('이미 등록된 이메일')
        elif email in seen_emails:
            errors.append('파일 내 중복 이메일')
        seen_emails.add(email)

        dept_id = pos_id = jf_id = None
        if r['부서']:
            dept_id = depts.get(r['부서'])
            if not dept_id:
                errors.append(f"부서 없음: {r['부서']}")
        if r['직급']:
            pos_id = poses.get(r['직급'])
            if not pos_id:
                errors.append(f"직급 없음: {r['직급']}")
        if r['직군']:
            jf_id = jfs.get(r['직군'])
            if not jf_id:
                errors.append(f"직군 없음: {r['직군']}")

        emp_type = 'full_time'
        if r['고용형태']:
            emp_type = EMP_TYPE_MAP.get(r['고용형태'])
            if not emp_type:
                errors.append(f"고용형태 오류: {r['고용형태']} (정규직/계약직/인턴/파트타임)")

        hire_date  = _norm_date(r['입사일'])
        birth_date = _norm_date(r['생년월일'])
        if hire_date is False:
            errors.append(f"입사일 형식 오류: {r['입사일']}")
        if birth_date is False:
            errors.append(f"생년월일 형식 오류: {r['생년월일']}")

        salary = None
        if r['기본급(월)']:
            try:
                salary = int(r['기본급(월)'].replace(',', '').replace('원', ''))
            except ValueError:
                errors.append(f"기본급 숫자 아님: {r['기본급(월)']}")

        results.append({
            'line': i, 'raw': r, 'errors': errors,
            'data': None if errors else {
                'name': r['이름'], 'email': email,
                'dept_id': dept_id, 'pos_id': pos_id, 'jf_id': jf_id,
                'employment_type': emp_type,
                'hire_date': hire_date, 'birth_date': birth_date,
                'phone': r['전화'] or None, 'salary': salary,
                'manager_email': r['매니저이메일'].lower() or None,
            },
        })

    valid = [x['data'] for x in results if not x['errors']]
    token = uuid.uuid4().hex
    if valid:
        with open(os.path.join(IMPORT_TMP_DIR, f'{token}.json'), 'w', encoding='utf-8') as fp:
            json.dump(valid, fp, ensure_ascii=False)

    return render_template('employees/import.html', step='preview',
                           results=results, valid_count=len(valid),
                           error_count=len(results) - len(valid),
                           token=token, columns=IMPORT_COLUMNS,
                           active_page='employees')


@app.route('/employees/import/confirm', methods=['POST'])
@admin_required
def employee_import_confirm():
    """2단계: 검증 통과분 일괄 등록 + 매니저 이메일 2차 매핑."""
    token      = request.form.get('token', '')
    initial_pw = request.form.get('initial_password', '').strip()

    if not token.isalnum():
        abort(400)
    pw_error = validate_password(initial_pw)
    if pw_error:
        flash(f'초기 비밀번호 오류: {pw_error}', 'danger')
        return redirect(url_for('employee_import'))

    tmp_path = os.path.join(IMPORT_TMP_DIR, f'{token}.json')
    if not os.path.exists(tmp_path):
        flash('임포트 세션이 만료됐습니다. 파일을 다시 업로드해주세요.', 'warning')
        return redirect(url_for('employee_import'))
    with open(tmp_path, encoding='utf-8') as fp:
        rows = json.load(fp)

    db = get_db()
    pw_hash = generate_password_hash(initial_pw)
    email_to_id = {}
    for r in rows:
        cur = db.execute(
            'INSERT INTO users (name, email, password_hash, role, department_id, position_id, '
            '  job_family_id, phone, hire_date, birth_date, employment_type) '
            "VALUES (?,?,?,'employee',?,?,?,?,?,?,?)",
            (r['name'], r['email'], pw_hash, r['dept_id'], r['pos_id'], r['jf_id'],
             r['phone'], r['hire_date'], r['birth_date'], r['employment_type'])
        )
        new_id = cur.lastrowid
        db.execute("UPDATE users SET emp_no = 'TC-' || printf('%05d', id) WHERE id=?", (new_id,))
        if r['salary']:
            db.execute('INSERT INTO employee_salary (user_id, base_salary) VALUES (?,?)',
                       (new_id, r['salary']))
        email_to_id[r['email']] = new_id
        register_tenant_user(r['email'], session.get('tenant_id', 1))

    # 매니저 이메일 2차 매핑 (같은 파일 안의 직원도 매니저로 지정 가능)
    unmatched_managers = []
    for r in rows:
        if not r['manager_email']:
            continue
        mid = email_to_id.get(r['manager_email'])
        if not mid:
            m = db.execute('SELECT id FROM users WHERE email=?', (r['manager_email'],)).fetchone()
            mid = m['id'] if m else None
        if mid:
            db.execute('UPDATE users SET manager_id=? WHERE id=?', (mid, email_to_id[r['email']]))
        else:
            unmatched_managers.append(f"{r['name']}({r['manager_email']})")
    db.commit()

    # peak headcount 갱신
    active_count = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    update_peak_headcount(session.get('tenant_id', 1), active_count)

    os.remove(tmp_path)
    log_audit('create', 'personal_info', None, f'CSV 직원 일괄 임포트 — {len(rows)}명')
    msg = f'{len(rows)}명 등록 완료.'
    if unmatched_managers:
        msg += f' (매니저 매핑 실패 {len(unmatched_managers)}건: {", ".join(unmatched_managers[:5])})'
    flash(msg, 'success' if not unmatched_managers else 'warning')
    return redirect(url_for('employees'))


# ══════════════════════════════════════════════════════════════
#  CSV 왕복 — 내보내기 → 수정 → 재업로드 일괄 수정 (Phase P1-4, improvement_plan.md)
# ══════════════════════════════════════════════════════════════

BULK_UPDATE_COLUMNS = ['사번', '이름', '부서', '직급', '직군', '고용형태', '기본급(월)', '매니저이메일']


@app.route('/employees/export-editable')
@admin_required
def export_employees_editable():
    """수정용 CSV 내보내기 — 사번을 키로 값을 고쳐 재업로드하면 일괄 반영된다."""
    import io
    from flask import Response
    db = get_db()
    rows = db.execute(
        "SELECT u.emp_no, u.name, d.name AS dept, p.name AS pos, jf.name AS jf, "
        "       u.employment_type, es.base_salary, mgr.email AS manager_email "
        "FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "LEFT JOIN job_families jf ON u.job_family_id=jf.id "
        "LEFT JOIN employee_salary es ON u.id=es.user_id "
        "LEFT JOIN users mgr ON u.manager_id=mgr.id "
        "WHERE u.role != 'guest' AND u.status='active' "
        "ORDER BY d.name, u.name"
    ).fetchall()

    out = io.StringIO()
    out.write(','.join(BULK_UPDATE_COLUMNS) + '\n')
    for r in rows:
        vals = [
            r['emp_no'] or '', r['name'] or '', r['dept'] or '', r['pos'] or '', r['jf'] or '',
            EMP_TYPE_KO.get(r['employment_type'], r['employment_type'] or ''),
            str(r['base_salary'] or ''), r['manager_email'] or '',
        ]
        out.write(','.join(f'"{v}"' if ',' in v else v for v in vals) + '\n')

    return Response('﻿' + out.getvalue(),
                     mimetype='text/csv; charset=utf-8',
                     headers={'Content-Disposition': 'attachment; filename=employee_bulk_update.csv'})


@app.route('/employees/bulk-update', methods=['GET', 'POST'])
@admin_required
def employee_bulk_update():
    """1단계: 수정된 CSV 재업로드 → 사번 매칭 → 변경분만 골라 미리보기."""
    db = get_db()
    if request.method == 'GET':
        return render_template('employees/bulk_update.html', step='upload',
                               columns=BULK_UPDATE_COLUMNS, active_page='employees')

    f = request.files.get('file')
    if not f or not f.filename:
        flash('CSV 파일을 선택해주세요.', 'warning')
        return redirect(url_for('employee_bulk_update'))

    rows = _read_csv_rows(f.read())
    if rows is None:
        flash('CSV를 읽을 수 없습니다. UTF-8 또는 Excel(CP949)로 저장했는지 확인해주세요.', 'danger')
        return redirect(url_for('employee_bulk_update'))
    if not rows:
        flash('데이터 행이 없습니다.', 'warning')
        return redirect(url_for('employee_bulk_update'))

    depts = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM departments').fetchall()}
    poses = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM positions').fetchall()}
    jfs   = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM job_families').fetchall()}

    current_map = {}
    for r in db.execute(
        "SELECT u.id, u.emp_no, u.name, u.department_id, u.position_id, u.job_family_id, "
        "       u.employment_type, u.manager_id, "
        "       d.name AS dept_name, p.name AS pos_name, jf.name AS jf_name, "
        "       mgr.email AS mgr_email, es.base_salary "
        "FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "LEFT JOIN job_families jf ON u.job_family_id=jf.id "
        "LEFT JOIN users mgr ON u.manager_id=mgr.id "
        "LEFT JOIN employee_salary es ON u.id=es.user_id "
        "WHERE u.role != 'guest' AND u.status='active'"
    ).fetchall():
        if r['emp_no']:
            current_map[r['emp_no']] = r

    results = []
    apply_rows = []
    for i, row in enumerate(rows, start=2):  # CSV 2행부터 (1행 = 헤더)
        r = {k: (row.get(k) or '').strip() for k in BULK_UPDATE_COLUMNS}
        errors = []
        emp_no = r['사번']
        if not emp_no:
            errors.append('사번 누락')
        cur = current_map.get(emp_no) if emp_no else None
        if emp_no and not cur:
            errors.append(f'사번 없음(또는 재직중 아님): {emp_no}')

        changes = {}
        if cur:
            if r['부서']:
                new_id = depts.get(r['부서'])
                if not new_id:
                    errors.append(f"부서 없음: {r['부서']}")
                elif new_id != cur['department_id']:
                    changes['department_id'] = {'label': '부서', 'old': cur['dept_name'] or '-', 'new': r['부서'], 'value': new_id}
            if r['직급']:
                new_id = poses.get(r['직급'])
                if not new_id:
                    errors.append(f"직급 없음: {r['직급']}")
                elif new_id != cur['position_id']:
                    changes['position_id'] = {'label': '직급', 'old': cur['pos_name'] or '-', 'new': r['직급'], 'value': new_id}
            if r['직군']:
                new_id = jfs.get(r['직군'])
                if not new_id:
                    errors.append(f"직군 없음: {r['직군']}")
                elif new_id != cur['job_family_id']:
                    changes['job_family_id'] = {'label': '직군', 'old': cur['jf_name'] or '-', 'new': r['직군'], 'value': new_id}
            if r['고용형태']:
                new_type = EMP_TYPE_MAP.get(r['고용형태'])
                if not new_type:
                    errors.append(f"고용형태 오류: {r['고용형태']} (정규직/계약직/인턴/파트타임)")
                elif new_type != cur['employment_type']:
                    changes['employment_type'] = {
                        'label': '고용형태',
                        'old': EMP_TYPE_KO.get(cur['employment_type'], cur['employment_type']),
                        'new': r['고용형태'], 'value': new_type,
                    }
            if r['기본급(월)']:
                try:
                    new_sal = int(r['기본급(월)'].replace(',', '').replace('원', ''))
                except ValueError:
                    errors.append(f"기본급 숫자 아님: {r['기본급(월)']}")
                else:
                    if new_sal != (cur['base_salary'] or 0):
                        changes['salary'] = {
                            'label': '기본급',
                            'old': f"{cur['base_salary'] or 0:,}원", 'new': f"{new_sal:,}원", 'value': new_sal,
                        }
            if r['매니저이메일']:
                mgr_email = r['매니저이메일'].lower()
                mgr = db.execute('SELECT id, email FROM users WHERE email=?', (mgr_email,)).fetchone()
                if not mgr:
                    errors.append(f"매니저 이메일 없음: {mgr_email}")
                elif mgr['id'] != cur['manager_id']:
                    changes['manager_id'] = {
                        'label': '매니저', 'old': cur['mgr_email'] or '-', 'new': mgr_email, 'value': mgr['id'],
                    }

        results.append({
            'line': i, 'emp_no': emp_no, 'name': r['이름'] or (cur['name'] if cur else ''),
            'errors': errors, 'changes': changes,
        })

        if cur and not errors and changes:
            sets, salary_val = {}, None
            for key, ch in changes.items():
                if key == 'salary':
                    salary_val = ch['value']
                else:
                    sets[key] = ch['value']
            apply_rows.append({'emp_id': cur['id'], 'sets': sets, 'salary': salary_val})

    error_count = sum(1 for x in results if x['errors'])
    no_change_count = sum(1 for x in results if not x['errors'] and not x['changes'])
    token = uuid.uuid4().hex
    if apply_rows:
        with open(os.path.join(IMPORT_TMP_DIR, f'bulk_{token}.json'), 'w', encoding='utf-8') as fp:
            json.dump(apply_rows, fp, ensure_ascii=False)

    return render_template('employees/bulk_update.html', step='preview',
                           results=results, valid_count=len(apply_rows),
                           error_count=error_count, no_change_count=no_change_count,
                           token=token, columns=BULK_UPDATE_COLUMNS,
                           active_page='employees')


@app.route('/employees/bulk-update/confirm', methods=['POST'])
@admin_required
def employee_bulk_update_confirm():
    """2단계: 미리보기에서 확인한 변경분을 일괄 반영 (부서/직급/직군/고용형태/매니저=직접 UPDATE, 급여=salary_history 기록)."""
    token = request.form.get('token', '')
    if not token.isalnum():
        abort(400)

    tmp_path = os.path.join(IMPORT_TMP_DIR, f'bulk_{token}.json')
    if not os.path.exists(tmp_path):
        flash('수정 세션이 만료됐습니다. 파일을 다시 업로드해주세요.', 'warning')
        return redirect(url_for('employee_bulk_update'))
    with open(tmp_path, encoding='utf-8') as fp:
        apply_rows = json.load(fp)

    db = get_db()
    field_count = 0
    for row in apply_rows:
        emp_id = row['emp_id']
        sets = row['sets']
        if sets:
            cols = ', '.join(f'{c}=?' for c in sets)
            db.execute(f'UPDATE users SET {cols} WHERE id=?', (*sets.values(), emp_id))
            field_count += len(sets)
        if row['salary'] is not None:
            new_sal = row['salary']
            old = db.execute('SELECT base_salary FROM employee_salary WHERE user_id=?', (emp_id,)).fetchone()
            if old:
                db.execute('UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
                           (new_sal, emp_id))
                old_sal = old['base_salary']
            else:
                db.execute('INSERT INTO employee_salary (user_id, base_salary) VALUES (?,?)', (emp_id, new_sal))
                old_sal = 0
            db.execute(
                'INSERT INTO salary_history (user_id, changed_by, old_base_salary, new_base_salary, reason) '
                'VALUES (?,?,?,?,?)',
                (emp_id, session['user_id'], old_sal, new_sal, 'CSV 일괄 수정')
            )
            field_count += 1
    db.commit()
    os.remove(tmp_path)

    log_audit('update', 'personal_info', None, f'CSV 일괄 수정 — {len(apply_rows)}명, 변경 {field_count}건')
    flash(f'{len(apply_rows)}명 정보가 수정되었습니다. (총 {field_count}건 변경)', 'success')
    return redirect(url_for('employees'))


# ══════════════════════════════════════════════════════════════
#  입사 예정자 (Phase C-11 — 외부 ATS 합격자 수신 + 직원 전환, saas_plan.md §5)
# ══════════════════════════════════════════════════════════════

HIRES_CSV_COLUMNS = ['이름', '이메일', '전화', '입사예정일', '부서', '직급', '직무', '연봉', '메모']
HIRE_SOURCE_LABEL = {'manual': '직접 입력', 'csv': 'CSV 임포트', 'webhook': 'ATS 웹훅', 'internal': '자체 채용'}


def _normalize_hire_date(val):
    """입사예정일 정규화: 2026-08-01 / 2026.08.01 / 20260801 모두 지원."""
    if not val:
        return None
    v = str(val).strip().replace('.', '-').replace('/', '-')
    if len(v) == 8 and v.isdigit():
        v = f'{v[:4]}-{v[4:6]}-{v[6:]}'
    try:
        return datetime.strptime(v, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def _ko_day(d):
    return '%s(%s)' % (d.strftime('%m.%d'), workplace.WEEKDAY_KO[d.weekday()])


def _start_date_rule_msg(conn, iso):
    """입사일 규칙(회사·건물 설정의 입사 요일, 공휴일 제외)에 어긋나면 안내문, 맞으면 None.
    비어 있으면 '협의 중'이라 통과시킨다."""
    if not iso:
        return None
    p = workplace.start_date_problem(conn, iso)
    if not p:
        return None
    msg, nxt = p
    return msg + (' — 가까운 입사일: ' + ', '.join(_ko_day(d) for d in nxt) if nxt else '')


def _start_date_choices(current=None, n=26):
    """입사일 고르는 칸에 넣을 날짜들. 규칙이 생기기 전에 잡힌 날은 맨 위에 그대로 남긴다."""
    db = get_db()
    out = [{'value': d.isoformat(),
            'label': '%s (%s)' % (d.strftime('%Y.%m.%d'), workplace.WEEKDAY_KO[d.weekday()])}
           for d in workplace.next_start_dates(db, date.today(), n)]
    if current and current not in [o['value'] for o in out]:
        out.insert(0, {'value': current, 'label': '%s (규칙 밖 · 이미 잡힌 날)' % current})
    return out


app.jinja_env.globals['start_date_choices'] = _start_date_choices
app.jinja_env.globals['start_weekdays_label'] = lambda: workplace.weekdays_label(workplace.settings(get_db())['weekdays'])


@app.route('/hires')
@recruiter_or_admin
def hires_list():
    db = get_db()
    status_filter = request.args.get('status', 'waiting')
    if status_filter not in ('waiting', 'converted', 'cancelled', 'all'):
        status_filter = 'waiting'

    q = ('SELECT h.*, u.name AS converted_name, u.emp_no AS converted_emp_no, '
         'o.code AS opening_code, p.level AS opening_level, p.name AS opening_pos '
         'FROM incoming_hires h '
         'LEFT JOIN users u ON h.converted_user_id=u.id '
         'LEFT JOIN job_openings o ON h.opening_id=o.id '
         'LEFT JOIN positions p ON o.position_id=p.id ')
    if status_filter != 'all':
        rows = db.execute(q + 'WHERE h.status=? ORDER BY h.start_date IS NULL, h.start_date, h.id',
                          (status_filter,)).fetchall()
    else:
        rows = db.execute(q + "ORDER BY CASE h.status WHEN 'waiting' THEN 0 ELSE 1 END, "
                              'h.start_date IS NULL, h.start_date, h.id').fetchall()

    # D-day 계산
    today = date.today()
    hires = []
    for r in rows:
        d = dict(r)
        d['dday'] = None
        if r['start_date']:
            try:
                d['dday'] = (date.fromisoformat(r['start_date']) - today).days
            except ValueError:
                pass
        hires.append(d)

    counts = {row['status']: row['c'] for row in db.execute(
        'SELECT status, COUNT(*) c FROM incoming_hires GROUP BY status').fetchall()}

    # 웹훅 토큰 (admin만 노출)
    api_token = None
    if session.get('user_role') == 'admin':
        api_token = get_or_create_api_token(session.get('tenant_id', 1))

    upcoming = workplace.upcoming_start_days(db, today) if status_filter == 'waiting' else []

    return render_template('hires/list.html',
                           hires=hires, counts=counts, status_filter=status_filter,
                           source_label=HIRE_SOURCE_LABEL,
                           api_token=api_token, upcoming=upcoming,
                           active_page='hires')


@app.route('/hires/new', methods=['POST'])
@recruiter_or_admin
def hires_new():
    db    = get_db()
    name  = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip() or None
    phone = request.form.get('phone', '').strip() or None
    start = _normalize_hire_date(request.form.get('start_date', ''))
    dept  = request.form.get('department_name', '').strip() or None
    pos   = request.form.get('position_name', '').strip() or None
    job   = request.form.get('job_title', '').strip() or None
    memo  = request.form.get('memo', '').strip() or None
    try:
        salary = int(request.form.get('salary', '').replace(',', '') or 0) or None
    except ValueError:
        salary = None

    rule = _start_date_rule_msg(db, start)
    if not name:
        flash('이름은 필수입니다.', 'error')
    elif rule:
        flash(rule, 'error')
    elif email and db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
        flash('이미 등록된 직원의 이메일입니다.', 'error')
    else:
        db.execute(
            'INSERT INTO incoming_hires (name, email, phone, start_date, department_name, '
            "position_name, job_title, salary, memo, source) VALUES (?,?,?,?,?,?,?,?,?,'manual')",
            (name, email, phone, start, dept, pos, job, salary, memo)
        )
        db.commit()
        flash(f'입사 예정자 {name}님이 등록되었습니다.', 'success')
    return redirect(url_for('hires_list'))


@app.route('/hires/<int:hire_id>/cancel', methods=['POST'])
@recruiter_or_admin
def hires_cancel(hire_id):
    db = get_db()
    h = db.execute('SELECT * FROM incoming_hires WHERE id=?', (hire_id,)).fetchone()
    if not h:
        abort(404)
    if h['status'] != 'waiting':
        flash('대기 중인 입사 예정자만 취소할 수 있습니다.', 'error')
    else:
        db.execute("UPDATE incoming_hires SET status='cancelled' WHERE id=?", (hire_id,))
        db.commit()
        flash(f'{h["name"]}님의 입사가 취소 처리되었습니다.', 'success')
    return redirect(url_for('hires_list'))


@app.route('/hires/import/template')
@recruiter_or_admin
def hires_import_template():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HIRES_CSV_COLUMNS)
    w.writerow(['김입사', 'kim.ipsa@example.com', '010-1234-5678', '2026-08-01',
                '개발팀', 'CL3', '백엔드 엔지니어', '52000000', '그리팅 합격자'])
    data = '﻿' + buf.getvalue()   # BOM — 한국 Excel 호환
    return app.response_class(
        data.encode('utf-8'),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="incoming_hires_template.csv"'}
    )


@app.route('/hires/import', methods=['POST'])
@recruiter_or_admin
def hires_import():
    """외부 ATS 합격자 CSV 임포트 — 검증 후 유효 행만 등록, 오류 행은 리포트."""
    db = get_db()
    f = request.files.get('csv_file')
    if not f or not f.filename:
        flash('CSV 파일을 선택해 주세요.', 'error')
        return redirect(url_for('hires_list'))

    rows = _read_csv_rows(f.read())
    if rows is None:
        flash('CSV 파일을 읽을 수 없습니다. UTF-8 또는 CP949(엑셀 기본) 인코딩인지 확인해 주세요.', 'error')
        return redirect(url_for('hires_list'))

    inserted, errors = 0, []
    for i, r in enumerate(rows, start=2):   # 2행부터 (1행=헤더)
        name = (r.get('이름') or '').strip()
        if not name:
            errors.append(f'{i}행: 이름 누락')
            continue
        email = (r.get('이메일') or '').strip() or None
        if email and db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            errors.append(f'{i}행({name}): 이미 등록된 직원 이메일')
            continue
        if email and db.execute(
                "SELECT id FROM incoming_hires WHERE email=? AND status='waiting'", (email,)).fetchone():
            errors.append(f'{i}행({name}): 이미 대기 중인 입사 예정자')
            continue
        start = _normalize_hire_date(r.get('입사예정일'))
        if (r.get('입사예정일') or '').strip() and not start:
            errors.append(f'{i}행({name}): 입사예정일 형식 오류 ({r.get("입사예정일")})')
            continue
        try:
            salary = int(str(r.get('연봉') or '').replace(',', '').strip() or 0) or None
        except ValueError:
            salary = None
        db.execute(
            'INSERT INTO incoming_hires (name, email, phone, start_date, department_name, '
            "position_name, job_title, salary, memo, source) VALUES (?,?,?,?,?,?,?,?,?,'csv')",
            (name, email, (r.get('전화') or '').strip() or None, start,
             (r.get('부서') or '').strip() or None, (r.get('직급') or '').strip() or None,
             (r.get('직무') or '').strip() or None, salary, (r.get('메모') or '').strip() or None)
        )
        inserted += 1
    db.commit()
    if inserted:
        log_audit('create', 'personal_info', None, f'입사 예정자 CSV 임포트 — {inserted}명')
    msg = f'{inserted}명 등록 완료.'
    if errors:
        msg += f' 오류 {len(errors)}건: ' + ' / '.join(errors[:5]) + (' …' if len(errors) > 5 else '')
    flash(msg, 'success' if not errors else 'warning')
    return redirect(url_for('hires_list'))


@app.route('/hires/token/regenerate', methods=['POST'])
@admin_required
def hires_token_regenerate():
    regenerate_api_token(session.get('tenant_id', 1))
    log_audit('update', 'auth', None, 'ATS 웹훅 API 토큰 재발급')
    flash('웹훅 토큰이 재발급되었습니다. 기존 토큰은 즉시 무효화됩니다 — 연동 중인 ATS에 새 토큰을 등록하세요.', 'success')
    return redirect(url_for('hires_list'))


@app.route('/api/hires', methods=['POST'])
def hires_webhook():
    """표준 웹훅 수신 — 외부 ATS가 합격자를 push하는 엔드포인트.

    인증: X-API-Token 헤더 (테넌트별 토큰, /hires 화면에서 발급)
    본문: JSON {"name": 필수, "email", "phone", "start_date", "department",
                "position", "job_title", "salary", "memo",
                "opening_code" 또는 "opening_id", "req_ref", "candidate_ref"}

    자리 카드(opening)를 함께 보내면 그 카드를 예약한다. 카드가 이미 찼거나
    다른 합격자가 예약해 두었으면 409로 막는다 — 승인된 정원을 넘길 수 없다.
    카드 없이 보내면 예전처럼 그냥 받되 "정원 밖"으로 남는다.
    """
    token = request.headers.get('X-API-Token', '')
    tenant = get_tenant_by_api_token(token)
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401

    payload = request.get_json(silent=True)
    if not payload:
        return {'ok': False, 'error': 'invalid JSON body'}, 400
    name = str(payload.get('name', '')).strip()
    if not name:
        return {'ok': False, 'error': 'name is required'}, 400

    start = _normalize_hire_date(payload.get('start_date'))
    try:
        salary = int(str(payload.get('salary') or '').replace(',', '') or 0) or None
    except ValueError:
        salary = None

    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        # 입사일 규칙 — 월·수(공휴일 제외)만. 어긋나면 가까운 입사일을 같이 돌려준다.
        prob = workplace.start_date_problem(conn, start) if start else None
        if prob:
            return {'ok': False, 'error': 'start_date not allowed', 'detail': prob[0],
                    'next_dates': [d.isoformat() for d in prob[1]]}, 422
        email = str(payload.get('email') or '').strip() or None
        if email:
            if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
                return {'ok': False, 'error': 'email already exists as employee'}, 409
            if conn.execute("SELECT id FROM incoming_hires WHERE email=? AND status='waiting'",
                            (email,)).fetchone():
                return {'ok': False, 'error': 'duplicate waiting hire'}, 409
        # 어느 자리에 앉히는가 — 카드를 찾고, 앉힐 수 있는 상태인지 본다
        op_code = str(payload.get('opening_code') or '').strip() or None
        op_id   = payload.get('opening_id')
        opening = None
        if op_code or op_id:
            if op_code:
                opening = conn.execute(
                    'SELECT * FROM job_openings WHERE code=?', (op_code,)).fetchone()
            else:
                opening = conn.execute(
                    'SELECT * FROM job_openings WHERE id=?', (op_id,)).fetchone()
            if not opening:
                return {'ok': False, 'error': 'opening not found',
                        'detail': op_code or op_id}, 404
            if opening['status'] not in ('approved', 'open'):
                return {'ok': False, 'error': 'opening not available',
                        'detail': '포지션 %s 는 이미 %s 상태입니다'
                                  % (opening['code'], opening['status'])}, 409
            taken = conn.execute(
                "SELECT id, name FROM incoming_hires "
                "WHERE opening_id=? AND status='waiting'", (opening['id'],)).fetchone()
            if taken:
                return {'ok': False, 'error': 'opening already claimed',
                        'detail': '포지션 %s 는 %s님이 이미 예약했습니다'
                                  % (opening['code'], taken['name'])}, 409

        cur = conn.execute(
            'INSERT INTO incoming_hires (name, email, phone, start_date, department_name, '
            'position_name, job_title, salary, memo, source, opening_id, req_ref, external_ref) '
            "VALUES (?,?,?,?,?,?,?,?,?,'webhook',?,?,?)",
            (name, email, str(payload.get('phone') or '').strip() or None, start,
             str(payload.get('department') or '').strip() or None,
             str(payload.get('position') or '').strip() or None,
             str(payload.get('job_title') or '').strip() or None,
             salary, str(payload.get('memo') or '').strip() or None,
             opening['id'] if opening else None,
             str(payload.get('req_ref') or '').strip() or None,
             str(payload.get('candidate_ref') or '').strip() or None)
        )
        # 관리자에게 인앱 알림
        admins = conn.execute("SELECT id FROM users WHERE role='admin' AND status='active'").fetchall()
        for a in admins:
            conn.execute(
                'INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                (a['id'], 'action', 'action', '입사 예정자 수신',
                 f'외부 ATS에서 합격자 {name}님이 등록되었습니다.'
                 + (f" 포지션: {opening['code']}" if opening else '')
                 + (f' 입사 예정일: {start}' if start else ''),
                 '/hires')
            )
        conn.commit()
        return {'ok': True, 'id': cur.lastrowid,
                'opening_code': opening['code'] if opening else None}, 201
    finally:
        conn.close()


@app.route('/api/openings', methods=['GET'])
def openings_api():
    """자리 카드 내주기 — Hire(ATS)가 "이 공고에 남은 자리가 몇이고, 각 자리는
    몇 레벨인가"를 당겨갈 때 쓰는 문 (T5).

    명부와 같은 이유로 당겨가기다. 자리는 채워지고 닫히고 늘어난다.
    밀어주면 한 번 실패한 전송이 두 시스템을 어긋난 채로 남긴다.

    인증: X-API-Token (/api/hires · /api/directory 와 같은 열쇠)
    질의: ?req_ref=REQ-12  또는  ?codes=OP-12-1,OP-12-2

    상태는 카드에 적힌 것 그대로 보낸다.
      approved/open  아직 비어 있다
      reserved       입사 예정자가 예약했다(오퍼 수락 → 아직 입사 전)
      filled         사람이 앉았다
      closed         닫혔다
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401

    req_ref = (request.args.get('req_ref') or '').strip()
    codes   = [c.strip() for c in (request.args.get('codes') or '').split(',') if c.strip()]
    if not req_ref and not codes:
        return {'ok': False, 'error': 'req_ref or codes required'}, 400

    sql = ('SELECT o.*, p.level AS pos_level, p.name AS pos_name, jf.name AS jf_name, '
           '       ih.name AS reserved_name, ih.start_date AS reserved_start, '
           '       u.name AS hired_name '
           'FROM job_openings o '
           'LEFT JOIN positions p ON o.position_id=p.id '
           'LEFT JOIN job_families jf ON o.job_family_id=jf.id '
           "LEFT JOIN incoming_hires ih ON ih.opening_id=o.id AND ih.status='waiting' "
           'LEFT JOIN users u ON o.hired_user_id=u.id ')
    params = []
    if req_ref:
        try:
            rid = int(str(req_ref).upper().replace('REQ-', '').strip())
        except ValueError:
            return {'ok': False, 'error': 'bad req_ref'}, 400
        sql += 'WHERE o.requisition_id=? '
        params.append(rid)
    else:
        sql += 'WHERE o.code IN (%s) ' % ','.join('?' * len(codes))
        params.extend(codes)
    sql += 'ORDER BY o.seq'

    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    seats = []
    for r in rows:
        state = r['status']
        if state in ('approved', 'open') and r['reserved_name']:
            state = 'reserved'
        seats.append({
            'code':   r['code'],
            'seq':    r['seq'],
            'state':  state,
            'open':   state in ('approved', 'open'),
            'level':  r['pos_level'],
            'level_label': (r['pos_name'] or ('L%s' % r['pos_level'] if r['pos_level'] else '')).strip()
                           if r['pos_level'] else (r['pos_name'] or ''),
            'family': r['jf_name'],
            'band':   [int((r['salary_min'] or 0) // 10000), int((r['salary_max'] or 0) // 10000)],
            'title':  r['title'],
            'who':    r['hired_name'] or r['reserved_name'],
            'start':  r['reserved_start'],
            'req_ref': 'REQ-%d' % r['requisition_id'],
        })
    return {'ok': True, 'as_of': datetime.now().isoformat(timespec='seconds'),
            'count': len(seats),
            'open': sum(1 for s in seats if s['open']),
            'seats': seats}


@app.route('/api/workplace/start-dates', methods=['GET'])
def workplace_start_dates_api():
    """입사 가능일 내주기 — Hire 오퍼에서 입사일을 고를 때 쓴다 (회의실·온보딩 V1 W3).

    입사 요일·공휴일·오리엔테이션 시간은 TalentCore 것이다. Hire 는 이 목록에서만 고르게 하고,
    보낼 때 /api/hires 가 한 번 더 검사한다(어긋나면 422 + 가까운 입사일).

    인증: X-API-Token (/api/hires · /api/directory 와 같은 열쇠)
    ?from=YYYY-MM-DD (기본 오늘) &n=개수(기본 26, 최대 104)
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401
    d_from = workplace.to_date(request.args.get('from')) or date.today()
    try:
        n = max(1, min(104, int(request.args.get('n') or 26)))
    except ValueError:
        n = 26
    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        st = workplace.settings(conn)
        dates = workplace.next_start_dates(conn, d_from, n, st)
        rm = workplace.room(conn, st['orientation_room'])
    finally:
        conn.close()
    return {'ok': True,
            'weekdays': st['weekdays'],
            'weekdays_label': workplace.weekdays_label(st['weekdays']),
            'orientation': {'room': st['orientation_room'],
                            'room_name': rm['name'] if rm else '',
                            'start': st['orientation_start'], 'end': st['orientation_end']},
            'dates': [{'date': d.isoformat(), 'weekday': workplace.WEEKDAY_KO[d.weekday()]} for d in dates]}


def _iv_slot(date_s, start_s, end_s):
    """Hire 면접 시간(분 단위 자유)을 회의실 격자(30분)에 맞춰 바깥으로 넓힌다. 14:10~14:55 → 14:00~15:00"""
    d = workplace.to_date(date_s)
    try:
        s, e = workplace.hm(start_s), workplace.hm(end_s)
    except (ValueError, TypeError):
        return None, None
    if not d or e <= s:
        return None, None
    step = workplace.SLOT_MIN
    s, e = s - s % step, e + (-e) % step
    base = datetime(d.year, d.month, d.day)
    return base + timedelta(minutes=s), base + timedelta(minutes=e)


def _iv_booking(conn, ref):
    if not ref:
        return None
    b = conn.execute("SELECT * FROM room_bookings WHERE ref=? AND kind='interview' AND status='active' "
                     "ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
    if not b:
        return None
    rm = workplace.room(conn, b['room_code'])
    return {'id': b['id'], 'room': b['room_code'], 'name': rm['name'] if rm else b['room_code'],
            'floor': rm['floor'] if rm else None, 'start': b['start_at'], 'end': b['end_at'],
            'label': '%s층 %s' % (rm['floor'], rm['name']) if rm else b['room_code']}


@app.route('/api/workplace/rooms/recommend', methods=['GET'])
def workplace_rooms_recommend_api():
    """면접실 추천 — Hire 가 면접 시간이 정해진 뒤 부른다 (회의실·온보딩 V1 W5).

    자동 배정은 하지 않는다. 인원·성격에 따라 맞는 방이 달라서, 추천만 내주고 채용 담당이 고른다.
    인증: X-API-Token
    ?date=YYYY-MM-DD&start=HH:MM&end=HH:MM&people=면접관+후보자 수&mode=onsite|video&ref=Hire 면접 id
    ref 를 주면 이미 잡아 둔 방(booked)도 같이 준다.
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401
    a = request.args
    s, e = _iv_slot(a.get('date'), a.get('start'), a.get('end'))
    if not s:
        return {'ok': False, 'error': '날짜·시간을 확인해 주세요'}, 400
    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        if not workplace.has_workplace(conn):
            return {'ok': True, 'configured': False, 'rooms': [], 'busy': []}
        people = a.get('people', type=int) or 2
        booked = _iv_booking(conn, (a.get('ref') or '').strip()[:120])
        # 이 면접이 이미 잡아 둔 방은 '사용 중'이 아니다 — 같은 시간이면 그대로 추천 목록에 둔다
        r = workplace.recommend_rooms(conn, s, e, people=people, mode=a.get('mode') or 'onsite',
                                      exclude_id=booked['id'] if booked else None)
        r['configured'] = True
        r['booked'] = booked
        site = (workplace.sites(conn) or [{}])[0]
        r['site'] = {'building': site.get('building', ''), 'address': site.get('address', '')}
    finally:
        conn.close()
    return r, (200 if r.get('ok') else 400)


@app.route('/api/workplace/rooms/book', methods=['POST'])
def workplace_rooms_book_api():
    """면접실 잡기 — 채용 담당이 추천 중 하나를 눌렀을 때. 같은 ref 로 다시 부르면 방·시간을 바꾼다.

    JSON {room, date, start, end, title, ref, people, booked_by, note}
    겹치면 409 + 겹친 예약 + 그 시간에 비어 있는 다른 추천.
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401
    j = request.get_json(silent=True) or {}
    s, e = _iv_slot(j.get('date'), j.get('start'), j.get('end'))
    ref = str(j.get('ref') or '').strip()[:120]
    if not s or not ref:
        return {'ok': False, 'error': '날짜·시간·ref 를 확인해 주세요'}, 400
    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        rm = workplace.room(conn, j.get('room') or '')
        if rm and rm['bookable_by'] == 'exec':
            return {'ok': False, 'error': '%s은 경영진 전용이라 면접실로 잡을 수 없습니다' % rm['name']}, 403
        old = _iv_booking(conn, ref)
        if old and old['room'] == j.get('room') and old['start'] == s.strftime('%Y-%m-%d %H:%M') \
                and old['end'] == e.strftime('%Y-%m-%d %H:%M'):
            return {'ok': True, 'booking': old, 'unchanged': True}
        if old:   # 바꾸기 — 새 방이 잡혀야만 옛 방을 놓는다(아래 실패 시 rollback)
            conn.execute("UPDATE room_bookings SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP WHERE id=?",
                         (old['id'],))
        people = int(j.get('people') or 0)
        bid, err, hit = workplace.create_booking(
            conn, j.get('room') or '', j.get('title') or '면접', s, e, kind='interview', ref=ref,
            attendees=people, note=j.get('note') or '', booked_by=(j.get('booked_by') or 'Hire'),
            skip_permission=True)
        if err:
            conn.rollback()
            body = {'ok': False, 'error': err}
            if hit:
                body['conflict'] = {'start': hit['start_at'], 'end': hit['end_at'], 'title': hit['title']}
                body['alternatives'] = workplace.recommend_rooms(conn, s, e, people=people or 2,
                                                                 mode=j.get('mode') or 'onsite')['rooms']
            return body, (409 if hit else 400)
        conn.commit()
        return {'ok': True, 'booking': _iv_booking(conn, ref), 'replaced': old}, 201
    finally:
        conn.close()


@app.route('/api/workplace/rooms/cancel', methods=['POST'])
def workplace_rooms_cancel_api():
    """면접실 놓기 — 면접이 취소되거나 화상으로 바뀌었을 때. JSON {ref}"""
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401
    ref = str((request.get_json(silent=True) or {}).get('ref') or '').strip()[:120]
    if not ref:
        return {'ok': False, 'error': 'ref required'}, 400
    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    try:
        n = conn.execute("UPDATE room_bookings SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP "
                         "WHERE ref=? AND kind='interview' AND status='active'", (ref,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'cancelled': n}


# ── Hire 로그인 넘겨주기 (H2) ─────────────────────────────────
#  Hire 에는 계정이 따로 없다. TalentCore 에 로그인한 사람이 누구인지를
#  2분짜리 서명 표에 담아 Hire 로 보내고, Hire 는 그 표를 서버끼리 다시 물어
#  (X-API-Token) 사람 정보를 받는다. 비밀번호는 Hire 로 넘어가지 않는다.
#  표를 받을 주소는 관리자가 /settings/hire 에 넣은 Hire 주소 하나뿐이다 —
#  아무 주소로나 표를 보내 주면 그 자체가 계정 탈취 통로가 된다.
SSO_HIRE_MAX_AGE = 120


def _sso_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt='hire-sso')


@app.route('/sso/hire')
def sso_hire():
    state = (request.args.get('state') or '').strip()
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', state):
        return 'Hire 로그인 요청이 올바르지 않습니다. Hire 로그인 화면에서 다시 눌러주세요.', 400
    if 'user_id' not in session or session.get('demo_mode'):
        if session.get('demo_mode'):
            session.clear()
        session['sso_next'] = request.full_path
        flash('TalentCore 계정으로 로그인하면 Hire 로 돌아갑니다.', 'info')
        return redirect(url_for('login'))
    if session.get('user_role') == 'guest':
        return 'Hire 에 들어갈 수 없는 계정입니다.', 403
    hire_url = _hire_config()['url']
    if not hire_url:
        return 'Hire 주소가 설정돼 있지 않습니다. 관리자에게 설정 > Hire 연동을 요청하세요.', 409
    t = _sso_serializer().dumps({'t': session.get('tenant_id', 1), 'u': session['user_id'], 's': state})
    log_audit('login', 'auth', session['user_id'], 'Hire 로그인 표 발급 (SSO)')
    return redirect(hire_url + '/api/auth/core?t=' + t)


@app.route('/api/sso/verify', methods=['POST'])
def sso_hire_verify():
    """Hire 가 받은 표를 확인한다. 표 발급 테넌트와 토큰 테넌트가 같아야 한다."""
    from itsdangerous import BadSignature, SignatureExpired
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401
    body = request.get_json(silent=True) or {}
    try:
        data = _sso_serializer().loads(str(body.get('t') or ''), max_age=SSO_HIRE_MAX_AGE)
    except SignatureExpired:
        return {'ok': False, 'error': 'expired'}, 400
    except BadSignature:
        return {'ok': False, 'error': 'bad ticket'}, 400
    if int(data.get('t') or 0) != int(tenant['id']):
        return {'ok': False, 'error': 'tenant mismatch'}, 403

    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        u = conn.execute(
            'SELECT u.id, u.emp_no, u.name, u.email, u.role, u.status, d.name AS dept, p.name AS position '
            'FROM users u LEFT JOIN departments d ON d.id = u.department_id '
            'LEFT JOIN positions p ON p.id = u.position_id WHERE u.id = ?',
            (data.get('u'),)).fetchone()
    finally:
        conn.close()
    if not u or (u['status'] or 'active') != 'active' or u['role'] == 'guest':
        return {'ok': False, 'error': 'inactive user'}, 403
    return {'ok': True, 'state': data.get('s'), 'user': {
        'id': 'core:%s:%s' % (tenant['id'], u['id']),
        'emp_no': u['emp_no'] or '',
        'name': u['name'] or '',
        'email': (u['email'] or '').strip().lower() or None,
        'dept': u['dept'] or '',
        'title': u['position'] or '',
        'role': u['role'] or 'employee',
    }}


@app.route('/api/directory', methods=['GET'])
def directory_api():
    """직원 명부 내주기 — Hire(ATS)가 면접관 명단을 당겨갈 때 쓰는 문 (T4).

    왜 '당겨가기'인가:
      조직도는 계속 바뀐다. 바뀔 때마다 밀어주면 한 번 실패한 전송이
      두 시스템을 어긋난 채로 남긴다. 당겨가기는 다음 회차에 저절로 맞춰진다.
      Greenhouse·Workday·Ashby 전부 이 방향이다.

    인증: X-API-Token 헤더 (테넌트별 토큰, /hires 화면에서 발급 — /api/hires 와 같은 열쇠)

    퇴사자를 빼고 보내지 않는다. 재직 여부를 active 로 붙여서 '전원'을 보낸다.
    빠진 사람을 지우는 방식이면, 이쪽 필터가 한 번 잘못될 때 저쪽 면접관이
    통째로 사라진다. 명단에서 지우는 판단은 받는 쪽이 하게 둔다.

    이름·직함·부서·메일·재직여부는 TalentCore 것이다. 면접 역할·EA·알림 채널·
    응답 기준은 Hire 것이라 여기서 보내지 않는다.
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401

    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT u.id, u.emp_no, u.name, u.email, u.role, u.status, '
            '       u.employment_type, u.hire_date, '
            '       d.name AS dept, p.name AS position, p.level AS level, '
            '       m.emp_no AS manager_emp_no '
            'FROM users u '
            'LEFT JOIN departments d ON d.id = u.department_id '
            'LEFT JOIN positions   p ON p.id = u.position_id '
            'LEFT JOIN users       m ON m.id = u.manager_id '
            "WHERE COALESCE(u.role,'') != 'guest' "
            'ORDER BY u.emp_no, u.id'
        ).fetchall()
    finally:
        conn.close()

    people = []
    for r in rows:
        # 사번이 없는 계정은 연결 열쇠가 없다 — 메일로 대신 잇게 두되 사번 칸은 비운다.
        if not (r['emp_no'] or r['email']):
            continue
        people.append({
            'emp_no': r['emp_no'] or '',
            'name': r['name'] or '',
            'email': (r['email'] or '').strip().lower() or None,
            'title': r['position'] or '',
            # 직급 레벨(1~9). Hire 가 '실장(L8) 이상이 면접관이면 발송 전에 한 번
            # 확인' 규칙을 걸려면 이 값이 있어야 한다. 이름만으로는 판단이 안 된다.
            'level': r['level'],
            'dept': r['dept'] or '',
            'role': r['role'] or 'employee',      # admin / manager / recruiter / employee
            'active': (r['status'] or 'active') == 'active',
            'employment_type': r['employment_type'] or None,
            'hire_date': r['hire_date'] or None,
            'manager_emp_no': r['manager_emp_no'] or None,
        })

    # 회사 이름도 같이 내준다. Hire 가 후보자에게 보내는 메일에 쓴다 —
    # 회사 이름의 주인은 TalentCore 이므로 저쪽에 따로 적어 두게 하면 언젠가 어긋난다.
    try:
        conn2 = sqlite3.connect(get_tenant_db_path(tenant['id']))
        conn2.row_factory = sqlite3.Row
        row = conn2.execute("SELECT value FROM company_settings WHERE key='name'").fetchone()
        conn2.close()
        org = (row['value'] if row else '') or _COMPANY_DEFAULTS['name']
    except Exception:
        org = _COMPANY_DEFAULTS['name']

    return {
        'ok': True,
        'as_of': datetime.now().isoformat(timespec='seconds'),
        'org': org,
        'count': len(people),
        'active': sum(1 for p in people if p['active']),
        'people': people,
    }


@app.route('/api/training/snapshot', methods=['GET'])
def training_snapshot_api():
    """연습(교육) 검사용 스냅숏 — Grow 교육 모드가 "배우는 사람이 진짜 화면에서
    무엇을 했는가"를 확인할 때 쓰는 문.

    읽기 전용이다. 쓰기는 두지 않는다 — 검사기가 데이터를 고치기 시작하면
    배운 게 아니라 채점이 결과를 만들어 버린다.

    인증: X-API-Token (/api/directory · /api/openings 와 같은 열쇠)
    질의: ?since=YYYY-MM-DD HH:MM:SS (그 시각 이후 만들어진 요청서만) · ?limit=N
    """
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return {'ok': False, 'error': 'invalid token'}, 401

    since = (request.args.get('since') or '').strip()
    try:
        limit = max(1, min(int(request.args.get('limit') or 30), 100))
    except ValueError:
        limit = 30

    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    try:
        args, where = [], ''
        if since:
            where = 'WHERE r.created_at >= ? '
            args.append(since)
        reqs = conn.execute(
            'SELECT r.id, r.title, r.status, r.hire_type, r.headcount, r.department_id, '
            '       r.requester_id, r.target_start_date, r.salary_min, r.salary_max, '
            '       r.backfill_user_id, r.collab_leader_id, r.created_at, r.updated_at, '
            '       d.name AS dept, u.name AS requester, bu.name AS backfill_name '
            'FROM job_requisitions r '
            'LEFT JOIN departments d ON d.id = r.department_id '
            'LEFT JOIN users u  ON u.id  = r.requester_id '
            'LEFT JOIN users bu ON bu.id = r.backfill_user_id '
            + where +
            'ORDER BY r.id DESC LIMIT ?', args + [limit]).fetchall()

        ids   = [r['id'] for r in reqs]
        marks = ','.join('?' * len(ids))
        lines = apprs = opens = []
        if ids:
            lines = conn.execute(
                'SELECT requisition_id, seq, headcount, position_id, salary_min, salary_max '
                'FROM requisition_lines WHERE requisition_id IN (%s) ORDER BY seq' % marks,
                ids).fetchall()
            apprs = conn.execute(
                'SELECT requisition_id, step_no, label, role_kind, status, approver_id, acted_at '
                'FROM requisition_approvals WHERE requisition_id IN (%s) ORDER BY requisition_id, step_no' % marks,
                ids).fetchall()
            opens = conn.execute(
                'SELECT o.id, o.requisition_id, o.seq, o.code, o.title, o.status, '
                '       o.external_ref, o.created_at, d.name AS dept '
                'FROM job_openings o LEFT JOIN departments d ON d.id = o.department_id '
                'WHERE o.requisition_id IN (%s) ORDER BY o.id' % marks, ids).fetchall()
        hires = conn.execute(
            'SELECT id, name, start_date, department_name, position_name, status, '
            '       opening_id, req_ref, created_at '
            'FROM incoming_hires ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    finally:
        conn.close()

    def rows_of(rows, rid):
        return [dict(x) for x in rows if x['requisition_id'] == rid]

    return {
        'ok': True,
        'as_of': datetime.now().isoformat(timespec='seconds'),
        'requisitions': [dict(r, lines=rows_of(lines, r['id']),
                              approvals=rows_of(apprs, r['id']),
                              openings=rows_of(opens, r['id'])) for r in reqs],
        'incoming_hires': [dict(h) for h in hires],
    }


# ── 연습(교육) 회사 — Grow 교육 모드 ─────────────────────────
# 연습 테넌트(is_training=1)에만 열린다. 실제 회사 데이터는 다른 DB 파일에 있고
# 이 아래 어떤 길로도 닿지 않는다.

def _training_tenant_or_error():
    """X-API-Token 으로 연습 테넌트를 찾는다. (tenant, 오류응답) 중 하나를 채워 준다."""
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return None, ({'ok': False, 'error': 'invalid token'}, 401)
    try:
        is_training = bool(tenant['is_training'])
    except (IndexError, KeyError):
        is_training = False
    if not is_training:
        return None, ({'ok': False, 'error': 'not a training tenant'}, 403)
    return tenant, None


@app.route('/api/training/ticket', methods=['POST'])
def training_ticket_api():
    """연습 회사 입장표 발급 — 2분짜리 일회용. Grow 서버만 부른다(토큰 필요).

    비밀번호 없는 입장을 이렇게 만든 이유: 링크에 긴 비밀값을 박아 두면
    그 링크가 곧 열쇠가 된다. 한 번 쓰면 죽는 표는 새어 나가도 쓸 수 없다.
    """
    tenant, err = _training_tenant_or_error()
    if err:
        return err
    ticket = issue_training_ticket(tenant['id'])
    base = request.url_root.rstrip('/')
    return {'ok': True, 'ticket': ticket,
            'url': f'{base}/training/enter?t={ticket}',
            'seconds': TRAINING_TICKET_SECONDS}


@app.route('/api/training/reset', methods=['POST'])
def training_reset_api():
    """연습 회사를 처음 상태로 되돌린다 — 연습 테넌트 DB만 지우고 다시 만든다."""
    tenant, err = _training_tenant_or_error()
    if err:
        return err
    if tenant['id'] == 1:
        return {'ok': False, 'error': 'refused'}, 403
    try:
        from training_company import reset_training_company
        reset_training_company(tenant['id'])
    except Exception as exc:                      # 파일이 열려 있으면 지울 수 없다
        app.logger.warning('training reset failed: %s', exc)
        return {'ok': False, 'error': 'reset failed'}, 500
    return {'ok': True, 'tenant_id': tenant['id']}


@app.route('/training/enter')
def training_enter():
    """입장표를 들고 오면 연습 회사 담당자로 로그인시킨다(비밀번호 없음)."""
    tenant = use_training_ticket(request.args.get('t', ''))
    if not tenant:
        flash('연습 회사 입장표가 만료되었습니다. Grow에서 다시 들어와 주세요.', 'error')
        return redirect(url_for('login'))

    from training_company import training_admin
    user = training_admin(get_tenant_db_path(tenant['id']))
    if not user:
        flash('연습 회사가 아직 준비되지 않았습니다.', 'error')
        return redirect(url_for('login'))

    session.clear()
    session['tenant_id']     = tenant['id']
    session['user_id']       = user['id']
    session['user_name']     = user['name']
    session['user_role']     = user['role']
    session['user_email']    = user['email']
    session['dept_name']     = user['dept_name'] or ''
    session['pos_name']      = user['pos_name']  or ''
    session['dept_id']       = user['department_id'] or 0
    session['onboarded']     = 1
    session['training_mode'] = True
    session['show_tour']     = False

    nxt = request.args.get('next', '')
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = url_for('dashboard')
    return redirect(nxt)


@app.route('/employees/<int:emp_id>/edit', methods=['GET', 'POST'])
@admin_required
def employee_edit(emp_id):
    db       = get_db()
    emp      = db.execute('SELECT * FROM users WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        abort(404)
    depts    = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses    = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs      = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    managers = db.execute(
        "SELECT id, name FROM users WHERE role IN ('admin','manager') AND status='active' AND id!=? ORDER BY name",
        (emp_id,)
    ).fetchall()
    error = None

    if request.method == 'POST':
        name             = request.form.get('name', '').strip()
        email            = request.form.get('email', '').strip()
        role             = request.form.get('role', 'employee')
        dept_id          = request.form.get('department_id') or None
        pos_id           = request.form.get('position_id') or None
        jf_id            = request.form.get('job_family_id') or None
        phone            = request.form.get('phone', '').strip() or None
        hire_date        = request.form.get('hire_date') or None
        birth_date       = request.form.get('birth_date') or None
        employment_type  = request.form.get('employment_type', 'full_time')
        work_type        = request.form.get('work_type', 'standard')
        manager_id       = request.form.get('manager_id') or None
        termination_date = request.form.get('termination_date') or None
        term_reason      = request.form.get('termination_reason', '').strip() or None
        new_pw           = request.form.get('password', '').strip()

        if not name or not email:
            error = '이름과 이메일은 필수입니다.'
        elif new_pw and validate_password(new_pw):
            error = validate_password(new_pw)
        elif db.execute('SELECT id FROM users WHERE email=? AND id!=?', (email, emp_id)).fetchone():
            error = '이미 사용 중인 이메일입니다.'
        else:
            if new_pw:
                db.execute(
                    'UPDATE users SET name=?, email=?, password_hash=?, role=?, '
                    'department_id=?, position_id=?, job_family_id=?, phone=?, '
                    'hire_date=?, birth_date=?, employment_type=?, work_type=?, manager_id=?, '
                    'termination_date=?, termination_reason=? WHERE id=?',
                    (name, email, generate_password_hash(new_pw), role,
                     dept_id, pos_id, jf_id, phone, hire_date, birth_date,
                     employment_type, work_type, manager_id, termination_date, term_reason, emp_id)
                )
            else:
                db.execute(
                    'UPDATE users SET name=?, email=?, role=?, '
                    'department_id=?, position_id=?, job_family_id=?, phone=?, '
                    'hire_date=?, birth_date=?, employment_type=?, work_type=?, manager_id=?, '
                    'termination_date=?, termination_reason=? WHERE id=?',
                    (name, email, role, dept_id, pos_id, jf_id, phone,
                     hire_date, birth_date, employment_type, work_type, manager_id,
                     termination_date, term_reason, emp_id)
                )
            db.commit()
            # ── master.db 이메일 변경 동기화 ─────────────────────
            tid = session.get('tenant_id', 1)
            old_email = emp['email']
            if old_email != email:
                update_tenant_user_email(old_email, email, tid)
            log_audit('update', 'personal_info', emp_id, f'직원 정보 수정 ({emp["name"]})')
            flash('직원 정보가 저장되었습니다.', 'success')
            return redirect(url_for('employee_detail', emp_id=emp_id))

    return render_template('employees/form.html',
                           mode='edit', depts=depts, poses=poses, jfs=jfs,
                           managers=managers, error=error, emp=emp,
                           active_page='employees')

ACTION_LABELS = {
    'dept_change':             '부서 이동',
    'position_change':         '직급 변경',
    'role_change':             '역할 변경',
    'employment_type_change':  '고용형태 변경',
    'manager_change':          '직속상관 변경',
    'salary_change':           '급여 변경',
}

@app.route('/employees/<int:emp_id>/action', methods=['POST'])
@admin_required
def employee_action(emp_id):
    from datetime import datetime as dt
    db   = get_db()
    emp  = db.execute('SELECT * FROM users WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        abort(404)

    action_type    = request.form.get('action_type')
    effective_date = request.form.get('effective_date', date.today().isoformat())
    reason         = request.form.get('reason', '').strip()

    if action_type not in ACTION_LABELS:
        flash('올바르지 않은 발령 유형입니다.', 'error')
        return redirect(url_for('employee_detail', emp_id=emp_id))

    from_value = to_value = None

    if action_type == 'dept_change':
        new_dept_id = request.form.get('new_dept_id')
        if not new_dept_id:
            flash('변경할 부서를 선택해주세요.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        old = db.execute('SELECT name FROM departments WHERE id=?', (emp['department_id'],)).fetchone()
        new = db.execute('SELECT name FROM departments WHERE id=?', (new_dept_id,)).fetchone()
        if not new:
            flash('유효하지 않은 부서입니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        from_value = old['name'] if old else '—'
        to_value   = f"{new['name']}|{new_dept_id}"

    elif action_type == 'position_change':
        new_pos_id = request.form.get('new_pos_id')
        if not new_pos_id:
            flash('변경할 직급을 선택해주세요.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        old = db.execute('SELECT name FROM positions WHERE id=?', (emp['position_id'],)).fetchone()
        new = db.execute('SELECT name FROM positions WHERE id=?', (new_pos_id,)).fetchone()
        if not new:
            flash('유효하지 않은 직급입니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        from_value = old['name'] if old else '—'
        to_value   = f"{new['name']}|{new_pos_id}"

    elif action_type == 'role_change':
        role_labels = {'employee':'Employee','manager':'Manager','recruiter':'Recruiter','admin':'HR Admin'}
        new_role   = request.form.get('new_role')
        if new_role not in role_labels:
            flash('유효하지 않은 역할입니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        from_value = role_labels.get(emp['role'], emp['role'])
        to_value   = new_role

    elif action_type == 'employment_type_change':
        et_labels  = {'full_time':'정규직','part_time':'시간제','contract':'계약직','intern':'인턴'}
        new_et     = request.form.get('new_employment_type')
        if new_et not in et_labels:
            flash('유효하지 않은 고용형태입니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        from_value = et_labels.get(emp['employment_type'], emp['employment_type'])
        to_value   = new_et

    elif action_type == 'manager_change':
        new_mgr_id = request.form.get('new_manager_id') or None
        if new_mgr_id and str(new_mgr_id) == str(emp_id):
            flash('본인을 직속상관으로 지정할 수 없습니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        old = db.execute('SELECT name FROM users WHERE id=?', (emp['manager_id'],)).fetchone() if emp['manager_id'] else None
        new = db.execute('SELECT name FROM users WHERE id=?', (new_mgr_id,)).fetchone() if new_mgr_id else None
        from_value = old['name'] if old else '없음'
        to_value   = f"{new['name'] if new else '없음'}|{new_mgr_id or ''}"

    elif action_type == 'salary_change':
        new_salary = int(request.form.get('new_salary', 0) or 0)
        if new_salary <= 0:
            flash('급여는 0보다 커야 합니다.', 'error')
            return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')
        old_row    = db.execute('SELECT base_salary FROM employee_salary WHERE user_id=?', (emp_id,)).fetchone()
        from_value = str(old_row['base_salary']) if old_row else '0'
        to_value   = str(new_salary)

    cur = db.execute(
        'INSERT INTO personnel_actions '
        '(user_id, action_type, from_value, to_value, effective_date, reason, status, processed_by) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (emp_id, action_type, from_value, to_value, effective_date, reason, 'pending', session['user_id'])
    )
    new_action_id = cur.lastrowid
    db.commit()

    # 결재선 설정: 관리자 전결이면 기안 즉시 승인·반영 (Phase C-13)
    if session.get('user_role') == 'admin' and get_approval_chain(db, 'personnel_action') == 'auto':
        pa = db.execute('SELECT * FROM personnel_actions WHERE id=?', (new_action_id,)).fetchone()
        today = date.today().isoformat()
        if pa['effective_date'] > today:
            db.execute("UPDATE personnel_actions SET status='approved', processed_by=?, applied_at=NULL WHERE id=?",
                       (session['user_id'], new_action_id))
            db.commit()
            add_notification(emp_id, 'info', 'action', '인사발령 확정 (미래발령)',
                             f'{pa["effective_date"]}에 {ACTION_LABELS[action_type]}이(가) 자동 반영될 예정입니다.',
                             url_for('employee_detail', emp_id=emp_id))
            flash(f'인사발령({ACTION_LABELS[action_type]})이 전결 처리되었습니다. 발령일({pa["effective_date"]})에 자동 반영됩니다.', 'success')
        else:
            _do_apply_action(db, pa)
            db.execute("UPDATE personnel_actions SET status='approved', processed_by=?, applied_at=CURRENT_TIMESTAMP WHERE id=?",
                       (session['user_id'], new_action_id))
            db.commit()
            add_notification(emp_id, 'info', 'action', '인사발령 처리 완료',
                             f'귀하에 대한 {ACTION_LABELS[action_type]} 처리가 완료되었습니다.',
                             url_for('employee_detail', emp_id=emp_id))
            flash(f'인사발령({ACTION_LABELS[action_type]})이 전결 처리되어 즉시 반영되었습니다.', 'success')
        log_audit('update', 'personal_info', emp_id, f'인사발령 전결 처리 — {ACTION_LABELS[action_type]}')
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')

    # 알림 발송: HR 전체에게 승인 대기 알림
    admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
    for admin in admins:
        add_notification(
            admin['id'], 'action', 'action',
            f"인사발령 기안: {emp['name']}",
            f"{emp['name']}님에 대한 {ACTION_LABELS[action_type]} 기안이 생성되었습니다.",
            url_for('employee_detail', emp_id=emp_id) + '#hr'
        )

    flash(f'인사발령({ACTION_LABELS[action_type]}) 기안이 완료되었습니다. 최종 승인 후 반영됩니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')


@app.route('/personnel-actions/<int:action_id>/approve', methods=['POST'])
@admin_required
def personnel_action_approve(action_id):
    db = get_db()
    pa = db.execute('SELECT * FROM personnel_actions WHERE id=?', (action_id,)).fetchone()
    if not pa: abort(404)
    if pa['status'] != 'pending':
        flash('이미 처리된 발령입니다.', 'error')
        return redirect(url_for('employee_detail', emp_id=pa['user_id']) + '#hr')

    emp_id = pa['user_id']
    a_type = pa['action_type']
    today  = date.today().isoformat()
    is_future = pa['effective_date'] > today

    if is_future:
        # 미래발령: 승인만 하고 실제 반영은 발령일에 자동 처리
        db.execute(
            "UPDATE personnel_actions SET status='approved', processed_by=?, applied_at=NULL WHERE id=?",
            (session['user_id'], action_id)
        )
        db.commit()
        add_notification(
            emp_id, 'info', 'action',
            '인사발령 승인 완료 (미래발령)',
            f'{pa["effective_date"]}에 발령이 자동 반영될 예정입니다.',
            url_for('employee_detail', emp_id=emp_id)
        )
        flash(f'인사발령이 승인되었습니다. 발령일({pa["effective_date"]})에 자동 반영됩니다.', 'success')
        return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')

    # 즉시발령: 바로 반영
    _do_apply_action(db, pa)
    db.execute(
        "UPDATE personnel_actions SET status='approved', processed_by=?, applied_at=CURRENT_TIMESTAMP WHERE id=?",
        (session['user_id'], action_id)
    )
    db.commit()

    # 알림 발송: 본인에게 발령 완료 알림
    add_notification(
        emp_id, 'info', 'action',
        "인사발령 처리 완료",
        f"귀하에 대한 {ACTION_LABELS[a_type]} 처리가 승인 및 반영되었습니다.",
        url_for('employee_detail', emp_id=emp_id)
    )
    # Slack DM: 본인에게 발령 확정 알림
    _pa_user = db.execute('SELECT email, name FROM users WHERE id=?', (emp_id,)).fetchone()
    if _pa_user and _pa_user['email']:
        from integrations.dispatcher import notify_slack
        notify_slack(
            _pa_user['email'],
            f"[TalentCore] 인사발령 확정\n"
            f"{ACTION_LABELS.get(a_type, a_type)} 발령이 처리됐습니다.\n"
            f"발령일: {pa['effective_date']}\n"
            f"TalentCore > 내 정보에서 변경사항을 확인하세요.",
            '인사발령 확정',
            name=_pa_user['name']
        )

    flash('인사발령이 최종 승인 및 반영되었습니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=emp_id) + '#hr')


@app.route('/personnel-actions/<int:action_id>/reject', methods=['POST'])
@admin_required
def personnel_action_reject(action_id):
    db = get_db()
    pa = db.execute('SELECT * FROM personnel_actions WHERE id=?', (action_id,)).fetchone()
    if not pa: abort(404)
    reason = request.form.get('reason', '').strip()
    db.execute(
        "UPDATE personnel_actions SET status='rejected', rejection_reason=?, processed_by=? WHERE id=?",
        (reason, session['user_id'], action_id)
    )
    db.commit()

    # 알림 발송: 본인(대상자)에게는 상황에 따라 필요 없을 수 있으나, 기안자에게 알리는 것이 Workday 표준
    # 여기서는 대상자에게도 반려 알림을 보냄
    add_notification(
        pa['user_id'], 'info', 'action',
        "인사발령 기안 반려",
        f"귀하에 대한 인사발령 기안이 반려되었습니다. (사유: {reason or '미기재'})",
        url_for('employee_detail', emp_id=pa['user_id'])
    )

    flash('인사발령 기안이 반려되었습니다.', 'warning')
    return redirect(url_for('employee_detail', emp_id=pa['user_id']) + '#hr')


@app.route('/termination/my', methods=['GET', 'POST'])
@login_required
def termination_my():
    if session.get('user_role') == 'guest':
        abort(403)

    db = get_db()
    uid = session['user_id']
    employee = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name, m.name AS manager_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        'LEFT JOIN users m ON u.manager_id = m.id '
        'WHERE u.id=?',
        (uid,)
    ).fetchone()
    if not employee:
        abort(404)

    open_request = db.execute(
        "SELECT * FROM termination_requests "
        "WHERE user_id=? AND status IN ('submitted','under_review','approved','in_progress') "
        'ORDER BY created_at DESC LIMIT 1',
        (uid,)
    ).fetchone()

    if request.method == 'POST':
        if open_request:
            flash('이미 진행 중인 퇴직 신청이 있습니다.', 'error')
            return redirect(url_for('termination_my'))

        request_type = request.form.get('request_type', 'voluntary')
        reason_code = request.form.get('reason_code') or 'other'
        requested_last_work_date = request.form.get('requested_last_work_date') or date.today().isoformat()
        requested_termination_date = request.form.get('requested_termination_date') or requested_last_work_date
        reason_detail = request.form.get('reason_detail', '').strip() or None
        handover_note = request.form.get('handover_note', '').strip() or None

        if request_type not in TERMINATE_TYPES:
            flash('Invalid termination type.', 'error')
            return redirect(url_for('termination_my'))
        if reason_code not in TERMINATION_REASON_CODES:
            reason_code = 'other'
        if requested_termination_date < requested_last_work_date:
            flash('퇴직일은 마지막 근무일과 같거나 이후여야 합니다.', 'error')
            return redirect(url_for('termination_my'))

        db.execute(
            'INSERT INTO termination_requests '
            '(user_id, request_type, request_source, status, notice_date, '
            ' requested_last_work_date, requested_termination_date, reason_code, reason_detail, '
            ' handover_note, created_by) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (
                uid, request_type, 'employee', 'submitted', date.today().isoformat(),
                requested_last_work_date, requested_termination_date, reason_code,
                reason_detail, handover_note, uid
            )
        )
        db.commit()
        # Slack DM: HR + 직속 매니저에게 퇴직 신청 알림
        _term_emp = db.execute(
            'SELECT u.name, u.manager_id, d.name AS dept_name FROM users u '
            'LEFT JOIN departments d ON d.id=u.department_id WHERE u.id=?', (uid,)
        ).fetchone()
        if _term_emp:
            from integrations.dispatcher import notify_slack_multi
            _hr_rows2 = db.execute("SELECT email, name FROM users WHERE role='admin' AND status='active'").fetchall()
            _targets2 = [(r['email'], r['name']) for r in _hr_rows2 if r['email']]
            if _term_emp['manager_id']:
                _mgr_row = db.execute('SELECT email, name FROM users WHERE id=?', (_term_emp['manager_id'],)).fetchone()
                if _mgr_row and _mgr_row['email']:
                    _targets2.append((_mgr_row['email'], _mgr_row['name']))
            notify_slack_multi(
                _targets2,
                f"[TalentCore] 퇴직 신청 접수\n"
                f"{_term_emp['name']}님({_term_emp['dept_name'] or ''})이 퇴직 신청서를 제출했습니다.\n"
                f"마지막 근무 예정일: {requested_last_work_date}\n"
                f"TalentCore > 퇴직 관리에서 확인 및 승인해주세요.",
                '퇴직 신청 접수'
            )
        flash('퇴직 신청이 접수되었습니다.', 'success')
        return redirect(url_for('termination_my'))

    history = db.execute(
        'SELECT tr.*, u.name AS created_by_name '
        'FROM termination_requests tr '
        'LEFT JOIN users u ON tr.created_by = u.id '
        'WHERE tr.user_id=? ORDER BY tr.created_at DESC',
        (uid,)
    ).fetchall()
    return render_template(
        'employees/termination_my.html',
        employee=employee,
        open_request=open_request,
        history=history,
        terminate_types=TERMINATE_TYPES,
        status_labels=TERMINATION_STATUS_LABELS,
        reason_codes=TERMINATION_REASON_CODES,
        today=date.today().isoformat(),
        active_page='termination_my'
    )


@app.route('/termination/requests')
@manager_or_admin
def termination_requests():
    db = get_db()
    status = request.args.get('status', '')
    params = []
    sql = (
        'SELECT tr.*, '
        'u.name AS employee_name, u.manager_id AS employee_manager_id, '
        'd.name AS dept_name, p.name AS pos_name, m.name AS manager_name '
        'FROM termination_requests tr '
        'JOIN users u ON tr.user_id = u.id '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        'LEFT JOIN users m ON u.manager_id = m.id '
    )
    where = []
    if session.get('user_role') == 'manager':
        where.append('u.manager_id = ?')
        params.append(session['user_id'])
    if status and status in TERMINATION_STATUS_LABELS:
        where.append('tr.status = ?')
        params.append(status)
    if where:
        sql += 'WHERE ' + ' AND '.join(where) + ' '
    sql += (
        "ORDER BY CASE tr.status "
        "WHEN 'submitted' THEN 1 "
        "WHEN 'under_review' THEN 2 "
        "WHEN 'approved' THEN 3 "
        "WHEN 'in_progress' THEN 4 "
        "WHEN 'completed' THEN 5 "
        "ELSE 6 END, tr.created_at DESC"
    )
    requests = db.execute(sql, params).fetchall()
    return render_template(
        'employees/termination_requests.html',
        requests=requests,
        status=status,
        status_labels=TERMINATION_STATUS_LABELS,
        terminate_types=TERMINATE_TYPES,
        active_page='termination_requests'
    )


@app.route('/termination/requests/new/<int:emp_id>', methods=['GET', 'POST'])
@manager_or_admin
def termination_request_new(emp_id):
    db = get_db()
    employee = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name, m.name AS manager_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        'LEFT JOIN users m ON u.manager_id = m.id '
        "WHERE u.id=? AND u.status='active'",
        (emp_id,)
    ).fetchone()
    if not employee:
        abort(404)
    if session.get('user_role') == 'manager' and employee['manager_id'] != session.get('user_id'):
        abort(403)

    open_request = db.execute(
        "SELECT id FROM termination_requests "
        "WHERE user_id=? AND status IN ('submitted','under_review','approved','in_progress') "
        'ORDER BY created_at DESC LIMIT 1',
        (emp_id,)
    ).fetchone()
    if open_request:
        return redirect(url_for('termination_request_detail', req_id=open_request['id']))

    if request.method == 'POST':
        request_type = request.form.get('request_type', 'mutual')
        reason_code = request.form.get('reason_code') or 'other'
        requested_last_work_date = request.form.get('requested_last_work_date') or date.today().isoformat()
        requested_termination_date = request.form.get('requested_termination_date') or requested_last_work_date
        reason_detail = request.form.get('reason_detail', '').strip() or None
        handover_note = request.form.get('handover_note', '').strip() or None

        if request_type not in TERMINATE_TYPES:
            flash('Invalid termination type.', 'error')
            return redirect(url_for('termination_request_new', emp_id=emp_id))
        if requested_termination_date < requested_last_work_date:
            flash('퇴직일은 마지막 근무일과 같거나 이후여야 합니다.', 'error')
            return redirect(url_for('termination_request_new', emp_id=emp_id))

        db.execute(
            'INSERT INTO termination_requests '
            '(user_id, request_type, request_source, status, notice_date, '
            ' requested_last_work_date, requested_termination_date, reason_code, reason_detail, '
            ' handover_note, created_by) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (
                emp_id, request_type,
                'hr' if session.get('user_role') == 'admin' else 'manager',
                'under_review',
                date.today().isoformat(),
                requested_last_work_date, requested_termination_date,
                reason_code if reason_code in TERMINATION_REASON_CODES else 'other',
                reason_detail, handover_note, session['user_id']
            )
        )
        db.commit()
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        flash('퇴직 처리가 시작되었습니다.', 'success')
        return redirect(url_for('termination_request_detail', req_id=new_id))

    return render_template(
        'employees/termination_new.html',
        employee=employee,
        terminate_types=TERMINATE_TYPES,
        reason_codes=TERMINATION_REASON_CODES,
        today=date.today().isoformat(),
        active_page='termination_requests'
    )


@app.route('/termination/requests/<int:req_id>', methods=['GET', 'POST'])
@login_required
def termination_request_detail(req_id):
    db = get_db()
    termination = db.execute(
        'SELECT tr.*, '
        'u.name AS employee_name, u.email AS employee_email, u.hire_date, u.status AS employee_status, '
        'u.manager_id AS employee_manager_id, d.name AS dept_name, p.name AS pos_name, '
        'm.name AS manager_name, c.name AS created_by_name, '
        'ma.name AS manager_approved_name, ha.name AS hr_approved_name '
        'FROM termination_requests tr '
        'JOIN users u ON tr.user_id = u.id '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        'LEFT JOIN users m ON u.manager_id = m.id '
        'LEFT JOIN users c ON tr.created_by = c.id '
        'LEFT JOIN users ma ON tr.manager_approved_by = ma.id '
        'LEFT JOIN users ha ON tr.hr_approved_by = ha.id '
        'WHERE tr.id=?',
        (req_id,)
    ).fetchone()
    if not termination:
        abort(404)

    can_manage = can_manage_termination_request(termination)
    can_view = can_manage or termination['user_id'] == session.get('user_id')
    if not can_view:
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'cancel_request' and termination['user_id'] == session['user_id']:
            if termination['status'] not in ('submitted', 'under_review'):
                flash('이미 처리 중인 요청은 취소할 수 없습니다.', 'error')
            else:
                db.execute(
                    "UPDATE termination_requests SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (req_id,)
                )
                db.commit()
                flash('퇴직 신청이 취소되었습니다.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'manager_approve':
            if not can_manage:
                abort(403)
            if termination['manager_approved_by']:
                flash('이미 매니저 검토가 완료된 요청입니다.', 'error')
            else:
                next_status = 'under_review' if session.get('user_role') == 'manager' else termination['status']
                db.execute(
                    'UPDATE termination_requests '
                    'SET manager_approved_by=?, manager_approved_at=CURRENT_TIMESTAMP, '
                    'status=?, updated_at=CURRENT_TIMESTAMP '
                    'WHERE id=?',
                    (session['user_id'], next_status, req_id)
                )
                db.commit()
                flash('매니저 검토가 완료되었습니다.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'reject_request':
            if not can_manage:
                abort(403)
            rejection_reason = request.form.get('rejection_reason', '').strip() or '검토 후 반려되었습니다.'
            db.execute(
                "UPDATE termination_requests "
                "SET status='rejected', rejection_reason=?, updated_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (rejection_reason, req_id)
            )
            db.commit()
            flash('퇴직 신청이 반려되었습니다.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'hr_approve':
            if session.get('user_role') != 'admin':
                abort(403)
            if not termination['manager_approved_by']:
                flash('HR 승인 전 매니저 검토가 필요합니다.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            final_last_work_date = request.form.get('final_last_work_date') or termination['requested_last_work_date']
            final_termination_date = request.form.get('final_termination_date') or termination['requested_termination_date']
            if final_termination_date < final_last_work_date:
                flash('퇴직일은 마지막 근무일과 같거나 이후여야 합니다.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            is_regrettable     = 1 if request.form.get('is_regrettable') == '1' else 0
            is_rehire_eligible = 1 if request.form.get('is_rehire_eligible') == '1' else 0
            exit_reason_cat    = request.form.get('exit_reason_category') or termination['reason_code'] or None

            db.execute(
                "UPDATE termination_requests "
                "SET hr_approved_by=?, hr_approved_at=CURRENT_TIMESTAMP, "
                "final_last_work_date=?, final_termination_date=?, "
                "is_regrettable=?, is_rehire_eligible=?, exit_reason_category=?, "
                "status='in_progress', updated_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (session['user_id'], final_last_work_date, final_termination_date,
                 is_regrettable, is_rehire_eligible, exit_reason_cat, req_id)
            )
            create_offboarding_tasks(db, req_id, final_last_work_date)
            db.commit()
            flash('HR 승인이 완료되었습니다. 오프보딩 태스크가 생성되었습니다.', 'success')
            # ── 외부 서비스 연동 트리거 ───────────────────────
            try:
                from integrations.dispatcher import on_employee_terminated
                emp_row = db.execute(
                    "SELECT u.name, u.email FROM users u "
                    "JOIN termination_requests tr ON tr.user_id=u.id WHERE tr.id=?", (req_id,)
                ).fetchone()
                if emp_row:
                    on_employee_terminated({
                        'name':           emp_row['name'],
                        'email':          emp_row['email'],
                        'last_work_date': final_last_work_date,
                    })
            except Exception as _ie:
                app.logger.warning(f'Integration error on employee_terminated: {_ie}')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'complete_task':
            task_id = request.form.get('task_id')
            task = db.execute(
                'SELECT * FROM offboarding_tasks WHERE id=? AND request_id=?',
                (task_id, req_id)
            ).fetchone()
            if not task:
                abort(404)

            allowed = (
                session.get('user_role') == 'admin' or
                (task['owner_role'] == 'employee' and termination['user_id'] == session['user_id']) or
                (task['owner_role'] == 'manager' and session.get('user_role') == 'manager' and can_manage)
            )
            if not allowed:
                abort(403)

            db.execute(
                "UPDATE offboarding_tasks "
                "SET status='completed', note=?, completed_by=?, completed_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (request.form.get('task_note', '').strip() or None, session['user_id'], task_id)
            )
            db.commit()
            flash('Task marked as completed.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'finalize_termination':
            if session.get('user_role') != 'admin':
                abort(403)
            if termination['status'] != 'in_progress':
                flash('This request is not ready for finalization.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            pending_tasks = db.execute(
                "SELECT COUNT(*) FROM offboarding_tasks WHERE request_id=? AND status='pending'",
                (req_id,)
            ).fetchone()[0]
            if pending_tasks:
                flash('모든 오프보딩 태스크를 완료한 후 최종 처리할 수 있습니다.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            term_date = request.form.get('final_termination_date') or termination['final_termination_date'] or termination['requested_termination_date']
            last_work_date = request.form.get('final_last_work_date') or termination['final_last_work_date'] or termination['requested_last_work_date']
            payslips = db.execute(
                'SELECT year, month, gross_pay FROM payslips '
                "WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 3",
                (termination['user_id'],)
            ).fetchall()
            preview = calc_severance(termination['hire_date'] or '', term_date, [dict(r) for r in payslips])
            severance_note = request.form.get('completion_note', '').strip() or None

            db.execute(
                "UPDATE users "
                "SET status='resigned', termination_date=?, termination_reason=? "
                'WHERE id=?',
                (
                    term_date,
                    request.form.get('termination_reason', '').strip() or TERMINATE_TYPES.get(termination['request_type'], ''),
                    termination['user_id']
                )
            )

            existing = db.execute(
                'SELECT id FROM severance_payments WHERE user_id=? AND termination_date=?',
                (termination['user_id'], term_date)
            ).fetchone()
            if preview.get('eligible') and not existing:
                db.execute(
                    'INSERT INTO severance_payments '
                    '(user_id, hire_date, termination_date, tenure_days, '
                    ' basis_total_pay, basis_days, avg_daily_wage, severance_amount, note, processed_by) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (
                        termination['user_id'], termination['hire_date'], term_date,
                        preview['tenure_days'], preview.get('basis_total_pay', 0),
                        preview.get('basis_days', 92), preview.get('avg_daily_wage', 0),
                        preview['severance_amount'], severance_note, session['user_id']
                    )
                )

            db.execute(
                "UPDATE termination_requests "
                "SET status='completed', final_last_work_date=?, final_termination_date=?, "
                "completed_by=?, completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (last_work_date, term_date, session['user_id'], req_id)
            )
            db.commit()
            flash('퇴직 처리가 최종 완료되었습니다.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

    tasks = db.execute(
        'SELECT t.*, u.name AS completed_by_name '
        'FROM offboarding_tasks t '
        'LEFT JOIN users u ON t.completed_by = u.id '
        'WHERE t.request_id=? ORDER BY t.id',
        (req_id,)
    ).fetchall()
    recent_payslips = db.execute(
        'SELECT year, month, gross_pay FROM payslips '
        "WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 3",
        (termination['user_id'],)
    ).fetchall()
    preview = calc_severance(
        termination['hire_date'] or '',
        termination['final_termination_date'] or termination['requested_termination_date'],
        [dict(r) for r in recent_payslips]
    )
    return render_template(
        'employees/termination_detail.html',
        termination=termination,
        tasks=tasks,
        preview=preview,
        can_manage=can_manage,
        status_labels=TERMINATION_STATUS_LABELS,
        terminate_types=TERMINATE_TYPES,
        reason_codes=TERMINATION_REASON_CODES,
        today=date.today().isoformat(),
        active_page='termination_requests' if can_manage else 'termination_my'
    )


@app.route('/employees/<int:emp_id>/offboard', methods=['GET', 'POST'])
@admin_required
def employee_offboard(emp_id):
    db  = get_db()
    open_request = db.execute(
        "SELECT id FROM termination_requests "
        "WHERE user_id=? AND status IN ('submitted','under_review','approved','in_progress') "
        'ORDER BY created_at DESC LIMIT 1',
        (emp_id,)
    ).fetchone()
    if open_request:
        return redirect(url_for('termination_request_detail', req_id=open_request['id']))

    emp = db.execute(
        'SELECT u.*, d.name dept_name, p.name pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id=p.id '
        "WHERE u.id=? AND u.status='active'", (emp_id,)
    ).fetchone()
    if not emp:
        flash('이미 퇴직 처리된 직원이거나 존재하지 않는 직원입니다.', 'error')
        return redirect(url_for('employees'))

    recent_payslips = db.execute(
        'SELECT year, month, gross_pay FROM payslips '
        "WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 3",
        (emp_id,)
    ).fetchall()
    payslip_list = [dict(r) for r in recent_payslips]
    preview = calc_severance(emp['hire_date'] or '', date.today().isoformat(), payslip_list)

    if request.method == 'POST':
        term_type   = request.form.get('term_type', 'voluntary')
        term_date   = request.form.get('term_date') or date.today().isoformat()
        term_reason = request.form.get('term_reason', '').strip() or TERMINATE_TYPES.get(term_type, '')
        note        = request.form.get('note', '').strip() or None

        db.execute(
            "UPDATE users SET status='resigned', termination_date=?, termination_reason=? WHERE id=?",
            (term_date, term_reason, emp_id)
        )
        result = calc_severance(emp['hire_date'] or '', term_date, payslip_list)
        if result.get('eligible'):
            db.execute(
                'INSERT INTO severance_payments '
                '(user_id, hire_date, termination_date, tenure_days, '
                ' basis_total_pay, basis_days, avg_daily_wage, severance_amount, note, processed_by) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (emp_id, emp['hire_date'], term_date,
                 result['tenure_days'], result.get('basis_total_pay', 0),
                 result.get('basis_days', 92), result.get('avg_daily_wage', 0),
                 result['severance_amount'], note, session['user_id'])
            )
            flash(f'{emp["name"]} 퇴직 처리 완료 — 퇴직금 {fmt_krw(result["severance_amount"])}원 기록됨', 'success')
        else:
            flash(f'{emp["name"]} 퇴직 처리 완료 (근속 1년 미만, 퇴직금 미발생)', 'success')
        db.commit()
        return redirect(url_for('employees'))

    return render_template('employees/offboard.html',
                           emp=emp, preview=preview,
                           terminate_types=TERMINATE_TYPES,
                           today=date.today().isoformat(),
                           active_page='employees')


@app.route('/employees/<int:emp_id>/severance', methods=['GET', 'POST'])
@admin_required
def employee_severance(emp_id):
    """퇴직 종합 정산 — 퇴직금 + 미사용연차수당 + 일할급여 자동계산."""
    import calendar as _cal
    import json as _json
    db  = get_db()
    emp = db.execute(
        'SELECT u.*, d.name dept_name, p.name pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id=p.id '
        'WHERE u.id=?', (emp_id,)
    ).fetchone()
    if not emp:
        abort(404)

    # 기존 처리 내역
    existing = db.execute(
        'SELECT * FROM severance_payments WHERE user_id=? ORDER BY processed_at DESC LIMIT 1',
        (emp_id,)
    ).fetchone()

    # 최근 3개월 payslip
    recent_payslips = db.execute(
        'SELECT year, month, gross_pay FROM payslips '
        "WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 3",
        (emp_id,)
    ).fetchall()
    payslip_list = [dict(r) for r in recent_payslips]

    term_date = emp['termination_date'] or date.today().isoformat()

    # 사용 연차일수 조회 (해당 연도, 단일 소스 — 반차·병가정책 포함)
    term_year = int(term_date[:4])
    used_days = get_leave_balance(db, emp_id, year=term_year)['used']

    # 마지막 월 일할계산 파라미터
    term_dt         = date.fromisoformat(term_date)
    days_in_month   = _cal.monthrange(term_dt.year, term_dt.month)[1]
    month_start     = date(term_dt.year, term_dt.month, 1)
    days_worked_last = (term_dt - month_start).days + 1

    # base_salary가 employee_salary에 있으므로 별도 조회
    sal_row = db.execute(
        'SELECT base_salary FROM employee_salary WHERE user_id=?', (emp_id,)
    ).fetchone()
    base_salary = sal_row['base_salary'] if sal_row else 0
    # 마지막 월 파라미터 재계산 (base_salary 확보 후)
    settlement = calc_separation_settlement(
        hire_date_str        = emp['hire_date'] or '',
        termination_date_str = term_date,
        recent_payslips      = payslip_list,
        used_leave_days      = used_days,
        final_month_base_salary = base_salary,
        final_month_days_worked = days_worked_last,
        final_month_days_total  = days_in_month,
    )

    if request.method == 'POST':
        note   = request.form.get('note', '').strip() or None
        sev    = settlement['severance']
        if sev.get('eligible'):
            db.execute(
                'INSERT INTO severance_payments '
                '(user_id, hire_date, termination_date, tenure_days, '
                ' basis_total_pay, basis_days, avg_daily_wage, severance_amount, note, processed_by) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (emp_id, emp['hire_date'], term_date,
                 sev['tenure_days'], sev.get('basis_total_pay', 0),
                 sev.get('basis_days', 92), sev.get('avg_daily_wage', 0),
                 settlement['total_settlement'], note, session['user_id'])
            )
            db.commit()
            flash(
                f'퇴직 정산 완료 — 총 {fmt_krw(settlement["total_settlement"])}원 '
                f'(퇴직금 {fmt_krw(sev["severance_amount"])}원 + '
                f'미사용연차 {fmt_krw(settlement["unused_leave"]["unused_leave_pay"])}원 포함)',
                'success'
            )
        else:
            flash('근속 1년 미만으로 퇴직금은 미발생입니다.', 'info')
        return redirect(url_for('employee_detail', emp_id=emp_id))

    return render_template('employees/severance.html',
                           emp=emp,
                           settlement=settlement,
                           existing=existing,
                           term_date=term_date,
                           used_days=used_days,
                           base_salary=base_salary,
                           fmt_krw=fmt_krw,
                           active_page='employees')


# ── Departments & Positions ──────────────────────────────────
@app.route('/departments', methods=['GET', 'POST'])
@admin_required
def departments():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_dept':
            name      = request.form.get('name', '').strip()
            parent_id = request.form.get('parent_id') or None
            dept_type = request.form.get('dept_type', 'team')
            if dept_type not in DEPT_TYPE_LABEL:
                dept_type = 'team'
            if name:
                db.execute('INSERT INTO departments (name, parent_id, dept_type) VALUES (?,?,?)',
                           (name, parent_id, dept_type))
                db.commit()
        elif action == 'delete_dept':
            db.execute('DELETE FROM departments WHERE id=?', (request.form.get('dept_id'),))
            db.commit()
        elif action == 'set_leader':
            # 부서장(조직 리더) 지정 — 면접 조율에서 1차 면접관(HM)의 출처가 된다
            did = request.form.get('dept_id')
            lid = request.form.get('leader_id') or None
            if did:
                db.execute('UPDATE departments SET leader_id=? WHERE id=?', (lid, did))
                db.commit()
        elif action == 'add_pos':
            name  = request.form.get('name', '').strip()
            level = request.form.get('level', 1)
            if name:
                db.execute('INSERT INTO positions (name, level) VALUES (?, ?)', (name, level))
                db.commit()
        elif action == 'delete_pos':
            db.execute('DELETE FROM positions WHERE id=?', (request.form.get('pos_id'),))
            db.commit()
        return redirect(url_for('departments'))

    depts = db.execute(
        'SELECT d.*, p.name AS parent_name, p.dept_type AS parent_type, '
        '       l.name AS leader_name, COUNT(u.id) AS member_count '
        'FROM departments d '
        'LEFT JOIN departments p ON d.parent_id = p.id '
        'LEFT JOIN users l ON d.leader_id = l.id AND l.status="active" '
        'LEFT JOIN users u ON u.department_id = d.id AND u.status="active" '
        'GROUP BY d.id ORDER BY d.dept_type, d.parent_id NULLS FIRST, d.name'
    ).fetchall()
    # 부서장 후보 — 조직당 그 조직과 그 아래 소속인원만 보여준다.
    # 전직원을 드롭다운 49개에 전부 뿌리면 페이지가 1MB를 넘기고 고르기도 어렵다.
    leader_pool = db.execute(
        'SELECT u.id, u.name, u.department_id, d.name AS dept_name, p.name AS pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions   p ON u.position_id   = p.id '
        'WHERE u.status="active" ORDER BY COALESCE(p.level,0) DESC, u.name'
    ).fetchall()
    children = {}
    for d in db.execute('SELECT id, parent_id FROM departments'):
        children.setdefault(d['parent_id'], []).append(d['id'])

    def _subtree(did):
        out, stack = [], [did]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children.get(cur, []))
        return set(out)

    by_dept = {}
    for u in leader_pool:
        by_dept.setdefault(u['department_id'], []).append(u)
    leader_choices = {}
    for d in depts:
        scope = _subtree(d['id'])
        mine = by_dept.get(d['id'], [])
        below = [u for did in sorted(scope - {d['id']}) for u in by_dept.get(did, [])]
        picked = {u['id'] for u in mine} | {u['id'] for u in below}
        # 이미 지정된 부서장이 밖에 있으면 사라지지 않게 붙인다
        outside = [u for u in leader_pool
                   if d['leader_id'] and u['id'] == d['leader_id'] and u['id'] not in picked]
        leader_choices[d['id']] = (mine, below, outside)
    all_depts = db.execute(
        'SELECT * FROM departments ORDER BY dept_type, parent_id NULLS FIRST, name'
    ).fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    return render_template('admin/departments.html',
                           depts=depts, all_depts=all_depts, poses=poses,
                           leader_choices=leader_choices,
                           dept_types=DEPT_TYPES,
                           dept_type_label=DEPT_TYPE_LABEL,
                           dept_type_color=DEPT_TYPE_COLOR,
                           dept_type_parent=DEPT_TYPE_PARENT_ALLOWED,
                           active_page='departments')


# ── Work Schedules ──────────────────────────────────────────
@app.route('/admin/schedules', methods=['GET', 'POST'])
@admin_required
def admin_schedules():
    from datetime import date
    db = get_db()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            name      = request.form.get('name', '').strip()
            stype     = request.form.get('schedule_type', 'fixed')
            work_days = ','.join(request.form.getlist('work_days') or ['mon','tue','wed','thu','fri'])
            w_start   = request.form.get('work_start') or None
            w_end     = request.form.get('work_end')   or None
            c_start   = request.form.get('core_start') or None
            c_end     = request.form.get('core_end')   or None
            d_hours   = int(request.form.get('daily_hours_min', 480) or 480)
            grace     = int(request.form.get('grace_minutes', 10) or 10)
            note      = request.form.get('note', '').strip() or None
            is_def    = 1 if request.form.get('is_default') else 0
            if name:
                if is_def:
                    db.execute('UPDATE work_schedules SET is_default=0')
                db.execute(
                    'INSERT INTO work_schedules '
                    '(name,schedule_type,work_days,work_start,work_end,core_start,core_end,'
                    'daily_hours_min,grace_minutes,is_default,note) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (name, stype, work_days, w_start, w_end, c_start, c_end,
                     d_hours, grace, is_def, note)
                )
                db.commit()
                flash(f'스케줄 "{name}" 이(가) 추가됐습니다.', 'success')

        elif action == 'delete':
            sid = request.form.get('schedule_id')
            db.execute('DELETE FROM user_schedule_assignments WHERE schedule_id=?', (sid,))
            db.execute('DELETE FROM work_schedules WHERE id=?', (sid,))
            db.commit()
            flash('스케줄이 삭제됐습니다.', 'info')

        elif action == 'set_default':
            sid = request.form.get('schedule_id')
            db.execute('UPDATE work_schedules SET is_default=0')
            db.execute('UPDATE work_schedules SET is_default=1 WHERE id=?', (sid,))
            db.commit()
            flash('기본 스케줄이 변경됐습니다.', 'success')

        elif action == 'assign_bulk':
            sid        = request.form.get('schedule_id')
            dept_id    = request.form.get('dept_id') or None
            eff_from   = request.form.get('effective_from') or date.today().isoformat()
            eff_to     = request.form.get('effective_to')   or None
            note       = request.form.get('note', '').strip() or None
            assigner   = session['user_id']
            if dept_id:
                users = db.execute(
                    "SELECT id FROM users WHERE department_id=? AND status='active'", (dept_id,)
                ).fetchall()
            else:
                users = db.execute("SELECT id FROM users WHERE status='active'").fetchall()
            for u in users:
                db.execute(
                    'INSERT INTO user_schedule_assignments '
                    '(user_id,schedule_id,effective_from,effective_to,note,assigned_by) '
                    'VALUES (?,?,?,?,?,?)',
                    (u['id'], sid, eff_from, eff_to, note, assigner)
                )
            db.commit()
            flash(f'{len(users)}명에게 스케줄이 배정됐습니다.', 'success')

        elif action == 'assign_individual':
            sid      = request.form.get('schedule_id')
            uid      = request.form.get('user_id')
            eff_from = request.form.get('effective_from') or date.today().isoformat()
            eff_to   = request.form.get('effective_to')   or None
            note     = request.form.get('note', '').strip() or None
            assigner = session['user_id']
            if uid and sid:
                db.execute(
                    'INSERT INTO user_schedule_assignments '
                    '(user_id,schedule_id,effective_from,effective_to,note,assigned_by) '
                    'VALUES (?,?,?,?,?,?)',
                    (uid, sid, eff_from, eff_to, note, assigner)
                )
                db.commit()
                flash('개별 스케줄 배정이 완료됐습니다.', 'success')

        elif action == 'unassign':
            aid = request.form.get('assign_id')
            db.execute('DELETE FROM user_schedule_assignments WHERE id=?', (aid,))
            db.commit()
            flash('배정이 해제됐습니다.', 'info')

        return redirect(url_for('admin_schedules'))

    schedules = db.execute(
        'SELECT ws.*, '
        '(SELECT COUNT(*) FROM user_schedule_assignments usa WHERE usa.schedule_id=ws.id) AS assign_count '
        'FROM work_schedules ws ORDER BY ws.is_default DESC, ws.name'
    ).fetchall()

    assignments = db.execute('''
        SELECT usa.id, usa.user_id, usa.schedule_id, usa.effective_from, usa.effective_to, usa.note,
               u.name AS user_name, u.emp_no,
               d.name AS dept_name,
               ws.name AS sched_name, ws.schedule_type
        FROM user_schedule_assignments usa
        JOIN users u ON usa.user_id = u.id
        LEFT JOIN departments d ON u.department_id = d.id
        JOIN work_schedules ws ON usa.schedule_id = ws.id
        ORDER BY usa.effective_from DESC
        LIMIT 200
    ''').fetchall()

    employees = db.execute(
        "SELECT u.id, u.name, u.emp_no, d.name dept_name "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "WHERE u.status='active' ORDER BY u.name"
    ).fetchall()

    depts = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall()

    from datetime import date
    return render_template('admin/schedules.html',
                           schedules=schedules,
                           assignments=assignments,
                           employees=employees,
                           depts=depts,
                           schedule_types=SCHEDULE_TYPES,
                           schedule_type_label=SCHEDULE_TYPE_LABEL,
                           schedule_type_color=SCHEDULE_TYPE_COLOR,
                           attendance_status_label=ATTENDANCE_STATUS_LABEL,
                           today_date=date.today().isoformat(),
                           active_page='schedules')


# ── Leave / Attendance ──────────────────────────────────────
# ── 휴가 메타 정보 ─────────────────────────────────────────────
# deduct: 'annual' = 연차차감, 'none' = 비차감
# fixed_days: None = 기간 선택, 숫자 = 고정일수
# max_days: None = 제한없음(연차잔여 기준), 숫자 = 법정 최대일
# ── Dashboard Widget Catalog ──────────────────────────────────────────────
WIDGET_CATALOG = {
    'admin': [
        {'key': 'kpi_cards',            'label': '인원 현황',           'icon': 'fa-chart-bar'},
        {'key': 'inbox',                'label': '미결 문서',              'icon': 'fa-inbox'},
        {'key': 'quick_actions',        'label': '바로가기',           'icon': 'fa-bolt'},
        {'key': 'payroll_summary',      'label': '이번 달 기한',           'icon': 'fa-won-sign'},
        {'key': 'open_positions',       'label': '미충원 포지션',     'icon': 'fa-briefcase'},
        {'key': 'overtime_violations',  'label': '주 52시간 초과',        'icon': 'fa-clock'},
        {'key': 'recent_employees',     'label': '최근 입사자',          'icon': 'fa-user-plus'},
        {'key': 'whos_out',             'label': '금일 부재',          'icon': 'fa-door-open'},
        {'key': 'announcements',        'label': '공지사항',            'icon': 'fa-bullhorn'},
    ],
    'manager': [
        {'key': 'kpi_cards',        'label': '인원 현황',           'icon': 'fa-chart-bar'},
        {'key': 'inbox',            'label': '미결 문서',              'icon': 'fa-inbox'},
        {'key': 'quick_actions',    'label': '바로가기',           'icon': 'fa-bolt'},
        {'key': 'team_performance', 'label': '팀 성과',             'icon': 'fa-chart-line'},
        {'key': 'upcoming_reviews', 'label': '평가 일정',          'icon': 'fa-calendar-check'},
        {'key': 'whos_out',         'label': '금일 부재',          'icon': 'fa-door-open'},
        {'key': 'announcements',    'label': '공지사항',            'icon': 'fa-bullhorn'},
    ],
    'employee': [
        {'key': 'kpi_cards',        'label': '인원 현황',           'icon': 'fa-chart-bar'},
        {'key': 'quick_actions',    'label': '바로가기',           'icon': 'fa-bolt'},
        {'key': 'my_goals',         'label': '내 목표',             'icon': 'fa-bullseye'},
        {'key': 'time_off_balance', 'label': '연차 잔여',           'icon': 'fa-calendar-check'},
        {'key': 'leave_requests',   'label': '휴가 신청 내역',       'icon': 'fa-calendar-times'},
        {'key': 'upcoming_leave',   'label': '예정된 휴가',          'icon': 'fa-calendar-alt'},
        {'key': 'announcements',    'label': '공지사항',            'icon': 'fa-bullhorn'},
    ],
}

# 역할별 기본 활성 위젯 (처음 로그인 시 / 미설정 시 적용)
DEFAULT_WIDGETS = {
    'admin':    {'kpi_cards', 'inbox', 'quick_actions', 'payroll_summary',
                 'open_positions', 'overtime_violations', 'whos_out', 'announcements'},
    'manager':  {'kpi_cards', 'inbox', 'quick_actions', 'team_performance',
                 'upcoming_reviews', 'whos_out', 'announcements'},
    'employee': {'kpi_cards', 'quick_actions', 'my_goals', 'time_off_balance',
                 'leave_requests', 'announcements'},
}

def get_widget_prefs(uid, role):
    """Return set of enabled widget keys for a user. Falls back to role default."""
    catalog  = WIDGET_CATALOG.get(role, [])
    all_keys = {w['key'] for w in catalog}
    db = get_db()
    rows = db.execute(
        'SELECT widget_key, enabled FROM dashboard_widgets WHERE user_id=?', (uid,)
    ).fetchall()
    if not rows:
        return DEFAULT_WIDGETS.get(role, all_keys)
    saved = {r['widget_key']: r['enabled'] for r in rows}
    enabled = set()
    for key in all_keys:
        # 저장된 값 우선, 없으면 DEFAULT_WIDGETS 기준
        if key in saved:
            if saved[key]:
                enabled.add(key)
        elif key in DEFAULT_WIDGETS.get(role, all_keys):
            enabled.add(key)
    return enabled

LEAVE_META = {
    # ── 연차 소진형 ──────────────────────────────────────────
    'annual': {
        'label': '연차휴가', 'group': '연차',
        'deduct': 'annual', 'fixed_days': None, 'max_days': None,
        'approval_flow': 'manager_only',
        'law': '근로기준법 §60',
        'pay_info': '통상임금 100% 유급 (사업주 부담)',
        'requires_docs': False, 'docs_note': '',
        'desc': '연간 부여된 유급 연차를 사용합니다.',
        'icon': 'fa-umbrella-beach', 'color': '#3b82f6',
    },
    'half_am': {
        'label': '오전 반차', 'group': '연차',
        'deduct': 'annual', 'fixed_days': 0.5, 'max_days': 0.5,
        'approval_flow': 'manager_only',
        'law': '근로기준법 §60',
        'pay_info': '통상임금 50% 유급',
        'requires_docs': False, 'docs_note': '',
        'desc': '오전(~13:00) 반일 유급휴가. 0.5일 연차 차감.',
        'icon': 'fa-sun', 'color': '#3b82f6',
    },
    'half_pm': {
        'label': '오후 반차', 'group': '연차',
        'deduct': 'annual', 'fixed_days': 0.5, 'max_days': 0.5,
        'approval_flow': 'manager_only',
        'law': '근로기준법 §60',
        'pay_info': '통상임금 50% 유급',
        'requires_docs': False, 'docs_note': '',
        'desc': '오후(13:00~) 반일 유급휴가. 0.5일 연차 차감.',
        'icon': 'fa-moon', 'color': '#3b82f6',
    },
    # ── 병가 (일수에 따라 분기) ──────────────────────────────
    'sick': {
        'label': '병가', 'group': '연차',
        'deduct': 'annual', 'fixed_days': None, 'max_days': 60,
        # 3일 이하: manager_only / 4일 이상: manager_hr (leave_new에서 days로 분기)
        'approval_flow': 'manager_only',
        'approval_hr_threshold': 3,   # 이 일수 초과 시 HR 추가 승인 필요
        'law': '취업규칙 (법정 의무 아님)',
        'pay_info': '취업규칙에 따라 상이. 통상 유급 처리.',
        'requires_docs': True,
        'docs_note': '4일 이상: 의사 진단서 제출 필요',
        'desc': '질병·부상으로 인한 휴가. 연차에서 차감됩니다.',
        'icon': 'fa-kit-medical', 'color': '#ef4444',
    },
    # ── 법정 특별휴가 — 연차 비차감, 매니저만 ────────────────
    'menstrual': {
        'label': '생리휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': 1, 'max_days': 1,
        'approval_flow': 'manager_only',
        'law': '근로기준법 §73',
        'pay_info': '법정 무급 (취업규칙으로 유급 전환 가능)',
        'requires_docs': False,
        'docs_note': '서류 불필요 — 청구만으로 사용 가능 (근기법 보장)',
        'desc': '월 1일 청구 가능. 별도 증빙 서류 불필요.',
        'icon': 'fa-venus', 'color': '#ec4899',
    },
    'compensation': {
        'label': '대체휴무', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'approval_flow': 'manager_only',
        'law': '근로기준법 §57',
        'pay_info': '연장·야간·휴일 수당 대체 지급 (수당 지급 대신 휴무)',
        'requires_docs': False, 'docs_note': '',
        'desc': '초과 근무 대신 부여받은 대체 휴무일.',
        'icon': 'fa-arrows-rotate', 'color': '#64748b',
    },
    'remote': {
        'label': '재택근무', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'approval_flow': 'manager_only',
        'law': '취업규칙',
        'pay_info': '통상임금 100% 유급',
        'requires_docs': False, 'docs_note': '',
        'desc': '재택근무 신청. 연차에서 차감되지 않습니다.',
        'icon': 'fa-house-laptop', 'color': '#64748b',
    },
    'outing': {
        'label': '외출', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'approval_flow': 'manager_only',
        'law': '취업규칙',
        'pay_info': '통상임금 100% 유급',
        'requires_docs': False, 'docs_note': '',
        'desc': '업무 관련 외출.',
        'icon': 'fa-person-walking', 'color': '#64748b',
    },
    # ── 법정 특별휴가 — 매니저 → HR 2단계 ───────────────────
    'paternity': {
        'label': '배우자출산휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': 10, 'max_days': 10,
        'approval_flow': 'manager_hr',
        'law': '근로기준법 §75 / 남녀고용평등법 §18의2',
        'pay_info': '10일 전액 유급 (우선지원기업: 고용보험 급여 신청 가능, 상한 월 230만원)',
        'requires_docs': True,
        'docs_note': '출생증명서 또는 출산예정일확인서 — HR에 원본 제출',
        'desc': '배우자 출산일로부터 90일 이내 연속 사용. 분할 1회 가능.',
        'icon': 'fa-person', 'color': '#8b5cf6',
    },
    'bereavement': {
        'label': '경조사휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 5,
        'approval_flow': 'manager_hr',
        'law': '취업규칙 (법정 의무 아님)',
        'pay_info': '취업규칙 기준 유급 (본인결혼 5일, 부모·배우자사망 5일, 자녀·형제사망 3일)',
        'requires_docs': True,
        'docs_note': '청첩장·부고장·사망진단서 등 — 사후 5일 이내 제출',
        'desc': '경조사 발생 시 규정 일수 부여.',
        'icon': 'fa-ribbon', 'color': '#6b7280',
    },
    'military': {
        'label': '예비군·공가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'approval_flow': 'manager_hr',
        'law': '병역법 §44 / 민방위기본법 §26',
        'pay_info': '공무 수행 기간 유급 (법정)',
        'requires_docs': True,
        'docs_note': '소집통지서·출석요구서 등 공문서 사전 제출',
        'desc': '예비군 훈련, 민방위, 기타 법정 공가.',
        'icon': 'fa-shield-halved', 'color': '#64748b',
    },
    'family_care': {
        'label': '가족돌봄휴직', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 90,
        'approval_flow': 'manager_hr',
        'law': '남녀고용평등법 §22의2',
        'pay_info': '무급 (연 10일 단기돌봄휴가는 별도 — 유급 권고)',
        'requires_docs': True,
        'docs_note': '가족관계증명서 + 돌봄 사유 확인서류',
        'desc': '가족 질병·사고로 인한 돌봄. 연 최대 90일.',
        'icon': 'fa-heart-pulse', 'color': '#f59e0b',
    },
    'fertility': {
        'label': '난임치료휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 3,
        'approval_flow': 'manager_hr',
        'law': '남녀고용평등법 §18의3',
        'pay_info': '1일차 유급 (고용보험 지원), 2~3일차 무급. 연 3일.',
        'requires_docs': True,
        'docs_note': '난임시술확인서 또는 의사진단서 제출 필요',
        'desc': '난임 시술일에 사용. 연 3일 한도.',
        'icon': 'fa-stethoscope', 'color': '#ec4899',
    },
    # ── HR 직행 (장기·고용보험 연동) ─────────────────────────
    'maternity': {
        'label': '출산전후휴가', 'group': 'HR 승인 필요',
        'deduct': 'none', 'fixed_days': 90, 'max_days': 120,
        'approval_flow': 'hr_direct',
        'law': '근로기준법 §74',
        'pay_info': (
            '우선지원기업: 90일 전액 고용보험 (상한 월 230만원)\n'
            '대기업: 최초 60일 사업주, 잔여 30일 고용보험'
        ),
        'requires_docs': True,
        'docs_note': '출산예정일확인서 (출산 전) 또는 출생증명서 (출산 후) — HR 제출',
        'desc': '출산 전후 90일 (다태아 120일) 유급. 출산 후 최소 45일 이상 포함 필수.',
        'icon': 'fa-baby', 'color': '#ec4899',
    },
    'miscarriage': {
        'label': '유산·사산휴가', 'group': 'HR 승인 필요',
        'deduct': 'none', 'fixed_days': None, 'max_days': 90,
        'approval_flow': 'hr_direct',
        'law': '근로기준법 §74③',
        'pay_info': '출산전후휴가와 동일 기준 적용 (고용보험)',
        'requires_docs': True,
        'docs_note': '의사 진단서 + 임신기간 확인서 (임신주수별 일수: 11주↓5일, 12~15주 10일, 16~21주 30일, 22~27주 60일, 28주↑90일)',
        'desc': '임신 중 유산·사산 발생 시 임신주수에 따라 부여.',
        'icon': 'fa-heart-broken', 'color': '#ef4444',
    },
    'parental': {
        'label': '육아휴직', 'group': 'HR 승인 필요',
        'deduct': 'none', 'fixed_days': None, 'max_days': 365,
        'approval_flow': 'hr_direct',
        'law': '남녀고용평등법 §19',
        'pay_info': (
            '1~3개월: 통상임금 80% (상한 월 150만원)\n'
            '4개월 이후: 통상임금 50% (상한 월 120만원)\n'
            '복직 후 6개월 뒤 25% 추가 지급 — 전액 고용보험'
        ),
        'requires_docs': True,
        'docs_note': '육아휴직 신청서 + 자녀 출생증명서 — 30일 전 서면 신청 필수',
        'desc': '만 8세(초등2) 이하 자녀 양육. 부부 각 1년. 분할 3회 가능.',
        'icon': 'fa-baby-carriage', 'color': '#10b981',
    },
    'parental_reduction': {
        'label': '육아기 근로단축', 'group': 'HR 승인 필요',
        'deduct': 'none', 'fixed_days': None, 'max_days': 365,
        'approval_flow': 'hr_direct',
        'law': '남녀고용평등법 §19의2',
        'pay_info': '단축 전후 임금 차액의 80% 고용보험 지원 (상한 월 200만원)',
        'requires_docs': True,
        'docs_note': '근로단축 신청서 + 자녀 출생증명서 — 30일 전 신청 권장',
        'desc': '주 15~35시간으로 단축. 만 12세(초등6) 이하 자녀. 급여 변경 수반.',
        'icon': 'fa-clock', 'color': '#0891b2',
    },
}

LEAVE_LABELS = {k: v['label'] for k, v in LEAVE_META.items()}

TERMINATION_STATUS_LABELS = {
    'draft': 'Draft',
    'submitted': 'Submitted',
    'under_review': 'Under Review',
    'approved': 'Approved',
    'in_progress': 'Offboarding',
    'completed': 'Completed',
    'rejected': 'Rejected',
    'cancelled': 'Cancelled',
}

TERMINATION_REASON_CODES = {
    'compensation':      '보상 (급여·복리후생)',
    'career_growth':     '커리어 성장 (승진·역할)',
    'manager':           '매니저 관계',
    'culture':           '문화·팀 적합도',
    'work_life_balance': '워라밸',
    'personal':          '개인 사정',
    'relocation':        '이사·지역 이동',
    'involuntary':       '비자발적 (권고사직·계약만료)',
}

EXIT_REASON_CATEGORY_LABEL = TERMINATION_REASON_CODES  # alias

OFFBOARDING_TASK_BLUEPRINTS = [
    ('handover', 'Handover plan and knowledge transfer', 'employee'),
    ('asset_return', 'Return company assets and corporate card', 'employee'),
    ('account_disable', 'Disable accounts and revoke permissions', 'admin'),
    ('payroll_close', 'Finalize payroll, unused leave, severance', 'admin'),
    ('documents', 'Prepare resignation and employment certificates', 'admin'),
]

TERMINATE_TYPES = {
    'voluntary':  '자발적 퇴직 (사직)',
    'mutual':     '합의 퇴직',
    'dismissal':  '권고사직',
    'contract':   '계약 만료',
    'retirement': '정년퇴직',
}

LEAVE_DAYS   = {
    'annual': 1.0, 'half_am': 0.5, 'half_pm': 0.5, 'sick': 1.0,
    'remote': 0.0, 'outing': 0.0,
    'maternity': 90.0, 'paternity': 10.0,
    'parental': 0.0, 'family_care': 0.0,
    'bereavement': 0.0, 'military': 0.0, 'compensation': 0.0,
}

def calc_working_days(start_str, end_str):
    """평일(월~금) 근무일수 계산"""
    from datetime import date, timedelta
    try:
        s = date.fromisoformat(start_str)
        e = date.fromisoformat(end_str)
    except (ValueError, TypeError):
        return 1.0
    days = 0.0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:   # 0=월 … 4=금
            days += 1.0
        cur += timedelta(days=1)
    return max(days, 0.0)


def create_offboarding_tasks(db, request_id, due_date):
    existing = db.execute(
        'SELECT COUNT(*) FROM offboarding_tasks WHERE request_id=?',
        (request_id,)
    ).fetchone()[0]
    if existing:
        return
    for task_type, title, owner_role in OFFBOARDING_TASK_BLUEPRINTS:
        db.execute(
            'INSERT INTO offboarding_tasks (request_id, task_type, title, owner_role, due_date) '
            'VALUES (?,?,?,?,?)',
            (request_id, task_type, title, owner_role, due_date)
        )


def can_manage_termination_request(req):
    if session.get('user_role') == 'admin':
        return True
    return (
        session.get('user_role') == 'manager' and
        req['employee_manager_id'] == session.get('user_id')
    )

@app.route('/leave')
@login_required
def leave_my():
    db  = get_db()
    uid = session['user_id']
    requests = db.execute(
        'SELECT r.*, u.name AS approver_name '
        'FROM leave_requests r '
        'LEFT JOIN users u ON r.approver_id = u.id '
        'WHERE r.user_id = ? ORDER BY r.created_at DESC',
        (uid,)
    ).fetchall()
    # 연차 사용일 합산 (단일 소스)
    _bal = get_leave_balance(db, uid)
    return render_template('leave/my.html', requests=requests,
                           used=_bal['used'], total=_bal['total'], labels=LEAVE_LABELS,
                           active_page='leave')

@app.route('/leave/new', methods=['GET', 'POST'])
@login_required
def leave_new():
    error = None
    if request.method == 'POST':
        leave_type    = request.form.get('type', '')
        start_date    = request.form.get('start_date', '')
        end_date      = request.form.get('end_date', '')
        reason        = request.form.get('reason', '').strip()
        duration_type = request.form.get('duration_type', 'full')  # full|am|pm|hours
        leave_hours   = request.form.get('leave_hours', '')

        # duration_type → leave_type 매핑 (연차 UI 통합)
        if leave_type == 'annual':
            if duration_type == 'am':
                leave_type = 'half_am'
            elif duration_type == 'pm':
                leave_type = 'half_pm'

        if not leave_type or not start_date or not end_date:
            error = '유형, 시작일, 종료일은 필수입니다.'
        elif leave_type not in LEAVE_META:
            error = '올바르지 않은 신청 유형입니다.'
        elif start_date > end_date and duration_type not in ('am', 'pm', 'hours'):
            error = '종료일이 시작일보다 앞설 수 없습니다.'
        else:
            db   = get_db()
            uid  = session['user_id']
            meta = LEAVE_META[leave_type]

            # 일수 계산
            if duration_type == 'hours' and leave_hours:
                # 시간 단위 연차 — hours/8 = days (소수)
                try:
                    h = float(leave_hours)
                    h = max(0.5, min(7.5, h))  # 0.5h ~ 7.5h 범위 제한
                except ValueError:
                    h = 1.0
                days      = round(h / 8, 4)
                end_date  = start_date   # 시간 단위는 당일만
                leave_type = 'annual'    # type은 annual로 저장
                reason = f'{h:.0f}시간 연차' + (f' — {reason}' if reason else '')
            elif meta['fixed_days'] is not None and meta['fixed_days'] > 0:
                days = meta['fixed_days']
            elif leave_type in ('half_am', 'half_pm'):
                days     = 0.5
                end_date = start_date   # 반차는 당일만
            elif meta['deduct'] == 'none' and leave_type not in ('remote', 'outing'):
                days = calc_working_days(start_date, end_date)
            else:
                days = calc_working_days(start_date, end_date)

            # 법정 최대일 초과 검사
            if meta['max_days'] and days > meta['max_days']:
                error = f'{meta["label"]} 최대 사용 가능일은 {meta["max_days"]}일입니다. (신청: {days:.0f}일)'

            # 연차 소진 유형: 잔여 연차 검사 (승인 대기 건 포함 — 이중 신청 방지)
            if not error and meta['deduct'] == 'annual':
                _bal = get_leave_balance(db, uid, include_pending=True)
                if _bal['used'] + days > _bal['total']:
                    error = (f'잔여 연차가 부족합니다. '
                             f'(잔여: {_bal["remaining"]:.1f}일 — 승인 대기 중인 신청 포함, 신청: {days:.1f}일)')

            # 기간 중복 검사
            if not error:
                overlap = db.execute(
                    "SELECT id FROM leave_requests "
                    "WHERE user_id=? AND status NOT IN ('cancelled','rejected') "
                    "AND start_date <= ? AND end_date >= ?",
                    (uid, end_date, start_date)
                ).fetchone()
                if overlap:
                    error = '해당 기간에 이미 신청된 휴가·재택이 있습니다.'

            if not error:
                db.execute(
                    'INSERT INTO leave_requests (user_id, type, start_date, end_date, days, reason) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (uid, leave_type, start_date, end_date, days, reason or None)
                )
                db.commit()

                # ── 승인 흐름 결정 ────────────────────────────────────
                emp = db.execute(
                    'SELECT name, manager_id, role FROM users WHERE id=?', (uid,)
                ).fetchone()
                emp_name = emp['name'] if emp else str(uid)

                # approval_flow: 'manager_only' | 'manager_hr' | 'hr_direct'
                approval_flow = meta.get('approval_flow', 'manager_only')
                # 병가: 일수에 따라 분기
                if leave_type == 'sick' and meta.get('approval_hr_threshold'):
                    if days > meta['approval_hr_threshold']:
                        approval_flow = 'manager_hr'
                # 매니저 본인 신청 → 상위 매니저 없으면 HR 직행
                if emp and emp['role'] == 'manager' and not emp['manager_id']:
                    approval_flow = 'hr_direct'

                detail_url = url_for('attendance_home', tab='approvals')

                if approval_flow == 'hr_direct':
                    admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
                    for admin in admins:
                        add_notification(
                            admin['id'], 'action', 'leave',
                            f"[HR 처리 필요] {meta['label']} 신청 — {emp_name}",
                            f"{emp_name}님이 {meta['label']}을(를) 신청했습니다. "
                            f"({start_date} ~ {end_date}) 서류 확인 후 승인해 주세요.",
                            detail_url
                        )
                else:
                    notify_id = emp['manager_id'] if emp and emp['manager_id'] else None
                    if notify_id:
                        add_notification(
                            notify_id, 'action', 'leave',
                            f"근태 신청: {emp_name}",
                            f"{emp_name}님이 {meta['label']}을(를) 신청했습니다. ({start_date} ~ {end_date})",
                            detail_url
                        )
                    else:
                        # 매니저 미지정 → HR 직행
                        admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
                        for admin in admins:
                            add_notification(
                                admin['id'], 'action', 'leave',
                                f"근태 신청 (매니저 미지정): {emp_name}",
                                f"{emp_name}님이 {meta['label']}을(를) 신청했습니다. ({start_date} ~ {end_date})",
                                detail_url
                            )

                # ── Slack 버튼 DM → 매니저 ──────────────────────────────
                req_row = db.execute(
                    'SELECT id FROM leave_requests WHERE user_id=? AND start_date=? AND end_date=? ORDER BY id DESC LIMIT 1',
                    (uid, start_date, end_date)
                ).fetchone()
                if req_row and emp and emp['manager_id']:
                    from integrations.slack import leave_approval_blocks, send_dm_blocks
                    from integrations.dispatcher import notify_slack
                    mgr = db.execute('SELECT email, name FROM users WHERE id=?', (emp['manager_id'],)).fetchone()
                    if mgr and mgr['email']:
                        blocks = leave_approval_blocks(
                            req_row['id'], emp_name, meta['label'],
                            start_date, end_date, int(days)
                        )
                        send_dm_blocks(mgr['email'],
                                       f"[TalentCore] {emp_name}님 {meta['label']} 승인 요청",
                                       blocks)

                flash(f'{meta["label"]} 신청이 완료되었습니다.', 'success')
                return redirect(url_for('attendance_home', tab='leaves'))

    # 연차 잔여일 계산 (폼에 표시용, 단일 소스)
    db  = get_db()
    uid = session['user_id']
    _bal = get_leave_balance(db, uid)
    annual_total  = _bal['total']
    annual_remain = round(_bal['remaining'], 1)

    # 법정 특별휴가 사용 현황 (올해)
    import json as _json
    year = date.today().year
    special_used = {}
    for lt in ('maternity','paternity','parental','family_care','bereavement','military','compensation'):
        row = db.execute(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE user_id=? AND type=? AND status!='cancelled' "
            "AND strftime('%Y',start_date)=?",
            (uid, lt, str(year))
        ).fetchone()
        special_used[lt] = row[0]

    return render_template('leave/new.html', error=error,
                           leave_meta=LEAVE_META,
                           annual_remain=annual_remain,
                           annual_total=annual_total,
                           special_used=_json.dumps(special_used),
                           active_page='leave_new')

@app.route('/leave/<int:req_id>')
@login_required
def leave_detail(req_id):
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role', 'employee')

    req = db.execute(
        'SELECT r.*, '
        'u.name  AS user_name, u.department_id, '
        'ma.name AS manager_approver_name, '
        'ha.name AS hr_approver_name '
        'FROM leave_requests r '
        'JOIN  users u  ON r.user_id     = u.id '
        'LEFT JOIN users ma ON r.manager_id  = ma.id '
        'LEFT JOIN users ha ON r.hr_id       = ha.id '
        'WHERE r.id = ?', (req_id,)
    ).fetchone()

    if not req:
        abort(404)
    if role not in ('admin', 'manager') and req['user_id'] != uid:
        abort(403)

    meta          = LEAVE_META.get(req['type'], {})
    approval_flow = meta.get('approval_flow', 'manager_only')

    # 병가 threshold 분기
    if req['type'] == 'sick' and meta.get('approval_hr_threshold'):
        approval_flow = (
            'manager_only' if req['days'] <= meta['approval_hr_threshold']
            else 'manager_hr'
        )

    # 현재 대기 역할
    current_awaiting = None
    if req['status'] == 'pending':
        current_awaiting = 'hr' if approval_flow == 'hr_direct' else 'manager'
    elif req['status'] == 'reviewed':
        current_awaiting = 'hr'

    can_cancel = (req['user_id'] == uid and req['status'] == 'pending')

    return render_template(
        'leave/detail.html',
        req=req, meta=meta,
        approval_flow=approval_flow,
        current_awaiting=current_awaiting,
        can_cancel=can_cancel,
        labels=LEAVE_LABELS,
        active_page='attendance_home'
    )


@app.route('/leave/<int:req_id>/cancel', methods=['POST'])
@login_required
def leave_cancel(req_id):
    db  = get_db()
    req = db.execute('SELECT * FROM leave_requests WHERE id=?', (req_id,)).fetchone()
    if req and req['user_id'] == session['user_id'] and req['status'] == 'pending':
        db.execute("UPDATE leave_requests SET status='cancelled' WHERE id=?", (req_id,))
        db.commit()
    return redirect(url_for('attendance_home', tab='leaves'))

@app.route('/attendance')
@manager_or_admin
def attendance():
    db      = get_db()
    status  = request.args.get('status', 'pending')
    dept_id = request.args.get('dept', '')
    depts   = db.execute('SELECT * FROM departments ORDER BY name').fetchall()

    sql = (
        'SELECT r.*, u.name AS user_name, u.department_id, '
        'd.name AS dept_name, p.name AS pos_name '
        'FROM leave_requests r '
        'JOIN users u ON r.user_id = u.id '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions   p ON u.position_id   = p.id '
        'WHERE r.status = ?'
    )
    params = [status]
    if dept_id:
        sql   += ' AND u.department_id = ?'
        params.append(dept_id)
    sql += ' ORDER BY r.created_at DESC'

    reqs = db.execute(sql, params).fetchall()
    pending_count = db.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE status='pending'"
    ).fetchone()[0]
    reviewed_count = db.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE status='reviewed'"
    ).fetchone()[0]
    return render_template('attendance/list.html', reqs=reqs, status=status,
                           depts=depts, dept_id=dept_id,
                           pending_count=pending_count, reviewed_count=reviewed_count,
                           labels=LEAVE_LABELS, active_page='attendance')

@app.route('/attendance/<int:req_id>/approve', methods=['POST'])
@manager_or_admin
def attendance_approve(req_id):
    db  = get_db()
    req = db.execute(
        'SELECT r.*, u.department_id, u.manager_id AS user_manager_id FROM leave_requests r '
        'JOIN users u ON r.user_id = u.id WHERE r.id=?', (req_id,)
    ).fetchone()
    if not req:
        abort(404)

    role = session.get('user_role')
    uid  = session.get('user_id')

    # 매니저 승인 단계
    if role == 'manager':
        if req['status'] != 'pending':
            flash('매니저 검토가 불가능한 상태입니다.', 'error')
            return redirect(url_for('attendance_home', tab='approvals'))
        # 본인 신청 자기 승인 방지
        if req['user_id'] == uid:
            flash('본인의 신청은 직접 승인할 수 없습니다.', 'error')
            return redirect(url_for('attendance_home', tab='approvals'))
        # 권한 검증: 같은 부서원 OR 직속 부하 매니저(신청자의 manager_id = 나)
        mgr_dept = session.get('dept_id') or 0
        is_direct_report = (req['user_manager_id'] == uid)
        same_dept = (mgr_dept != 0 and req['department_id'] == mgr_dept)
        if not is_direct_report and not same_dept:
            abort(403)
        
        # approval_flow 확인 → manager_only이면 즉시 확정
        req_meta = LEAVE_META.get(req['type'], {})
        approval_flow = req_meta.get('approval_flow', 'manager_only')
        if req['type'] == 'sick' and req_meta.get('approval_hr_threshold'):
            approval_flow = 'manager_only' if req['days'] <= req_meta['approval_hr_threshold'] else 'manager_hr'
        # 결재선 설정 오버라이드 (Phase C-13)
        _chain = get_approval_chain(db, 'leave')
        if _chain != 'meta_default':
            approval_flow = _chain

        req_name = db.execute('SELECT name FROM users WHERE id=?', (req['user_id'],)).fetchone()
        req_username = req_name['name'] if req_name else str(req['user_id'])

        if approval_flow == 'manager_only':
            # 매니저 승인 = 즉시 최종 확정
            db.execute(
                "UPDATE leave_requests SET status='approved', approver_id=?, "
                "manager_id=?, manager_approved_at=CURRENT_TIMESTAMP WHERE id=?",
                (uid, uid, req_id)
            )
            db.commit()
            add_notification(
                req['user_id'], 'info', 'leave',
                f"{req_meta.get('label','휴가')} 승인 완료",
                f"신청하신 {req_meta.get('label','휴가')}이(가) 승인되었습니다.",
                url_for('attendance_home', tab='leaves')
            )
            # Slack DM
            _req_user = db.execute('SELECT email, name FROM users WHERE id=?', (req['user_id'],)).fetchone()
            if _req_user and _req_user['email']:
                from integrations.dispatcher import notify_slack
                _label = req_meta.get('label', '휴가')
                notify_slack(
                    _req_user['email'],
                    f"[TalentCore] {_label} 신청이 승인됐습니다.\n"
                    f"기간: {req['start_date']} ~ {req['end_date']} ({req['days']}일)\n"
                    f"TalentCore > 근태에서 확인하세요.",
                    '휴가 승인',
                    name=_req_user['name']
                )
            flash(f'{req_meta.get("label","휴가")} 승인이 완료되었습니다.', 'success')
        else:
            # manager_hr / hr_direct: 검토 완료 → HR 대기
            db.execute(
                "UPDATE leave_requests SET status='reviewed', manager_id=?, manager_approved_at=CURRENT_TIMESTAMP WHERE id=?",
                (uid, req_id)
            )
            db.commit()
            admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
            for admin in admins:
                add_notification(
                    admin['id'], 'action', 'leave',
                    f"[HR 최종 승인 필요] {req_meta.get('label','')} — {req_username}",
                    f"매니저 검토가 완료됐습니다. HR 최종 승인이 필요합니다.",
                    url_for('attendance_home', tab='approvals')
                )
            flash('매니저 검토 완료. HR 최종 승인 대기 중입니다.', 'success')

    # HR(Admin) 최종 승인 단계
    elif role == 'admin':
        req_meta      = LEAVE_META.get(req['type'], {})
        approval_flow = req_meta.get('approval_flow', 'manager_only')
        if req['type'] == 'sick' and req_meta.get('approval_hr_threshold'):
            approval_flow = (
                'manager_only' if req['days'] <= req_meta['approval_hr_threshold']
                else 'manager_hr'
            )
        # 결재선 설정 오버라이드 (Phase C-13)
        _chain = get_approval_chain(db, 'leave')
        if _chain != 'meta_default':
            approval_flow = _chain

        # manager_hr 타입: 매니저 검토(reviewed) 완료 후에만 HR 승인 가능
        if approval_flow == 'manager_hr' and req['status'] == 'pending':
            flash(
                f"이 유형({req_meta.get('label','')})은 매니저 검토가 먼저 완료되어야 합니다. "
                f"현재 상태: 대기(pending) — 담당 매니저에게 먼저 검토를 요청하세요.",
                'error'
            )
            return redirect(url_for('attendance_home', tab='approvals'))

        if req['status'] not in ['pending', 'reviewed']:
            flash('최종 승인이 불가능한 상태입니다.', 'error')
            return redirect(url_for('attendance_home', tab='approvals'))

        db.execute(
            "UPDATE leave_requests SET status='approved', hr_id=?, hr_approved_at=CURRENT_TIMESTAMP, approver_id=? WHERE id=?",
            (uid, uid, req_id)
        )
        db.commit()
        add_notification(
            req['user_id'], 'info', 'leave',
            f"{req_meta.get('label','휴가')} 최종 승인 완료",
            f"신청하신 {req_meta.get('label','휴가')}이(가) HR 최종 승인됐습니다.",
            url_for('attendance_home', tab='leaves')
        )
        # Slack DM
        _req_user2 = db.execute('SELECT email, name FROM users WHERE id=?', (req['user_id'],)).fetchone()
        if _req_user2 and _req_user2['email']:
            from integrations.dispatcher import notify_slack
            _label2 = req_meta.get('label', '휴가')
            notify_slack(
                _req_user2['email'],
                f"[TalentCore] {_label2} 신청이 HR 최종 승인됐습니다.\n"
                f"기간: {req['start_date']} ~ {req['end_date']} ({req['days']}일)\n"
                f"TalentCore > 근태에서 확인하세요.",
                '휴가 HR 승인',
                name=_req_user2['name']
            )
        flash('HR 최종 승인이 완료됐습니다.', 'success')

    return redirect(url_for('attendance_home', tab='approvals'))

@app.route('/attendance/<int:req_id>/reject', methods=['POST'])
@manager_or_admin
def attendance_reject(req_id):
    db  = get_db()
    req = db.execute(
        'SELECT r.*, u.department_id FROM leave_requests r '
        'JOIN users u ON r.user_id = u.id WHERE r.id=?', (req_id,)
    ).fetchone()
    if not req:
        abort(404)
    if req['status'] not in ['pending', 'reviewed']:
        flash('반려가 불가능한 상태입니다.', 'error')
        return redirect(url_for('attendance_home', tab='approvals'))
    
    if session.get('user_role') == 'manager':
        cur_uid = session.get('user_id')
        if req['user_id'] == cur_uid:
            flash('본인의 신청은 직접 반려할 수 없습니다.', 'error')
            return redirect(url_for('attendance_home', tab='approvals'))
        mgr_dept = session.get('dept_id') or 0
        # reject 라우트에서도 user_manager_id 조회
        req_ext = db.execute(
            'SELECT u.manager_id FROM leave_requests r JOIN users u ON r.user_id=u.id WHERE r.id=?',
            (req_id,)
        ).fetchone()
        is_direct_report = req_ext and req_ext['manager_id'] == cur_uid
        same_dept = (mgr_dept != 0 and req['department_id'] == mgr_dept)
        if not is_direct_report and not same_dept:
            abort(403)
    
    reason = request.form.get('reason', '').strip() or None
    db.execute(
        "UPDATE leave_requests SET status='rejected', approver_id=?, reject_reason=? WHERE id=?",
        (session['user_id'], reason, req_id)
    )
    db.commit()

    # 알림 발송: 본인에게 반려 알림
    add_notification(
        req['user_id'], 'info', 'leave',
        "근태 반려 안내",
        f"신청하신 근태가 반려되었습니다. (사유: {reason or '미기재'})",
        url_for('leave_my')
    )
    # Slack DM
    _rej_user = db.execute('SELECT email, name FROM users WHERE id=?', (req['user_id'],)).fetchone()
    if _rej_user and _rej_user['email']:
        from integrations.dispatcher import notify_slack
        notify_slack(
            _rej_user['email'],
            f"[TalentCore] 휴가/근태 신청이 반려됐습니다.\n"
            f"기간: {req['start_date']} ~ {req['end_date']}\n"
            f"사유: {reason or '미기재'}\n"
            f"문의는 매니저에게 Slack DM으로 연락하세요.",
            '휴가 반려',
            name=_rej_user['name']
        )

    flash('신청이 반려되었습니다.', 'warning')
    return redirect(url_for('attendance_home', tab='approvals'))

@app.route('/attendance/calendar')
@login_required
def attendance_calendar():
    import calendar as cal_mod
    from datetime import date, timedelta

    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']

    today = date.today()
    raw   = request.args.get('month', today.strftime('%Y-%m'))
    try:
        y, m = int(raw[:4]), int(raw[5:7])
        if not (1 <= m <= 12):
            raise ValueError
    except (ValueError, IndexError):
        y, m = today.year, today.month

    prev_m = date(y, m, 1) - timedelta(days=1)
    next_m = date(y, m, cal_mod.monthrange(y, m)[1]) + timedelta(days=1)

    # 부서 필터 (Admin/Manager 전용)
    dept_filter = None
    departments = []
    if role in ('admin', 'manager'):
        departments = db.execute(
            "SELECT id, name FROM departments ORDER BY name"
        ).fetchall()
        raw_dept = request.args.get('dept', '')
        if raw_dept.isdigit():
            dept_filter = int(raw_dept)

    COLOR_MAP = {
        'annual':  ('#eff6ff', '#2563eb'),
        'half_am': ('#eff6ff', '#2563eb'),
        'half_pm': ('#eff6ff', '#2563eb'),
        'sick':    ('#fff7ed', '#ea580c'),
        'remote':  ('#f0fdf4', '#16a34a'),
        'outing':  ('#faf5ff', '#9333ea'),
    }

    if role in ('admin', 'manager'):
        if dept_filter:
            reqs = db.execute(
                "SELECT r.*, u.name AS user_name, d.name AS dept_name "
                "FROM leave_requests r "
                "JOIN users u ON r.user_id = u.id "
                "LEFT JOIN departments d ON u.department_id = d.id "
                "WHERE r.status='approved' AND u.department_id=? "
                "ORDER BY r.start_date",
                (dept_filter,)
            ).fetchall()
        else:
            reqs = db.execute(
                "SELECT r.*, u.name AS user_name, d.name AS dept_name "
                "FROM leave_requests r "
                "JOIN users u ON r.user_id = u.id "
                "LEFT JOIN departments d ON u.department_id = d.id "
                "WHERE r.status='approved' ORDER BY r.start_date"
            ).fetchall()
    else:
        reqs = db.execute(
            "SELECT r.*, u.name AS user_name, d.name AS dept_name "
            "FROM leave_requests r "
            "JOIN users u ON r.user_id = u.id "
            "LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE r.status='approved' "
            "AND u.department_id=(SELECT department_id FROM users WHERE id=?) "
            "ORDER BY r.start_date",
            (uid,)
        ).fetchall()

    # 오늘 부재자 목록
    today_absent = []
    for r in reqs:
        try:
            sd = date.fromisoformat(r['start_date'])
            ed = date.fromisoformat(r['end_date'])
        except ValueError:
            continue
        if sd <= today <= ed:
            today_absent.append({
                'name':      r['user_name'],
                'dept_name': r['dept_name'] if 'dept_name' in r.keys() else '',
                'type':      LEAVE_LABELS.get(r['type'], r['type']),
                'color':     COLOR_MAP.get(r['type'], ('#f1f5f9', '#475569'))[0],
                'tc':        COLOR_MAP.get(r['type'], ('#f1f5f9', '#475569'))[1],
            })

    events_by_date = {}
    for r in reqs:
        try:
            sd = date.fromisoformat(r['start_date'])
            ed = date.fromisoformat(r['end_date'])
        except ValueError:
            continue
        cur = sd
        while cur <= ed:
            key = cur.isoformat()
            events_by_date.setdefault(key, []).append({
                'name':       r['user_name'],
                'type':       LEAVE_LABELS.get(r['type'], r['type']),
                'color':      COLOR_MAP.get(r['type'], ('#f1f5f9', '#475569'))[0],
                'text_color': COLOR_MAP.get(r['type'], ('#f1f5f9', '#475569'))[1],
            })
            cur += timedelta(days=1)

    # 일요일 기준 시작 셀: offset = (weekday+1) % 7
    first_day  = date(y, m, 1)
    offset     = (first_day.weekday() + 1) % 7
    start_cell = first_day - timedelta(days=offset)

    cells = []
    cur = start_cell
    for _ in range(42):
        all_events = events_by_date.get(cur.isoformat(), [])
        cells.append({
            'day':           cur.day,
            'current_month': cur.month == m,
            'is_today':      cur == today,
            'events':        all_events[:3],
            'extra':         max(0, len(all_events) - 3),
        })
        cur += timedelta(days=1)

    return render_template('attendance/calendar.html',
                           calendar_cells=cells,
                           year=y, month=m, labels=LEAVE_LABELS,
                           prev_month=prev_m.strftime('%Y-%m'),
                           next_month=next_m.strftime('%Y-%m'),
                           departments=departments,
                           dept_filter=dept_filter,
                           today_absent=today_absent,
                           active_page='attendance')


# ══════════════════════════════════════════════════════════════
#  결재 대기함 (P1-1 승인 허브) — 모든 대기 문서를 한 화면에
# ══════════════════════════════════════════════════════════════
APPROVAL_KINDS = [
    # key, 구분, 문서번호 접두
    ('leave', '휴가', 'LV'), ('overtime', '연장근로', 'OT'), ('goal', '목표 승인', 'GL'),
    ('appeal', '이의신청', 'AP'), ('requisition', '채용 요청', 'REQ'), ('certificate', '증명서', 'CT'),
    ('personnel', '인사발령', 'PA'), ('termination', '퇴직', 'TR'), ('payroll', '급여 확정', 'PY'),
    ('hire', '입사 처리', 'HR'), ('offer', '오퍼 승인', 'OF'),
]


def _due_state(due, today):
    """기한 → (표시값, 상태키). 상태키: idle / wait / late"""
    if not due:
        return '—', 'idle'
    try:
        d = (date.fromisoformat(str(due)[:10]) - today).days
    except ValueError:
        return '—', 'idle'
    if d < 0:
        return f'{-d}일 초과', 'late'
    if d == 0:
        return 'D-day', 'wait'
    return f'D-{d}', ('wait' if d <= 3 else 'idle')


def _plus_days(ts, n):
    try:
        return (date.fromisoformat(str(ts)[:10]) + timedelta(days=n)).isoformat()
    except (ValueError, TypeError):
        return None


def collect_approval_rows(db, uid, role, dept_id):
    """결재 대기 문서를 한 줄 형식으로 모은다.
    기한 기준: 휴가·연장근로=사용일, 목표=목표 마감일, 채용 요청=결재 단계 기한,
    발령=발령일, 퇴직=최종 근무일, 입사=입사일, 증명서=접수+3일, 이의신청=접수+7일."""
    is_admin = (role == 'admin')
    dept_id = dept_id or 0
    today = date.today()
    rows_out = []

    def add(key, doc, target, detail, requester, requested_at, due, link, note=''):
        label, st = _due_state(due, today)
        rows_out.append({
            'key': key, 'kind': dict((k, l) for k, l, _ in APPROVAL_KINDS)[key],
            'doc': doc, 'target': target, 'detail': detail, 'requester': requester or '—',
            'requested_at': (requested_at or '')[:10], 'due': (str(due)[:10] if due else ''),
            'due_label': label, 'state': st, 'link': link, 'note': note,
            # 옛 화면 호환
            'title': f'{target} — {detail}', 'sub': note,
        })

    # 휴가
    if is_admin:
        rs = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, lr.days, lr.status, lr.created_at, u.name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE lr.status IN ('pending','reviewed')").fetchall()
    else:
        rs = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, lr.days, lr.status, lr.created_at, u.name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE lr.status='pending' AND (u.department_id=? OR u.manager_id=?)", (dept_id, uid)).fetchall()
    for r in rs:
        period = r['start_date'] if r['start_date'] == r['end_date'] else f"{r['start_date']} ~ {r['end_date']}"
        add('leave', f"LV-{r['id']}", r['name'],
            f"{LEAVE_LABELS.get(r['type'], r['type'])} {('%g' % r['days']) if r['days'] else ''}일 · {period}",
            r['name'], r['created_at'], r['start_date'], url_for('attendance_home', tab='approvals'),
            '팀장 검토 완료' if r['status'] == 'reviewed' else '')

    # 연장근로
    if is_admin:
        rs = db.execute(
            "SELECT o.id, o.date, o.ot_minutes, o.status, o.created_at, u.name "
            "FROM overtime_requests o JOIN users u ON o.user_id=u.id "
            "WHERE o.status IN ('pending','reviewed')").fetchall()
    else:
        rs = db.execute(
            "SELECT o.id, o.date, o.ot_minutes, o.status, o.created_at, u.name "
            "FROM overtime_requests o JOIN users u ON o.user_id=u.id "
            "WHERE o.status='pending' AND (u.department_id=? OR u.manager_id=?)", (dept_id, uid)).fetchall()
    for r in rs:
        m = r['ot_minutes'] or 0
        add('overtime', f"OT-{r['id']}", r['name'], f"{r['date']} · {m // 60}시간 {m % 60:02d}분",
            r['name'], r['created_at'], r['date'], url_for('attendance_home', tab='ot'),
            '팀장 검토 완료' if r['status'] == 'reviewed' else '')

    # 목표 승인
    sql = ("SELECT u.id AS uid, u.name, COUNT(*) AS cnt, c.id AS cid, c.name AS cycle_name, "
           "c.goal_deadline, MIN(g.created_at) AS created_at "
           "FROM performance_goals g JOIN users u ON g.user_id=u.id "
           "JOIN performance_cycles c ON g.cycle_id=c.id "
           "WHERE g.approval_status='submitted' AND c.status='active' ")
    try:
        if is_admin:
            rs = db.execute(sql + "GROUP BY u.id, c.id").fetchall()
        else:
            rs = db.execute(sql + "AND (u.department_id=? OR u.manager_id=?) GROUP BY u.id, c.id",
                            (dept_id, uid)).fetchall()
    except sqlite3.OperationalError:
        rs = []
    for r in rs:
        add('goal', f"GL-{r['cid']}-{r['uid']}", r['name'], f"{r['cycle_name']} · 목표 {r['cnt']}건",
            r['name'], r['created_at'], r['goal_deadline'], url_for('performance', cycle=r['cid']))

    # 등급 이의신청
    sql = ("SELECT ga.id, ga.old_grade, ga.created_at, ga.cycle_id, u.name, c.name AS cycle_name "
           "FROM grade_appeals ga JOIN users u ON ga.user_id=u.id "
           "JOIN performance_cycles c ON ga.cycle_id=c.id WHERE ga.status='pending' ")
    rs = db.execute(sql).fetchall() if is_admin else db.execute(sql + "AND u.manager_id=?", (uid,)).fetchall()
    for r in rs:
        add('appeal', f"AP-{r['id']}", r['name'], f"{r['cycle_name']} · 현재 {r['old_grade']}등급",
            r['name'], r['created_at'], _plus_days(r['created_at'], 7),
            url_for('performance_appeals', cycle=r['cycle_id']))

    # 채용 요청 — 지금 차례인 결재 단계만
    base_sql = (
        'SELECT r.id, r.title, r.headcount, r.created_at, r.hire_type, '
        '       d.name AS dept_name, u.name AS requester_name, '
        '       a.step_no, a.label AS step_label, a.role_kind, a.due_at '
        'FROM job_requisitions r '
        'JOIN requisition_approvals a ON a.requisition_id=r.id '
        "     AND a.status='waiting' "
        "     AND a.step_no = (SELECT MIN(step_no) FROM requisition_approvals "
        "                      WHERE requisition_id=r.id AND status='waiting') "
        'LEFT JOIN departments d ON r.department_id=d.id '
        'LEFT JOIN users u ON r.requester_id=u.id '
        "WHERE r.status IN ('pending_dept','pending_hr') ")
    try:
        if is_admin:
            rs = db.execute(base_sql).fetchall()
        else:
            # 상신 때 박아 둔 결재자·대결자 기준. 결재선 2판 이전 요청서만 예전 부서 규칙으로 찾는다
            me = session['user_id']
            rs = db.execute(base_sql + "AND (a.assignee_id=? OR a.delegate_id=? "
                            "     OR (a.assignee_id IS NULL AND a.role_kind='dept_head' AND r.department_id=?))",
                            (me, me, dept_id)).fetchall()
            # 대결자는 결재자가 부재일 때만 차례가 온다
            rs = [r for r in rs if me in _step_approver_ids(
                db, db.execute('SELECT * FROM job_requisitions WHERE id=?', (r['id'],)).fetchone(),
                db.execute('SELECT * FROM requisition_approvals WHERE requisition_id=? AND step_no=?',
                           (r['id'], r['step_no'])).fetchone())]
    except sqlite3.OperationalError:
        rs = []
    for r in rs:
        add('requisition', f"REQ-{r['id']}", r['dept_name'] or '부서 미지정',
            f"{r['title']} {r['headcount']}명 · {REQUISITION_HIRE_TYPE_LABEL.get(r['hire_type'] or 'new_planned', '')}",
            r['requester_name'], r['created_at'], r['due_at'],
            url_for('requisition_detail', req_id=r['id']),
            f"{r['step_no']}단계 {r['step_label'] or ''}".strip())

    # 오퍼 밴드 초과 — Hire 에서 올라온 것. 요청서와 같은 결재선을 탄다.
    off_sql = (
        'SELECT o.id, o.cand_name, o.position_title, o.department_name, o.requester_name, '
        '       o.base, o.band_hi, o.created_at, a.step_no, a.label AS step_label, a.due_at '
        'FROM offer_approvals o '
        'JOIN offer_approval_steps a ON a.offer_id=o.id '
        "     AND a.status='waiting' "
        "     AND a.step_no = (SELECT MIN(step_no) FROM offer_approval_steps "
        "                      WHERE offer_id=o.id AND status='waiting') "
        "WHERE o.status='pending' ")
    try:
        if is_admin:
            rs = db.execute(off_sql).fetchall()
        else:
            me = session['user_id']
            rs = db.execute(off_sql + 'AND (a.assignee_id=? OR a.delegate_id=?)', (me, me)).fetchall()
            rs = [r for r in rs if me in _step_approver_ids(
                db, db.execute('SELECT * FROM offer_approvals WHERE id=?', (r['id'],)).fetchone(),
                db.execute('SELECT * FROM offer_approval_steps WHERE offer_id=? AND step_no=?',
                           (r['id'], r['step_no'])).fetchone())]
    except sqlite3.OperationalError:
        rs = []
    for r in rs:
        over = (r['base'] or 0) - (r['band_hi'] or 0)
        add('offer', f"OF-{r['id']}", r['cand_name'],
            f"{r['position_title'] or '직무 미기재'} · {r['base']:,}만원"
            + (f" (밴드 상한 +{over:,}만원)" if over > 0 else ''),
            r['requester_name'], r['created_at'], r['due_at'],
            url_for('offer_approval_detail', oid=r['id']),
            f"{r['step_no']}단계 {r['step_label'] or ''}".strip())

    if is_admin:
        CERT_LABELS = {'employment': '재직증명서', 'career': '경력증명서', 'income': '소득증명', 'resignation': '퇴직확인서'}
        for r in db.execute(
                "SELECT cr.id, cr.cert_type, cr.purpose, cr.created_at, u.name "
                "FROM certificate_requests cr JOIN users u ON cr.user_id=u.id "
                "WHERE cr.status='pending'").fetchall():
            add('certificate', f"CT-{r['id']}", r['name'],
                f"{CERT_LABELS.get(r['cert_type'], r['cert_type'])} · {r['purpose'] or '용도 미기재'}",
                r['name'], r['created_at'], _plus_days(r['created_at'], 3), url_for('certificates_hub'))

        for r in db.execute(
                "SELECT pa.id, pa.action_type, pa.from_value, pa.to_value, pa.effective_date, "
                "pa.created_at, pa.user_id, u.name "
                "FROM personnel_actions pa JOIN users u ON pa.user_id=u.id "
                "WHERE pa.status='pending'").fetchall():
            add('personnel', f"PA-{r['id']}", r['name'],
                f"{ACTION_LABELS.get(r['action_type'], r['action_type'])} · "
                f"{r['from_value'] or '—'} → {(r['to_value'] or '—').split('|')[0]}",
                '인사팀', r['created_at'], r['effective_date'],
                url_for('employee_detail', emp_id=r['user_id']) + '#hr')

        for r in db.execute(
                "SELECT tr.id, tr.requested_last_work_date, tr.created_at, u.name "
                "FROM termination_requests tr JOIN users u ON tr.user_id=u.id "
                "WHERE tr.status IN ('submitted','under_review')").fetchall():
            add('termination', f"TR-{r['id']}", r['name'],
                f"퇴직 신청 · 최종 근무일 {r['requested_last_work_date'] or '미정'}",
                r['name'], r['created_at'], r['requested_last_work_date'], url_for('termination_requests'))

        try:
            rs = db.execute(
                "SELECT year, month, COUNT(*) AS cnt, MIN(created_at) AS created_at "
                "FROM payslips WHERE status='draft' GROUP BY year, month").fetchall()
        except sqlite3.OperationalError:
            rs = []
        for r in rs:
            add('payroll', f"PY-{r['year']}{r['month']:02d}", f"{r['year']}-{r['month']:02d} 급여대장",
                f"초안 {r['cnt']}건 · 미공개", '급여', r['created_at'], None, url_for('compensation'))

        try:
            rs = db.execute(
                "SELECT id, name, start_date, created_at, department_name FROM incoming_hires "
                "WHERE status='waiting' AND start_date IS NOT NULL AND start_date <= ?",
                ((today + timedelta(days=7)).isoformat(),)).fetchall()
        except sqlite3.OperationalError:
            rs = []
        for r in rs:
            add('hire', f"HR-{r['id']}", r['name'],
                f"{r['department_name'] or '부서 미정'} · 입사일 {r['start_date']} · 직원 전환",
                '채용', r['created_at'], r['start_date'], url_for('hires_list'))

    order = {'late': 0, 'wait': 1, 'idle': 2}
    rows_out.sort(key=lambda x: (order[x['state']], x['due'] or '9999', x['requested_at']))
    return rows_out


@app.route('/approvals')
@manager_or_admin
def approvals_hub():
    rows = collect_approval_rows(get_db(), session['user_id'], session['user_role'], session.get('dept_id'))
    counts = {}
    for r in rows:
        counts[r['key']] = counts.get(r['key'], 0) + 1
    kinds = [{'key': k, 'label': l, 'count': counts[k]} for k, l, _ in APPROVAL_KINDS if counts.get(k)]
    late = sum(1 for r in rows if r['state'] == 'late')
    return render_template('approvals/hub.html',
                           rows=rows, kinds=kinds, total=len(rows), late=late,
                           active_page='approvals')


# ── Payroll ─────────────────────────────────────────────────
@app.route('/payroll')
@login_required
def payroll_list():
    """내 문서 허브 — 카드 3개로 각 페이지 연결"""
    db  = get_db()
    uid = session['user_id']

    slips_count = db.execute(
        "SELECT COUNT(*) FROM payslips WHERE user_id=? AND status='confirmed'", (uid,)
    ).fetchone()[0]

    contracts_count = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE employee_id=?", (uid,)
    ).fetchone()[0]

    pending_sign_count = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE employee_id=? AND status='sent'", (uid,)
    ).fetchone()[0]

    return render_template('payroll/list.html',
                           slips_count=slips_count,
                           contracts_count=contracts_count,
                           pending_sign_count=pending_sign_count,
                           active_page='my_docs')


@app.route('/payroll/slips')
@login_required
def payroll_slips():
    """급여명세서 목록 + 내 기본급 변화 타임라인 (R3-C)"""
    db  = get_db()
    uid = session['user_id']
    slips = db.execute(
        'SELECT year, month, gross_pay, total_deduction, net_pay '
        "FROM payslips WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC",
        (uid,)
    ).fetchall()
    # 내 기본급 변화 이력 (기본급이 실제로 바뀐 건만)
    my_history = db.execute(
        'SELECT * FROM salary_history '
        'WHERE user_id=? AND old_base_salary != new_base_salary '
        'ORDER BY changed_at DESC LIMIT 12',
        (uid,)
    ).fetchall()
    return render_template('payroll/slips.html',
                           slips=slips, my_history=my_history,
                           fmt_krw=fmt_krw,
                           active_page='my_docs')

@app.route('/payroll/<int:year>/<int:month>')
@login_required
def payroll_detail(year, month):
    db  = get_db()
    uid = session['user_id']
    slip = db.execute(
        'SELECT p.*, u.name, u.email, u.hire_date, '
        'd.name AS dept_name, pos.name AS pos_name '
        'FROM payslips p '
        'JOIN users u ON p.user_id = u.id '
        'LEFT JOIN departments d   ON u.department_id = d.id '
        'LEFT JOIN positions   pos ON u.position_id   = pos.id '
        "WHERE p.user_id=? AND p.year=? AND p.month=? AND p.status='confirmed'",
        (uid, year, month)
    ).fetchone()
    if not slip:
        abort(404)
    return render_template('payroll/detail.html', slip=slip,
                           year=year, month=month, fmt_krw=fmt_krw,
                           active_page='payroll')

# ── 급여 계산 단일 경로 (미리보기·정산 보드·관리자 급여 공통) ──
def _payroll_month_ctx(db, year, month):
    import calendar as cal_mod
    _apply_due_comp_reviews(db)   # 서명·적용일 도래한 보상 검토를 급여 계산 전에 반영
    first_day = f"{year}-{month:02d}-01"
    last_day  = f"{year}-{month:02d}-{cal_mod.monthrange(year, month)[1]}"
    return {
        'year': year, 'month': month, 'first_day': first_day, 'last_day': last_day,
        'holidays': {h['date'] for h in db.execute(
            'SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?', (first_day, last_day)).fetchall()},
        'benefits': {r['key']: dict(r) for r in db.execute(
            "SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type='monthly_fixed'").fetchall()},
    }


def compute_payslip_for(db, e, ctx):
    """직원 1명의 월 급여 계산. e: id, base_salary, meal_allowance, transport_allowance."""
    uid, base = e['id'], e['base_salary'] or 0
    checkins = db.execute('SELECT * FROM checkins WHERE user_id=? AND date BETWEEN ? AND ?',
                          (uid, ctx['first_day'], ctx['last_day'])).fetchall()
    total_ot_pay = 0
    for c in checkins:
        is_h = c['date'] in ctx['holidays']
        total_ot_pay += calc_extra_pay(
            c['overtime_min'] or 0, c['night_min'] or 0, base, is_holiday=is_h,
            holiday_regular_min=(c['regular_min'] or 0) if is_h else 0)['total_extra_pay']
    # 승인된 OT 신청 (체크인 없는 날만)
    checkin_dates = {c['date'] for c in checkins}
    for ot in db.execute("SELECT ot_minutes, date FROM overtime_requests "
                         "WHERE user_id=? AND status='approved' AND date BETWEEN ? AND ?",
                         (uid, ctx['first_day'], ctx['last_day'])).fetchall():
        if ot['date'] not in checkin_dates:
            total_ot_pay += calc_extra_pay(ot['ot_minutes'] or 0, 0, base,
                                           is_holiday=(ot['date'] in ctx['holidays']))['total_extra_pay']

    overrides = {r['benefit_key']: dict(r) for r in db.execute(
        'SELECT * FROM employee_benefit_overrides WHERE user_id=?', (uid,)).fetchall()}
    extra_benefits = []
    for key, cfg in ctx['benefits'].items():
        meta, ov = BENEFIT_CATALOG.get(key, {}), overrides.get(key)
        if ov and not ov['enabled']:
            continue
        amount = ov['amount'] if ov else cfg['amount']
        if not amount and cfg.get('pct'):
            amount = int(base * cfg['pct'] / 100)
        if amount and amount > 0:
            extra_benefits.append({'key': key, 'name': meta.get('name', key), 'amount': amount,
                                   'tax_exempt': meta.get('tax_exempt', False),
                                   'monthly_limit': meta.get('monthly_limit')})

    bonus = db.execute(
        "SELECT COALESCE(SUM(amount),0) amt, MAX(COALESCE(period_months,1)) n FROM bonus_payments "
        "WHERE user_id=? AND bonus_type='perf_bonus' AND pay_date BETWEEN ? AND ?",
        (uid, ctx['first_day'], ctx['last_day'])).fetchone()
    info = db.execute('SELECT gender, withholding_rate FROM users WHERE id=?', (uid,)).fetchone()
    dependents = db.execute('SELECT * FROM employee_dependents WHERE user_id=?', (uid,)).fetchall()
    return calc_payslip(
        base, e['meal_allowance'] or 0, e['transport_allowance'] or 0,
        overtime_pay=total_ot_pay, extra_benefits=extra_benefits, dependents=dependents,
        is_female=bool(info and info['gender'] == 'F'),
        rate_pct=(info['withholding_rate'] if info and info['withholding_rate'] else 100),
        pay_date=(ctx['year'], ctx['month']),
        bonus_amount=bonus['amt'] or 0, bonus_period_months=bonus['n'] or 1,
    )


_PAYSLIP_CALC_COLS = ('base_salary', 'meal_allowance', 'transport_allowance', 'overtime_pay',
                      'national_pension', 'health_insurance', 'long_term_care', 'employment_insurance',
                      'income_tax', 'local_income_tax', 'gross_pay', 'total_deduction', 'net_pay',
                      'income_deduction', 'earned_income', 'total_personal_deduction',
                      'num_dependents', 'child_tax_credit_amount', 'withholding_rate',
                      'perf_bonus', 'bonus_period_months')


def _payslip_values(result):
    import json as _json
    vals = {k: result[k] for k in _PAYSLIP_CALC_COLS}
    vals['bonus_pay'] = result.get('benefits_gross', 0)
    vals['benefits_json'] = _json.dumps(result.get('benefits_breakdown', []), ensure_ascii=False)
    return vals


def insert_draft_payslip(db, uid, year, month, result):
    vals = _payslip_values(result)
    cols = ', '.join(vals)
    db.execute(f"INSERT INTO payslips (user_id, year, month, {cols}, status) "
               f"VALUES (?,?,?,{','.join('?' * len(vals))},'draft')",
               (uid, year, month, *vals.values()))


def recalc_draft_payslips(db, year, month):
    """해당 월 초안을 현재 급여·부양가족·원천징수 비율·2026 기준으로 다시 계산. 변경 건수 반환."""
    ctx = _payroll_month_ctx(db, year, month)
    drafts = db.execute(
        "SELECT p.id, p.user_id, p.net_pay, p.total_deduction, s.base_salary, s.meal_allowance, "
        "s.transport_allowance FROM payslips p JOIN employee_salary s ON s.user_id=p.user_id "
        "WHERE p.year=? AND p.month=? AND p.status='draft'", (year, month)).fetchall()
    changed = 0
    for d in drafts:
        e = {'id': d['user_id'], 'base_salary': d['base_salary'],
             'meal_allowance': d['meal_allowance'], 'transport_allowance': d['transport_allowance']}
        vals = _payslip_values(compute_payslip_for(db, e, ctx))
        if vals['net_pay'] != d['net_pay'] or vals['total_deduction'] != d['total_deduction']:
            changed += 1
        db.execute(f"UPDATE payslips SET {', '.join(k + '=?' for k in vals)} WHERE id=?",
                   (*vals.values(), d['id']))
    return len(drafts), changed


# ── 급여 2단계 확정 (P0-2: 자동계산 초안 → 담당자 확정 → 공개·발송) ──
@app.route('/payroll/confirm', methods=['POST'])
@admin_required
def payroll_confirm():
    """해당 월 draft 명세를 일괄 확정 — 직원 공개 + 인앱 알림 + 이메일 발송."""
    db    = get_db()
    year  = request.form.get('year', type=int)
    month = request.form.get('month', type=int)
    if not year or not month:
        flash('연도와 월을 확인해주세요.', 'error')
        return redirect(url_for('compensation'))

    drafts = db.execute(
        "SELECT p.*, u.email, u.name FROM payslips p JOIN users u ON p.user_id=u.id "
        "WHERE p.year=? AND p.month=? AND p.status='draft'", (year, month)
    ).fetchall()
    if not drafts:
        flash(f'{year}년 {month}월에 확정할 초안이 없습니다.', 'error')
        return redirect(url_for('compensation'))

    db.execute("UPDATE payslips SET status='confirmed' WHERE year=? AND month=? AND status='draft'",
               (year, month))
    db.commit()

    for p in drafts:
        add_notification(
            p['user_id'], 'info', 'payroll',
            f'{year}년 {month}월 급여명세서가 확정되었습니다',
            f'실수령액 {fmt_krw(p["net_pay"])}원 · 명세서를 확인해보세요.',
            link=f'/payroll/{year}/{month}'
        )
        if p['email']:
            # 급여명세 이메일 (근로기준법 §48 교부 의무, SMTP 미설정 시 데모 모드)
            try:
                from integrations.email_sender import send_payslip_email
                send_payslip_email({'email': p['email'], 'name': p['name']}, {
                    'year': year, 'month': month,
                    'gross_pay': p['gross_pay'],
                    'total_deduction': p['total_deduction'],
                    'net_pay': p['net_pay'],
                })
            except Exception as _ee:
                app.logger.warning(f'payslip email failed: {_ee}')

    log_audit('update', 'salary', None, f'{year}년 {month}월 급여 확정 — {len(drafts)}건 공개·발송')
    flash(f'{year}년 {month}월 급여 {len(drafts)}건이 확정되어 직원에게 공개·발송되었습니다.', 'success')
    return redirect(url_for('compensation', tab='ops', py=year, pm=month))


@app.route('/payroll/discard-drafts', methods=['POST'])
@admin_required
def payroll_discard_drafts():
    """해당 월 draft 명세 폐기 — 급여 항목 수정 후 재생성용."""
    db    = get_db()
    year  = request.form.get('year', type=int)
    month = request.form.get('month', type=int)
    cur = db.execute("DELETE FROM payslips WHERE year=? AND month=? AND status='draft'",
                     (year, month))
    db.commit()
    if cur.rowcount:
        log_audit('delete', 'salary', None, f'{year}년 {month}월 급여 초안 {cur.rowcount}건 폐기 (재생성 목적)')
        flash(f'{year}년 {month}월 초안 {cur.rowcount}건을 폐기했습니다. 급여 항목 수정 후 다시 생성하세요.', 'success')
    else:
        flash('폐기할 초안이 없습니다. (확정된 명세는 폐기할 수 없습니다)', 'error')
    return redirect(url_for('compensation', tab='ops', py=year, pm=month))


@app.route('/payroll/preview', methods=['POST'])
@admin_required
def payroll_preview():
    """급여 생성 전 미리보기 — INSERT 없이 계산 결과만 JSON 반환"""
    import calendar as cal_mod, json as _json
    db    = get_db()
    year  = int(request.form.get('year', 2026))
    month = int(request.form.get('month', 1))
    if not (1 <= month <= 12):
        return {'error': '올바른 월을 입력해주세요.'}, 400

    ctx = _payroll_month_ctx(db, year, month)

    emps = db.execute(
        "SELECT u.id, u.name, d.name AS dept_name, p.name AS pos_name, "
        "s.base_salary, s.meal_allowance, s.transport_allowance "
        "FROM users u "
        "JOIN employee_salary s ON u.id = s.user_id "
        "LEFT JOIN departments d ON u.department_id = d.id "
        "LEFT JOIN positions   p ON u.position_id   = p.id "
        "WHERE u.status = 'active' ORDER BY d.name, u.name"
    ).fetchall()

    rows = []
    total_net = 0
    new_count = 0
    for e in emps:
        already = db.execute(
            'SELECT 1 FROM payslips WHERE user_id=? AND year=? AND month=?',
            (e['id'], year, month)
        ).fetchone() is not None

        if already:
            rows.append({
                'name': e['name'], 'dept': e['dept_name'] or '—',
                'base': e['base_salary'], 'gross': 0, 'deduction': 0, 'net': 0,
                'already': True
            })
            continue

        result = compute_payslip_for(db, e, ctx)
        rows.append({
            'name': e['name'], 'dept': e['dept_name'] or '—',
            'base': result['base_salary'],
            'gross': result['gross_pay'],
            'deduction': result['total_deduction'],
            'net': result['net_pay'],
            'already': False
        })
        total_net += result['net_pay']
        new_count += 1

    return {'year': year, 'month': month, 'rows': rows,
            'total_net': total_net, 'new_count': new_count}


@app.route('/payroll/bulk-raise', methods=['GET', 'POST'])
@admin_required
def payroll_bulk_raise():
    # C1: 인상 반영 경로 단일화 — 일괄 %는 연봉 조정안, 성과 연동은 보상 검토
    flash('일괄 인상은 연봉 조정안, 성과등급 연동 인상은 보상 검토에서 진행합니다.', 'warning')
    return redirect(url_for('salary_adjustments'))


# ── 공제 기준 대조 (2026 간이세액표·4대보험 요율 vs 저장된 명세) ──
def _stored_taxable_monthly(p):
    import json as _json
    taxable = (p['base_salary'] or 0) + (p['overtime_pay'] or 0) \
        + max(0, (p['meal_allowance'] or 0) - 200_000) + max(0, (p['transport_allowance'] or 0) - 200_000)
    try:
        taxable += sum(int(x.get('taxable_part', 0)) for x in _json.loads(p['benefits_json'] or '[]'))
    except (ValueError, TypeError, AttributeError):
        pass
    return taxable


@app.route('/payroll/withholding', methods=['GET', 'POST'])
@admin_required
def payroll_withholding():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        year, month = request.form.get('year', type=int), request.form.get('month', type=int)
        if action == 'set_rate':
            uid, rate = request.form.get('user_id', type=int), request.form.get('rate', type=int)
            if uid and rate in WITHHOLDING_RATES:
                db.execute('UPDATE users SET withholding_rate=? WHERE id=?', (rate, uid))
                db.commit()
                log_audit('update', 'salary', uid, f'원천징수 비율 {rate}%')
                flash(f'원천징수 비율 {rate}% 저장 · 초안은 [초안 재계산] 시 반영', 'success')
        elif action == 'recalc' and year and month:
            total, changed = recalc_draft_payslips(db, year, month)
            db.commit()
            if total:
                log_audit('update', 'salary', None, f'{year}년 {month}월 급여 초안 {total}건 재계산 (변경 {changed}건)')
                flash(f'{year}년 {month}월 초안 {total}건 재계산 · 금액 변경 {changed}건', 'success')
            else:
                flash(f'{year}년 {month}월 초안 없음 · 확정된 명세는 재계산 대상이 아닙니다', 'warning')
        return redirect(url_for('payroll_withholding', py=year, pm=month, only=request.form.get('only') or None))

    months = db.execute('SELECT DISTINCT year, month FROM payslips ORDER BY year DESC, month DESC LIMIT 12').fetchall()
    sel_year, sel_month = request.args.get('py', type=int), request.args.get('pm', type=int)
    if not (sel_year and sel_month) and months:
        sel_year, sel_month = months[0]['year'], months[0]['month']
    only_diff = request.args.get('only') == 'diff'

    emps = db.execute(
        "SELECT u.id, u.name, u.gender, COALESCE(u.withholding_rate, 100) AS rate, d.name AS dept_name, "
        "p.id AS pid, p.status, p.base_salary, p.meal_allowance, p.transport_allowance, p.overtime_pay, "
        "p.benefits_json, p.national_pension, p.health_insurance, p.long_term_care, p.employment_insurance, "
        "p.income_tax, p.local_income_tax, p.total_deduction, p.perf_bonus, p.bonus_period_months "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN payslips p ON p.user_id=u.id AND p.year=? AND p.month=? "
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') ORDER BY d.name, u.name",
        (sel_year or 0, sel_month or 0)).fetchall()
    deps = {}
    for r in db.execute('SELECT * FROM employee_dependents').fetchall():
        deps.setdefault(r['user_id'], []).append(r)

    rows, stats = [], {'total': 0, 'with_slip': 0, 'diff': 0, 'diff_sum': 0, 'drafts': 0}
    for e in emps:
        stats['total'] += 1
        pd = calc_personal_deductions(deps.get(e['id'], []), e['gender'] == 'F')
        row = {'id': e['id'], 'name': e['name'], 'dept': e['dept_name'] or '부서 미지정', 'rate': e['rate'],
               'family': pd['num_dependents'], 'kids': pd['children_tax_credit_count'],
               'status': e['status'], 'has_slip': e['pid'] is not None}
        if e['pid'] is not None:
            stats['with_slip'] += 1
            stats['drafts'] += e['status'] == 'draft'
            taxable = _stored_taxable_monthly(e)
            ins = calc_insurance(taxable, (sel_year, sel_month))
            wh = calc_simple_withholding(taxable, pd['num_dependents'], pd['children_tax_credit_count'], e['rate'])
            if e['perf_bonus']:
                bw = calc_bonus_withholding(taxable, e['perf_bonus'], e['bonus_period_months'] or 1,
                                            pd['num_dependents'], pd['children_tax_credit_count'], e['rate'])
                wh = {'income_tax': wh['income_tax'] + bw['income_tax'],
                      'local_income_tax': wh['local_income_tax'] + bw['local_income_tax']}
                ins = dict(ins, employment_insurance=int((taxable + e['perf_bonus']) * ins['rates']['employment']) // 10 * 10)
            new_ins = ins['national_pension'] + ins['health_insurance'] + ins['long_term_care'] + ins['employment_insurance']
            old_ins = (e['national_pension'] or 0) + (e['health_insurance'] or 0) + (e['long_term_care'] or 0) + (e['employment_insurance'] or 0)
            old_tax = (e['income_tax'] or 0) + (e['local_income_tax'] or 0)
            new_tax = wh['income_tax'] + wh['local_income_tax']
            row.update(taxable=taxable, old_ins=old_ins, new_ins=new_ins, old_tax=old_tax, new_tax=new_tax,
                       diff=(new_ins + new_tax) - (old_ins + old_tax))
            if row['diff']:
                stats['diff'] += 1
                stats['diff_sum'] += row['diff']
        if only_diff and not (row['has_slip'] and row['diff']):
            continue
        rows.append(row)
    rows.sort(key=lambda r: (not (r['has_slip'] and r['diff']), -abs(r.get('diff', 0))))

    return render_template('payroll/withholding.html', rows=rows, stats=stats, months=months,
                           sel_year=sel_year, sel_month=sel_month, only_diff=only_diff,
                           rates=WITHHOLDING_RATES, ins_rates=calc_insurance(0, (sel_year or 2026, sel_month or 1))['rates'])


# ── v0.73: 보상 관리 통합 허브 ───────────────────────────────────────────────
@app.route('/compensation', methods=['GET', 'POST'])
@admin_required
def compensation():
    from payroll_utils import calc_compa_ratio, compa_band as _compa_band, calc_payslip, calc_extra_pay, check_min_wage, merit_from_matrix
    import calendar as cal_mod, json as _json, datetime
    db  = get_db()
    cfg = get_company_config()

    if request.method == 'POST':
        action = request.form.get('action', '')
        _tab   = request.form.get('_tab', 'ops')

        if action == 'update_salary':
            uid    = int(request.form.get('user_id'))
            base   = int(request.form.get('base_salary', 0))
            meal   = int(request.form.get('meal_allowance', 0))
            trans  = int(request.form.get('transport_allowance', 0))
            reason = request.form.get('reason', '').strip()
            mw  = check_min_wage(base)
            old = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (uid,)).fetchone()
            if old:
                db.execute(
                    'INSERT INTO salary_history '
                    '(user_id, changed_by, old_base_salary, new_base_salary, '
                    'old_meal, new_meal, old_transport, new_transport, reason) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (uid, session['user_id'],
                     old['base_salary'], base,
                     old['meal_allowance'], meal,
                     old['transport_allowance'], trans,
                     reason or None)
                )
            db.execute(
                'INSERT INTO employee_salary (user_id, base_salary, meal_allowance, transport_allowance) '
                'VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                'base_salary=excluded.base_salary, meal_allowance=excluded.meal_allowance, '
                'transport_allowance=excluded.transport_allowance, updated_at=CURRENT_TIMESTAMP',
                (uid, base, meal, trans)
            )
            db.commit()
            log_audit('update', 'salary', uid, f'개별 급여 수정 (기본급 {base:,}원)')
            flash('급여가 저장되었습니다.' if mw['ok'] else
                  f'급여 저장 완료 — 최저임금 미달 (부족 {fmt_krw(mw["shortage"])}원)',
                  'success' if mw['ok'] else 'warning')

        elif action == 'generate':
            year  = int(request.form.get('year', datetime.date.today().year))
            month = int(request.form.get('month', datetime.date.today().month))
            if not (1 <= month <= 12):
                flash('올바른 월을 입력해주세요.', 'danger')
                return redirect(url_for('compensation', tab='ops'))
            else:
                ctx = _payroll_month_ctx(db, year, month)
                emps_sal = db.execute(
                    "SELECT u.id, s.base_salary, s.meal_allowance, s.transport_allowance "
                    "FROM users u JOIN employee_salary s ON u.id=s.user_id WHERE u.status='active'"
                ).fetchall()
                count = 0
                for e in emps_sal:
                    if db.execute('SELECT 1 FROM payslips WHERE user_id=? AND year=? AND month=?',
                                  (e['id'], year, month)).fetchone():
                        continue
                    insert_draft_payslip(db, e['id'], year, month, compute_payslip_for(db, e, ctx))
                    # P0-2: 초안 단계 — 직원 공개·알림·이메일은 '확정' 시점에 일괄 실행
                    count += 1
                db.commit()
                flash(f'{year}년 {month}월 급여 {count}건이 초안으로 생성되었습니다. '
                      '금액 검토 후 [확정·발송]을 눌러야 직원에게 공개됩니다.', 'success')
                # 정산 보드가 해당 월을 계속 보여주도록 (R3-A)
                return redirect(url_for('compensation', tab='ops', py=year, pm=month))

        elif action == 'update_band':
            sg_id      = int(request.form.get('sg_id', 0))
            min_salary = int(request.form.get('min_salary') or 0)
            mid_salary = int(request.form.get('mid_salary') or 0)
            max_salary = int(request.form.get('max_salary') or 0)
            if sg_id:
                db.execute('UPDATE salary_grades SET min_salary=?, mid_salary=?, max_salary=? WHERE id=?',
                           (min_salary, mid_salary, max_salary, sg_id))
                db.commit()
                flash('밴드가 저장되었습니다.', 'success')
            _tab = 'structure'

        elif action == 'update_matrix':
            for grade in ['S', 'A', 'B', 'C', 'D']:
                for band in ['below', 'at', 'above']:
                    val = float(request.form.get(f'pct_{grade}_{band}', 0))
                    db.execute(
                        'INSERT INTO merit_matrix (performance_grade, compa_band, increase_pct) '
                        'VALUES (?,?,?) ON CONFLICT(performance_grade, compa_band) '
                        'DO UPDATE SET increase_pct=excluded.increase_pct',
                        (grade, band, val)
                    )
            db.commit()
            log_audit('update', 'salary', None, '인상 매트릭스 변경')
            flash('인상 매트릭스가 저장되었습니다.', 'success')
            _tab = 'acr'

        elif action == 'update_bonus_months':
            for grade in ['S', 'A', 'B', 'C', 'D']:
                try:
                    val = max(0.0, min(24.0, float(request.form.get(f'bonus_{grade}', 0) or 0)))
                except ValueError:
                    continue
                db.execute(
                    'INSERT INTO grade_bonus_config (grade, bonus_months) VALUES (?,?) '
                    'ON CONFLICT(grade) DO UPDATE SET bonus_months=excluded.bonus_months, updated_at=CURRENT_TIMESTAMP',
                    (grade, val))
            db.commit()
            log_audit('update', 'salary', None, '등급별 성과상여 개월수 변경')
            flash('등급별 성과상여 개월수가 저장되었습니다.', 'success')
            _tab = 'acr'

        elif action in ('bulk_raise', 'merit_apply'):
            flash('일괄 인상은 연봉 조정안, 성과등급 연동 인상은 보상 검토에서 진행합니다.', 'warning')
            return redirect(url_for('acr_list' if action == 'merit_apply' else 'salary_adjustments'))

        return redirect(url_for('compensation', tab=_tab))

    # ── GET ──────────────────────────────────────────────────────────────────
    _apply_due_comp_reviews(db)
    today       = datetime.date.today()
    today_year  = today.year
    today_month = today.month
    _tab        = request.args.get('tab', 'ops')
    if _tab == 'analysis':
        return redirect(url_for('comp_analysis'))

    active_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND role NOT IN ('admin','guest')"
    ).fetchone()[0]
    this_month_done = db.execute(
        'SELECT COUNT(DISTINCT user_id) FROM payslips WHERE year=? AND month=?',
        (today_year, today_month)
    ).fetchone()[0]
    active_acr = db.execute(
        "SELECT * FROM compensation_review_cycles WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # P0-2: 미확정 초안 현황 (월별)
    draft_months = db.execute(
        "SELECT year, month, COUNT(*) AS cnt, SUM(net_pay) AS total_net "
        "FROM payslips WHERE status='draft' GROUP BY year, month ORDER BY year DESC, month DESC"
    ).fetchall()

    # ── R3-A: 이번 달 정산 보드 (v1.4.0) ─────────────────────────
    try:
        sel_year  = int(request.args.get('py', today_year))
        sel_month = int(request.args.get('pm', today_month))
    except (ValueError, TypeError):
        sel_year, sel_month = today_year, today_month
    if not (1 <= sel_month <= 12):
        sel_year, sel_month = today_year, today_month

    month_first = f"{sel_year}-{sel_month:02d}-01"
    month_last  = f"{sel_year}-{sel_month:02d}-{cal_mod.monthrange(sel_year, sel_month)[1]:02d}"

    # 급여 미설정 직원 (정산 대상에서 빠지는 사람)
    missing_salary = db.execute(
        "SELECT u.id, u.name, d.name dept_name FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN employee_salary s ON u.id=s.user_id "
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') "
        "AND COALESCE(s.base_salary,0)=0 ORDER BY u.name"
    ).fetchall()

    # 이달 입사/퇴사 (일할·정산 주의 대상)
    month_hires = db.execute(
        "SELECT id, name, hire_date FROM users WHERE hire_date BETWEEN ? AND ? AND status='active'",
        (month_first, month_last)
    ).fetchall()
    month_leavers = db.execute(
        "SELECT id, name, termination_date FROM users WHERE termination_date BETWEEN ? AND ?",
        (month_first, month_last)
    ).fetchall()
    hire_map = {h['id']: h['hire_date'] for h in month_hires}

    # 선택월 초안·확정 현황
    sel_draft_cnt = db.execute(
        "SELECT COUNT(*) FROM payslips WHERE year=? AND month=? AND status='draft'",
        (sel_year, sel_month)).fetchone()[0]
    sel_confirmed_cnt = db.execute(
        "SELECT COUNT(*) FROM payslips WHERE year=? AND month=? AND status='confirmed'",
        (sel_year, sel_month)).fetchone()[0]

    # 정산 단계 판정: 1=생성 전 / 2=초안 검토 / 3=확정 완료
    if sel_draft_cnt:
        pay_step = 2
    elif sel_confirmed_cnt:
        pay_step = 3
    else:
        pay_step = 1

    # 검토 테이블 — 선택월 초안 + 전월 대비 증감
    prev_y, prev_m = (sel_year - 1, 12) if sel_month == 1 else (sel_year, sel_month - 1)
    prev_map = {r['user_id']: r['net_pay'] for r in db.execute(
        'SELECT user_id, net_pay FROM payslips WHERE year=? AND month=?', (prev_y, prev_m)
    ).fetchall()}
    review_rows = []
    review_alerts = 0
    if pay_step == 2:
        drafts = db.execute(
            "SELECT p.*, u.name, d.name dept_name FROM payslips p "
            "JOIN users u ON p.user_id=u.id "
            "LEFT JOIN departments d ON u.department_id=d.id "
            "WHERE p.year=? AND p.month=? AND p.status='draft' ORDER BY d.name, u.name",
            (sel_year, sel_month)
        ).fetchall()
        for p in drafts:
            prev_net = prev_map.get(p['user_id'])
            diff_pct = None
            if prev_net:
                diff_pct = round((p['net_pay'] - prev_net) / prev_net * 100, 1)
            is_alert  = diff_pct is not None and abs(diff_pct) >= 20
            is_newbie = p['user_id'] in hire_map
            if is_alert:
                review_alerts += 1
            review_rows.append({**dict(p), 'diff_pct': diff_pct,
                                'is_alert': is_alert, 'is_newbie': is_newbie,
                                'extra_pay': (p['overtime_pay'] or 0) + (p['bonus_pay'] or 0)})
        # 이상 건 먼저, 이후 부서·이름순 유지
        review_rows.sort(key=lambda r: (not r['is_alert'], r['dept_name'] or '', r['name']))

    sel_confirmed_net = db.execute(
        "SELECT COALESCE(SUM(net_pay),0) FROM payslips WHERE year=? AND month=? AND status='confirmed'",
        (sel_year, sel_month)).fetchone()[0]

    # 급여일 D-day (선택월 기준)
    pay_day = int(cfg.get('pay_day', 25) or 25)
    try:
        pay_date = datetime.date(sel_year, sel_month, min(pay_day, cal_mod.monthrange(sel_year, sel_month)[1]))
        pay_dday = (pay_date - today).days
    except ValueError:
        pay_date, pay_dday = None, None

    # R2: growth 이하 요금제용 KPI (ACR·Compa 카드 대체)
    avg_base_salary = db.execute(
        "SELECT AVG(s.base_salary) FROM employee_salary s "
        "JOIN users u ON u.id=s.user_id WHERE u.status='active'"
    ).fetchone()[0] or 0

    # R3-C: 월 인건비 추이 (최근 6개월 확정 급여 총지급액)
    labor_trend = list(reversed(db.execute(
        "SELECT year, month, SUM(gross_pay) AS total, COUNT(*) AS cnt "
        "FROM payslips WHERE status='confirmed' "
        "GROUP BY year, month ORDER BY year DESC, month DESC LIMIT 6"
    ).fetchall()))
    labor_trend_max = max((r['total'] for r in labor_trend), default=0)

    raw_emps = db.execute(
        'SELECT u.id, u.name, d.name dept_name, p.name pos_name, '
        'COALESCE(s.base_salary,0) base_salary, '
        'COALESCE(s.meal_allowance,0) meal_allowance, '
        'COALESCE(s.transport_allowance,0) transport_allowance, '
        'sg.mid_salary '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions p ON u.position_id=p.id '
        'LEFT JOIN employee_salary s ON u.id=s.user_id '
        'LEFT JOIN salary_grades sg ON sg.position_id=u.position_id AND sg.job_family_id=u.job_family_id '
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') ORDER BY d.name, u.name"
    ).fetchall()
    emps = []
    for e in raw_emps:
        ratio = calc_compa_ratio(e['base_salary'], e['mid_salary'])
        emps.append({**dict(e), 'compa_ratio': ratio, 'compa_band': _compa_band(ratio)})

    total_salary_spend = sum(e['base_salary'] for e in emps)
    compa_outliers = sum(1 for e in emps
                         if e['compa_ratio'] and (e['compa_ratio'] < 0.8 or e['compa_ratio'] > 1.2))

    positions    = db.execute('SELECT id, name, level FROM positions ORDER BY level').fetchall()
    job_families = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    band_rows    = db.execute(
        'SELECT sg.*, p.name pos_name, jf.name jf_name '
        'FROM salary_grades sg '
        'JOIN positions p ON sg.position_id=p.id '
        'JOIN job_families jf ON sg.job_family_id=jf.id'
    ).fetchall()
    band_matrix = {(r['position_id'], r['job_family_id']): r for r in band_rows}

    matrix_rows = db.execute(
        'SELECT * FROM merit_matrix ORDER BY performance_grade, compa_band'
    ).fetchall()
    matrix = {(r['performance_grade'], r['compa_band']): r['increase_pct'] for r in matrix_rows}

    cycles = db.execute(
        'SELECT c.*, u.name creator_name, pc.name perf_name FROM compensation_review_cycles c '
        'LEFT JOIN users u ON c.created_by=u.id '
        'LEFT JOIN performance_cycles pc ON pc.id=c.perf_cycle_id ORDER BY c.id DESC'
    ).fetchall()

    departments = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall()

    # ── 보상 검토 요약 (보상 검토 탭) ──
    acr_stage_counts = {}
    for r in db.execute('SELECT cr.cycle_id, cr.status, cr.applied_at, ct.status contract_status '
                        'FROM compensation_reviews cr LEFT JOIN contracts ct ON ct.id=cr.contract_id').fetchall():
        d = acr_stage_counts.setdefault(r['cycle_id'], {})
        st = _acr_stage(r)
        d[st] = d.get(st, 0) + 1
    bonus_months = {r['grade']: r['bonus_months'] for r in
                    db.execute('SELECT grade, bonus_months FROM grade_bonus_config').fetchall()}

    return render_template('payroll/compensation.html',
        active_count=active_count,
        this_month_done=this_month_done,
        draft_months=draft_months,
        sel_year=sel_year, sel_month=sel_month,
        pay_step=pay_step, pay_date=pay_date, pay_dday=pay_dday,
        missing_salary=missing_salary,
        month_hires=month_hires, month_leavers=month_leavers,
        sel_draft_cnt=sel_draft_cnt, sel_confirmed_cnt=sel_confirmed_cnt,
        sel_confirmed_net=sel_confirmed_net,
        review_rows=review_rows, review_alerts=review_alerts,
        labor_trend=labor_trend, labor_trend_max=labor_trend_max,
        active_acr=active_acr,
        compa_outliers=compa_outliers,
        avg_base_salary=avg_base_salary,
        total_salary_spend=total_salary_spend,
        emps=emps,
        today_year=today_year,
        today_month=today_month,
        positions=positions,
        job_families=job_families,
        band_matrix=band_matrix,
        matrix=matrix,
        cycles=cycles,
        departments=departments,
        cfg=cfg,
        fmt_krw=fmt_krw,
        active_tab=_tab,
        active_page='compensation',
        acr_stage_counts=acr_stage_counts,
        acr_stage_label=ACR_STAGE_LABEL,
        bonus_months=bonus_months,
    )


# ══ 연봉 조정안 — 제안↔반영 분리 + 적용일 지정 (R3-B, v1.4.1) ══════════════

ADJUSTMENT_STATUS_LABEL = {
    'draft': '작성 중', 'scheduled': '적용 예약', 'applied': '적용 완료', 'cancelled': '취소',
}


def _apply_salary_adjustment(db, adj):
    """조정안 반영 — employee_salary 갱신 + salary_history 기록 (+선택 시 직원 알림)."""
    items = db.execute(
        'SELECT ai.* FROM salary_adjustment_items ai WHERE ai.adjustment_id=? AND ai.pct != 0',
        (adj['id'],)
    ).fetchall()
    applied = 0
    for it in items:
        cur = db.execute('SELECT base_salary FROM employee_salary WHERE user_id=?',
                         (it['user_id'],)).fetchone()
        if not cur:
            continue
        db.execute(
            'INSERT INTO salary_history (user_id, changed_by, old_base_salary, new_base_salary, reason) '
            'VALUES (?,?,?,?,?)',
            (it['user_id'], adj['created_by'], cur['base_salary'], it['new_salary'],
             f'연봉 조정 「{adj["name"]}」 {it["pct"]:+.1f}%'
             + (f' — {it["reason"]}' if it['reason'] else ''))
        )
        db.execute(
            'UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
            (it['new_salary'], it['user_id'])
        )
        applied += 1
    db.execute(
        "UPDATE salary_adjustments SET status='applied', applied_at=CURRENT_TIMESTAMP WHERE id=?",
        (adj['id'],)
    )
    db.commit()
    if adj['notify_employees']:
        for it in items:
            add_notification(
                it['user_id'], 'info', 'salary',
                '기본급이 조정되었습니다',
                f'{adj["effective_date"]}부로 월 기본급이 {it["pct"]:+.1f}% 조정되었습니다. 자세한 내용은 급여명세서를 확인하세요.',
                link='/payroll'
            )
    log_audit('update', 'salary', None,
              f'연봉 조정 「{adj["name"]}」 적용 — {applied}명 (발효일 {adj["effective_date"]})')
    return applied


def _apply_due_salary_adjustments(db):
    """발효일이 도래한 예약 조정안 자동 반영 (멱등 — 보상 화면 진입 시 확인)."""
    due = db.execute(
        "SELECT * FROM salary_adjustments WHERE status='scheduled' AND effective_date <= ?",
        (date.today().isoformat(),)
    ).fetchall()
    for adj in due:
        n = _apply_salary_adjustment(db, adj)
        flash(f'예약된 연봉 조정 「{adj["name"]}」이 발효일 도래로 적용되었습니다 ({n}명).', 'success')


@app.route('/compensation/adjustments')
@admin_required
def salary_adjustments():
    db = get_db()
    _apply_due_salary_adjustments(db)
    rows = db.execute(
        'SELECT a.*, u.name creator_name, '
        '(SELECT COUNT(*) FROM salary_adjustment_items ai WHERE ai.adjustment_id=a.id) item_count, '
        '(SELECT COUNT(*) FROM salary_adjustment_items ai WHERE ai.adjustment_id=a.id AND ai.pct != 0) target_count '
        'FROM salary_adjustments a JOIN users u ON a.created_by=u.id '
        'ORDER BY a.id DESC'
    ).fetchall()
    departments = db.execute(
        "SELECT id, name FROM departments WHERE dept_type='team' OR dept_type IS NULL ORDER BY name"
    ).fetchall()
    cfg = get_company_config()
    return render_template('payroll/adjustments.html',
                           rows=rows, departments=departments, cfg=cfg,
                           status_label=ADJUSTMENT_STATUS_LABEL,
                           today=date.today().isoformat(),
                           active_page='compensation')


@app.route('/compensation/adjustments/new', methods=['POST'])
@admin_required
def salary_adjustment_new():
    db   = get_db()
    name = request.form.get('name', '').strip()
    effective_date = request.form.get('effective_date', '').strip()
    mode = request.form.get('mode', 'zero')          # zero | flat
    if mode == 'merit':
        flash('성과등급 연동 인상은 보상 검토에서 진행합니다.', 'warning')
        return redirect(url_for('acr_list'))
    dept_id = request.form.get('dept_id', type=int)  # 선택 (없으면 전체)
    try:
        flat_pct = float(request.form.get('flat_pct', 0) or 0)
    except ValueError:
        flat_pct = 0

    if not name or not effective_date:
        flash('조정안 이름과 적용일을 입력해주세요.', 'error')
        return redirect(url_for('salary_adjustments'))

    scope_sql, scope_args = '', []
    if dept_id:
        scope_sql  = 'AND u.department_id=? '
        scope_args = [dept_id]
    emps = db.execute(
        'SELECT u.id, s.base_salary, '
        '(SELECT cr.final_grade FROM calibration_results cr '
        ' JOIN performance_cycles pc ON cr.cycle_id=pc.id '
        ' WHERE cr.user_id=u.id AND cr.final_grade IS NOT NULL '
        ' ORDER BY pc.start_date DESC LIMIT 1) perf_grade '
        'FROM users u JOIN employee_salary s ON u.id=s.user_id '
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') AND s.base_salary > 0 "
        + scope_sql, scope_args
    ).fetchall()
    if not emps:
        flash('조정 대상 직원이 없습니다.', 'error')
        return redirect(url_for('salary_adjustments'))

    cur = db.execute(
        'INSERT INTO salary_adjustments (name, effective_date, created_by) VALUES (?,?,?)',
        (name, effective_date, session['user_id'])
    )
    adj_id = cur.lastrowid
    for e in emps:
        pct = flat_pct if mode == 'flat' else 0
        new_salary = int(e['base_salary'] * (1 + pct / 100))
        db.execute(
            'INSERT INTO salary_adjustment_items (adjustment_id, user_id, old_salary, pct, new_salary) '
            'VALUES (?,?,?,?,?)',
            (adj_id, e['id'], e['base_salary'], pct, new_salary)
        )
    db.commit()
    log_audit('create', 'salary', None, f'연봉 조정안 「{name}」 생성 ({len(emps)}명, 적용일 {effective_date})')
    flash(f'조정안 「{name}」이 생성되었습니다. 인상률을 검토·수정한 뒤 적용하세요.', 'success')
    return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))


@app.route('/compensation/adjustments/<int:adj_id>')
@admin_required
def salary_adjustment_detail(adj_id):
    db = get_db()
    _apply_due_salary_adjustments(db)
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()
    if not adj:
        abort(404)
    items = db.execute(
        'SELECT ai.*, u.name, d.name dept_name, p.name pos_name '
        'FROM salary_adjustment_items ai '
        'JOIN users u ON ai.user_id=u.id '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions p ON u.position_id=p.id '
        'WHERE ai.adjustment_id=? ORDER BY d.name, u.name',
        (adj_id,)
    ).fetchall()
    # 최근 성과 등급 (참고 컬럼)
    grades = {r['user_id']: r['final_grade'] for r in db.execute(
        'SELECT cr.user_id, cr.final_grade FROM calibration_results cr '
        'JOIN performance_cycles pc ON cr.cycle_id=pc.id '
        'WHERE cr.id IN (SELECT MAX(cr2.id) FROM calibration_results cr2 GROUP BY cr2.user_id)'
    ).fetchall()}
    target_count = sum(1 for it in items if it['pct'] != 0)
    avg_pct = round(sum(it['pct'] for it in items if it['pct'] != 0) / target_count, 2) if target_count else 0
    total_increase = sum(it['new_salary'] - it['old_salary'] for it in items)
    return render_template('payroll/adjustment_detail.html',
                           adj=adj, items=items, grades=grades,
                           target_count=target_count, avg_pct=avg_pct,
                           total_increase=total_increase,
                           status_label=ADJUSTMENT_STATUS_LABEL,
                           today=date.today().isoformat(),
                           active_page='compensation')


@app.route('/compensation/adjustments/<int:adj_id>/save', methods=['POST'])
@admin_required
def salary_adjustment_save(adj_id):
    db  = get_db()
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()
    if not adj:
        abort(404)
    if adj['status'] != 'draft':
        flash('작성 중 상태의 조정안만 수정할 수 있습니다. (예약된 조정안은 예약 취소 후 수정)', 'error')
        return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))

    items = db.execute(
        'SELECT id, user_id, old_salary FROM salary_adjustment_items WHERE adjustment_id=?', (adj_id,)
    ).fetchall()
    for it in items:
        raw = request.form.get(f'pct_{it["id"]}', '').strip()
        try:
            pct = round(float(raw), 1) if raw != '' else 0.0
        except ValueError:
            continue
        pct = max(-50.0, min(100.0, pct))
        reason = request.form.get(f'reason_{it["id"]}', '').strip() or None
        new_salary = int(it['old_salary'] * (1 + pct / 100))
        db.execute(
            'UPDATE salary_adjustment_items SET pct=?, new_salary=?, reason=? WHERE id=?',
            (pct, new_salary, reason, it['id'])
        )
    # 적용일도 함께 수정 가능
    eff = request.form.get('effective_date', '').strip()
    if eff:
        db.execute('UPDATE salary_adjustments SET effective_date=? WHERE id=?', (eff, adj_id))
    db.commit()
    flash('조정안이 저장되었습니다.', 'success')
    return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))


@app.route('/compensation/adjustments/<int:adj_id>/apply', methods=['POST'])
@admin_required
def salary_adjustment_apply(adj_id):
    db  = get_db()
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()
    if not adj:
        abort(404)
    if adj['status'] not in ('draft', 'scheduled'):
        flash('이미 적용되었거나 취소된 조정안입니다.', 'error')
        return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))

    notify = 1 if request.form.get('notify_employees') else 0
    db.execute('UPDATE salary_adjustments SET notify_employees=? WHERE id=?', (notify, adj_id))
    db.commit()
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()

    if adj['effective_date'] <= date.today().isoformat():
        n = _apply_salary_adjustment(db, adj)
        flash(f'연봉 조정 「{adj["name"]}」이 적용되었습니다 — {n}명 반영, salary_history 기록 완료.', 'success')
    else:
        db.execute("UPDATE salary_adjustments SET status='scheduled' WHERE id=?", (adj_id,))
        db.commit()
        log_audit('update', 'salary', None,
                  f'연봉 조정 「{adj["name"]}」 적용 예약 (발효일 {adj["effective_date"]})')
        flash(f'적용이 예약되었습니다 — {adj["effective_date"]} 발효. 발효일 전까지 예약 취소 후 수정할 수 있습니다.', 'success')
    return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))


@app.route('/compensation/adjustments/<int:adj_id>/unschedule', methods=['POST'])
@admin_required
def salary_adjustment_unschedule(adj_id):
    db  = get_db()
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()
    if not adj:
        abort(404)
    if adj['status'] != 'scheduled':
        flash('예약 상태의 조정안이 아닙니다.', 'error')
    else:
        db.execute("UPDATE salary_adjustments SET status='draft' WHERE id=?", (adj_id,))
        db.commit()
        flash('적용 예약이 취소되었습니다. 다시 수정할 수 있습니다.', 'success')
    return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))


@app.route('/compensation/adjustments/<int:adj_id>/delete', methods=['POST'])
@admin_required
def salary_adjustment_delete(adj_id):
    db  = get_db()
    adj = db.execute('SELECT * FROM salary_adjustments WHERE id=?', (adj_id,)).fetchone()
    if not adj:
        abort(404)
    if adj['status'] == 'applied':
        flash('이미 적용된 조정안은 삭제할 수 없습니다. (이력 보존)', 'error')
        return redirect(url_for('salary_adjustment_detail', adj_id=adj_id))
    db.execute('DELETE FROM salary_adjustment_items WHERE adjustment_id=?', (adj_id,))
    db.execute('DELETE FROM salary_adjustments WHERE id=?', (adj_id,))
    db.commit()
    log_audit('delete', 'salary', None, f'연봉 조정안 「{adj["name"]}」 삭제')
    flash('조정안이 삭제되었습니다.', 'success')
    return redirect(url_for('salary_adjustments'))


# ── C4: 보상 분석 (형평성 · 인상 시뮬레이션 · 인건비 예측) ─────────────────────
CA_TENURE_BUCKETS = [(0, 1, '1년 미만'), (1, 3, '1~3년'), (3, 5, '3~5년'), (5, 10, '5~10년'), (10, 99, '10년 이상')]
CA_MIN_GROUP = 3          # 성별 그룹당 최소 인원 — 미만이면 '표본 부족'
CA_GAP_REVIEW = 5.0       # |여/남 차이| ≥ 5% → '검토'
CA_SANJAE_DEFAULT = 1.47  # 2026 산재보험 평균 요율(%) — 업종별로 다름


def _ca_population(db):
    """분석 대상: 재직 · 관리자/게스트 제외 · 기본급 등록자 (보상 검토 대상과 같은 기준)"""
    return [dict(r) for r in db.execute(
        "SELECT u.id, u.name, u.gender, u.hire_date, u.termination_date, u.employment_type, "
        "u.department_id, u.position_id, u.job_family_id, d.name dept_name, p.name pos_name, "
        "COALESCE(p.level, 0) pos_level, jf.name family_name, s.base_salary, "
        "COALESCE(s.meal_allowance,0) meal, COALESCE(s.transport_allowance,0) trans, "
        "sg.min_salary, sg.mid_salary, sg.max_salary "
        "FROM users u JOIN employee_salary s ON s.user_id = u.id "
        "LEFT JOIN departments d ON d.id = u.department_id "
        "LEFT JOIN positions p ON p.id = u.position_id "
        "LEFT JOIN job_families jf ON jf.id = u.job_family_id "
        "LEFT JOIN salary_grades sg ON sg.id = (SELECT id FROM salary_grades x WHERE x.position_id=u.position_id "
        "  AND x.job_family_id=u.job_family_id LIMIT 1) "
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') AND s.base_salary > 0 "
        "ORDER BY u.name").fetchall()]


def _ca_median(vals):
    v = sorted(vals)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _ca_tenure(hire_date, today):
    try:
        return (today - date.fromisoformat(str(hire_date)[:10])).days / 365.25
    except (TypeError, ValueError):
        return None


def _ca_gap_pct(f_avg, m_avg):
    """여성 평균이 남성 평균보다 몇 % 높/낮은지 (음수 = 여성이 낮음)"""
    if not f_avg or not m_avg:
        return None
    return round((f_avg / m_avg - 1) * 100, 1)


def _ca_group_rows(emps, keyfn, salary_key='base_salary'):
    groups = {}
    for e in emps:
        k = keyfn(e)
        if k is None:
            continue
        g = groups.setdefault(k, {'key': k, 'F': [], 'M': [], 'all': 0})
        g['all'] += 1
        if e['gender'] in ('F', 'M'):
            g[e['gender']].append(e[salary_key])
    rows = []
    for k in sorted(groups, key=lambda x: (x[0], x[1]) if isinstance(x, tuple) else x):
        g = groups[k]
        nf, nm = len(g['F']), len(g['M'])
        f_avg = sum(g['F']) / nf if nf else None
        m_avg = sum(g['M']) / nm if nm else None
        gap = _ca_gap_pct(f_avg, m_avg)
        thin = nf < CA_MIN_GROUP or nm < CA_MIN_GROUP
        rows.append({'label': k[1] if isinstance(k, tuple) else k, 'n': g['all'], 'nf': nf, 'nm': nm,
                     'f_avg': f_avg, 'm_avg': m_avg, 'f_med': _ca_median(g['F']), 'm_med': _ca_median(g['M']),
                     'gap': gap, 'thin': thin, 'review': (not thin and gap is not None and abs(gap) >= CA_GAP_REVIEW)})
    return rows


def _ca_gaps(emps, salary_key='base_salary'):
    """단순 격차(전체 평균) · 직급 보정 격차(같은 직급 안 여/남 차이의 인원 가중 평균)"""
    f = [e[salary_key] for e in emps if e['gender'] == 'F']
    m = [e[salary_key] for e in emps if e['gender'] == 'M']
    raw = _ca_gap_pct(sum(f) / len(f) if f else None, sum(m) / len(m) if m else None)
    num = den = 0
    for r in _ca_group_rows(emps, lambda e: (e['pos_level'], e['pos_name'] or '직급 없음'), salary_key):
        if r['gap'] is not None and r['nf'] and r['nm']:
            num += r['gap'] * (r['nf'] + r['nm'])
            den += r['nf'] + r['nm']
    return {'nf': len(f), 'nm': len(m), 'f_avg': sum(f) / len(f) if f else None,
            'm_avg': sum(m) / len(m) if m else None, 'raw': raw,
            'adjusted': round(num / den, 1) if den else None}


def _ca_latest_grades(db):
    """가장 최근 보정 확정 등급 {user_id: grade} · 주기명 (보정 결과 테이블이 없으면 빈 값)"""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calibration_results'").fetchone():
        return {}, None
    row = db.execute("SELECT cycle_id FROM calibration_results WHERE final_grade IS NOT NULL "
                     "ORDER BY cycle_id DESC LIMIT 1").fetchone()
    if not row:
        return {}, None
    cyc = db.execute('SELECT name FROM performance_cycles WHERE id=?', (row['cycle_id'],)).fetchone()
    grades = {r['user_id']: r['final_grade'] for r in db.execute(
        'SELECT user_id, final_grade FROM calibration_results WHERE cycle_id=? AND final_grade IS NOT NULL',
        (row['cycle_id'],)).fetchall()}
    return grades, (cyc['name'] if cyc else f'주기 {row["cycle_id"]}')


def _ca_stability_rate(headcount):
    """고용보험 고용안정·직업능력개발사업 사업주 요율 (150명 미만 0.25% · 150~999명 0.65% · 1,000명 이상 0.85%)"""
    if headcount < 150:
        return 0.0025
    if headcount < 1000:
        return 0.0065
    return 0.0085


def _ca_employer_cost(pay, meal, trans, bonus, rates, stab, sanjae):
    """사업주 부담 4대보험 + 산재 (월, 추정). pay=기본급, bonus=그 달 상여 — 국민연금은 기준소득월액이라 상여 제외"""
    taxable = pay + max(0, meal - 200_000) + max(0, trans - 200_000)
    pension_base = min(max(taxable, rates['pension_min']), rates['pension_max'])
    wage = taxable + bonus
    health = wage * rates['health']
    return int(pension_base * rates['pension'] + health + health * rates['ltc_ratio']
               + wage * (rates['employment'] + stab) + wage * sanjae)


def _ca_month_add(y, m, k):
    t = y * 12 + (m - 1) + k
    return t // 12, t % 12 + 1


def _ca_ym(s):
    try:
        return int(str(s)[:4]), int(str(s)[5:7])
    except (TypeError, ValueError):
        return None


def _ca_equity(db, emps, today):
    by = request.args.get('by', 'level')
    keyfns = {
        'level':  lambda e: (e['pos_level'], e['pos_name'] or '직급 없음'),
        'family': lambda e: (0, e['family_name'] or '직무 없음'),
        'dept':   lambda e: (0, e['dept_name'] or '부서 없음'),
    }
    if by not in ('level', 'family', 'dept'):
        by = 'level'
    rows = _ca_group_rows(emps, keyfns[by])
    tenure_rows = _ca_group_rows(emps, lambda e: next(
        ((i, lbl) for i, (lo, hi, lbl) in enumerate(CA_TENURE_BUCKETS)
         if (_ca_tenure(e['hire_date'], today) is not None and lo <= max(0, _ca_tenure(e['hire_date'], today)) < hi)),
        None))
    # 개별 검토 대상: 밴드 하한 미만 · Compa-Ratio 0.85 미만 · 같은 직급·직무 중위값보다 15% 넘게 낮음
    peers = {}
    for e in emps:
        peers.setdefault((e['position_id'], e['job_family_id']), []).append(e['base_salary'])
    flagged = []
    for e in emps:
        reasons = []
        annual = e['base_salary'] * 12
        compa = round(annual / e['mid_salary'], 2) if e['mid_salary'] else None
        if e['min_salary'] and annual < e['min_salary']:
            reasons.append('밴드 하한 미만')
        if compa is not None and compa < 0.85:
            reasons.append('Compa-Ratio 0.85 미만')
        grp = peers.get((e['position_id'], e['job_family_id']), [])
        med = _ca_median(grp) if len(grp) >= CA_MIN_GROUP else None
        if med and e['base_salary'] < med * 0.85:
            reasons.append(f'동일 직급·직무 중위값 대비 {round((e["base_salary"] / med - 1) * 100)}%')
        if reasons:
            flagged.append({**e, 'annual': annual, 'compa': compa, 'peer_med': med, 'reasons': reasons})
    flagged.sort(key=lambda x: (x['compa'] if x['compa'] is not None else 9, x['name']))
    return {'by': by, 'rows': rows, 'tenure_rows': tenure_rows, 'flagged': flagged,
            'gaps': _ca_gaps(emps), 'no_gender': sum(1 for e in emps if e['gender'] not in ('F', 'M')),
            'review_cnt': sum(1 for r in rows if r['review'])}


def _ca_raise(db, emps, today, rates_fn):
    from payroll_utils import calc_compa_ratio, merit_from_matrix
    grades, grade_cycle = _ca_latest_grades(db)
    last_cycle = db.execute('SELECT name, budget_pct, effective_date FROM compensation_review_cycles '
                            'ORDER BY id DESC LIMIT 1').fetchone()
    mode = request.args.get('mode', 'matrix')
    if mode not in ('matrix', 'flat'):
        mode = 'matrix'
    no_grades = mode == 'matrix' and not grades
    if no_grades:
        mode = 'flat'
    try:
        budget = max(0.0, min(50.0, float(request.args.get('budget') or
                                          (last_cycle['budget_pct'] if last_cycle and last_cycle['budget_pct'] else 4.0))))
    except ValueError:
        budget = 4.0
    try:
        flat = max(-20.0, min(50.0, float(request.args.get('flat') or budget)))
    except ValueError:
        flat = budget
    nxt = _ca_month_add(today.year, today.month, 1)
    default_eff = f'{nxt[0]}-{nxt[1]:02d}'
    if last_cycle and last_cycle['effective_date'] and str(last_cycle['effective_date'])[:7] >= today.isoformat()[:7]:
        default_eff = str(last_cycle['effective_date'])[:7]
    eff = request.args.get('eff') or default_eff
    if not _ca_ym(eff + '-01'):
        eff = default_eff
    ey, em = _ca_ym(eff + '-01')
    floor = request.args.get('floor', '1') == '1'

    stab = _ca_stability_rate(len(emps))
    rates = rates_fn((ey, em))
    sanjae = CA_SANJAE_DEFAULT / 100
    out, by_grade, by_dept = [], {}, {}
    for e in emps:
        g = grades.get(e['id'])
        compa = calc_compa_ratio(e['base_salary'], e['mid_salary'])
        pct = (merit_from_matrix(db, g, compa) if g else 0.0) if mode == 'matrix' else flat
        new = _acr_new_salary(e['base_salary'], pct)
        floored = False
        if floor and e['min_salary'] and new * 12 < e['min_salary']:
            new = -(-(-(-e['min_salary'] // 12)) // 10) * 10
            floored = True
        cost_old = _ca_employer_cost(e['base_salary'], e['meal'], e['trans'], 0, rates, stab, sanjae)
        cost_new = _ca_employer_cost(new, e['meal'], e['trans'], 0, rates, stab, sanjae)
        r = {**e, 'grade': g, 'pct': pct, 'new': new, 'delta': new - e['base_salary'], 'floored': floored,
             'burden_delta': cost_new - cost_old}
        out.append(r)
        gk = g or '등급 없음'
        bg = by_grade.setdefault(gk, {'label': gk, 'n': 0, 'cur': 0, 'delta': 0})
        bd = by_dept.setdefault(e['dept_name'] or '부서 없음', {'label': e['dept_name'] or '부서 없음', 'n': 0, 'cur': 0, 'delta': 0})
        for b in (bg, bd):
            b['n'] += 1
            b['cur'] += e['base_salary']
            b['delta'] += r['delta']
    cur_sum = sum(e['base_salary'] for e in emps)
    delta = sum(r['delta'] for r in out)
    burden = sum(r['burden_delta'] for r in out)
    severance = delta // 12
    budget_amt = int(cur_sum * budget / 100)
    months_left = 13 - em if ey == today.year else (12 if ey > today.year else 0)
    grade_order = {g: i for i, g in enumerate('SABCD')}
    for b in list(by_grade.values()) + list(by_dept.values()):
        b['pct'] = round(b['delta'] / b['cur'] * 100, 2) if b['cur'] else 0
    gender = []
    for gk, lbl in (('F', '여성'), ('M', '남성')):
        rs = [r for r in out if r['gender'] == gk]
        cur = sum(r['base_salary'] for r in rs)
        gender.append({'label': lbl, 'n': len(rs), 'pct': round(sum(r['delta'] for r in rs) / cur * 100, 2) if cur else 0,
                       'delta': sum(r['delta'] for r in rs)})
    after = [{**r, 'base_salary': r['new']} for r in out]
    return {
        'mode': mode, 'no_grades': no_grades, 'grade_cycle': grade_cycle, 'graded': sum(1 for r in out if r['grade']),
        'budget': budget, 'flat': flat, 'eff': eff, 'ey': ey, 'em': em, 'floor': floor, 'last_cycle': last_cycle,
        'n': len(out), 'cur_sum': cur_sum, 'new_sum': cur_sum + delta, 'delta': delta, 'annual': delta * 12,
        'burden': burden, 'severance': severance, 'total_month': delta + burden + severance,
        'months_left': months_left, 'year_impact': (delta + burden + severance) * months_left,
        'budget_amt': budget_amt, 'used_pct': round(delta / budget_amt * 100, 1) if budget_amt else None,
        'over': budget_amt and delta > budget_amt, 'avg_pct': round(delta / cur_sum * 100, 2) if cur_sum else 0,
        'floored': sum(1 for r in out if r['floored']),
        'by_grade': sorted(by_grade.values(), key=lambda b: grade_order.get(b['label'], 9)),
        'by_dept': sorted(by_dept.values(), key=lambda b: -b['delta']),
        'gender': gender, 'gap_before': _ca_gaps(emps), 'gap_after': _ca_gaps(after),
        'top': sorted(out, key=lambda r: -r['delta'])[:15],
    }


def _ca_forecast(db, emps, today, rates_fn):
    hires = request.args.get('hires', '1') == '1'
    raises = request.args.get('raises', '1') == '1'
    try:
        sanjae_pct = max(0.0, min(20.0, float(request.args.get('sanjae') or CA_SANJAE_DEFAULT)))
    except ValueError:
        sanjae_pct = CA_SANJAE_DEFAULT
    sanjae = sanjae_pct / 100
    months = [_ca_month_add(today.year, today.month, k) for k in range(12)]
    first_key = months[0][0] * 12 + months[0][1]
    idx = lambda ym: ym[0] * 12 + ym[1] - first_key   # noqa: E731
    events = []

    # 사람별 월 기본급 경로: [(시작 인덱스, 기본급)] — 예정 인상·조정안 반영
    people = {e['id']: {**e, 'steps': [(0, e['base_salary'])], 'start': 0, 'end': 12} for e in emps}
    for p in people.values():
        hy = _ca_ym(p['hire_date'])
        if hy and idx(hy) > 0:
            p['start'] = min(12, idx(hy))
            events.append({'i': p['start'], 'kind': '입사 예정', 'who': p['name'], 'amt': p['base_salary']})
        ty = _ca_ym(p['termination_date'])
        if ty and idx(ty) < 12:
            p['end'] = max(0, idx(ty) + 1)
            events.append({'i': max(0, idx(ty)), 'kind': '퇴사 예정', 'who': p['name'], 'amt': -p['base_salary']})
    if raises:
        for r in db.execute(
                "SELECT ai.user_id, ai.new_salary, a.effective_date, a.name FROM salary_adjustment_items ai "
                "JOIN salary_adjustments a ON a.id = ai.adjustment_id "
                "WHERE a.status='scheduled' AND ai.new_salary != ai.old_salary").fetchall():
            p, ym = people.get(r['user_id']), _ca_ym(r['effective_date'])
            if p and ym and idx(ym) < 12:
                p['steps'].append((max(0, idx(ym)), r['new_salary']))
                events.append({'i': max(0, idx(ym)), 'kind': '연봉 조정안', 'who': f'{p["name"]} · {r["name"]}',
                               'amt': r['new_salary'] - p['base_salary']})
        for r in db.execute(
                "SELECT cr.*, c.effective_date, c.name cycle_name FROM compensation_reviews cr "
                "JOIN compensation_review_cycles c ON c.id = cr.cycle_id "
                "WHERE cr.status='approved' AND cr.applied_at IS NULL").fetchall():
            p, ym = people.get(r['employee_id']), _ca_ym(r['effective_date'])
            if p and ym and idx(ym) < 12:
                _, sal = _acr_final(r)
                if sal and sal != p['base_salary']:
                    p['steps'].append((max(0, idx(ym)), sal))
                    events.append({'i': max(0, idx(ym)), 'kind': '보상 검토 인상', 'who': f'{p["name"]} · {r["cycle_name"]}',
                                   'amt': sal - p['base_salary']})

    bonus = [0] * 12
    for b in db.execute('SELECT b.amount, b.pay_date, b.bonus_type, u.name FROM bonus_payments b '
                        'JOIN users u ON u.id = b.user_id').fetchall():
        ym = _ca_ym(b['pay_date'])
        if ym and 0 <= idx(ym) < 12:
            bonus[idx(ym)] += b['amount'] or 0
    if raises:
        for r in db.execute(
                "SELECT cr.proposed_bonus, c.bonus_pay_date, c.effective_date, u.name FROM compensation_reviews cr "
                "JOIN compensation_review_cycles c ON c.id = cr.cycle_id JOIN users u ON u.id = cr.employee_id "
                "WHERE cr.status='approved' AND cr.bonus_payment_id IS NULL AND COALESCE(cr.proposed_bonus,0) > 0").fetchall():
            ym = _ca_ym(r['bonus_pay_date'] or r['effective_date'])
            if ym and 0 <= idx(ym) < 12:
                bonus[idx(ym)] += r['proposed_bonus']

    openings = []
    no_date = 0
    if hires:
        for o in db.execute(
                "SELECT o.title, o.target_start_date, o.salary_min, o.salary_max, d.name dept_name "
                "FROM job_openings o LEFT JOIN departments d ON d.id = o.department_id "
                "WHERE o.status IN ('approved','open')").fetchall():
            ym = _ca_ym(o['target_start_date'])
            if not ym:
                no_date += 1
                continue
            annual = ((o['salary_min'] or 0) + (o['salary_max'] or 0)) / 2 if o['salary_max'] else (o['salary_min'] or 0)
            monthly = int(annual / 12) // 10 * 10
            if monthly <= 0 or idx(ym) >= 12:
                continue
            openings.append({'i': max(0, idx(ym)), 'monthly': monthly})
            events.append({'i': max(0, idx(ym)), 'kind': '채용 계획', 'who': f'{o["title"]} · {o["dept_name"] or "-"}',
                           'amt': monthly})

    stab = _ca_stability_rate(len(emps))
    rows = []
    for i, (y, m) in enumerate(months):
        rates = rates_fn((y, m))
        hc = pay = allow = burden = 0
        for p in people.values():
            if not (p['start'] <= i < p['end']):
                continue
            sal = max((s for s in p['steps'] if s[0] <= i), key=lambda s: s[0])[1]
            hc += 1
            pay += sal
            allow += p['meal'] + p['trans']
            burden += _ca_employer_cost(sal, p['meal'], p['trans'], 0, rates, stab, sanjae)
        for o in openings:
            if o['i'] <= i:
                hc += 1
                pay += o['monthly']
                burden += _ca_employer_cost(o['monthly'], 0, 0, 0, rates, stab, sanjae)
        # 상여에 붙는 사업주 부담 (건강·장기요양·고용·산재)
        burden += int(bonus[i] * (rates['health'] * (1 + rates['ltc_ratio']) + rates['employment'] + stab + sanjae))
        severance = (pay + allow + bonus[i]) // 12
        total = pay + allow + bonus[i] + burden + severance
        rows.append({'y': y, 'm': m, 'hc': hc, 'pay': pay, 'allow': allow, 'bonus': bonus[i],
                     'burden': burden, 'severance': severance, 'total': total})
    for ev in events:
        ev['y'], ev['m'] = months[min(11, ev['i'])]
    events.sort(key=lambda ev: (ev['i'], ev['kind'], ev['who']))
    last_ps = db.execute("SELECT year, month, COUNT(*) n, SUM(gross_pay) gross FROM payslips "
                         "WHERE status='confirmed' GROUP BY year, month ORDER BY year DESC, month DESC LIMIT 1").fetchone()
    peak = max((r['total'] for r in rows), default=0)
    return {'hires': hires, 'raises': raises, 'sanjae': sanjae_pct, 'stab': stab * 100, 'rows': rows,
            'events': events, 'no_date': no_date, 'last_ps': last_ps, 'peak': peak,
            'sum_total': sum(r['total'] for r in rows), 'sum_pay': sum(r['pay'] + r['allow'] for r in rows),
            'sum_bonus': sum(r['bonus'] for r in rows), 'sum_burden': sum(r['burden'] for r in rows),
            'sum_sev': sum(r['severance'] for r in rows)}


@app.route('/compensation/analysis')
@admin_required
def comp_analysis():
    from payroll_utils import get_insurance_rates
    db = get_db()
    today = date.today()
    view = request.args.get('view', 'equity')
    if view not in ('equity', 'raise', 'forecast'):
        view = 'equity'
    emps = _ca_population(db)
    ctx = {'view': view, 'n': len(emps), 'today': today}
    if view == 'equity':
        ctx['eq'] = _ca_equity(db, emps, today)
    elif view == 'raise':
        ctx['rs'] = _ca_raise(db, emps, today, get_insurance_rates)
    else:
        ctx['fc'] = _ca_forecast(db, emps, today, get_insurance_rates)
    return render_template('payroll/comp_analysis.html', active_page='comp_analysis', **ctx)


# ── 기존 라우트 → /compensation 리디렉트 ──────────────────────────────────────
@app.route('/admin/payroll-legacy')
@admin_required
def admin_payroll_redirect():
    return redirect(url_for('compensation', tab='ops'))

@app.route('/admin/salary-bands-legacy')
@admin_required
def salary_bands_redirect():
    return redirect(url_for('compensation', tab='structure'))


# ── v0.51: Salary Band 관리 ──────────────────────────────────────────────────
@app.route('/admin/salary-bands', methods=['GET', 'POST'])
@admin_required
def salary_bands():
    from payroll_utils import calc_compa_ratio
    db = get_db()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_band':
            sg_id      = int(request.form.get('sg_id', 0))
            min_salary = int(request.form.get('min_salary') or 0)
            mid_salary = int(request.form.get('mid_salary') or 0)
            max_salary = int(request.form.get('max_salary') or 0)
            if sg_id:
                db.execute(
                    'UPDATE salary_grades SET min_salary=?, mid_salary=?, max_salary=? WHERE id=?',
                    (min_salary, mid_salary, max_salary, sg_id)
                )
                db.commit()
                flash('밴드가 저장되었습니다.', 'success')
        elif action == 'update_matrix':
            for grade in ['S','A','B','C','D']:
                for band in ['below','at','above']:
                    key = f'pct_{grade}_{band}'
                    val = float(request.form.get(key, 0))
                    db.execute(
                        '''INSERT INTO merit_matrix (performance_grade, compa_band, increase_pct)
                           VALUES (?,?,?)
                           ON CONFLICT(performance_grade, compa_band)
                           DO UPDATE SET increase_pct=excluded.increase_pct''',
                        (grade, band, val)
                    )
            db.commit()
            log_audit('update', 'salary', None, '인상 매트릭스 변경')
            flash('인상 매트릭스가 저장되었습니다.', 'success')
        return redirect(url_for('salary_bands'))

    # 직급·직군 목록
    positions   = db.execute('SELECT id, name, level FROM positions ORDER BY level').fetchall()
    job_families = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()

    # band_matrix: {(position_id, job_family_id): row}
    band_rows = db.execute(
        'SELECT sg.*, p.name pos_name, jf.name jf_name '
        'FROM salary_grades sg '
        'JOIN positions    p  ON sg.position_id   = p.id '
        'JOIN job_families jf ON sg.job_family_id = jf.id'
    ).fetchall()
    band_matrix = {(r['position_id'], r['job_family_id']): r for r in band_rows}

    # merit_matrix 15칸
    matrix_rows = db.execute(
        'SELECT * FROM merit_matrix ORDER BY performance_grade, compa_band'
    ).fetchall()
    matrix = {(r['performance_grade'], r['compa_band']): r['increase_pct'] for r in matrix_rows}

    # 직원별 Compa-Ratio 집계
    raw_emps = db.execute(
        '''SELECT u.id, u.name, d.name dept_name,
                  p.name pos_name, jf.name jf_name,
                  COALESCE(s.base_salary, 0) base_salary,
                  sg.min_salary, sg.mid_salary, sg.max_salary
           FROM users u
           LEFT JOIN employee_salary s  ON u.id = s.user_id
           LEFT JOIN departments     d  ON u.department_id  = d.id
           LEFT JOIN positions       p  ON u.position_id    = p.id
           LEFT JOIN job_families    jf ON u.job_family_id  = jf.id
           LEFT JOIN salary_grades   sg ON sg.position_id   = u.position_id
                                       AND sg.job_family_id = u.job_family_id
           WHERE u.status = \'active\' AND u.role NOT IN (\'admin\',\'guest\')
           ORDER BY d.name, u.name'''
    ).fetchall()
    emp_rows = []
    for e in raw_emps:
        ratio = calc_compa_ratio(e['base_salary'], e['mid_salary'])
        emp_rows.append({**dict(e), 'compa_ratio': ratio})

    return render_template('payroll/salary_bands.html',
                           positions=positions,
                           job_families=job_families,
                           band_matrix=band_matrix,
                           matrix=matrix,
                           emps=emp_rows,
                           active_page='salary_bands')


@app.route('/admin/payroll', methods=['GET', 'POST'])
@admin_required
def admin_payroll():
    db    = get_db()
    error = None
    msg   = None

    if request.method == 'POST':
        action = request.form.get('action')

        # 급여 정보 수정
        if action == 'update_salary':
            uid    = int(request.form.get('user_id'))
            base   = int(request.form.get('base_salary', 0))
            meal   = int(request.form.get('meal_allowance', 0))
            trans  = int(request.form.get('transport_allowance', 0))
            reason = request.form.get('reason', '').strip()
            mw     = check_min_wage(base)

            # 변경 전 값 조회 → salary_history 기록
            old = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (uid,)).fetchone()
            if old:
                db.execute(
                    'INSERT INTO salary_history '
                    '(user_id, changed_by, old_base_salary, new_base_salary, '
                    'old_meal, new_meal, old_transport, new_transport, reason) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (uid, session['user_id'],
                     old['base_salary'], base,
                     old['meal_allowance'], meal,
                     old['transport_allowance'], trans,
                     reason or None)
                )

            db.execute(
                'INSERT INTO employee_salary (user_id, base_salary, meal_allowance, transport_allowance) '
                'VALUES (?, ?, ?, ?) '
                'ON CONFLICT(user_id) DO UPDATE SET '
                'base_salary=excluded.base_salary, '
                'meal_allowance=excluded.meal_allowance, '
                'transport_allowance=excluded.transport_allowance, '
                'updated_at=CURRENT_TIMESTAMP',
                (uid, base, meal, trans)
            )
            db.commit()
            log_audit('update', 'salary', uid, f'개별 급여 수정 (기본급 {base:,}원)')
            if not mw['ok']:
                msg = (f'급여가 저장되었으나 최저임금 미달입니다. '
                       f'(기본급 {fmt_krw(base)}원 < 최저임금 {fmt_krw(mw["min_monthly"])}원, '
                       f'부족액 {fmt_krw(mw["shortage"])}원)')
            else:
                msg = '급여 정보가 저장되었습니다.'

        # 월 급여 일괄 생성 (근태 + 복리후생 연동)
        elif action == 'generate':
            import calendar as cal_mod
            import json as _json
            year  = int(request.form.get('year', 2026))
            month = int(request.form.get('month', 1))
            if not (1 <= month <= 12):
                error = '올바른 월을 입력해주세요.'
            else:
                ctx = _payroll_month_ctx(db, year, month)

                emps = db.execute(
                    "SELECT u.id, s.base_salary, s.meal_allowance, s.transport_allowance "
                    "FROM users u "
                    "JOIN employee_salary s ON u.id = s.user_id "
                    "WHERE u.status = 'active'"
                ).fetchall()

                count = 0
                for e in emps:
                    if db.execute('SELECT 1 FROM payslips WHERE user_id=? AND year=? AND month=?', (e['id'], year, month)).fetchone():
                        continue

                    insert_draft_payslip(db, e['id'], year, month, compute_payslip_for(db, e, ctx))
                    # P0-2: 초안 단계 — 알림·Slack·이메일은 '확정' 시점에 일괄 실행
                    count += 1
                db.commit()
                msg = (f'{year}년 {month}월 급여명세서 {count}건이 초안으로 생성되었습니다. '
                       '검토 후 [확정·발송] 시 직원에게 공개됩니다.')

    from payroll_utils import calc_compa_ratio, compa_band as _compa_band
    raw_emps = db.execute(
        'SELECT u.id, u.name, d.name AS dept_name, p.name AS pos_name, '
        'COALESCE(s.base_salary, 0) AS base_salary, '
        'COALESCE(s.meal_allowance, 0) AS meal_allowance, '
        'COALESCE(s.transport_allowance, 0) AS transport_allowance, '
        'sg.mid_salary '
        'FROM users u '
        'LEFT JOIN departments  d  ON u.department_id  = d.id '
        'LEFT JOIN positions    p  ON u.position_id    = p.id '
        'LEFT JOIN employee_salary s ON u.id = s.user_id '
        'LEFT JOIN salary_grades sg ON sg.position_id  = u.position_id '
        '                          AND sg.job_family_id = u.job_family_id '
        "WHERE u.status='active' ORDER BY d.name, u.name"
    ).fetchall()
    emps = []
    for e in raw_emps:
        ratio = calc_compa_ratio(e['base_salary'], e['mid_salary'])
        emps.append({**dict(e), 'compa_ratio': ratio,
                     'compa_band': _compa_band(ratio)})
    return render_template('payroll/admin.html', emps=emps,
                           error=error, msg=msg, fmt_krw=fmt_krw,
                           active_page='admin_payroll')


# ── C1: 보상 검토 — 보정 등급 → 인상안·성과상여 → 연봉계약서 서명 → 적용일 급여 반영 ──
ACR_STAGE_LABEL = {
    'pending': '인상안 작성', 'returned': '반려', 'submitted': 'HR 검토',
    'sign_wait': '서명 대기', 'contract_rejected': '서명 거절',
    'signed': '적용일 대기', 'applied': '반영 완료',
}


def _acr_stage(r):
    """r: status, applied_at, contract_status"""
    if r['applied_at']:
        return 'applied'
    if r['status'] == 'approved':
        if r['contract_status'] == 'signed':
            return 'signed'
        if r['contract_status'] in ('rejected', 'cancelled'):
            return 'contract_rejected'
        return 'sign_wait'
    if r['status'] == 'submitted':
        return 'submitted'
    if r['status'] == 'rejected':
        return 'returned'
    return 'pending'


def _acr_new_salary(cur, pct):
    return int((cur or 0) * (1 + (pct or 0) / 100)) // 10 * 10


def _acr_final(r):
    """HR 조정값 우선, 없으면 부서장 인상안 → (인상률, 변경 월 기본급)"""
    if r['hr_override_pct'] is not None:
        return r['hr_override_pct'], r['hr_override_salary'] or _acr_new_salary(r['current_salary'], r['hr_override_pct'])
    pct = r['proposed_increase_pct'] or 0
    return pct, r['proposed_salary'] or _acr_new_salary(r['current_salary'], pct)


def _acr_period_months(db, perf_cycle_id):
    """성과상여 지급대상기간(개월) — 성과 주기 시작~종료, 1~12"""
    pc = db.execute('SELECT start_date, end_date FROM performance_cycles WHERE id=?',
                    (perf_cycle_id,)).fetchone() if perf_cycle_id else None
    try:
        days = (date.fromisoformat(pc['end_date'][:10]) - date.fromisoformat(pc['start_date'][:10])).days + 1
    except (TypeError, ValueError):
        return 12
    return max(1, min(12, round(days / 30.44)))


def _grade_rewards(db):
    """등급별 인상률(밴드 중간 기준)·성과상여 개월수 — 인상 매트릭스·상여 설정이 단일 출처"""
    at = {r['performance_grade']: r['increase_pct'] for r in
          db.execute("SELECT performance_grade, increase_pct FROM merit_matrix WHERE compa_band='at'").fetchall()}
    months = {r['grade']: r['bonus_months'] for r in
              db.execute('SELECT grade, bonus_months FROM grade_bonus_config').fetchall()}
    return [{'grade': g, 'pct': at.get(g, 0), 'months': months.get(g, 0)} for g in 'SABCD']


def _acr_rows(db, cycle_id, extra='', args=()):
    return db.execute(
        '''SELECT cr.*, u.name emp_name, d.name dept_name, p.name pos_name, m.name mgr_name,
                  (SELECT sg.mid_salary FROM salary_grades sg
                   WHERE sg.position_id=u.position_id AND sg.job_family_id=u.job_family_id LIMIT 1) mid_salary,
                  ct.status contract_status, ct.signed_at contract_signed_at
           FROM compensation_reviews cr
           JOIN users u ON u.id = cr.employee_id
           LEFT JOIN departments d ON d.id = COALESCE(cr.department_id, u.department_id)
           LEFT JOIN positions   p ON p.id = u.position_id
           LEFT JOIN users       m ON m.id = cr.manager_id
           LEFT JOIN contracts  ct ON ct.id = cr.contract_id
           WHERE cr.cycle_id = ?''' + extra + ' ORDER BY d.name, u.name',
        (cycle_id, *args)).fetchall()


def _acr_contract_html(db, cycle, rev):
    from html import escape as _esc
    pct, sal = _acr_final(rev)
    co  = get_company_info()
    emp = db.execute('SELECT u.name, d.name dept, p.name pos FROM users u '
                     'LEFT JOIN departments d ON d.id=u.department_id '
                     'LEFT JOIN positions p ON p.id=u.position_id WHERE u.id=?',
                     (rev['employee_id'],)).fetchone()
    perf = db.execute('SELECT name FROM performance_cycles WHERE id=?', (cycle['perf_cycle_id'],)).fetchone() \
        if cycle['perf_cycle_id'] else None
    eff = (cycle['effective_date'] or date.today().isoformat())[:10]
    try:
        ed = date.fromisoformat(eff)
        try:
            end = ed.replace(year=ed.year + 1) - timedelta(days=1)
        except ValueError:
            end = ed + timedelta(days=364)
        period = f'{ed.isoformat()} ~ {end.isoformat()}'
    except ValueError:
        period = eff
    cur, bonus = rev['current_salary'] or 0, rev['proposed_bonus'] or 0
    grade = rev['perf_grade'] or '미평가'
    pay_date = (cycle['bonus_pay_date'] or eff)[:7].replace('-', '년 ') + '월'
    B = 'border:1px solid #c8c8c8;padding:7px 10px;'
    TH = f'style="{B}background:#f4f4f4;text-align:left;font-weight:600;width:16%;"'
    TD = f'style="{B}"'
    TR = f'style="{B}text-align:right;font-variant-numeric:tabular-nums;"'
    TBL = 'style="width:100%;border-collapse:collapse;margin:10px 0 16px;font-size:13px;"'
    H3 = 'style="font-size:14px;font-weight:700;margin:22px 0 6px;"'
    n = 2
    parts = [
        '<h2 style="text-align:center;font-size:21px;font-weight:700;letter-spacing:.35em;margin:0 0 26px;">연봉계약서</h2>',
        f'<p style="line-height:1.8;">{_esc(co["name"])}(이하 "회사")와 {_esc(emp["name"])}(이하 "근로자")은 '
        f'「{_esc(cycle["name"])}」 결과에 따라 다음과 같이 연봉계약을 체결한다.</p>',
        f'<table {TBL}><tr><th {TH}>성명</th><td {TD}>{_esc(emp["name"])}</td><th {TH}>소속</th><td {TD}>{_esc(emp["dept"] or "-")}</td></tr>'
        f'<tr><th {TH}>직위</th><td {TD}>{_esc(emp["pos"] or "-")}</td><th {TH}>성과등급</th><td {TD}>{_esc(grade)}'
        f'{" (" + _esc(perf["name"]) + ")" if perf else ""}</td></tr>'
        f'<tr><th {TH}>적용기간</th><td {TD} colspan="3">{period}</td></tr></table>',
        f'<h3 {H3}>제1조 (연봉)</h3>',
        f'<table {TBL}><tr><th {TH}>구분</th><th {TH} style="{B}background:#f4f4f4;text-align:right;">종전</th>'
        f'<th style="{B}background:#f4f4f4;text-align:right;font-weight:600;">변경</th></tr>'
        f'<tr><td {TD}>월 기본급</td><td {TR}>{cur:,}원</td><td {TR}>{sal:,}원</td></tr>'
        f'<tr><td {TD}>연 기본급 (월 기본급 × 12)</td><td {TR}>{cur * 12:,}원</td><td {TR}>{sal * 12:,}원</td></tr>'
        f'<tr><td {TD}>인상률</td><td {TR}>-</td><td {TR}>{pct:+.1f}%</td></tr></table>',
        '<p style="line-height:1.8;">식대·교통비 등 수당과 연장·야간·휴일근로수당은 취업규칙 및 관계 법령에 따라 별도 지급한다.</p>',
    ]
    if bonus > 0:
        parts += [f'<h3 {H3}>제2조 (성과상여)</h3>',
                  f'<p style="line-height:1.8;">회사는 성과등급 {_esc(grade)}에 따른 성과상여 <b>{bonus:,}원</b>을 '
                  f'{pay_date} 급여에 포함하여 지급하며, 소득세법에 따라 원천징수한다.</p>']
        n = 3
    parts += [
        f'<h3 {H3}>제{n}조 (기타)</h3>',
        '<p style="line-height:1.8;">이 계약에 정하지 않은 사항은 근로기준법, 취업규칙 및 종전 근로계약에 따른다. '
        '이 계약은 전자서명으로 체결하며, 변경된 연봉은 적용기간 개시일(서명일이 늦은 경우 서명일)부터 적용한다.</p>',
        f'<p style="text-align:center;margin:30px 0 20px;">{date.today().year}년 {date.today().month}월 {date.today().day}일</p>',
        f'<table style="width:100%;font-size:13px;line-height:1.8;"><tr>'
        f'<td style="width:50%;vertical-align:top;"><b>회사</b><br>{_esc(co["name"])}<br>{_esc(co["address"])}<br>대표이사 {_esc(co["ceo"])} (인)</td>'
        f'<td style="width:50%;vertical-align:top;"><b>근로자</b><br>{_esc(emp["name"])}<br>{_esc(emp["dept"] or "")}<br>(전자서명)</td>'
        f'</tr></table>',
    ]
    return ''.join(parts)


def _acr_issue_contract(db, cycle, rev):
    title = f'연봉계약서 ({cycle["name"]})'
    cur = db.execute(
        'INSERT INTO contracts (template_id, employee_id, issued_by, title, content_html, comp_review_id) '
        'VALUES (NULL,?,?,?,?,?)',
        (rev['employee_id'], session['user_id'], title, _acr_contract_html(db, cycle, rev), rev['id']))
    db.execute('UPDATE compensation_reviews SET contract_id=? WHERE id=?', (cur.lastrowid, rev['id']))
    add_notification(rev['employee_id'], 'action', 'contract', f'서명 요청 — {title}',
                     '연봉계약서 서명 요청', url_for('contract_view', cid=cur.lastrowid))
    return cur.lastrowid


def _apply_due_comp_reviews(db):
    """서명 완료된 보상 검토 → 성과상여 지급 예약(bonus_payments) + 적용일 도래 시 기본급 반영. 반영 건수 반환."""
    today = date.today().isoformat()
    rows = db.execute(
        "SELECT cr.*, c.name cycle_name, c.effective_date, c.bonus_pay_date, c.perf_cycle_id, ct.signed_at "
        "FROM compensation_reviews cr "
        "JOIN compensation_review_cycles c ON c.id = cr.cycle_id "
        "JOIN contracts ct ON ct.id = cr.contract_id "
        "WHERE cr.status='approved' AND ct.status='signed' "
        "AND (cr.applied_at IS NULL OR (cr.bonus_payment_id IS NULL AND COALESCE(cr.proposed_bonus,0) > 0))"
    ).fetchall()
    if not rows:
        return 0
    period_cache, applied = {}, 0
    for r in rows:
        if r['bonus_payment_id'] is None and (r['proposed_bonus'] or 0) > 0:
            if r['perf_cycle_id'] not in period_cache:
                period_cache[r['perf_cycle_id']] = _acr_period_months(db, r['perf_cycle_id'])
            pay = (r['bonus_pay_date'] or r['effective_date'] or today)[:10]
            y, m = int(pay[:4]), int(pay[5:7])
            # 해당 월 명세가 이미 확정됐으면 다음 미확정 월로 이월
            while db.execute("SELECT 1 FROM payslips WHERE user_id=? AND year=? AND month=? AND status='confirmed'",
                             (r['employee_id'], y, m)).fetchone():
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                pay = f'{y}-{m:02d}-01'
            cur = db.execute(
                'INSERT INTO bonus_payments (user_id, bonus_type, amount, pay_date, note, period_months) '
                'VALUES (?,?,?,?,?,?)',
                (r['employee_id'], 'perf_bonus', r['proposed_bonus'], pay,
                 f'성과상여 — {r["cycle_name"]} ({r["perf_grade"] or "-"})', period_cache[r['perf_cycle_id']]))
            db.execute('UPDATE compensation_reviews SET bonus_payment_id=? WHERE id=?', (cur.lastrowid, r['id']))
        eff = max((r['effective_date'] or today)[:10], (r['signed_at'] or today)[:10])
        if r['applied_at'] is None and eff <= today:
            pct, sal = _acr_final(r)
            old = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (r['employee_id'],)).fetchone()
            db.execute(
                'INSERT INTO salary_history (user_id, changed_by, old_base_salary, new_base_salary, '
                'old_meal, new_meal, old_transport, new_transport, reason) VALUES (?,?,?,?,?,?,?,?,?)',
                (r['employee_id'], r['approved_by'], old['base_salary'] if old else 0, sal,
                 old['meal_allowance'] if old else 0, old['meal_allowance'] if old else 0,
                 old['transport_allowance'] if old else 0, old['transport_allowance'] if old else 0,
                 f'보상 검토 「{r["cycle_name"]}」 {pct:+.1f}% (연봉계약서 서명)'))
            if old:
                db.execute('UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
                           (sal, r['employee_id']))
            else:
                db.execute('INSERT INTO employee_salary (user_id, base_salary) VALUES (?,?)', (r['employee_id'], sal))
            db.execute('UPDATE compensation_reviews SET applied_at=CURRENT_TIMESTAMP WHERE id=?', (r['id'],))
            db.execute(
                'INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                (r['employee_id'], 'info', 'salary', '연봉 반영 완료',
                 f'{eff}부 월 기본급 {sal:,}원 ({pct:+.1f}%)', f'/payroll/total-compensation/{r["employee_id"]}'))
            applied += 1
    db.commit()
    if applied:
        log_audit('update', 'salary', None, f'보상 검토 연봉 반영 — {applied}명')
    return applied


def _acr_cycle_or_404(db, cycle_id):
    c = db.execute('SELECT c.*, pc.name perf_name, pc.start_date perf_start, pc.end_date perf_end '
                   'FROM compensation_review_cycles c '
                   'LEFT JOIN performance_cycles pc ON pc.id = c.perf_cycle_id WHERE c.id=?', (cycle_id,)).fetchone()
    if not c:
        abort(404)
    return c


@app.route('/payroll/acr')
@login_required
def acr_list():
    """보상 검토 주기 목록 + 성과 주기에서 생성"""
    role, uid = session['user_role'], session['user_id']
    if role not in ('admin', 'manager'):
        abort(403)
    db = get_db()
    _apply_due_comp_reviews(db)
    is_admin = role == 'admin'
    per = {}
    for r in db.execute(
            'SELECT cr.*, ct.status contract_status FROM compensation_reviews cr '
            'LEFT JOIN contracts ct ON ct.id = cr.contract_id'
            + ('' if is_admin else ' WHERE cr.manager_id=?'), () if is_admin else (uid,)).fetchall():
        s = per.setdefault(r['cycle_id'], {'n': 0, 'cur': 0, 'new': 0, 'bonus': 0, 'stages': {}})
        st = _acr_stage(r)
        s['stages'][st] = s['stages'].get(st, 0) + 1
        s['n'] += 1
        _, sal = _acr_final(r)
        s['cur'] += r['current_salary'] or 0
        s['new'] += sal
        s['bonus'] += r['proposed_bonus'] or 0
    cycles = []
    for c in db.execute('SELECT c.*, pc.name perf_name FROM compensation_review_cycles c '
                        'LEFT JOIN performance_cycles pc ON pc.id = c.perf_cycle_id ORDER BY c.id DESC').fetchall():
        if not is_admin and (c['id'] not in per or c['status'] == 'draft'):
            continue
        s = per.get(c['id'], {'n': 0, 'cur': 0, 'new': 0, 'bonus': 0, 'stages': {}})
        budget = int(s['cur'] * 12 * (c['budget_pct'] or 0) / 100)
        used = (s['new'] - s['cur']) * 12
        cycles.append({**dict(c), **s, 'budget': budget, 'used': used,
                       'over': bool(c['budget_pct']) and used > budget,
                       'todo': s['stages'].get('pending', 0) + s['stages'].get('returned', 0)})

    perf_cycles, sel_perf, defaults = [], None, {}
    if is_admin:
        perf_cycles = db.execute(
            "SELECT pc.*, (SELECT COUNT(*) FROM calibration_results r WHERE r.cycle_id=pc.id AND r.final_grade IS NOT NULL) graded, "
            "(SELECT COUNT(*) FROM compensation_review_cycles c WHERE c.perf_cycle_id=pc.id) linked "
            "FROM performance_cycles pc WHERE pc.stage IN ('calibration','appeal','closed') OR pc.status='closed' "
            "ORDER BY pc.id DESC").fetchall()
        want = request.args.get('perf_cycle', type=int)
        sel_perf = next((p for p in perf_cycles if p['id'] == want), None) \
            or next((p for p in perf_cycles if p['graded'] and not p['linked']), None)
        t = date.today()
        eff = date(t.year + (t.month == 12), t.month % 12 + 1, 1).isoformat()
        defaults = {'name': f'{sel_perf["name"]} 보상 검토' if sel_perf else '', 'effective_date': eff,
                    'bonus_pay_date': eff, 'budget_pct': 4.0}
    return render_template('payroll/acr_list.html', cycles=cycles, perf_cycles=perf_cycles,
                           sel_perf=sel_perf, defaults=defaults, is_admin=is_admin,
                           stage_label=ACR_STAGE_LABEL, grade_rewards=_grade_rewards(db),
                           active_page='acr')


@app.route('/payroll/acr/new', methods=['POST'])
@admin_required
def acr_new():
    db = get_db()
    perf_id = request.form.get('perf_cycle_id', type=int)
    pc = db.execute('SELECT * FROM performance_cycles WHERE id=?', (perf_id,)).fetchone() if perf_id else None
    if not pc:
        flash('연결할 성과 주기를 선택하세요.', 'error')
        return redirect(url_for('acr_list'))
    graded = db.execute('SELECT COUNT(*) FROM calibration_results WHERE cycle_id=? AND final_grade IS NOT NULL',
                        (perf_id,)).fetchone()[0]
    if not graded:
        flash(f'「{pc["name"]}」 보정 등급 확정 인원 0명 · 보정 완료 후 생성', 'error')
        return redirect(url_for('acr_list', perf_cycle=perf_id))
    name = request.form.get('name', '').strip() or f'{pc["name"]} 보상 검토'
    eff = request.form.get('effective_date', '').strip()
    bonus_date = request.form.get('bonus_pay_date', '').strip() or eff
    try:
        date.fromisoformat(eff)
        date.fromisoformat(bonus_date)
        budget_pct = max(0.0, min(50.0, float(request.form.get('budget_pct') or 0)))
    except ValueError:
        flash('적용일·상여 지급일·예산 비율을 확인하세요.', 'error')
        return redirect(url_for('acr_list', perf_cycle=perf_id))
    cur = db.execute(
        'INSERT INTO compensation_review_cycles (name, review_year, effective_date, created_by, '
        'perf_cycle_id, budget_pct, bonus_pay_date) VALUES (?,?,?,?,?,?,?)',
        (name, int(eff[:4]), eff, session['user_id'], perf_id, budget_pct, bonus_date))
    db.commit()
    log_audit('create', 'salary', None, f'보상 검토 「{name}」 생성 (성과 주기 {pc["name"]})')
    flash(f'「{name}」 생성 · 오픈 시 인상안이 부서장에게 전달됩니다', 'success')
    return redirect(url_for('acr_detail', cycle_id=cur.lastrowid))


@app.route('/payroll/acr/<int:cycle_id>/open', methods=['POST'])
@admin_required
def acr_open(cycle_id):
    from payroll_utils import calc_compa_ratio, merit_from_matrix
    db = get_db()
    cycle = _acr_cycle_or_404(db, cycle_id)
    if cycle['status'] != 'draft':
        flash('초안 상태에서만 오픈할 수 있습니다.', 'error')
        return redirect(url_for('acr_detail', cycle_id=cycle_id))
    grades = {r['user_id']: r['final_grade'] for r in db.execute(
        'SELECT user_id, final_grade FROM calibration_results WHERE cycle_id=? AND final_grade IS NOT NULL',
        (cycle['perf_cycle_id'],)).fetchall()}
    months = {r['grade']: r['bonus_months'] for r in db.execute('SELECT grade, bonus_months FROM grade_bonus_config').fetchall()}
    emps = db.execute(
        "SELECT u.id, u.manager_id, u.department_id, s.base_salary, "
        "(SELECT sg.mid_salary FROM salary_grades sg WHERE sg.position_id=u.position_id "
        " AND sg.job_family_id=u.job_family_id LIMIT 1) mid_salary "
        "FROM users u JOIN employee_salary s ON s.user_id = u.id "
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest') AND s.base_salary > 0").fetchall()
    per_mgr = {}
    for e in emps:
        g = grades.get(e['id'])
        pct = merit_from_matrix(db, g, calc_compa_ratio(e['base_salary'], e['mid_salary'])) if g else 0.0
        bonus = int(e['base_salary'] * months.get(g, 0)) // 10 * 10 if g else 0
        db.execute(
            'INSERT OR IGNORE INTO compensation_reviews (cycle_id, employee_id, manager_id, department_id, '
            'current_salary, perf_grade, suggested_pct, proposed_increase_pct, proposed_salary, proposed_bonus) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (cycle_id, e['id'], e['manager_id'], e['department_id'], e['base_salary'], g, pct, pct,
             _acr_new_salary(e['base_salary'], pct), bonus))
        if e['manager_id']:
            per_mgr[e['manager_id']] = per_mgr.get(e['manager_id'], 0) + 1
    db.execute("UPDATE compensation_review_cycles SET status='open' WHERE id=?", (cycle_id,))
    db.commit()
    link = url_for('acr_detail', cycle_id=cycle_id)
    for mid, n in per_mgr.items():
        db.execute('INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                   (mid, 'action', 'payroll', f'보상 검토 — {cycle["name"]}', f'팀원 {n}명 인상안 확인·제출', link))
    db.commit()
    log_audit('update', 'salary', None, f'보상 검토 「{cycle["name"]}」 오픈 — {len(emps)}명, 등급 {len(grades)}명')
    flash(f'오픈 · 대상 {len(emps)}명 · 부서장 {len(per_mgr)}명에게 인상안 전달', 'success')
    return redirect(url_for('acr_detail', cycle_id=cycle_id))


@app.route('/payroll/acr/<int:cycle_id>/close', methods=['POST'])
@admin_required
def acr_close(cycle_id):
    db = get_db()
    cycle = _acr_cycle_or_404(db, cycle_id)
    db.execute("UPDATE compensation_review_cycles SET status='closed' WHERE id=?", (cycle_id,))
    db.commit()
    log_audit('update', 'salary', None, f'보상 검토 「{cycle["name"]}」 마감')
    flash('보상 검토가 마감되었습니다. 서명 완료분은 적용일에 계속 반영됩니다.', 'success')
    return redirect(url_for('acr_detail', cycle_id=cycle_id))


@app.route('/payroll/acr/<int:cycle_id>')
@login_required
def acr_detail(cycle_id):
    """부서장: 팀 인상안 작성·제출 / HR: 전체 검토·승인·계약서 발송"""
    from payroll_utils import calc_compa_ratio
    role, uid = session['user_role'], session['user_id']
    if role not in ('admin', 'manager'):
        abort(403)
    db = get_db()
    cycle = _acr_cycle_or_404(db, cycle_id)
    is_admin = role == 'admin'
    if not is_admin and cycle['status'] == 'draft':
        abort(403)
    _apply_due_comp_reviews(db)
    rows = _acr_rows(db, cycle_id, '' if is_admin else ' AND cr.manager_id=?', () if is_admin else (uid,))
    if not is_admin and not rows:
        abort(403)

    sel_dept, sel_stage = request.args.get('dept', ''), request.args.get('stage', '')
    budget_pct = cycle['budget_pct'] or 0
    items, stage_counts, groups = [], {k: 0 for k in ACR_STAGE_LABEL}, {}
    tot = {'n': 0, 'cur': 0, 'new': 0, 'bonus': 0}
    for r in rows:
        pct, sal = _acr_final(r)
        stage = _acr_stage(r)
        stage_counts[stage] += 1
        dept = r['dept_name'] or '부서 미지정'
        g = groups.setdefault(dept, {'dept': dept, 'n': 0, 'cur': 0, 'new': 0, 'bonus': 0})
        for d in (g, tot):
            d['n'] += 1
            d['cur'] += r['current_salary'] or 0
            d['new'] += sal
            d['bonus'] += r['proposed_bonus'] or 0
        if (sel_dept and dept != sel_dept) or (sel_stage and stage != sel_stage):
            continue
        items.append({**dict(r), 'dept': dept, 'final_pct': pct, 'final_salary': sal, 'stage': stage,
                      'compa': calc_compa_ratio(r['current_salary'], r['mid_salary']),
                      'compa_new': calc_compa_ratio(sal, r['mid_salary']),
                      'mgr_edit': (not is_admin and cycle['status'] == 'open' and stage in ('pending', 'returned')),
                      'hr_edit': (is_admin and ((cycle['status'] == 'open' and stage in ('pending', 'returned', 'submitted'))
                                                or stage == 'contract_rejected'))})
    for d in list(groups.values()) + [tot]:
        d['budget'] = int(d['cur'] * 12 * budget_pct / 100)
        d['used'] = (d['new'] - d['cur']) * 12
        d['over'] = bool(budget_pct) and d['used'] > d['budget']
    return render_template('payroll/acr.html', cycle=cycle, items=items, stage_counts=stage_counts,
                           groups=sorted(groups.values(), key=lambda x: x['dept']), tot=tot,
                           is_admin=is_admin, sel_dept=sel_dept, sel_stage=sel_stage,
                           stage_label=ACR_STAGE_LABEL, period_months=_acr_period_months(db, cycle['perf_cycle_id']),
                           grade_rewards=_grade_rewards(db), copilot=copilot_view(), active_page='acr')


@app.route('/payroll/acr/<int:cycle_id>/submit', methods=['POST'])
@login_required
def acr_submit(cycle_id):
    """부서장: 인상안 저장 / 제출"""
    db, uid = get_db(), session['user_id']
    cycle = _acr_cycle_or_404(db, cycle_id)
    if cycle['status'] != 'open':
        flash('진행 중인 보상 검토가 아닙니다.', 'error')
        return redirect(url_for('acr_detail', cycle_id=cycle_id))
    submit = request.form.get('action') == 'submit'
    n = 0
    for eid in request.form.getlist('emp_id', type=int):
        rev = db.execute('SELECT * FROM compensation_reviews WHERE cycle_id=? AND employee_id=? AND manager_id=?',
                         (cycle_id, eid, uid)).fetchone()
        if not rev or rev['status'] not in ('pending', 'rejected'):
            continue
        try:
            pct = round(max(-10.0, min(50.0, float(request.form.get(f'pct_{eid}') or 0))), 1)
        except ValueError:
            continue
        db.execute('UPDATE compensation_reviews SET proposed_increase_pct=?, proposed_salary=?, manager_note=?, status=? '
                   'WHERE id=?',
                   (pct, _acr_new_salary(rev['current_salary'], pct), request.form.get(f'note_{eid}', '').strip(),
                    'submitted' if submit else rev['status'], rev['id']))
        n += 1
    db.commit()
    if submit and n:
        link = url_for('acr_detail', cycle_id=cycle_id, stage='submitted')
        for a in db.execute("SELECT id FROM users WHERE role='admin' AND status='active'").fetchall():
            db.execute('INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                       (a['id'], 'action', 'payroll', f'보상 검토 제출 — {cycle["name"]}',
                        f'{session.get("user_name", "부서장")} · {n}명', link))
        db.commit()
        log_audit('update', 'salary', None, f'보상 검토 「{cycle["name"]}」 인상안 제출 — {n}명')
    flash((f'{n}명 제출 · HR 검토 대기' if submit else f'{n}명 임시저장') if n else '변경 대상 없음',
          'success' if n else 'warning')
    return redirect(url_for('acr_detail', cycle_id=cycle_id))


@app.route('/payroll/acr/<int:cycle_id>/approve', methods=['POST'])
@admin_required
def acr_approve(cycle_id):
    """HR: 승인 → 연봉계약서 발송 / 반려 / 서명 거절분 재발송"""
    db = get_db()
    cycle = _acr_cycle_or_404(db, cycle_id)
    action = request.form.get('action', 'approve')
    back = redirect(url_for('acr_detail', cycle_id=cycle_id, dept=request.form.get('dept') or None,
                            stage=request.form.get('stage') or None))
    allowed = {'approve': ('pending', 'returned', 'submitted'), 'return': ('pending', 'submitted'),
               'reissue': ('contract_rejected',)}.get(action)
    if not allowed:
        abort(400)
    if action != 'reissue' and cycle['status'] != 'open':
        flash('진행 중인 보상 검토가 아닙니다.', 'error')
        return back
    done, issued, returned_mgrs = 0, [], {}
    for eid in request.form.getlist('emp_id', type=int):
        rev = db.execute('SELECT cr.*, ct.status contract_status FROM compensation_reviews cr '
                         'LEFT JOIN contracts ct ON ct.id=cr.contract_id WHERE cr.cycle_id=? AND cr.employee_id=?',
                         (cycle_id, eid)).fetchone()
        if not rev or _acr_stage(rev) not in allowed:
            continue
        note = request.form.get(f'hr_note_{eid}', '').strip() or rev['hr_note']
        if action == 'return':
            db.execute("UPDATE compensation_reviews SET status='rejected', hr_note=? WHERE id=?", (note, rev['id']))
            if rev['manager_id']:
                returned_mgrs[rev['manager_id']] = returned_mgrs.get(rev['manager_id'], 0) + 1
            done += 1
            continue
        try:
            raw_pct = request.form.get(f'hr_pct_{eid}', '').strip()
            hr_pct = round(max(-10.0, min(50.0, float(raw_pct))), 1) if raw_pct else None
            raw_bonus = request.form.get(f'bonus_{eid}', '').strip().replace(',', '')
            bonus = max(0, int(float(raw_bonus))) // 10 * 10 if raw_bonus else (rev['proposed_bonus'] or 0)
        except ValueError:
            continue
        if hr_pct is not None and hr_pct == round(rev['proposed_increase_pct'] or 0, 1):
            hr_pct = None
        db.execute("UPDATE compensation_reviews SET hr_override_pct=?, hr_override_salary=?, hr_note=?, proposed_bonus=?, "
                   "status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
                   (hr_pct, _acr_new_salary(rev['current_salary'], hr_pct) if hr_pct is not None else None,
                    note, bonus, session['user_id'], rev['id']))
        rev = db.execute('SELECT * FROM compensation_reviews WHERE id=?', (rev['id'],)).fetchone()
        issued.append((_acr_issue_contract(db, cycle, rev), rev['employee_id']))
        done += 1
    db.commit()
    for mid, n in returned_mgrs.items():
        add_notification(mid, 'action', 'payroll', f'보상 검토 반려 — {cycle["name"]}', f'{n}명 인상안 재작성',
                         url_for('acr_detail', cycle_id=cycle_id))
    if issued:
        try:
            from integrations.dispatcher import notify_slack
            for _cid, eid in issued:
                u = db.execute('SELECT email, name FROM users WHERE id=?', (eid,)).fetchone()
                if u and u['email']:
                    notify_slack(u['email'], f'[TalentCore] 연봉계약서 서명 요청\n「{cycle["name"]}」 연봉계약서가 도착했습니다.',
                                 '연봉계약서 서명 요청', name=u['name'])
        except Exception:
            pass
    label = {'approve': '승인·연봉계약서 발송', 'return': '반려', 'reissue': '연봉계약서 재발송'}[action]
    if done:
        log_audit('update', 'salary', None, f'보상 검토 「{cycle["name"]}」 {label} — {done}명')
    flash(f'{done}명 {label}' if done else '처리 대상 없음 (단계 확인)', 'success' if done else 'warning')
    return back


# ── 총보상 명세 (C1: 급여 확정분 + 성과상여 + 급여 외 복리후생 + 최근 보상 검토) ──
@app.route('/payroll/total-compensation/<int:uid>')
@login_required
def total_compensation(uid):
    from payroll_utils import calc_compa_ratio
    role = session['user_role']
    if role != 'admin' and session['user_id'] != uid:
        abort(403)
    db = get_db()
    _apply_due_comp_reviews(db)
    emp = db.execute(
        'SELECT u.*, d.name dept_name, p.name pos_name, jf.name jf_name FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id = jf.id WHERE u.id=?', (uid,)).fetchone()
    if not emp:
        abort(404)
    year = request.args.get('year', type=int) or date.today().year
    years = [r[0] for r in db.execute('SELECT DISTINCT year FROM payslips WHERE user_id=? ORDER BY year DESC', (uid,)).fetchall()]
    if year not in years:
        years = sorted(set(years) | {year}, reverse=True)

    payslips = db.execute("SELECT * FROM payslips WHERE user_id=? AND year=? AND status='confirmed' ORDER BY month",
                          (uid, year)).fetchall()
    sums = {k: sum((p[k] or 0) for p in payslips) for k in
            ('base_salary', 'meal_allowance', 'transport_allowance', 'overtime_pay', 'bonus_pay', 'perf_bonus',
             'gross_pay', 'total_deduction', 'net_pay')}
    paid_months = {p['month'] for p in payslips if p['perf_bonus']}

    bonuses = db.execute("SELECT * FROM bonus_payments WHERE user_id=? AND substr(pay_date,1,4)=? ORDER BY pay_date",
                         (uid, str(year))).fetchall()
    scheduled_bonus = sum(b['amount'] for b in bonuses
                          if b['bonus_type'] == 'perf_bonus' and int(b['pay_date'][5:7]) not in paid_months)
    other_bonus = sum(b['amount'] for b in bonuses if b['bonus_type'] != 'perf_bonus')

    benefit_items = []
    for cfg in db.execute("SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type IN ('annual_budget','reimbursement')").fetchall():
        if cfg['amount']:
            benefit_items.append({'name': BENEFIT_CATALOG.get(cfg['key'], {}).get('name', cfg['key']),
                                  'type': PAYMENT_TYPE_LABELS.get(cfg['payment_type'], (cfg['payment_type'],))[0],
                                  'annual': cfg['amount']})
    benefit_total = sum(i['annual'] for i in benefit_items)

    salary_row = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (uid,)).fetchone()
    base_salary = salary_row['base_salary'] if salary_row else 0
    avg_month = int(sums['gross_pay'] / len(payslips)) if payslips else base_salary
    severance = avg_month * len(payslips) // 12 if payslips else 0
    total_comp = sums['gross_pay'] + scheduled_bonus + other_bonus + benefit_total + severance

    cal = db.execute('SELECT r.final_grade, pc.name FROM calibration_results r JOIN performance_cycles pc ON pc.id=r.cycle_id '
                     'WHERE r.user_id=? AND r.final_grade IS NOT NULL ORDER BY pc.start_date DESC LIMIT 1', (uid,)).fetchone()
    band = db.execute('SELECT min_salary, mid_salary, max_salary FROM salary_grades WHERE position_id=? AND job_family_id=? LIMIT 1',
                      (emp['position_id'], emp['job_family_id'])).fetchone() \
        if emp['position_id'] and emp['job_family_id'] else None
    compa = calc_compa_ratio(base_salary, band['mid_salary'] if band else None)

    review = db.execute(
        'SELECT cr.*, c.name cycle_name, c.effective_date, ct.status contract_status, ct.id cid '
        'FROM compensation_reviews cr JOIN compensation_review_cycles c ON c.id=cr.cycle_id '
        "LEFT JOIN contracts ct ON ct.id=cr.contract_id WHERE cr.employee_id=? AND cr.status='approved' "
        'ORDER BY cr.id DESC LIMIT 1', (uid,)).fetchone()
    review_info = None
    if review:
        pct, sal = _acr_final(review)
        review_info = {**dict(review), 'final_pct': pct, 'final_salary': sal, 'stage': _acr_stage(review)}

    return render_template('payroll/total_comp.html', emp=emp, year=year, years=years,
                           payslips=payslips, sums=sums, bonuses=bonuses,
                           scheduled_bonus=scheduled_bonus, other_bonus=other_bonus,
                           benefit_items=benefit_items, benefit_total=benefit_total,
                           severance=severance, total_comp=total_comp,
                           base_salary=base_salary, salary_row=salary_row,
                           cal=cal, band=band, compa=compa, review=review_info,
                           stage_label=ACR_STAGE_LABEL, is_admin=(role == 'admin'),
                           active_page='payroll')


# ── v0.53: Pay Equity — admin/analytics에서 호출 ──────────────────────────────
def get_pay_equity_data(db):
    from payroll_utils import calc_compa_ratio, compa_band
    rows = db.execute(
        '''SELECT u.id, u.name,
                  d.name dept_name, p.name pos_name, jf.name jf_name,
                  COALESCE(s.base_salary,0) base_salary,
                  sg.min_salary, sg.mid_salary, sg.max_salary,
                  (SELECT cr.final_grade FROM calibration_results cr
                   WHERE cr.user_id=u.id ORDER BY cr.id DESC LIMIT 1) perf_grade
           FROM users u
           LEFT JOIN departments   d  ON u.department_id  = d.id
           LEFT JOIN positions     p  ON u.position_id    = p.id
           LEFT JOIN job_families  jf ON u.job_family_id  = jf.id
           LEFT JOIN employee_salary s ON u.id = s.user_id
           LEFT JOIN salary_grades sg  ON sg.position_id  = u.position_id
                                      AND sg.job_family_id = u.job_family_id
           WHERE u.status=\'active\' AND u.role NOT IN (\'admin\',\'guest\')
           ORDER BY d.name, u.name'''
    ).fetchall()
    result = []
    for r in rows:
        ratio = calc_compa_ratio(r['base_salary'], r['mid_salary'])
        band  = compa_band(ratio)
        result.append({**dict(r), 'compa_ratio': ratio, 'compa_band': band})
    return result


@app.route('/payroll/salary-table')
@admin_required
def salary_table():
    db = get_db()
    positions   = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    job_families = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    grades_raw  = db.execute(
        'SELECT sg.job_family_id, sg.position_id, sg.annual_salary '
        'FROM salary_grades sg'
    ).fetchall()
    # {(job_family_id, position_id): annual_salary}
    grade_map = {(r['job_family_id'], r['position_id']): r['annual_salary'] for r in grades_raw}
    return render_template('payroll/salary_table.html',
                           positions=positions,
                           job_families=job_families,
                           grade_map=grade_map,
                           fmt_krw=fmt_krw,
                           active_page='salary_table')


# ── Certificate helpers ───────────────────────────────────────
def _cert_user(db):
    """admin은 ?user_id=X 로 대상 지정 가능, 나머지는 본인"""
    role = session.get('user_role')
    uid  = int(request.args.get('user_id', session['user_id']))
    if role not in ('admin',) and uid != session['user_id']:
        abort(403)
    user = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name, '
        '       jf.name AS jf_name '
        'FROM users u '
        'LEFT JOIN departments d   ON u.department_id  = d.id '
        'LEFT JOIN positions   p   ON u.position_id    = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id  = jf.id '
        'WHERE u.id=?', (uid,)
    ).fetchone()
    if not user:
        abort(404)
    return user


# ── Certificate hub ───────────────────────────────────────────
@app.route('/certificates')
@login_required
def certificates_hub():
    db   = get_db()
    role = session.get('user_role')
    uid  = session['user_id']
    
    # 일반 직원은 본인 신청 내역만, 어드민은 전체 내역
    if role == 'admin':
        requests = db.execute(
            'SELECT r.*, u.name as user_name FROM certificate_requests r '
            'JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC'
        ).fetchall()
        employees = db.execute(
            "SELECT id, name, department_id FROM users "
            "WHERE status IN ('active','resigned') AND role != 'guest' ORDER BY name"
        ).fetchall()
    else:
        requests = db.execute(
            'SELECT * FROM certificate_requests WHERE user_id=? ORDER BY created_at DESC',
            (uid,)
        ).fetchall()
        employees = []

    # 출력 타겟 (Admin이 직원 대리 발급 시 사용하던 기존 로직 유지)
    target_id = request.args.get('user_id', uid)
    if role != 'admin': target_id = uid
    target = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
        'FROM users u LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions p ON u.position_id=p.id WHERE u.id=?', (target_id,)
    ).fetchone()

    cur_year = date.today().year
    years = list(range(cur_year, cur_year - 5, -1))
    return render_template('certificate/hub.html',
                           requests=requests,
                           employees=employees,
                           target=target,
                           selected_uid=target_id,
                           years=years,
                           active_page='certificates')

@app.route('/certificate/request', methods=['POST'])
@login_required
def certificate_request():
    db = get_db()
    cert_type = request.form.get('cert_type')
    purpose   = request.form.get('purpose')
    uid       = session['user_id']

    if cert_type not in ('employment','career','income','resignation'):
        flash('유효하지 않은 증명서 종류입니다.', 'error')
        return redirect(url_for('certificates_hub'))

    # 결재선 설정: auto면 신청 즉시 자동 발급 (Phase C-13)
    if get_approval_chain(db, 'certificate') == 'auto':
        db.execute(
            "INSERT INTO certificate_requests (user_id, cert_type, purpose, status, approved_at) "
            "VALUES (?,?,?,'approved',CURRENT_TIMESTAMP)",
            (uid, cert_type, purpose)
        )
        db.commit()
        flash('증명서가 발급되었습니다. 아래 발급 내역에서 바로 출력할 수 있습니다.', 'success')
        return redirect(url_for('certificates_hub'))

    db.execute(
        'INSERT INTO certificate_requests (user_id, cert_type, purpose) VALUES (?,?,?)',
        (uid, cert_type, purpose)
    )
    db.commit()

    # 알림 발송: HR 전체에게 신청 알림
    admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
    for admin in admins:
        add_notification(
            admin['id'], 'action', 'cert',
            f"증명서 신청: {session['user_name']}",
            f"{session['user_name']}님이 {cert_type} 발급을 신청했습니다.",
            url_for('certificates_hub')
        )

    flash('증명서 발급 신청이 완료되었습니다. HR 승인을 기다려 주세요.', 'success')
    return redirect(url_for('certificates_hub'))

@app.route('/certificate/<int:req_id>/approve', methods=['POST'])
@admin_required
def certificate_approve(req_id):
    db = get_db()
    req = db.execute('SELECT user_id, cert_type FROM certificate_requests WHERE id=?', (req_id,)).fetchone()
    
    db.execute(
        "UPDATE certificate_requests SET status='approved', approver_id=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
        (session['user_id'], req_id)
    )
    db.commit()

    # 알림 발송: 본인에게 승인 알림
    add_notification(
        req['user_id'], 'info', 'cert',
        "증명서 승인 완료",
        f"신청하신 증명서 발급이 승인되었습니다. 지금 출력하실 수 있습니다.",
        url_for('certificates_hub')
    )

    flash('증명서 발급을 승인했습니다.', 'success')
    return redirect(url_for('certificates_hub'))

@app.route('/certificate/<int:req_id>/reject', methods=['POST'])
@admin_required
def certificate_reject(req_id):
    db = get_db()
    req = db.execute('SELECT user_id FROM certificate_requests WHERE id=?', (req_id,)).fetchone()
    reason = request.form.get('reason', '').strip()
    
    db.execute(
        "UPDATE certificate_requests SET status='rejected', reject_reason=?, approver_id=? WHERE id=?",
        (reason, session['user_id'], req_id)
    )
    db.commit()

    # 알림 발송: 본인에게 반려 알림
    add_notification(
        req['user_id'], 'info', 'cert',
        "증명서 반려 안내",
        f"증명서 발급 신청이 반려되었습니다. (사유: {reason or '미기재'})",
        url_for('certificates_hub')
    )

    flash('증명서 발급 신청을 반려했습니다.', 'warning')
    return redirect(url_for('certificates_hub'))


# ── Certificate ──────────────────────────────────────────────
@app.route('/certificate/view/<int:req_id>')
@login_required
def cert_view(req_id):
    db   = get_db()
    req  = db.execute('SELECT * FROM certificate_requests WHERE id=?', (req_id,)).fetchone()
    if not req: abort(404)
    
    # 본인 혹은 어드민만 조회 가능
    if session.get('user_role') != 'admin' and req['user_id'] != session['user_id']:
        abort(403)
    
    # 승인된 상태에서만 출력 가능
    if req['status'] != 'approved':
        flash('승인되지 않은 증명서는 조회할 수 없습니다.', 'error')
        return redirect(url_for('certificates_hub'))

    uid   = req['user_id']
    user  = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
        'FROM users u LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions p ON u.position_id=p.id WHERE u.id=?', (uid,)
    ).fetchone()
    
    today = date.today()
    c_info = get_company_info()

    if req['cert_type'] == 'employment':
        cert_no = f"EMP-{req['approved_at'][:7].replace('-','')}-{uid:04d}"
        return render_template('certificate/employment.html', user=user, today=today.strftime('%Y년 %m월 %d일'), cert_no=cert_no, company=c_info)
    
    elif req['cert_type'] == 'career':
        cert_no = f"CAR-{req['approved_at'][:7].replace('-','')}-{uid:04d}"
        return render_template('certificate/career.html', user=user, today=today.strftime('%Y년 %m월 %d일'), cert_no=cert_no, company=c_info)
    
    elif req['cert_type'] == 'resignation':
        cert_no = f"RES-{req['approved_at'][:7].replace('-','')}-{uid:04d}"
        return render_template('certificate/resignation.html', user=user, today=today.strftime('%Y년 %m월 %d일'), cert_no=cert_no, company=c_info)
    
    elif req['cert_type'] == 'income':
        # 소득증명은 연도 파라미터가 추가로 필요할 수 있음 (기본은 신청일 기준 전년도 혹은 현재년도)
        year = today.year
        slips = db.execute("SELECT * FROM payslips WHERE user_id=? AND year=? AND status='confirmed' ORDER BY month", (uid, year)).fetchall()
        annual_gross = sum(s['gross_pay'] for s in slips)
        annual_tax = sum(s['income_tax'] for s in slips)
        annual_local_tax = sum(s['local_income_tax'] for s in slips)
        annual_pension = sum(s['national_pension'] for s in slips)
        annual_health = sum(s['health_insurance'] for s in slips)
        annual_ltcare = sum(s['long_term_care'] for s in slips)
        annual_emp_ins = sum(s['employment_insurance'] for s in slips)
        annual_net = sum(s['net_pay'] for s in slips)
        cert_no = f"INC-{year}-{uid:04d}"
        return render_template('certificate/income.html', user=user, year=year, slips=slips, annual_gross=annual_gross, annual_tax=annual_tax, annual_local_tax=annual_local_tax,
                               annual_pension=annual_pension, annual_health=annual_health, annual_ltcare=annual_ltcare, annual_emp_ins=annual_emp_ins, annual_net=annual_net,
                               today=today.strftime('%Y년 %m월 %d일'), cert_no=cert_no, company=c_info)
    
    abort(400)


# ── Performance ──────────────────────────────────────────────
SCORE_LABELS = {5: 'S — 탁월', 4: 'A — 우수', 3: 'B — 양호', 2: 'C — 개선필요', 1: 'D — 미흡'}

# ── 성과 주기 상태머신 (v1.1.0, saas_plan.md §4) ─────────────────
CYCLE_STAGES = ['goal', 'progress', 'review', 'calibration', 'appeal', 'closed']
CYCLE_STAGE_LABEL = {
    'goal':        '목표 수립',
    'progress':    '진행 중',
    'review':      '평가 진행',
    'calibration': 'HR 조정',
    'appeal':      '결과 공개·이의신청',
    'closed':      '종료',
}
CYCLE_STAGE_DESC = {
    'goal':        '직원이 목표를 작성하고 팀장이 확정합니다. 확정 전에는 평가할 수 없습니다.',
    'progress':    '목표가 확정되어 진행률을 수시로 업데이트하는 단계입니다.',
    'review':      '자기평가·다면평가·팀장 평가를 작성하는 단계입니다.',
    'calibration': 'HR이 부서별 등급 분포를 확인하고 최종 등급을 조정·확정하는 단계입니다.',
    'appeal':      '등급이 본인에게 공개되었습니다. 이의신청 기간 내 1회 재검토를 요청할 수 있습니다.',
    'closed':      '주기가 종료되었습니다. 등급별 인상률·상여 연동을 진행할 수 있습니다.',
}
GOAL_APPROVAL_LABEL = {
    'draft':     '작성 중',
    'submitted': '승인 대기',
    'confirmed': '확정',
    'returned':  '반려',
}

# ── C3 평가 양식 · 역량 평가 · 목표 정렬 ─────────────────────────────
PEER_PROMPT_COLS = ('strength', 'comment', 'improvement')   # 동료 서술 문항 1~3 → peer_reviews 컬럼


def cycle_form(cycle):
    """주기에 복사된 평가 양식. form_json 이 없는 기존 주기는 legacy=True (옛 산식·옛 문항 유지)."""
    raw = cycle['form_json'] if cycle is not None and 'form_json' in cycle.keys() else None
    try:
        f = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        f = None
    base = DEFAULT_REVIEW_FORM
    if not f:
        return {'legacy': True, 'form_id': None, 'name': '기존 산식',
                'goal_weight': 100, 'comp_weight': 0,
                'self_weight': None, 'peer_weight': None, 'mgr_weight': None,
                'competencies': [], 'peer_prompts': [dict(x) for x in base['peer_prompts']],
                'upward_questions': list(UPWARD_QUESTIONS)}
    out = {'legacy': False}
    for k in ('form_id', 'name', 'goal_weight', 'comp_weight', 'self_weight', 'peer_weight', 'mgr_weight'):
        out[k] = f.get(k, base.get(k))
    out['competencies'] = [c for c in (f.get('competencies') or []) if c.get('key') and c.get('name')]
    prompts = [dict(x) for x in (f.get('peer_prompts') or base['peer_prompts'])][:3]
    while len(prompts) < 3:
        prompts.append({'label': '', 'desc': ''})
    out['peer_prompts'] = prompts
    out['upward_questions'] = [q for q in (f.get('upward_questions') or []) if q][:5] or list(UPWARD_QUESTIONS)
    return out


def form_basis(form):
    """산식 한 줄 — 화면 하단 근거 표기용."""
    if form['legacy']:
        return '기존 산식 · 자기·동료·매니저 점수 단순 평균 · 역량 평가 없음'
    parts = [f"업적 {form['goal_weight']}%"]
    if form['comp_weight']:
        parts.append(f"역량 {form['comp_weight']}%")
    return (' + '.join(parts) + f" · 자기 {form['self_weight']} / 동료 {form['peer_weight']} / 매니저 {form['mgr_weight']}"
            + ' · 없는 평가자는 제외하고 남은 비중으로 환산')


def _default_review_form(db):
    return db.execute('SELECT * FROM review_forms ORDER BY is_default DESC, id LIMIT 1').fetchone()


def default_form_weights():
    """초기 설정·회사 설정의 업적/역량 비중 프리셋 표시용."""
    try:
        row = _default_review_form(get_db())
    except sqlite3.Error:
        row = None
    return {'goal': row['goal_weight'] if row else 70, 'comp': row['comp_weight'] if row else 30,
            'id': row['id'] if row else None, 'name': row['name'] if row else '기본 평가 양식'}


app.jinja_env.globals['default_form_weights'] = default_form_weights


def save_default_form_weights(db, f):
    """초기 설정·회사 설정 — 기본 평가 양식의 업적/역량 비중 (프리셋 100/0 · 70/30 · 50/50)."""
    if 'form_goal_weight' not in f:
        return
    try:
        gw = int(f.get('form_goal_weight'))
    except (TypeError, ValueError):
        return
    if gw not in (100, 70, 50):
        return
    row = _default_review_form(db)
    if row:
        db.execute('UPDATE review_forms SET goal_weight=?, comp_weight=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (gw, 100 - gw, row['id']))


def _parse_review_form(f):
    """양식 편집 POST → (data, error)."""
    import re as _re

    def _int(k):
        try:
            return int(f.get(k, ''))
        except (TypeError, ValueError):
            return None
    name = f.get('name', '').strip()[:60]
    gw, cw = _int('goal_weight'), _int('comp_weight')
    sw, pw, mw = _int('self_weight'), _int('peer_weight'), _int('mgr_weight')
    names, descs, keys = f.getlist('comp_name'), f.getlist('comp_desc'), f.getlist('comp_key')
    comps, used = [], set()
    for i, nm in enumerate(names):
        nm = nm.strip()
        if not nm:
            continue
        key = keys[i].strip() if i < len(keys) else ''
        if not _re.fullmatch(r'[a-z0-9_]{1,40}', key) or key in used:
            key = 'c' + uuid.uuid4().hex[:8]
        used.add(key)
        comps.append({'key': key, 'name': nm[:40], 'desc': (descs[i].strip() if i < len(descs) else '')[:200]})
    prompts = [{'label': f.get(f'prompt_label_{i}', '').strip()[:30],
                'desc': f.get(f'prompt_desc_{i}', '').strip()[:100]} for i in range(3)]
    questions = [q.strip()[:200] for q in f.getlist('upward_q') if q.strip()]
    data = {'name': name, 'goal_weight': gw, 'comp_weight': cw, 'self_weight': sw, 'peer_weight': pw,
            'mgr_weight': mw, 'competencies': comps, 'peer_prompts': prompts, 'upward_questions': questions[:5]}
    err = None
    if not name:
        err = '양식 이름을 입력해주세요.'
    elif None in (gw, cw, sw, pw, mw) or min(gw, cw, sw, pw, mw) < 0:
        err = '비중은 0 이상의 정수로 입력해주세요.'
    elif gw + cw != 100:
        err = f'업적·역량 비중의 합이 100이어야 합니다 (현재 {gw + cw}).'
    elif sw + pw + mw != 100:
        err = f'자기·동료·매니저 비중의 합이 100이어야 합니다 (현재 {sw + pw + mw}).'
    elif mw == 0:
        err = '매니저 평가 비중은 0보다 커야 합니다.'
    elif len(comps) > 10:
        err = '역량은 최대 10개까지 등록할 수 있습니다.'
    elif cw > 0 and not comps:
        err = '역량 비중이 있으면 역량을 1개 이상 등록해주세요.'
    elif not any(x['label'] for x in prompts):
        err = '동료 평가 서술 문항을 1개 이상 입력해주세요.'
    elif not questions:
        err = '상향 평가 문항을 1개 이상 입력해주세요.'
    elif len(questions) > 5:
        err = '상향 평가 문항은 최대 5개입니다.'
    return data, err


def _form_row_dict(row):
    return {'id': row['id'], 'name': row['name'], 'goal_weight': row['goal_weight'],
            'comp_weight': row['comp_weight'], 'self_weight': row['self_weight'],
            'peer_weight': row['peer_weight'], 'mgr_weight': row['mgr_weight'],
            'competencies': json.loads(row['competencies'] or '[]'),
            'peer_prompts': json.loads(row['peer_prompts'] or '[]'),
            'upward_questions': json.loads(row['upward_questions'] or '[]'),
            'is_default': row['is_default']}


@app.route('/performance/forms')
@admin_required
def review_forms():
    db = get_db()
    rows = db.execute(
        'SELECT f.*, (SELECT COUNT(*) FROM performance_cycles c WHERE c.form_id=f.id) AS used, '
        "(SELECT GROUP_CONCAT(c.name, ' · ') FROM performance_cycles c WHERE c.form_id=f.id) AS used_names "
        'FROM review_forms f ORDER BY f.is_default DESC, f.id'
    ).fetchall()
    forms = []
    for r in rows:
        d = _form_row_dict(r)
        d['used'], d['used_names'] = r['used'], r['used_names']
        forms.append(d)
    legacy = db.execute("SELECT name, stage FROM performance_cycles WHERE form_json IS NULL ORDER BY start_date DESC").fetchall()
    return render_template('performance/review_forms.html', forms=forms, legacy_cycles=legacy,
                           cycle_stage_label=CYCLE_STAGE_LABEL, active_page='review_forms')


@app.route('/performance/forms/new', methods=['POST'])
@admin_required
def review_form_new():
    db = get_db()
    src = _default_review_form(db)
    f = _form_row_dict(src) if src else dict(DEFAULT_REVIEW_FORM)
    cur = db.execute(
        'INSERT INTO review_forms (name, goal_weight, comp_weight, self_weight, peer_weight, mgr_weight, '
        'competencies, peer_prompts, upward_questions, is_default) VALUES (?,?,?,?,?,?,?,?,?,0)',
        (f"{f['name']} 사본", f['goal_weight'], f['comp_weight'], f['self_weight'], f['peer_weight'],
         f['mgr_weight'], json.dumps(f['competencies'], ensure_ascii=False),
         json.dumps(f['peer_prompts'], ensure_ascii=False), json.dumps(f['upward_questions'], ensure_ascii=False)))
    db.commit()
    log_audit('create', 'performance', cur.lastrowid, '평가 양식 생성 (기본 양식 복사)')
    return redirect(url_for('review_form_edit', form_id=cur.lastrowid))


@app.route('/performance/forms/<int:form_id>', methods=['GET', 'POST'])
@admin_required
def review_form_edit(form_id):
    db = get_db()
    row = db.execute('SELECT * FROM review_forms WHERE id=?', (form_id,)).fetchone()
    if not row:
        abort(404)
    form, error = _form_row_dict(row), None
    if request.method == 'POST':
        data, error = _parse_review_form(request.form)
        if error:
            data['id'], data['is_default'] = form_id, row['is_default']
            form = data
        else:
            db.execute(
                'UPDATE review_forms SET name=?, goal_weight=?, comp_weight=?, self_weight=?, peer_weight=?, '
                'mgr_weight=?, competencies=?, peer_prompts=?, upward_questions=?, updated_at=CURRENT_TIMESTAMP '
                'WHERE id=?',
                (data['name'], data['goal_weight'], data['comp_weight'], data['self_weight'], data['peer_weight'],
                 data['mgr_weight'], json.dumps(data['competencies'], ensure_ascii=False),
                 json.dumps(data['peer_prompts'], ensure_ascii=False),
                 json.dumps(data['upward_questions'], ensure_ascii=False), form_id))
            db.commit()
            log_audit('update', 'performance', form_id, f'평가 양식 수정: {data["name"]}')
            flash('평가 양식을 저장했습니다. 진행 중인 주기에는 자동 반영되지 않습니다.', 'success')
            return redirect(url_for('review_form_edit', form_id=form_id))
    used = db.execute('SELECT name, stage, status FROM performance_cycles WHERE form_id=? ORDER BY start_date DESC',
                      (form_id,)).fetchall()
    return render_template('performance/review_form_edit.html', form=form, error=error, used=used,
                           cycle_stage_label=CYCLE_STAGE_LABEL, active_page='review_forms')


@app.route('/performance/forms/<int:form_id>/default', methods=['POST'])
@admin_required
def review_form_default(form_id):
    db = get_db()
    row = db.execute('SELECT id, name FROM review_forms WHERE id=?', (form_id,)).fetchone()
    if not row:
        abort(404)
    db.execute('UPDATE review_forms SET is_default = CASE WHEN id=? THEN 1 ELSE 0 END', (form_id,))
    db.commit()
    log_audit('update', 'performance', form_id, f'기본 평가 양식 지정: {row["name"]}')
    flash(f'"{row["name"]}"을 기본 양식으로 지정했습니다. 새 주기를 만들 때 먼저 선택됩니다.', 'success')
    return redirect(url_for('review_forms'))


@app.route('/performance/forms/<int:form_id>/delete', methods=['POST'])
@admin_required
def review_form_delete(form_id):
    db = get_db()
    row = db.execute('SELECT * FROM review_forms WHERE id=?', (form_id,)).fetchone()
    if not row:
        abort(404)
    used = db.execute('SELECT COUNT(*) FROM performance_cycles WHERE form_id=?', (form_id,)).fetchone()[0]
    if row['is_default']:
        flash('기본 양식은 삭제할 수 없습니다. 다른 양식을 기본으로 지정한 뒤 삭제하세요.', 'error')
    elif used:
        flash(f'평가 주기 {used}개에서 사용한 양식은 삭제할 수 없습니다.', 'error')
    else:
        db.execute('DELETE FROM review_forms WHERE id=?', (form_id,))
        db.commit()
        log_audit('delete', 'performance', form_id, f'평가 양식 삭제: {row["name"]}')
        flash('평가 양식을 삭제했습니다.', 'success')
    return redirect(url_for('review_forms'))


@app.route('/performance/cycles/<int:cycle_id>/form', methods=['POST'])
@admin_required
def performance_cycle_form(cycle_id):
    """목표 수립·진행 단계 주기에 양식 (다시) 적용 — 평가가 시작되면 잠김."""
    db = get_db()
    cyc = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cyc:
        abort(404)
    frm = db.execute('SELECT * FROM review_forms WHERE id=?', (request.form.get('form_id', type=int),)).fetchone()
    if cyc['status'] != 'active' or cyc['stage'] not in ('goal', 'progress'):
        flash('평가 양식은 목표 수립·진행 단계에서만 바꿀 수 있습니다. 평가가 시작되면 잠깁니다.', 'error')
    elif not frm:
        flash('양식을 선택해주세요.', 'error')
    else:
        db.execute('UPDATE performance_cycles SET form_id=?, form_json=? WHERE id=?',
                   (frm['id'], review_form_snapshot(frm), cycle_id))
        db.commit()
        log_audit('update', 'performance', cycle_id, f'{cyc["name"]} 평가 양식 적용: {frm["name"]}')
        flash(f'"{cyc["name"]}"에 "{frm["name"]}" 양식을 적용했습니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/competencies/self', methods=['POST'])
@login_required
def performance_competency_self():
    db, uid = get_db(), session['user_id']
    cycle_id = request.form.get('cycle_id', type=int)
    cyc = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cyc:
        abort(404)
    form = cycle_form(cyc)
    if cyc['stage'] != 'review' or form['legacy'] or not form['competencies']:
        flash('역량 자기평가는 역량이 포함된 주기의 "평가 진행" 단계에서만 작성할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))
    saved = 0
    for comp in form['competencies']:
        try:
            score = int(request.form.get(f'cs_{comp["key"]}', ''))
        except (TypeError, ValueError):
            continue
        if not 1 <= score <= 5:
            continue
        comment = request.form.get(f'csc_{comp["key"]}', '').strip()[:1000] or None
        db.execute(
            'INSERT INTO competency_scores (cycle_id, user_id, comp_key, rater_type, rater_id, score, comment) '
            "VALUES (?, ?, ?, 'self', ?, ?, ?) "
            'ON CONFLICT(cycle_id, user_id, comp_key, rater_id) DO UPDATE SET score=excluded.score, '
            'comment=excluded.comment, updated_at=CURRENT_TIMESTAMP',
            (cycle_id, uid, comp['key'], uid, score, comment))
        saved += 1
    db.commit()
    if saved:
        flash(f'역량 자기평가 {saved}개를 저장했습니다.', 'success')
    else:
        flash('저장된 역량 평가가 없습니다. 점수를 선택해주세요.', 'error')
    return redirect(url_for('performance', cycle=cycle_id) + '#comp-self')


def _org_goal_ok(db, cycle_id, owner_dept, org_goal_id):
    """개인 목표가 연결할 수 있는 상위 목표인지 — 같은 주기의 전사 목표 또는 소속 부서 목표."""
    og = db.execute('SELECT * FROM org_goals WHERE id=? AND cycle_id=?', (org_goal_id, cycle_id)).fetchone()
    if not og:
        return False
    return og['scope'] == 'company' or (og['department_id'] and og['department_id'] == owner_dept)


def _align_options(db, cycle_id, dept_id):
    if not cycle_id:
        return []
    return db.execute(
        "SELECT o.id, o.title, o.scope, o.cycle_id, d.name AS dept_name FROM org_goals o "
        "LEFT JOIN departments d ON o.department_id=d.id "
        "WHERE o.cycle_id=? AND (o.scope='company' OR o.department_id=?) "
        "ORDER BY o.scope='dept', o.id", (cycle_id, int(dept_id or 0))
    ).fetchall()


@app.route('/performance/goals/<int:goal_id>/align', methods=['POST'])
@login_required
def performance_goal_align(goal_id):
    db, uid, role = get_db(), session['user_id'], session['user_role']
    g = db.execute(
        'SELECT g.*, u.department_id AS owner_dept, u.manager_id AS owner_mgr, c.status AS cyc_status, c.stage AS cyc_stage '
        'FROM performance_goals g JOIN users u ON g.user_id=u.id JOIN performance_cycles c ON g.cycle_id=c.id '
        'WHERE g.id=?', (goal_id,)).fetchone()
    if not g:
        abort(404)
    if g['user_id'] != uid and role != 'admin':
        if role != 'manager' or (g['owner_mgr'] != uid and g['owner_dept'] != int(session.get('dept_id') or 0)):
            abort(403)
    back = _safe_next(url_for('performance', cycle=g['cycle_id']))
    if g['cyc_status'] != 'active' or g['cyc_stage'] not in ('goal', 'progress'):
        flash('목표 정렬은 목표 수립·진행 단계에서만 바꿀 수 있습니다.', 'error')
        return redirect(back)
    target = request.form.get('aligned_goal_id', type=int)
    if target and not _org_goal_ok(db, g['cycle_id'], g['owner_dept'], target):
        flash('같은 주기의 전사 목표 또는 소속 부서 목표에만 연결할 수 있습니다.', 'error')
        return redirect(back)
    db.execute('UPDATE performance_goals SET aligned_goal_id=? WHERE id=?', (target or None, goal_id))
    db.commit()
    flash('상위 목표 연결을 저장했습니다.' if target else '상위 목표 연결을 해제했습니다.', 'success')
    return redirect(back)


@app.route('/performance/alignment')
@login_required
def goal_alignment():
    db, uid, role = get_db(), session['user_id'], session['user_role']
    my_dept = int(session.get('dept_id') or 0)
    cycles = db.execute('SELECT * FROM performance_cycles ORDER BY start_date DESC').fetchall()
    active = next((c for c in cycles if c['status'] == 'active'), None)
    sel_id = request.args.get('cycle', type=int)
    cycle = next((c for c in cycles if c['id'] == sel_id), active) if sel_id else active
    if not cycle and cycles:
        cycle = cycles[0]
    cid = cycle['id'] if cycle else 0

    org = db.execute(
        'SELECT o.*, d.name AS dept_name, ow.name AS owner_name FROM org_goals o '
        'LEFT JOIN departments d ON o.department_id=d.id LEFT JOIN users ow ON o.owner_id=ow.id '
        'WHERE o.cycle_id=? ORDER BY d.name, o.id', (cid,)).fetchall()
    personal = db.execute(
        'SELECT g.id, g.title, g.progress, g.weight, g.approval_status, g.aligned_goal_id, g.user_id, '
        'u.name AS user_name, u.department_id, u.manager_id, d.name AS dept_name '
        'FROM performance_goals g JOIN users u ON g.user_id=u.id LEFT JOIN departments d ON u.department_id=d.id '
        'WHERE g.cycle_id=? ORDER BY u.name, g.id', (cid,)).fetchall()

    def visible(pg):
        if role == 'admin' or pg['user_id'] == uid:
            return True
        return role == 'manager' and (pg['manager_id'] == uid or (my_dept and pg['department_id'] == my_dept))

    kids = {}
    for pg in personal:
        if pg['aligned_goal_id']:
            kids.setdefault(pg['aligned_goal_id'], []).append(pg)
    org_ids = {o['id'] for o in org}

    def node(o):
        own = kids.get(o['id'], [])
        vals = [pg['progress'] or 0 for pg in own]
        subs = [node(s) for s in org if s['parent_id'] == o['id']]
        vals += [s['rollup'] for s in subs if s['rollup'] is not None]
        return {'goal': o, 'subs': subs, 'aligned_n': len(own),
                'people': [pg for pg in own if visible(pg)],
                'rollup': round(sum(vals) / len(vals)) if vals else None,
                'can_edit': cycle['status'] == 'active' and (
                    role == 'admin' or (role == 'manager' and o['created_by'] == uid))}
    company = [node(o) for o in org if o['scope'] == 'company']
    loose = [node(o) for o in org if o['scope'] == 'dept' and (not o['parent_id'] or o['parent_id'] not in org_ids)]

    unaligned = {}
    for pg in personal:
        if not pg['aligned_goal_id'] or pg['aligned_goal_id'] not in org_ids:
            u = unaligned.setdefault(pg['department_id'] or 0, {'dept_name': pg['dept_name'] or '부서 미지정', 'n': 0, 'goals': []})
            u['n'] += 1
            if visible(pg) and role in ('admin', 'manager'):
                u['goals'].append(pg)
    unaligned_list = sorted(unaligned.items(), key=lambda kv: (kv[0] != my_dept, kv[1]['dept_name']))
    total = len(personal)
    aligned_total = sum(1 for pg in personal if pg['aligned_goal_id'] in org_ids)

    depts = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall() if role == 'admin' else \
        db.execute('SELECT id, name FROM departments WHERE id=?', (my_dept,)).fetchall()
    align_opts = {}
    for o in org:
        align_opts.setdefault('company' if o['scope'] == 'company' else o['department_id'], []).append(o)
    return render_template('performance/alignment.html', cycles=cycles, cycle=cycle,
                           company=company, loose=loose, unaligned=unaligned_list,
                           total=total, aligned_total=aligned_total,
                           company_goals=[o for o in org if o['scope'] == 'company'],
                           depts=depts, align_opts=align_opts, my_dept=my_dept,
                           can_create=bool(cycle) and cycle['status'] == 'active' and (
                               role == 'admin' or (role == 'manager' and my_dept)),
                           goal_approval_label=GOAL_APPROVAL_LABEL,
                           active_page='goal_alignment')


def _org_goal_form_check(db, cycle, role, my_dept, scope, dept_id, parent_id):
    if not cycle or cycle['status'] != 'active':
        return '종료된 주기에는 목표를 추가·수정할 수 없습니다.'
    if scope not in ('company', 'dept'):
        return '구분을 선택해주세요.'
    if role != 'admin':
        if scope != 'dept' or dept_id != my_dept:
            return '매니저는 소속 부서 목표만 만들 수 있습니다.'
    if scope == 'dept' and not db.execute('SELECT 1 FROM departments WHERE id=?', (dept_id,)).fetchone():
        return '부서를 선택해주세요.'
    if parent_id:
        par = db.execute("SELECT 1 FROM org_goals WHERE id=? AND cycle_id=? AND scope='company'",
                         (parent_id, cycle['id'])).fetchone()
        if not par or scope != 'dept':
            return '부서 목표만 같은 주기의 전사 목표에 연결할 수 있습니다.'
    return None


@app.route('/performance/alignment/goals', methods=['POST'])
@manager_or_admin
def org_goal_new():
    db, uid, role = get_db(), session['user_id'], session['user_role']
    my_dept = int(session.get('dept_id') or 0)
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (request.form.get('cycle_id', type=int),)).fetchone()
    scope = request.form.get('scope', 'dept')
    dept_id = request.form.get('department_id', type=int) if scope == 'dept' else None
    parent_id = request.form.get('parent_id', type=int) or None
    title = request.form.get('title', '').strip()[:120]
    desc = request.form.get('description', '').strip()[:1000] or None
    if not cycle:
        abort(404)
    if role != 'admin' and (scope != 'dept' or dept_id != my_dept):
        abort(403)
    err = _org_goal_form_check(db, cycle, role, my_dept, scope, dept_id, parent_id) or (None if title else '목표명을 입력해주세요.')
    if err:
        flash(err, 'error')
        return redirect(url_for('goal_alignment', cycle=cycle['id']))
    cur = db.execute(
        'INSERT INTO org_goals (cycle_id, scope, department_id, parent_id, title, description, owner_id, created_by) '
        'VALUES (?,?,?,?,?,?,?,?)', (cycle['id'], scope, dept_id, parent_id, title, desc, uid, uid))
    db.commit()
    log_audit('create', 'performance', cur.lastrowid, f'{"전사" if scope == "company" else "부서"} 목표 등록: {title}')
    flash('목표를 등록했습니다.', 'success')
    return redirect(url_for('goal_alignment', cycle=cycle['id']))


@app.route('/performance/alignment/goals/<int:og_id>', methods=['POST'])
@manager_or_admin
def org_goal_edit(og_id):
    db, uid, role = get_db(), session['user_id'], session['user_role']
    og = db.execute('SELECT * FROM org_goals WHERE id=?', (og_id,)).fetchone()
    if not og:
        abort(404)
    if role != 'admin' and og['created_by'] != uid:
        abort(403)
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (og['cycle_id'],)).fetchone()
    back = url_for('goal_alignment', cycle=og['cycle_id'])
    if cycle['status'] != 'active':
        flash('종료된 주기에는 목표를 추가·수정할 수 없습니다.', 'error')
        return redirect(back)
    if request.form.get('action') == 'delete':
        db.execute('UPDATE org_goals SET parent_id=NULL WHERE parent_id=?', (og_id,))
        db.execute('UPDATE performance_goals SET aligned_goal_id=NULL WHERE aligned_goal_id=?', (og_id,))
        db.execute('DELETE FROM org_goals WHERE id=?', (og_id,))
        db.commit()
        log_audit('delete', 'performance', og_id, f'조직 목표 삭제: {og["title"]}')
        flash('목표를 삭제했습니다. 연결된 하위 목표는 연결이 해제됩니다.', 'success')
        return redirect(back)
    title = request.form.get('title', '').strip()[:120]
    desc = request.form.get('description', '').strip()[:1000] or None
    parent_id = request.form.get('parent_id', type=int) or None
    if parent_id == og_id:
        parent_id = None
    err = _org_goal_form_check(db, cycle, role, int(session.get('dept_id') or 0), og['scope'],
                               og['department_id'], parent_id) or (None if title else '목표명을 입력해주세요.')
    if err:
        flash(err, 'error')
        return redirect(back)
    db.execute('UPDATE org_goals SET title=?, description=?, parent_id=? WHERE id=?', (title, desc, parent_id, og_id))
    db.commit()
    flash('목표를 수정했습니다.', 'success')
    return redirect(back)


def _c3_member_context(db, emp_uid, cycle, viewer_uid):
    form = cycle_form(cycle)
    rows = db.execute('SELECT comp_key, rater_type, rater_id, score, comment FROM competency_scores '
                      'WHERE cycle_id=? AND user_id=?', (cycle['id'], emp_uid)).fetchall()
    return {'form': form, 'basis': form_basis(form),
            'comp_self': {r['comp_key']: r for r in rows if r['rater_type'] == 'self'},
            'comp_mine': {r['comp_key']: r for r in rows if r['rater_type'] == 'manager' and r['rater_id'] == viewer_uid},
            'org_goals': {r['id']: r for r in db.execute('SELECT id, title, scope FROM org_goals WHERE cycle_id=?',
                                                         (cycle['id'],)).fetchall()}}


@app.route('/performance')
@login_required
def performance():
    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']

    cycles = db.execute(
        "SELECT * FROM performance_cycles ORDER BY start_date DESC"
    ).fetchall()
    active_cycle = next((c for c in cycles if c['status'] == 'active'), None)

    # URL ?cycle= 파라미터로 주기 선택 (정수 변환으로 안전하게 처리)
    try:
        selected_cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        selected_cycle_id = 0
    if selected_cycle_id:
        selected_cycle = next((c for c in cycles if c['id'] == selected_cycle_id), active_cycle)
    else:
        selected_cycle = active_cycle

    cycle_id = selected_cycle['id'] if selected_cycle else 0

    if role in ('admin', 'manager'):
        # manager는 자기 부서 팀원만 조회 (r2 = 현재 로그인 사용자가 남긴 평가)
        mgr_dept = int(session.get('dept_id') or 0)
        base_sql = (
            'SELECT g.*, u.id AS user_id, u.name AS user_name, u.manager_id AS emp_manager_id, '
            'd.name AS dept_name, '
            'AVG(r.score) AS avg_score, COUNT(DISTINCT r.id) AS review_count, '
            'COUNT(DISTINCT r2.id) AS my_review_count '
            'FROM performance_goals g '
            'JOIN users u ON g.user_id = u.id '
            'LEFT JOIN departments d ON u.department_id = d.id '
            'LEFT JOIN performance_reviews r ON g.id = r.goal_id '
            'LEFT JOIN performance_reviews r2 ON g.id = r2.goal_id AND r2.reviewer_id = ? '
        )
        if role == 'manager' and mgr_dept:
            goals = db.execute(
                base_sql +
                'WHERE g.cycle_id = ? AND u.department_id = ? '
                'GROUP BY g.id ORDER BY u.name, g.created_at',
                (uid, cycle_id, mgr_dept)
            ).fetchall()
        else:
            goals = db.execute(
                base_sql +
                'WHERE g.cycle_id = ? '
                'GROUP BY g.id ORDER BY u.name, g.created_at',
                (uid, cycle_id)
            ).fetchall()
    else:
        goals = db.execute(
            'SELECT g.*, AVG(r.score) AS avg_score, COUNT(r.id) AS review_count '
            'FROM performance_goals g '
            'LEFT JOIN performance_reviews r ON g.id = r.goal_id '
            'WHERE g.user_id=? AND g.cycle_id=? '
            'GROUP BY g.id ORDER BY g.created_at',
            (uid, cycle_id)
        ).fetchall()

    # ── 주기 단계 (상태머신) ──────────────────────────────
    stage = selected_cycle['stage'] if selected_cycle else None
    include_peer = bool(selected_cycle['include_peer']) if selected_cycle else False

    # 단계 마감일 + D-day (R1-D)
    stage_deadline = None
    stage_dday     = None
    if selected_cycle:
        keys = selected_cycle.keys()
        if stage == 'goal' and 'goal_deadline' in keys:
            stage_deadline = selected_cycle['goal_deadline']
        elif stage == 'review' and 'review_deadline' in keys:
            stage_deadline = selected_cycle['review_deadline']
        if stage_deadline:
            try:
                stage_dday = (date.fromisoformat(stage_deadline) - date.today()).days
            except (ValueError, TypeError):
                stage_dday = None

    # ── 직원 전용 추가 데이터 ─────────────────────────────
    peer_assignments_mine = []   # 내가 써야 할 피어리뷰
    calibration_result    = None # 내 캘리브레이션 결과
    todo_items            = []   # 지금 해야 할 일
    my_goal_state         = None # 내 목표 세트 상태 (제출/확정 워크플로우)
    my_appeal             = None # 내 이의신청
    can_appeal            = False

    if role == 'employee' and cycle_id:
        # 피어리뷰 배정 + 완료 여부 (주기에 다면평가가 포함된 경우만)
        if include_peer:
            peer_assignments_mine = db.execute(
                'SELECT pa.reviewee_id, pa.cycle_id, u.name AS reviewee_name, '
                '       pr.id AS done_id '
                'FROM peer_assignments pa '
                'JOIN users u ON pa.reviewee_id = u.id '
                'LEFT JOIN peer_reviews pr '
                '  ON pr.cycle_id=pa.cycle_id AND pr.reviewee_id=pa.reviewee_id '
                '  AND pr.reviewer_id=pa.reviewer_id AND pr.review_type=\'peer\' '
                'WHERE pa.cycle_id=? AND pa.reviewer_id=?',
                (cycle_id, uid)
            ).fetchall()

        # 캘리브레이션 결과 (공개된 것만)
        calibration_result = db.execute(
            'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=? AND is_shared=1',
            (cycle_id, uid)
        ).fetchone()

        # 내 목표 세트 상태
        weight_sum = sum(g['weight'] for g in goals)
        statuses = {g['approval_status'] for g in goals}
        if not goals:
            set_status = 'none'
        elif 'returned' in statuses:
            set_status = 'returned'
        elif statuses == {'confirmed'}:
            set_status = 'confirmed'
        elif 'submitted' in statuses and statuses <= {'submitted', 'confirmed'}:
            set_status = 'submitted'
        else:
            set_status = 'draft'
        return_comments = [g['return_comment'] for g in goals if g['return_comment']]
        my_goal_state = {
            'status': set_status,
            'weight_sum': weight_sum,
            'count': len(goals),
            'can_submit': (stage == 'goal' and set_status in ('draft', 'returned')
                           and 3 <= len(goals) <= 5 and weight_sum == 100),
            'return_comment': return_comments[0] if return_comments else None,
        }

        # 이의신청 상태
        my_appeal = db.execute(
            'SELECT * FROM grade_appeals WHERE cycle_id=? AND user_id=?',
            (cycle_id, uid)
        ).fetchone()
        if (stage == 'appeal' and calibration_result and not my_appeal
                and selected_cycle['appeal_until']
                and str(date.today()) <= selected_cycle['appeal_until']):
            can_appeal = True

        # To-Do 계산 (단계별)
        if stage == 'goal':
            if not goals:
                todo_items.append({
                    'icon': 'fa-plus', 'color': '#1d4ed8',
                    'text': '이번 주기 목표를 등록하세요 (3~5개, 가중치 합 100%)',
                    'url': url_for('performance_goal_new')
                })
            elif my_goal_state['can_submit']:
                todo_items.append({
                    'icon': 'fa-paper-plane', 'color': '#1d4ed8',
                    'text': '목표 작성 완료 — 팀장 승인을 요청하세요',
                    'url': url_for('performance') + '?tab=goals'
                })
            elif set_status == 'returned':
                todo_items.append({
                    'icon': 'fa-rotate-left', 'color': '#dc2626',
                    'text': '목표가 반려되었습니다 — 수정 후 다시 제출하세요',
                    'url': url_for('performance') + '?tab=goals'
                })
        elif stage == 'review':
            goals_no_self = [g for g in goals if not g['self_score']]
            if goals_no_self:
                todo_items.append({
                    'icon': 'fa-pen', 'color': '#dc2626',
                    'text': f'자기평가 미완료 목표 {len(goals_no_self)}개',
                    'url': url_for('performance_self_review', goal_id=goals_no_self[0]['id'])
                })
            peer_undone = [p for p in peer_assignments_mine if not p['done_id']]
            if peer_undone:
                todo_items.append({
                    'icon': 'fa-star', 'color': '#d97706',
                    'text': f'작성 대기 중인 동료 평가 {len(peer_undone)}명',
                    'url': url_for('peer_reviews_page')
                })
        elif stage == 'appeal' and can_appeal:
            todo_items.append({
                'icon': 'fa-gavel', 'color': '#7c3aed',
                'text': f'평가 결과가 공개되었습니다 — 이의신청 가능 기간: {selected_cycle["appeal_until"]}까지',
                'url': url_for('performance') + '?tab=result'
            })

    # ── 매니저/관리자: 사람 단위 팀 현황 (R1-A) ──────────────
    team_rows       = []
    team_summary    = {}
    if role in ('admin', 'manager') and cycle_id:
        # 등급 조회 (HR 조정 단계 이후 표시용)
        grade_rows = db.execute(
            'SELECT user_id, final_grade, is_shared FROM calibration_results WHERE cycle_id=?',
            (cycle_id,)
        ).fetchall()
        grades = {r['user_id']: r for r in grade_rows}

        by_user = {}
        for g in goals:
            by_user.setdefault(g['user_id'], []).append(g)

        for emp_uid, glist in by_user.items():
            first = glist[0]
            confirmed = [g for g in glist if g['approval_status'] == 'confirmed']
            submitted = sum(1 for g in glist if g['approval_status'] == 'submitted')
            draft     = sum(1 for g in glist if g['approval_status'] == 'draft')
            returned  = sum(1 for g in glist if g['approval_status'] == 'returned')
            weight_sum   = sum(g['weight'] for g in glist)
            avg_progress = round(sum((g['progress'] or 0) for g in glist) / len(glist)) if glist else 0
            self_done    = sum(1 for g in confirmed if g['self_score'])
            my_reviewed  = sum(1 for g in confirmed if g['my_review_count'])
            gr = grades.get(emp_uid)

            if stage == 'goal':
                incomplete = bool(submitted or draft or returned or not glist)
            elif stage == 'review':
                incomplete = bool(confirmed) and (self_done < len(confirmed) or my_reviewed < len(confirmed))
            else:
                incomplete = False

            team_rows.append({
                'user_id': emp_uid, 'name': first['user_name'],
                'dept_name': first['dept_name'], 'manager_id': first['emp_manager_id'],
                'goal_count': len(glist), 'weight_sum': weight_sum,
                'confirmed': len(confirmed), 'submitted': submitted,
                'draft': draft, 'returned': returned,
                'avg_progress': avg_progress,
                'self_done': self_done, 'self_total': len(confirmed),
                'my_reviewed': my_reviewed,
                'grade': (gr['final_grade'] if gr and (role == 'admin' or gr['is_shared']) else None),
                'incomplete': incomplete,
            })

        # 목표 미작성 팀원도 표시 (goal 단계 처리 대상)
        if role == 'manager' and mgr_dept:
            no_goal_emps = db.execute(
                'SELECT u.id, u.name, u.manager_id, d.name AS dept_name FROM users u '
                'LEFT JOIN departments d ON u.department_id=d.id '
                "WHERE u.status='active' AND u.role NOT IN ('guest') AND u.department_id=? "
                'ORDER BY u.name', (mgr_dept,)
            ).fetchall()
        else:
            no_goal_emps = db.execute(
                'SELECT u.id, u.name, u.manager_id, d.name AS dept_name FROM users u '
                'LEFT JOIN departments d ON u.department_id=d.id '
                "WHERE u.status='active' AND u.role NOT IN ('guest') "
                'ORDER BY u.name'
            ).fetchall()
        for e in no_goal_emps:
            if e['id'] in by_user or e['id'] == uid:
                continue
            team_rows.append({
                'user_id': e['id'], 'name': e['name'],
                'dept_name': e['dept_name'], 'manager_id': e['manager_id'],
                'goal_count': 0, 'weight_sum': 0,
                'confirmed': 0, 'submitted': 0, 'draft': 0, 'returned': 0,
                'avg_progress': 0, 'self_done': 0, 'self_total': 0, 'my_reviewed': 0,
                'grade': None,
                'incomplete': (stage == 'goal'),
            })

        team_rows.sort(key=lambda r: r['name'])
        team_summary = {
            'total': len(team_rows),
            'goal_pending':  sum(1 for r in team_rows if r['submitted']),
            'goal_drafting': sum(1 for r in team_rows if r['draft'] or r['returned'] or not r['goal_count']),
            'goal_done':     sum(1 for r in team_rows if r['goal_count'] and not (r['submitted'] or r['draft'] or r['returned'])),
            'self_missing':  sum(1 for r in team_rows if r['self_total'] and r['self_done'] < r['self_total']),
            'review_pending': sum(1 for r in team_rows if r['self_total'] and r['my_reviewed'] < r['self_total']),
            'review_done':   sum(1 for r in team_rows if r['self_total'] and r['my_reviewed'] >= r['self_total']),
            'avg_progress':  round(sum(r['avg_progress'] for r in team_rows) / len(team_rows)) if team_rows else 0,
        }

    # ── C3 평가 양식 · 역량 자기평가 · 목표 정렬 ──
    form = cycle_form(selected_cycle) if selected_cycle else None
    my_comp = {}
    if form and not form['legacy'] and cycle_id and role == 'employee':
        my_comp = {r['comp_key']: r for r in db.execute(
            "SELECT comp_key, score, comment FROM competency_scores "
            "WHERE cycle_id=? AND user_id=? AND rater_type='self'", (cycle_id, uid)).fetchall()}
        missing = [c for c in form['competencies'] if c['key'] not in my_comp]
        if stage == 'review' and missing:
            todo_items.append({'icon': 'fa-list-check', 'color': '#dc2626',
                               'text': f'역량 자기평가 미완료 {len(missing)}개',
                               'url': url_for('performance', cycle=cycle_id) + '#comp-self'})
    org_goal_map = {r['id']: r for r in db.execute(
        'SELECT id, title, scope FROM org_goals WHERE cycle_id=?', (cycle_id,)).fetchall()} if cycle_id else {}

    return render_template('performance/index.html',
                           cycles=cycles, active_cycle=active_cycle,
                           selected_cycle=selected_cycle,
                           stage=stage, include_peer=include_peer,
                           cycle_stages=CYCLE_STAGES,
                           cycle_stage_label=CYCLE_STAGE_LABEL,
                           cycle_stage_desc=CYCLE_STAGE_DESC,
                           goal_approval_label=GOAL_APPROVAL_LABEL,
                           goals=goals, score_labels=SCORE_LABELS,
                           peer_assignments_mine=peer_assignments_mine,
                           calibration_result=calibration_result,
                           todo_items=todo_items,
                           my_goal_state=my_goal_state,
                           my_appeal=my_appeal, can_appeal=can_appeal,
                           team_rows=team_rows, team_summary=team_summary,
                           stage_deadline=stage_deadline, stage_dday=stage_dday,
                           grade_rewards=_grade_rewards(get_db()),
                           goal_checkin=_latest_checkins(db, [g['id'] for g in goals]),
                           ck_label=CHECKIN_STATUS_LABEL, ck_cls=CHECKIN_STATUS_CLS,
                           form=form, form_basis=form_basis(form) if form else '', my_comp=my_comp,
                           org_goal_map=org_goal_map,
                           align_options=_align_options(db, cycle_id, session.get('dept_id')),
                           active_page='performance')

# ── C5 Copilot 평가 보조 ─────────────────────────────────────────
def copilot_view():
    """템플릿용 Copilot 상태 — 회사 설정 on/off + 제공자·모델·연결 여부."""
    on = get_company_config().get('copilot_enabled')
    return {**copilot.status(), 'on': True if on is None else bool(int(on))}


def copilot_api(f):
    """Copilot JSON 엔드포인트 — 로그인·회사 설정 확인. 저장이 아닌 조회성 요청이라 체험 모드에서도 허용."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return {'error': '로그인이 필요합니다.'}, 401
        if not copilot_view()['on']:
            return {'error': 'Copilot이 꺼져 있습니다. 회사 설정에서 켤 수 있습니다.'}, 403
        return f(*args, **kwargs)
    return decorated


def _copilot_log(db, feature, target_uid, source, warn_count=0):
    if session.get('demo_mode'):
        return
    db.execute('INSERT INTO copilot_logs (user_id, feature, target_user_id, source, warn_count) VALUES (?,?,?,?,?)',
               (session['user_id'], feature, target_uid, source, warn_count))
    db.commit()


def _copilot_usage(db, days=30):
    rows = db.execute("SELECT feature, COUNT(*) n, SUM(warn_count) w FROM copilot_logs "
                      "WHERE created_at >= datetime('now', ?) GROUP BY feature", (f'-{days} days',)).fetchall()
    return {r['feature']: {'n': r['n'], 'w': r['w'] or 0} for r in rows}


def _copilot_cycle(db, emp_uid):
    """팀원 평가 Copilot 공통 — 권한(매니저·관리자 + 팀원 소유권)과 주기."""
    if session.get('user_role') not in ('admin', 'manager'):
        abort(403)
    _team_member_or_403(db, emp_uid)
    data = request.get_json(silent=True) or {}
    try:
        cycle_id = int(data.get('cycle_id') or 0)
    except (TypeError, ValueError):
        cycle_id = 0
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    return cycle, data


def _copilot_evidence(db, emp_uid, cycle):
    """평가 초안 근거 — 평가자가 화면에서 볼 수 있는 범위만, 이름 없이."""
    uid, role = session['user_id'], session['user_role']
    start = (cycle['start_date'] or '0000-01-01')[:10]
    end = (cycle['end_date'] or '9999-12-31')[:10] + ' 23:59:59'
    goals = db.execute("SELECT id, title, progress, self_comment FROM performance_goals "
                       "WHERE cycle_id=? AND user_id=? AND approval_status='confirmed' ORDER BY created_at",
                       (cycle['id'], emp_uid)).fetchall()
    cks = _latest_checkins(db, [g['id'] for g in goals])
    form = cycle_form(cycle)
    self_comp = {r['comp_key']: r['comment'] for r in db.execute(
        "SELECT comp_key, comment FROM competency_scores WHERE cycle_id=? AND user_id=? AND rater_type='self'",
        (cycle['id'], emp_uid)).fetchall()}
    pc = get_perf_culture()
    vis_sql, vis_args = _fb_visible(uid, role, session.get('dept_id'), pc['public_praise'])
    fb = db.execute(FB_SELECT + f"WHERE f.to_id=? AND f.created_at BETWEEN ? AND ? AND {vis_sql} "
                    "ORDER BY f.id DESC LIMIT 30", [emp_uid, start, end] + vis_args).fetchall()
    peer = db.execute("SELECT strength, comment, improvement FROM peer_reviews "
                      "WHERE cycle_id=? AND reviewee_id=? AND review_type='peer'", (cycle['id'], emp_uid)).fetchall()
    open_actions = db.execute("SELECT COUNT(*) FROM one_on_one_actions a JOIN one_on_ones o ON a.meeting_id=o.id "
                              "WHERE o.employee_id=? AND a.status='open'", (emp_uid,)).fetchone()[0]

    def _ck(g):
        c = cks.get(g['id'])
        return {'status': c['status'], 'progress': c['progress'], 'comment': c['comment'] or '',
                'date': c['created_at']} if c else None
    return {
        'goals': [{'id': g['id'], 'title': g['title'], 'progress': g['progress'] or 0,
                   'self_comment': g['self_comment'] or '', 'checkin': _ck(g)} for g in goals],
        'competencies': [{'key': c['key'], 'name': c['name'], 'desc': c.get('desc', ''),
                          'self_comment': self_comp.get(c['key']) or ''} for c in form['competencies']],
        'feedback': [{'kind': f['kind'], 'content': f['content']} for f in fb],
        'peer': [dict(p) for p in peer] if len(peer) >= 3 else [],
        'peer_hidden': len(peer) if 0 < len(peer) < 3 else 0,
        'peer_labels': [p.get('label') or '' for p in form['peer_prompts']],
        'open_actions': open_actions,
    }


@app.route('/performance/copilot/<int:emp_uid>/draft', methods=['POST'])
@copilot_api
def copilot_review_draft(emp_uid):
    db = get_db()
    cycle, _ = _copilot_cycle(db, emp_uid)
    res = copilot.review_draft(_copilot_evidence(db, emp_uid, cycle))
    _copilot_log(db, 'review_draft', emp_uid, res['source'])
    return res


@app.route('/performance/copilot/<int:emp_uid>/check', methods=['POST'])
@copilot_api
def copilot_bias_check(emp_uid):
    db = get_db()
    cycle, data = _copilot_cycle(db, emp_uid)
    items = []
    for it in (data.get('items') or [])[:60]:
        if not isinstance(it, dict):
            continue
        try:
            score = int(it.get('score'))
        except (TypeError, ValueError):
            score = None
        items.append({'field': str(it.get('field') or '')[:40], 'label': str(it.get('label') or '')[:80],
                      'text': str(it.get('text') or '')[:2000], 'score': score if score in (1, 2, 3, 4, 5) else None})
    res = copilot.bias_check(items)
    rows = db.execute("SELECT u.gender, AVG(r.score) AS avg FROM performance_reviews r "
                      "JOIN performance_goals g ON r.goal_id=g.id JOIN users u ON g.user_id=u.id "
                      "WHERE r.reviewer_id=? AND g.cycle_id=? GROUP BY g.user_id",
                      (session['user_id'], cycle['id'])).fetchall()
    res['pattern'] = copilot.rating_pattern([dict(r) for r in rows])
    _copilot_log(db, 'bias_check', emp_uid, res['source'], len(res['warnings']) + (1 if res['pattern']['warn'] else 0))
    return res


@app.route('/payroll/acr/<int:cycle_id>/copilot/<int:emp_id>', methods=['POST'])
@copilot_api
def copilot_raise_note(cycle_id, emp_id):
    from payroll_utils import calc_compa_ratio
    role, uid = session.get('user_role'), session['user_id']
    if role not in ('admin', 'manager'):
        abort(403)
    db = get_db()
    _acr_cycle_or_404(db, cycle_id)
    rows = _acr_rows(db, cycle_id, ' AND cr.employee_id=?' + ('' if role == 'admin' else ' AND cr.manager_id=?'),
                     (emp_id,) if role == 'admin' else (emp_id, uid))
    if not rows:
        abort(403)
    r = rows[0]
    data = request.get_json(silent=True) or {}
    pct, _ = _acr_final(r)
    try:
        if data.get('pct') not in (None, ''):
            pct = float(data['pct'])
    except (TypeError, ValueError):
        pass
    new_salary = _acr_new_salary(r['current_salary'], pct)
    band = db.execute('SELECT sg.min_salary, sg.max_salary FROM salary_grades sg JOIN users u '
                      'ON sg.position_id=u.position_id AND sg.job_family_id=u.job_family_id WHERE u.id=? LIMIT 1',
                      (emp_id,)).fetchone()
    res = copilot.raise_note({
        'grade': r['perf_grade'], 'pct': pct, 'suggested_pct': r['suggested_pct'],
        'compa': calc_compa_ratio(r['current_salary'], r['mid_salary']),
        'compa_new': calc_compa_ratio(new_salary, r['mid_salary']), 'new_salary': new_salary,
        'band_min': band['min_salary'] if band else None, 'band_max': band['max_salary'] if band else None,
        'note': str(data.get('note') or '')[:500]})
    _copilot_log(db, 'raise_note', emp_id, res['source'], len(res['warnings']))
    return res


@app.route('/performance/goals/ai-assist', methods=['POST'])
@copilot_api
def performance_goal_ai_assist():
    """Copilot — 목표 SMART 분석. Claude 연결 시 개선안, 아니면 규칙 기반."""
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()[:300]
    if not title:
        return {'error': '목표 제목을 먼저 입력해주세요.'}, 400
    res = copilot.goal_assist(title, data.get('category') or 'KPI', (data.get('job') or '')[:60])
    _copilot_log(get_db(), 'goal_assist', None, res['source'])
    return res


@app.route('/performance/goals/new', methods=['GET', 'POST'])
@login_required
def performance_goal_new():
    db   = get_db()
    uid  = session['user_id']
    # 목표 등록은 '목표 수립' 단계인 진행 중 주기에서만 가능
    cycles = db.execute(
        "SELECT * FROM performance_cycles WHERE status='active' AND stage='goal' "
        "ORDER BY start_date DESC"
    ).fetchall()
    error = None

    if request.method == 'POST':
        cycle_id = request.form.get('cycle_id')
        category = request.form.get('category', 'KPI')
        title    = request.form.get('title', '').strip()
        desc     = request.form.get('description', '').strip() or None
        weight   = int(request.form.get('weight', 100))

        cycle = next((c for c in cycles if str(c['id']) == str(cycle_id)), None)
        my_status = db.execute(
            "SELECT DISTINCT approval_status FROM performance_goals WHERE cycle_id=? AND user_id=?",
            (cycle_id, uid)
        ).fetchall() if cycle_id else []
        statuses = {r['approval_status'] for r in my_status}

        if not cycle_id or not title:
            error = '평가 주기와 목표명은 필수입니다.'
        elif not cycle:
            error = '목표 수립 단계인 평가 주기에서만 목표를 등록할 수 있습니다.'
        elif 'submitted' in statuses:
            error = '목표를 이미 제출했습니다. 팀장 승인(또는 반려) 후에 수정할 수 있습니다.'
        elif 'confirmed' in statuses:
            error = '목표가 이미 확정되었습니다. 변경이 필요하면 팀장에게 문의하세요.'
        elif not (1 <= weight <= 100):
            error = '가중치는 1~100 사이여야 합니다.'
        else:
            count = db.execute(
                'SELECT COUNT(*) FROM performance_goals WHERE cycle_id=? AND user_id=?',
                (cycle_id, uid)
            ).fetchone()[0]
            if count >= 5:
                error = '목표는 최대 5개까지 등록할 수 있습니다.'
            else:
                aligned = request.form.get('aligned_goal_id', type=int)
                if aligned and not _org_goal_ok(db, cycle_id, int(session.get('dept_id') or 0), aligned):
                    aligned = None
                db.execute(
                    'INSERT INTO performance_goals (cycle_id, user_id, category, title, description, weight, approval_status, aligned_goal_id) '
                    "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
                    (cycle_id, uid, category, title, desc, weight, aligned)
                )
                db.commit()
                return redirect(url_for('performance'))

    # 주기별 내 현재 가중치 합계 (폼에서 잔여 가중치 안내용)
    my_weights = {}
    for c in cycles:
        my_weights[c['id']] = db.execute(
            'SELECT COALESCE(SUM(weight),0) FROM performance_goals WHERE cycle_id=? AND user_id=?',
            (c['id'], uid)
        ).fetchone()[0]

    goal_templates_list = db.execute(
        "SELECT id, title, description, category, weight FROM goal_templates WHERE is_active=1 ORDER BY category, title"
    ).fetchall()
    return render_template('performance/goal_form.html',
                           cycles=cycles, error=error,
                           my_weights=my_weights,
                           goal_templates=goal_templates_list,
                           align_options=[o for c in cycles for o in _align_options(db, c['id'], session.get('dept_id'))],
                           copilot=copilot_view(), active_page='performance')


@app.route('/performance/goals/submit', methods=['POST'])
@login_required
def performance_goals_submit():
    """목표 세트 제출 → 팀장 승인 요청 (3~5개, 가중치 합 100%)"""
    db  = get_db()
    uid = session['user_id']
    cycle_id = request.form.get('cycle_id', type=int)

    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    if cycle['stage'] != 'goal':
        flash('목표 수립 단계에서만 제출할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))

    goals = db.execute(
        'SELECT * FROM performance_goals WHERE cycle_id=? AND user_id=?',
        (cycle_id, uid)
    ).fetchall()
    weight_sum = sum(g['weight'] for g in goals)
    statuses = {g['approval_status'] for g in goals}

    if 'submitted' in statuses:
        flash('이미 제출된 목표입니다. 팀장 승인을 기다려 주세요.', 'error')
    elif statuses == {'confirmed'} and goals:
        flash('이미 확정된 목표입니다.', 'error')
    elif not (3 <= len(goals) <= 5):
        flash(f'목표는 3~5개여야 합니다. (현재 {len(goals)}개)', 'error')
    elif weight_sum != 100:
        flash(f'가중치 합계가 100%여야 합니다. (현재 {weight_sum}%)', 'error')
    else:
        db.execute(
            "UPDATE performance_goals SET approval_status='submitted', return_comment=NULL "
            "WHERE cycle_id=? AND user_id=?",
            (cycle_id, uid)
        )
        db.commit()
        # 직속 매니저에게 알림 (없으면 admin 전체)
        mgr = db.execute('SELECT manager_id FROM users WHERE id=?', (uid,)).fetchone()
        targets = []
        if mgr and mgr['manager_id']:
            targets = [mgr['manager_id']]
        else:
            targets = [r['id'] for r in db.execute(
                "SELECT id FROM users WHERE role='admin' AND status='active'").fetchall()]
        for t in targets:
            add_notification(
                t, 'action', 'perf',
                '목표 승인 요청',
                f'{session.get("user_name","직원")}님이 {cycle["name"]} 목표 {len(goals)}개를 제출했습니다.',
                link='/performance?cycle=%d' % cycle_id
            )
        flash('목표를 제출했습니다. 팀장 승인 후 확정됩니다.', 'success')
    return redirect(url_for('performance', cycle=cycle_id))


@app.route('/performance/goals/<int:user_id>/approve', methods=['POST'])
@manager_or_admin
def performance_goals_approve(user_id):
    """팀장/HR — 제출된 목표 세트 승인(확정) 또는 반려"""
    db       = get_db()
    uid      = session['user_id']
    role     = session['user_role']
    cycle_id = request.form.get('cycle_id', type=int)
    action   = request.form.get('action', 'approve')  # approve | return
    comment  = request.form.get('comment', '').strip() or None

    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    emp   = db.execute('SELECT id, name, manager_id, department_id FROM users WHERE id=?', (user_id,)).fetchone()
    if not cycle or not emp:
        abort(404)
    # 권한: admin 또는 직속 매니저(manager_id) 또는 같은 부서 매니저
    if role != 'admin':
        my_dept = int(session.get('dept_id') or 0)
        if emp['manager_id'] != uid and emp['department_id'] != my_dept:
            abort(403)
    if cycle['stage'] != 'goal':
        flash('목표 수립 단계에서만 승인/반려할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))

    submitted = db.execute(
        "SELECT COUNT(*) FROM performance_goals "
        "WHERE cycle_id=? AND user_id=? AND approval_status='submitted'",
        (cycle_id, user_id)
    ).fetchone()[0]
    if not submitted:
        flash('승인 대기 중인 목표가 없습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))

    if action == 'return':
        if not comment:
            flash('반려 시에는 사유를 입력해야 합니다.', 'error')
            return redirect(url_for('performance', cycle=cycle_id))
        db.execute(
            "UPDATE performance_goals SET approval_status='returned', return_comment=? "
            "WHERE cycle_id=? AND user_id=? AND approval_status='submitted'",
            (comment, cycle_id, user_id)
        )
        db.commit()
        add_notification(
            user_id, 'action', 'perf',
            '목표가 반려되었습니다',
            f'{cycle["name"]} 목표 반려 — 사유: {comment}',
            link='/performance?cycle=%d' % cycle_id
        )
        flash(f'{emp["name"]}님의 목표를 반려했습니다.', 'success')
    else:
        db.execute(
            "UPDATE performance_goals SET approval_status='confirmed', return_comment=NULL "
            "WHERE cycle_id=? AND user_id=? AND approval_status='submitted'",
            (cycle_id, user_id)
        )
        db.commit()
        add_notification(
            user_id, 'info', 'perf',
            '목표가 확정되었습니다',
            f'{cycle["name"]} 목표가 팀장 승인으로 확정되었습니다. 진행률을 수시로 업데이트하세요.',
            link='/performance?cycle=%d' % cycle_id
        )
        flash(f'{emp["name"]}님의 목표를 확정했습니다.', 'success')
    return redirect(url_for('performance', cycle=cycle_id))


@app.route('/performance/goals/<int:goal_id>/delete', methods=['POST'])
@login_required
def performance_goal_delete(goal_id):
    """본인 목표 삭제 — 목표 수립 단계 + 미확정(draft/returned) 상태에서만"""
    db   = get_db()
    uid  = session['user_id']
    goal = db.execute(
        'SELECT g.*, c.stage FROM performance_goals g '
        'JOIN performance_cycles c ON g.cycle_id=c.id WHERE g.id=?', (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)
    if goal['user_id'] != uid:
        abort(403)
    if goal['stage'] != 'goal' or goal['approval_status'] not in ('draft', 'returned'):
        flash('확정·제출된 목표는 삭제할 수 없습니다. 팀장에게 문의하세요.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))
    db.execute('DELETE FROM performance_reviews WHERE goal_id=?', (goal_id,))
    db.execute('DELETE FROM performance_goals WHERE id=?', (goal_id,))
    db.commit()
    flash('목표를 삭제했습니다.', 'success')
    return redirect(url_for('performance', cycle=goal['cycle_id']))

@app.route('/performance/goals/<int:goal_id>/review', methods=['GET', 'POST'])
@manager_or_admin
def performance_review(goal_id):
    db   = get_db()
    goal = db.execute(
        'SELECT g.*, u.name AS user_name, c.stage AS cycle_stage '
        'FROM performance_goals g JOIN users u ON g.user_id = u.id '
        'JOIN performance_cycles c ON g.cycle_id = c.id '
        'WHERE g.id=?', (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)
    if goal['cycle_stage'] != 'review':
        flash('팀장 평가는 "평가 진행" 단계에서만 작성할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))
    if goal['approval_status'] != 'confirmed':
        flash('확정되지 않은 목표는 평가할 수 없습니다. 목표 승인을 먼저 진행하세요.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))

    existing = db.execute(
        'SELECT * FROM performance_reviews WHERE goal_id=? AND reviewer_id=?',
        (goal_id, session['user_id'])
    ).fetchone()
    error = None

    if request.method == 'POST':
        score   = int(request.form.get('score', 3))
        comment = request.form.get('comment', '').strip() or None
        if not (1 <= score <= 5):
            error = '점수는 1~5 사이여야 합니다.'
        else:
            db.execute(
                'INSERT INTO performance_reviews (goal_id, reviewer_id, score, comment) '
                'VALUES (?, ?, ?, ?) '
                'ON CONFLICT(goal_id, reviewer_id) DO UPDATE SET score=excluded.score, '
                'comment=excluded.comment, created_at=CURRENT_TIMESTAMP',
                (goal_id, session['user_id'], score, comment)
            )
            db.commit()
            return redirect(url_for('performance'))

    return render_template('performance/review.html',
                           goal=goal, existing=existing,
                           score_labels=SCORE_LABELS, error=error,
                           active_page='performance')


# ── 팀원 상세 처리 패널 (R1-A, v1.3.0) ────────────────────────
def _team_member_or_403(db, emp_uid):
    """매니저 소유권 검증 — admin 전체, manager는 직속 부하 또는 같은 부서만."""
    emp = db.execute(
        'SELECT u.*, d.name AS dept_name FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id WHERE u.id=?', (emp_uid,)
    ).fetchone()
    if not emp:
        abort(404)
    if session['user_role'] != 'admin':
        my_dept = int(session.get('dept_id') or 0)
        if emp['manager_id'] != session['user_id'] and emp['department_id'] != my_dept:
            abort(403)
    return emp


def _team_pending_uids(db, cycle, viewer_uid, role, mgr_dept):
    """현재 단계에서 처리 필요한 팀원 user_id 목록 (이름순)."""
    scope_sql   = ''
    scope_args  = []
    if role == 'manager' and mgr_dept:
        scope_sql  = 'AND u.department_id=? '
        scope_args = [mgr_dept]
    if cycle['stage'] == 'goal':
        rows = db.execute(
            'SELECT DISTINCT u.id, u.name FROM performance_goals g JOIN users u ON g.user_id=u.id '
            "WHERE g.cycle_id=? AND g.approval_status='submitted' " + scope_sql +
            'ORDER BY u.name',
            [cycle['id']] + scope_args
        ).fetchall()
    elif cycle['stage'] == 'review':
        rows = db.execute(
            'SELECT DISTINCT u.id, u.name FROM performance_goals g JOIN users u ON g.user_id=u.id '
            "WHERE g.cycle_id=? AND g.approval_status='confirmed' " + scope_sql +
            'AND NOT EXISTS (SELECT 1 FROM performance_reviews r WHERE r.goal_id=g.id AND r.reviewer_id=?) '
            'ORDER BY u.name',
            [cycle['id']] + scope_args + [viewer_uid]
        ).fetchall()
    else:
        rows = []
    return [r['id'] for r in rows]


@app.route('/performance/team/<int:emp_uid>')
@manager_or_admin
def performance_team_member(emp_uid):
    """팀원 상세 처리 패널 — 목표 승인 + 평가 입력을 한 화면에서 (R1-A)."""
    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']
    emp  = _team_member_or_403(db, emp_uid)

    try:
        cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        cycle_id = 0
    if cycle_id:
        cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    else:
        cycle = db.execute("SELECT * FROM performance_cycles WHERE status='active' LIMIT 1").fetchone()
    if not cycle:
        flash('평가 주기가 없습니다.', 'error')
        return redirect(url_for('performance'))

    goals = db.execute(
        'SELECT g.*, AVG(r.score) AS avg_score, COUNT(DISTINCT r.id) AS review_count, '
        'r2.score AS my_score, r2.comment AS my_comment '
        'FROM performance_goals g '
        'LEFT JOIN performance_reviews r ON g.id = r.goal_id '
        'LEFT JOIN performance_reviews r2 ON g.id = r2.goal_id AND r2.reviewer_id = ? '
        'WHERE g.cycle_id=? AND g.user_id=? '
        'GROUP BY g.id ORDER BY g.created_at',
        (uid, cycle['id'], emp_uid)
    ).fetchall()

    confirmed    = [g for g in goals if g['approval_status'] == 'confirmed']
    weight_sum   = sum(g['weight'] for g in goals)
    avg_progress = round(sum((g['progress'] or 0) for g in goals) / len(goals)) if goals else 0
    self_done    = sum(1 for g in confirmed if g['self_score'])
    self_avg     = (sum(g['self_score'] for g in confirmed if g['self_score']) / self_done) if self_done else None
    submitted    = sum(1 for g in goals if g['approval_status'] == 'submitted')

    # 지난 사이클 등급 (참고 패널)
    prev_grade = db.execute(
        'SELECT cr.final_grade, pc.name AS cycle_name FROM calibration_results cr '
        'JOIN performance_cycles pc ON cr.cycle_id=pc.id '
        'WHERE cr.user_id=? AND cr.cycle_id != ? ORDER BY pc.start_date DESC LIMIT 1',
        (emp_uid, cycle['id'])
    ).fetchone()

    # 이번 사이클 확정 등급 (조정 단계 이후)
    cur_grade = db.execute(
        'SELECT final_grade, is_shared FROM calibration_results WHERE cycle_id=? AND user_id=?',
        (cycle['id'], emp_uid)
    ).fetchone()

    # 다음 처리 대상 팀원 (이름순, 본인 제외)
    mgr_dept = int(session.get('dept_id') or 0)
    pending  = [u for u in _team_pending_uids(db, cycle, uid, role, mgr_dept) if u != emp_uid]
    next_uid = pending[0] if pending else None

    return render_template('performance/team_member.html',
                           emp=emp, cycle=cycle, stage=cycle['stage'],
                           goals=goals, confirmed_count=len(confirmed),
                           weight_sum=weight_sum, avg_progress=avg_progress,
                           self_done=self_done, self_avg=self_avg,
                           submitted=submitted,
                           prev_grade=prev_grade, cur_grade=cur_grade,
                           next_uid=next_uid, pending_count=len(pending),
                           cycle_stages=CYCLE_STAGES,
                           cycle_stage_label=CYCLE_STAGE_LABEL,
                           goal_approval_label=GOAL_APPROVAL_LABEL,
                           score_labels=SCORE_LABELS,
                           c2=_c2_member_context(db, emp_uid, cycle, uid, role, session.get('dept_id')),
                           c3=_c3_member_context(db, emp_uid, cycle, uid),
                           ck_label=CHECKIN_STATUS_LABEL, ck_cls=CHECKIN_STATUS_CLS,
                           kind_label=FEEDBACK_KIND_LABEL,
                           copilot=copilot_view(), active_page='performance')


@app.route('/performance/team/<int:emp_uid>/review', methods=['POST'])
@manager_or_admin
def performance_team_member_review(emp_uid):
    """팀원 상세 패널에서 목표별 평가 일괄 저장 (R1-A)."""
    db   = get_db()
    uid  = session['user_id']
    _team_member_or_403(db, emp_uid)

    cycle_id = request.form.get('cycle_id', type=int)
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    if cycle['stage'] != 'review':
        flash('팀장 평가는 "평가 진행" 단계에서만 작성할 수 있습니다.', 'error')
        return redirect(url_for('performance_team_member', emp_uid=emp_uid, cycle=cycle_id))

    goals = db.execute(
        "SELECT id FROM performance_goals WHERE cycle_id=? AND user_id=? AND approval_status='confirmed'",
        (cycle_id, emp_uid)
    ).fetchall()

    saved = 0
    for g in goals:
        raw = request.form.get(f'score_{g["id"]}', '').strip()
        if not raw:
            continue
        try:
            score = int(raw)
        except ValueError:
            continue
        if not (1 <= score <= 5):
            continue
        comment = request.form.get(f'comment_{g["id"]}', '').strip() or None
        db.execute(
            'INSERT INTO performance_reviews (goal_id, reviewer_id, score, comment) '
            'VALUES (?, ?, ?, ?) '
            'ON CONFLICT(goal_id, reviewer_id) DO UPDATE SET score=excluded.score, '
            'comment=excluded.comment, created_at=CURRENT_TIMESTAMP',
            (g['id'], uid, score, comment)
        )
        saved += 1

    # C3 — 역량 매니저 평가 (양식에 역량이 있는 주기)
    for comp in cycle_form(cycle)['competencies']:
        try:
            score = int(request.form.get(f'comp_{comp["key"]}', ''))
        except (TypeError, ValueError):
            continue
        if not 1 <= score <= 5:
            continue
        comment = request.form.get(f'compc_{comp["key"]}', '').strip()[:1000] or None
        db.execute(
            'INSERT INTO competency_scores (cycle_id, user_id, comp_key, rater_type, rater_id, score, comment) '
            "VALUES (?, ?, ?, 'manager', ?, ?, ?) "
            'ON CONFLICT(cycle_id, user_id, comp_key, rater_id) DO UPDATE SET score=excluded.score, '
            'comment=excluded.comment, updated_at=CURRENT_TIMESTAMP',
            (cycle_id, emp_uid, comp['key'], uid, score, comment))
        saved += 1
    db.commit()

    if saved:
        flash(f'평가 {saved}건을 저장했습니다.', 'success')
    else:
        flash('저장된 평가가 없습니다. 점수를 선택해주세요.', 'error')

    if request.form.get('save_next'):
        mgr_dept = int(session.get('dept_id') or 0)
        pending  = [u for u in _team_pending_uids(db, cycle, uid, session['user_role'], mgr_dept) if u != emp_uid]
        if pending:
            return redirect(url_for('performance_team_member', emp_uid=pending[0], cycle=cycle_id))
        flash('이번 단계에서 처리할 팀원을 모두 완료했습니다. ', 'success')
        return redirect(url_for('performance', cycle=cycle_id))
    return redirect(url_for('performance_team_member', emp_uid=emp_uid, cycle=cycle_id))


@app.route('/performance/remind', methods=['POST'])
@manager_or_admin
def performance_remind():
    """단계별 미완료자 리마인드 일괄 발송 (R1-D) — 매니저는 자기 부서만."""
    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']
    cycle_id = request.form.get('cycle_id', type=int)
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)

    mgr_dept  = int(session.get('dept_id') or 0)
    scope_sql, scope_args = '', []
    if role == 'manager' and mgr_dept:
        scope_sql  = 'AND u.department_id=? '
        scope_args = [mgr_dept]

    stage = cycle['stage']
    targets = []
    if stage == 'goal':
        # 목표 미확정(미작성 포함) 직원 — 미작성자는 부서 스코프의 목표 없는 직원까지
        rows = db.execute(
            "SELECT DISTINCT u.id FROM users u "
            "LEFT JOIN performance_goals g ON g.user_id=u.id AND g.cycle_id=? "
            "WHERE u.status='active' AND u.role NOT IN ('guest','admin') " + scope_sql +
            "AND (g.id IS NULL OR g.approval_status IN ('draft','returned'))",
            [cycle_id] + scope_args
        ).fetchall()
        targets = [r['id'] for r in rows]
        title, content = '목표 수립을 완료해 주세요', \
            f'{cycle["name"]} 목표 작성·제출이 아직 완료되지 않았습니다.' + \
            (f' 마감일: {cycle["goal_deadline"]}' if cycle['goal_deadline'] else '')
    elif stage == 'review':
        rows = db.execute(
            "SELECT DISTINCT u.id FROM users u "
            "JOIN performance_goals g ON g.user_id=u.id AND g.cycle_id=? "
            "WHERE g.approval_status='confirmed' AND g.self_score IS NULL " + scope_sql,
            [cycle_id] + scope_args
        ).fetchall()
        targets = [r['id'] for r in rows]
        title, content = '자기평가를 작성해 주세요', \
            f'{cycle["name"]} 자기평가가 아직 제출되지 않았습니다.' + \
            (f' 마감일: {cycle["review_deadline"]}' if cycle['review_deadline'] else '')
    else:
        flash('리마인드는 목표 수립·평가 진행 단계에서만 발송할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))

    for t in targets:
        add_notification(t, 'action', 'perf', title, content,
                         link='/performance?cycle=%d' % cycle_id)
    if targets:
        log_audit('create', 'performance', None,
                  f'{cycle["name"]} {CYCLE_STAGE_LABEL[stage]} 미완료 리마인드 발송 ({len(targets)}명)')
        flash(f'미완료자 {len(targets)}명에게 리마인드를 발송했습니다.', 'success')
    else:
        flash('리마인드 대상이 없습니다. 모두 완료된 상태입니다.', 'success')
    return redirect(url_for('performance', cycle=cycle_id))


@app.route('/performance/acknowledge', methods=['POST'])
@login_required
def performance_acknowledge():
    """직원 — 공개된 평가 결과 확인 (R1-D, Workday Acknowledgement 패턴)."""
    db  = get_db()
    uid = session['user_id']
    cycle_id = request.form.get('cycle_id', type=int)
    row = db.execute(
        'SELECT id, acknowledged_at FROM calibration_results '
        'WHERE cycle_id=? AND user_id=? AND is_shared=1', (cycle_id, uid)
    ).fetchone()
    if not row:
        flash('공개된 평가 결과가 없습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))
    if not row['acknowledged_at']:
        db.execute('UPDATE calibration_results SET acknowledged_at=CURRENT_TIMESTAMP WHERE id=?',
                   (row['id'],))
        db.commit()
        flash('평가 결과 확인이 기록되었습니다.', 'success')
    return redirect(url_for('performance', cycle=cycle_id))


# ── Performance Cycles (Admin) ────────────────────────────────
@app.route('/performance/cycles')
@admin_required
def performance_cycles():
    db     = get_db()
    cycles = db.execute(
        'SELECT pc.*, COUNT(pg.id) AS goal_count '
        'FROM performance_cycles pc '
        'LEFT JOIN performance_goals pg ON pc.id = pg.cycle_id '
        'GROUP BY pc.id ORDER BY pc.start_date DESC'
    ).fetchall()

    # 활성 주기 단계별 미완료 현황 (전환 전 체크리스트, R1-D)
    pending_info = {}
    for cyc in cycles:
        if cyc['status'] != 'active':
            continue
        if cyc['stage'] == 'goal':
            parts = []
            if not cyc['roster_locked_at']:
                parts.append('평가 명단 미확정')
            n = db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
                "WHERE cycle_id=? AND approval_status != 'confirmed'", (cyc['id'],)
            ).fetchone()[0]
            if n:
                parts.append(f'목표 미확정 {n}명')
            if cyc['include_peer'] and not cyc['peer_locked_at']:
                parts.append('다면평가 배정 미확정')
            if parts:
                pending_info[cyc['id']] = ' · '.join(parts)
        elif cyc['stage'] == 'review':
            no_self = db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
                "WHERE cycle_id=? AND approval_status='confirmed' AND self_score IS NULL", (cyc['id'],)
            ).fetchone()[0]
            no_mgr = db.execute(
                "SELECT COUNT(*) FROM performance_goals g "
                "WHERE g.cycle_id=? AND g.approval_status='confirmed' "
                "AND NOT EXISTS (SELECT 1 FROM performance_reviews r WHERE r.goal_id=g.id)", (cyc['id'],)
            ).fetchone()[0]
            parts = []
            if no_self: parts.append(f'자기평가 미제출 {no_self}명')
            if no_mgr:  parts.append(f'매니저 미평가 목표 {no_mgr}개')
            if parts:
                pending_info[cyc['id']] = ' · '.join(parts)
        elif cyc['stage'] == 'appeal':
            n = db.execute(
                "SELECT COUNT(*) FROM grade_appeals WHERE cycle_id=? AND status='pending'", (cyc['id'],)
            ).fetchone()[0]
            ack = db.execute(
                'SELECT COUNT(*) FROM calibration_results WHERE cycle_id=? AND acknowledged_at IS NOT NULL',
                (cyc['id'],)
            ).fetchone()[0]
            total = db.execute(
                'SELECT COUNT(*) FROM calibration_results WHERE cycle_id=?', (cyc['id'],)
            ).fetchone()[0]
            parts = [f'결과 확인 {ack}/{total}명']
            if n: parts.append(f'미처리 이의 {n}건')
            pending_info[cyc['id']] = ' · '.join(parts)

    return render_template('performance/cycles.html', cycles=cycles,
                           pending_info=pending_info,
                           cycle_forms={c['id']: cycle_form(c) for c in cycles},
                           review_forms=db.execute('SELECT id, name, is_default, goal_weight, comp_weight FROM review_forms '
                                                   'ORDER BY is_default DESC, id').fetchall(),
                           today=date.today().isoformat(),
                           cycle_stages=CYCLE_STAGES,
                           cycle_stage_label=CYCLE_STAGE_LABEL,
                           cycle_stage_desc=CYCLE_STAGE_DESC,
                           active_page='performance_cycles')


@app.route('/performance/cycles/new', methods=['POST'])
@admin_required
def performance_cycle_new():
    db           = get_db()
    name         = request.form.get('name', '').strip()
    start_date   = request.form.get('start_date', '').strip()
    end_date     = request.form.get('end_date', '').strip()
    include_peer = 1 if request.form.get('include_peer') else 0
    goal_deadline   = request.form.get('goal_deadline', '').strip() or None
    review_deadline = request.form.get('review_deadline', '').strip() or None

    if not name or not start_date or not end_date:
        flash('모든 항목을 입력해 주세요.', 'error')
        return redirect(url_for('performance_cycles'))
    if start_date >= end_date:
        flash('종료일은 시작일보다 이후여야 합니다.', 'error')
        return redirect(url_for('performance_cycles'))

    # 기존 active 사이클이 있으면 자동 closed 처리
    frm = db.execute('SELECT * FROM review_forms WHERE id=?', (request.form.get('form_id', type=int),)).fetchone() \
        or _default_review_form(db)
    db.execute("UPDATE performance_cycles SET status='closed', stage='closed' WHERE status='active'")
    db.execute(
        "INSERT INTO performance_cycles (name, start_date, end_date, status, stage, include_peer, "
        "goal_deadline, review_deadline, form_id, form_json) "
        "VALUES (?, ?, ?, 'active', 'goal', ?, ?, ?, ?, ?)",
        (name, start_date, end_date, include_peer, goal_deadline, review_deadline,
         frm['id'] if frm else None, review_form_snapshot(frm) if frm else None)
    )
    db.commit()
    flash(f'평가 주기 "{name}"이 생성되었습니다. 목표 수립 단계부터 시작합니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/cycles/<int:cycle_id>/deadlines', methods=['POST'])
@admin_required
def performance_cycle_deadlines(cycle_id):
    """단계별 마감일 수정 (R1-D)."""
    db = get_db()
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    goal_deadline   = request.form.get('goal_deadline', '').strip() or None
    review_deadline = request.form.get('review_deadline', '').strip() or None
    db.execute('UPDATE performance_cycles SET goal_deadline=?, review_deadline=? WHERE id=?',
               (goal_deadline, review_deadline, cycle_id))
    db.commit()
    flash('단계별 마감일이 저장되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/cycles/<int:cycle_id>/stage', methods=['POST'])
@admin_required
def performance_cycle_stage(cycle_id):
    """주기 단계 전환 (상태머신: goal→progress→review→calibration→appeal→closed)"""
    db    = get_db()
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    direction = request.form.get('direction', 'next')
    cur_idx   = CYCLE_STAGES.index(cycle['stage']) if cycle['stage'] in CYCLE_STAGES else 0

    if direction == 'prev':
        if cur_idx == 0:
            flash('첫 단계입니다.', 'error')
            return redirect(url_for('performance_cycles'))
        new_stage = CYCLE_STAGES[cur_idx - 1]
    else:
        if cur_idx >= len(CYCLE_STAGES) - 1:
            flash('이미 종료된 주기입니다.', 'error')
            return redirect(url_for('performance_cycles'))
        new_stage = CYCLE_STAGES[cur_idx + 1]

    # ── 단계 진입 시 부수 효과 ─────────────────────────────
    if new_stage == 'appeal' and direction == 'next':
        # 등급 확정·본인 공개 + 이의신청 7일 기간 시작
        confirmed = db.execute(
            'SELECT COUNT(*) FROM calibration_results WHERE cycle_id=?', (cycle_id,)
        ).fetchone()[0]
        if confirmed == 0:
            flash('확정된 등급이 없습니다. HR 조정 단계에서 등급을 먼저 확정하세요.', 'error')
            return redirect(url_for('performance_cycles'))
        appeal_until = (date.today() + timedelta(days=7)).isoformat()
        db.execute('UPDATE calibration_results SET is_shared=1 WHERE cycle_id=?', (cycle_id,))
        db.execute("UPDATE performance_cycles SET stage='appeal', appeal_until=? WHERE id=?",
                   (appeal_until, cycle_id))
        db.commit()
        shared_rows = db.execute(
            'SELECT user_id, final_grade FROM calibration_results WHERE cycle_id=?', (cycle_id,)
        ).fetchall()
        for r in shared_rows:
            add_notification(
                r['user_id'], 'info', 'perf',
                '성과 평가 결과가 공개되었습니다',
                f'{cycle["name"]} 최종 등급: {r["final_grade"]} · 이의신청은 {appeal_until}까지 1회 가능합니다.',
                link='/performance?cycle=%d&tab=result' % cycle_id
            )
        log_audit('update', 'performance', None,
                  f'평가 주기 "{cycle["name"]}" 등급 공개 + 이의신청 기간 시작 (~{appeal_until}, {len(shared_rows)}명)')
        flash(f'{len(shared_rows)}명의 등급이 공개되었습니다. 이의신청 기간: {appeal_until}까지.', 'success')
        return redirect(url_for('performance_cycles'))

    if new_stage == 'closed':
        # 미처리 이의신청이 있으면 경고
        pending = db.execute(
            "SELECT COUNT(*) FROM grade_appeals WHERE cycle_id=? AND status='pending'", (cycle_id,)
        ).fetchone()[0]
        if pending:
            flash(f'미처리 이의신청이 {pending}건 있습니다. 먼저 처리해 주세요.', 'error')
            return redirect(url_for('performance_cycles'))
        db.execute("UPDATE performance_cycles SET stage='closed', status='closed' WHERE id=?", (cycle_id,))
        db.commit()
        flash(f'"{cycle["name"]}" 주기가 종료되었습니다. 보상 검토에서 등급 연동 인상·성과상여를 진행할 수 있습니다.', 'success')
        return redirect(url_for('performance_cycles'))

    # 전환 체크리스트 경고 (R1-D — 진행은 허용하되 미완료 현황을 알림)
    if direction == 'next' and new_stage == 'progress':
        unconfirmed = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
            "WHERE cycle_id=? AND approval_status != 'confirmed'", (cycle_id,)
        ).fetchone()[0]
        if unconfirmed:
            flash(f'목표가 아직 확정되지 않은 직원이 {unconfirmed}명 있습니다. 목표 수립 단계가 지나면 신규 등록·제출이 제한됩니다.', 'error')
    if direction == 'next' and new_stage == 'calibration':
        no_self = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
            "WHERE cycle_id=? AND approval_status='confirmed' AND self_score IS NULL", (cycle_id,)
        ).fetchone()[0]
        no_mgr = db.execute(
            "SELECT COUNT(*) FROM performance_goals g "
            "WHERE g.cycle_id=? AND g.approval_status='confirmed' "
            "AND NOT EXISTS (SELECT 1 FROM performance_reviews r WHERE r.goal_id=g.id)", (cycle_id,)
        ).fetchone()[0]
        if no_self or no_mgr:
            flash(f'자기평가 미제출 {no_self}명 · 매니저 미평가 목표 {no_mgr}개 상태로 조정 단계에 진입했습니다. 점수 없는 항목은 집계에서 빠집니다.', 'error')

    # 일반 전환 (뒤로 갈 때 closed → 재활성화)
    db.execute('UPDATE performance_cycles SET stage=? WHERE id=?', (new_stage, cycle_id))
    if cycle['status'] == 'closed' and new_stage != 'closed':
        db.execute("UPDATE performance_cycles SET status='active' WHERE id=?", (cycle_id,))
        db.execute("UPDATE performance_cycles SET status='closed', stage='closed' "
                   "WHERE status='active' AND id != ?", (cycle_id,))
    if new_stage == 'review':
        db.commit()
        # 평가 시작 알림 (목표 있는 직원에게)
        targets = db.execute(
            'SELECT DISTINCT user_id FROM performance_goals WHERE cycle_id=?', (cycle_id,)
        ).fetchall()
        for t in targets:
            add_notification(
                t['user_id'], 'action', 'perf',
                '평가가 시작되었습니다',
                f'{cycle["name"]} 자기평가를 작성해 주세요.' + (' 배정된 다면평가도 함께 작성해 주세요.' if cycle['include_peer'] else ''),
                link='/performance?cycle=%d' % cycle_id
            )
    db.commit()
    flash(f'단계가 "{CYCLE_STAGE_LABEL[new_stage]}"(으)로 변경되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/cycles/<int:cycle_id>/close', methods=['POST'])
@admin_required
def performance_cycle_close(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    db.execute("UPDATE performance_cycles SET status='closed', stage='closed' WHERE id=?", (cycle_id,))
    db.commit()
    flash(f'"{cycle["name"]}" 평가 주기가 마감되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/cycles/<int:cycle_id>/activate', methods=['POST'])
@admin_required
def performance_cycle_activate(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    # 기존 active 사이클 종료 후 대상 활성화 (종료 상태였다면 이의신청 단계로 복귀)
    db.execute("UPDATE performance_cycles SET status='closed', stage='closed' WHERE status='active'")
    reopen_stage = cycle['stage'] if cycle['stage'] not in (None, 'closed') else 'appeal'
    db.execute("UPDATE performance_cycles SET status='active', stage=? WHERE id=?", (reopen_stage, cycle_id))
    db.commit()
    flash(f'"{cycle["name"]}" 평가 주기가 활성화되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


# ── P1 평가 명단: 대상자·평가자 ──────────────────────────────
PERF_EXCLUDE_LABEL = {
    'new':     '입사 {m}개월 미만',
    'leaving': '퇴사 예정',
    'intern':  '인턴',
    'manual':  '직접 제외',
}
PERF_REVIEWER_SRC = {
    'manager':     '보고라인',
    'dept_leader': '부서장',
    'up_leader':   '상위 부서장',
    'manual':      '직접 지정',
}


def perf_min_months(db=None):
    """평가 대상에서 빼는 신입 기준 개월 수 (기본 3개월)."""
    try:
        row = (db or get_db()).execute(
            'SELECT perf_min_months FROM company_config WHERE id=1').fetchone()
        if row and row['perf_min_months'] is not None:
            return int(row['perf_min_months'])
    except Exception:
        pass
    return 3


def perf_exclude_label(code, min_m):
    return PERF_EXCLUDE_LABEL.get(code, '').replace('{m}', str(min_m))


def _months_between(d1, d2):
    """d1(입사일)부터 d2까지 만으로 몇 개월."""
    m = (d2.year - d1.year) * 12 + (d2.month - d1.month)
    if d2.day < d1.day:
        m -= 1
    return m


def _roster_defaults(db, cycle):
    """주기 기준으로 재직자 전원의 기본 포함 여부·평가자를 계산한다."""
    base = cycle['end_date'] or date.today().isoformat()
    try:
        base_d = date.fromisoformat(base)
    except (ValueError, TypeError):
        base_d = date.today()
    min_m = perf_min_months(db)

    deps   = db.execute('SELECT id, parent_id, leader_id FROM departments').fetchall()
    parent = {d['id']: d['parent_id'] for d in deps}
    leader = {d['id']: d['leader_id'] for d in deps}

    users = db.execute(
        "SELECT id, department_id, manager_id, hire_date, employment_type, termination_date "
        "FROM users WHERE status='active' AND role != 'guest'"
    ).fetchall()
    active_ids = {u['id'] for u in users}

    def _leader_up(did, uid):
        """부서장 → 상위 부서장 순으로 본인이 아닌 재직자를 찾는다."""
        seen, cur, first = set(), did, True
        while cur and cur not in seen:
            seen.add(cur)
            lid = leader.get(cur)
            if lid and lid != uid and lid in active_ids:
                return lid, ('dept_leader' if first else 'up_leader')
            cur, first = parent.get(cur), False
        return None, ''

    out = {}
    for u in users:
        code = ''
        if u['employment_type'] == 'intern':
            code = 'intern'
        elif u['termination_date'] and u['termination_date'] <= base:
            code = 'leaving'
        elif u['hire_date']:
            try:
                if _months_between(date.fromisoformat(u['hire_date']), base_d) < min_m:
                    code = 'new'
            except (ValueError, TypeError):
                pass
        if u['manager_id'] and u['manager_id'] != u['id'] and u['manager_id'] in active_ids:
            rid, src = u['manager_id'], 'manager'
        else:
            rid, src = _leader_up(u['department_id'], u['id'])
        out[u['id']] = {'included': 0 if code else 1, 'exclude_code': code,
                        'reviewer_id': rid, 'reviewer_src': src}
    return out


def build_cycle_roster(db, cycle, uid):
    """명단을 만들거나 새로고침한다. 사람이 손댄 줄(manual=1)은 그대로 둔다."""
    defaults = _roster_defaults(db, cycle)
    have = {r['user_id']: r['manual'] for r in db.execute(
        'SELECT user_id, manual FROM cycle_participants WHERE cycle_id=?', (cycle['id'],)).fetchall()}

    added = changed = 0
    for user_id, d in defaults.items():
        if user_id not in have:
            db.execute(
                'INSERT INTO cycle_participants '
                '(cycle_id, user_id, included, exclude_code, reviewer_id, reviewer_src, updated_by) '
                'VALUES (?,?,?,?,?,?,?)',
                (cycle['id'], user_id, d['included'], d['exclude_code'],
                 d['reviewer_id'], d['reviewer_src'], uid))
            added += 1
        elif not have[user_id]:
            db.execute(
                'UPDATE cycle_participants SET included=?, exclude_code=?, reviewer_id=?, '
                '       reviewer_src=?, updated_by=?, updated_at=CURRENT_TIMESTAMP '
                'WHERE cycle_id=? AND user_id=?',
                (d['included'], d['exclude_code'], d['reviewer_id'], d['reviewer_src'],
                 uid, cycle['id'], user_id))
            changed += 1

    removed = [u for u, m in have.items() if u not in defaults and not m]
    for u in removed:
        db.execute('DELETE FROM cycle_participants WHERE cycle_id=? AND user_id=?', (cycle['id'], u))
    db.commit()
    return added, changed, len(removed)


def _roster_cycle(db, cycle_id):
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    return cycle


@app.route('/performance/cycles/<int:cycle_id>/roster')
@admin_required
def performance_roster(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    view  = request.args.get('view', 'in')
    q     = (request.args.get('q') or '').strip()
    min_m = perf_min_months(db)

    rows = db.execute(
        'SELECT cp.*, u.name, u.emp_no, u.hire_date, u.employment_type, '
        '       d.name AS dept, p.name AS position, r.name AS reviewer_name '
        'FROM cycle_participants cp '
        'JOIN users u ON u.id = cp.user_id '
        'LEFT JOIN departments d ON d.id = u.department_id '
        'LEFT JOIN positions   p ON p.id = u.position_id '
        'LEFT JOIN users       r ON r.id = cp.reviewer_id '
        'WHERE cp.cycle_id=? ORDER BY d.name, u.name', (cycle_id,)).fetchall()

    summary = {
        'total': len(rows),
        'inc':   sum(1 for r in rows if r['included']),
        'exc':   sum(1 for r in rows if not r['included']),
        'norev': sum(1 for r in rows if r['included'] and not r['reviewer_id']),
    }

    def _keep(r):
        if q and q not in (r['name'] or '') and q not in (r['dept'] or ''):
            return False
        if view == 'in':
            return bool(r['included'])
        if view == 'out':
            return not r['included']
        if view == 'norev':
            return bool(r['included']) and not r['reviewer_id']
        return True

    shown = [r for r in rows if _keep(r)]

    # 평가자를 몇 명씩 보는지 — 한 사람에게 몰렸는지 확인용
    load = {}
    for r in rows:
        if r['included'] and r['reviewer_id']:
            load[r['reviewer_id']] = load.get(r['reviewer_id'], 0) + 1

    candidates = db.execute(
        "SELECT u.id, u.name, d.name AS dept FROM users u "
        "LEFT JOIN departments d ON d.id = u.department_id "
        "WHERE u.status='active' AND u.role != 'guest' ORDER BY d.name, u.name").fetchall()

    return render_template('performance/roster.html',
                           cycle=cycle, rows=shown, summary=summary, view=view, q=q,
                           load=load, candidates=candidates, min_months=min_m,
                           exclude_label=PERF_EXCLUDE_LABEL, reviewer_src=PERF_REVIEWER_SRC,
                           active_page='performance_cycles')


@app.route('/performance/cycles/<int:cycle_id>/roster/build', methods=['POST'])
@admin_required
def performance_roster_build(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    if cycle['roster_locked_at']:
        flash('명단이 확정되어 있습니다. 확정을 풀고 다시 만들어 주세요.', 'error')
        return redirect(url_for('performance_roster', cycle_id=cycle_id))
    added, changed, removed = build_cycle_roster(db, cycle, session['user_id'])
    parts = []
    if added:   parts.append(f'새로 {added}명')
    if changed: parts.append(f'갱신 {changed}명')
    if removed: parts.append(f'제외 {removed}명')
    flash('명단을 만들었습니다. ' + (' · '.join(parts) if parts else '바뀐 사람이 없습니다.'), 'success')
    return redirect(url_for('performance_roster', cycle_id=cycle_id))


@app.route('/performance/cycles/<int:cycle_id>/roster/bulk', methods=['POST'])
@admin_required
def performance_roster_bulk(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    if cycle['roster_locked_at']:
        flash('명단이 확정되어 있어 바꿀 수 없습니다.', 'error')
        return redirect(url_for('performance_roster', cycle_id=cycle_id))

    action = request.form.get('action', '')
    ids    = [int(v) for v in request.form.getlist('user_ids') if v.isdigit()]
    back   = url_for('performance_roster', cycle_id=cycle_id,
                     view=request.form.get('view', 'in'), q=request.form.get('q', '') or None)
    if not ids:
        flash('먼저 대상을 선택해 주세요.', 'error')
        return redirect(back)

    uid  = session['user_id']
    marks = ','.join('?' * len(ids))
    if action == 'include':
        db.execute(f"UPDATE cycle_participants SET included=1, exclude_code='', manual=1, "
                   f"updated_by=?, updated_at=CURRENT_TIMESTAMP "
                   f"WHERE cycle_id=? AND user_id IN ({marks})", [uid, cycle_id] + ids)
        flash(f'{len(ids)}명을 평가 대상에 넣었습니다.', 'success')
    elif action == 'exclude':
        db.execute(f"UPDATE cycle_participants SET included=0, exclude_code='manual', manual=1, "
                   f"updated_by=?, updated_at=CURRENT_TIMESTAMP "
                   f"WHERE cycle_id=? AND user_id IN ({marks})", [uid, cycle_id] + ids)
        flash(f'{len(ids)}명을 평가 대상에서 뺐습니다.', 'success')
    elif action == 'reviewer':
        rid = request.form.get('reviewer_id', type=int)
        if not rid:
            flash('평가자를 골라 주세요.', 'error')
            return redirect(back)
        if rid in ids:
            flash('자기 자신을 평가자로 지정할 수 없습니다.', 'error')
            return redirect(back)
        db.execute(f"UPDATE cycle_participants SET reviewer_id=?, reviewer_src='manual', manual=1, "
                   f"updated_by=?, updated_at=CURRENT_TIMESTAMP "
                   f"WHERE cycle_id=? AND user_id IN ({marks})", [rid, uid, cycle_id] + ids)
        name = db.execute('SELECT name FROM users WHERE id=?', (rid,)).fetchone()
        flash(f'{len(ids)}명의 평가자를 {name["name"] if name else ""}(으)로 바꿨습니다.', 'success')
    else:
        flash('알 수 없는 작업입니다.', 'error')
        return redirect(back)

    db.commit()
    return redirect(back)


@app.route('/performance/cycles/<int:cycle_id>/roster/lock', methods=['POST'])
@admin_required
def performance_roster_lock(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    if cycle['roster_locked_at']:
        db.execute('UPDATE performance_cycles SET roster_locked_at=NULL WHERE id=?', (cycle_id,))
        db.commit()
        flash('명단 확정을 풀었습니다.', 'success')
        return redirect(url_for('performance_roster', cycle_id=cycle_id))

    norev = db.execute('SELECT COUNT(*) FROM cycle_participants '
                       'WHERE cycle_id=? AND included=1 AND reviewer_id IS NULL',
                       (cycle_id,)).fetchone()[0]
    if norev:
        flash(f'평가자가 없는 대상 {norev}명을 먼저 지정해 주세요.', 'error')
        return redirect(url_for('performance_roster', cycle_id=cycle_id, view='norev'))
    total = db.execute('SELECT COUNT(*) FROM cycle_participants WHERE cycle_id=? AND included=1',
                       (cycle_id,)).fetchone()[0]
    if not total:
        flash('평가 대상이 한 명도 없습니다.', 'error')
        return redirect(url_for('performance_roster', cycle_id=cycle_id))

    db.execute('UPDATE performance_cycles SET roster_locked_at=CURRENT_TIMESTAMP WHERE id=?', (cycle_id,))
    db.commit()
    flash(f'평가 대상 {total}명으로 명단을 확정했습니다.', 'success')
    return redirect(url_for('performance_roster', cycle_id=cycle_id))


# ── P3 진행 관리·독촉 ─────────────────────────────────────────
# 할 일 종류 → (표시 이름, 받는 사람 쪽, 알림 링크)
PERF_TODO = {
    'goal_write':  ('목표 작성',   'self',     '/performance?cycle={c}'),
    'goal_submit': ('목표 제출',   'self',     '/performance?cycle={c}'),
    'goal_ok':     ('목표 승인',   'reviewer', '/performance?cycle={c}'),
    'self':        ('자기평가',    'self',     '/performance?cycle={c}'),
    'mgr':         ('평가자 평가', 'reviewer', '/performance?cycle={c}'),
    'peer':        ('동료평가',    'peer',     '/performance/peer?cycle={c}'),
}
PERF_REMIND_HOURS = 20   # 같은 사람에게 다시 보내기까지 최소 간격


def _progress_scope_dept():
    """매니저는 자기 부서만. 관리자는 None(전체)."""
    if session.get('user_role') == 'manager':
        return int(session.get('dept_id') or 0) or -1
    return None


def perf_progress(db, cycle, dept_only=None):
    """주기 대상자별 진행 상태와 밀린 일.
    rows: {user_id, name, dept, position, reviewer_id, reviewer_name,
           goal(상태), goals_n, self_n, mgr_n, peer_done, peer_total, todo:[(kind, 받는사람 id)]}"""
    cid   = cycle['id']
    stage = cycle['stage']
    sql = ('SELECT cp.user_id, cp.reviewer_id, u.name, u.emp_no, u.department_id, '
           '       d.name AS dept, p.name AS position, r.name AS reviewer_name '
           'FROM cycle_participants cp JOIN users u ON u.id = cp.user_id '
           'LEFT JOIN departments d ON d.id = u.department_id '
           'LEFT JOIN positions   p ON p.id = u.position_id '
           'LEFT JOIN users       r ON r.id = cp.reviewer_id '
           'WHERE cp.cycle_id=? AND cp.included=1 ')
    args = [cid]
    if dept_only is not None:
        sql += 'AND u.department_id=? '
        args.append(dept_only)
    people = db.execute(sql + 'ORDER BY d.name, u.name', args).fetchall()

    goals = {}
    for g in db.execute(
            'SELECT g.user_id, g.approval_status, g.self_score, '
            '       (SELECT COUNT(*) FROM performance_reviews r WHERE r.goal_id=g.id) AS rv '
            'FROM performance_goals g WHERE g.cycle_id=?', (cid,)):
        goals.setdefault(g['user_id'], []).append(g)

    peers = {}
    for a in db.execute(
            "SELECT pa.reviewee_id, pa.reviewer_id, u.name AS reviewer_name, "
            "       EXISTS(SELECT 1 FROM peer_reviews pr WHERE pr.cycle_id=pa.cycle_id "
            "              AND pr.reviewee_id=pa.reviewee_id AND pr.reviewer_id=pa.reviewer_id "
            "              AND pr.review_type='peer') AS done "
            "FROM peer_assignments pa JOIN users u ON u.id = pa.reviewer_id "
            "WHERE pa.cycle_id=?", (cid,)):
        peers.setdefault(a['reviewee_id'], []).append(a)

    rows = []
    for p in people:
        gs = goals.get(p['user_id'], [])
        sts = {g['approval_status'] for g in gs}
        if not gs:
            goal = 'none'
        elif sts == {'confirmed'}:
            goal = 'confirmed'
        elif 'submitted' in sts and sts <= {'submitted', 'confirmed'}:
            goal = 'submitted'
        else:
            goal = 'draft'
        conf   = [g for g in gs if g['approval_status'] == 'confirmed']
        self_n = sum(1 for g in conf if g['self_score'] is not None)
        mgr_n  = sum(1 for g in conf if g['rv'])
        ps     = peers.get(p['user_id'], []) if cycle['include_peer'] else []
        peer_done = sum(1 for a in ps if a['done'])

        todo = []
        me, rv = p['user_id'], p['reviewer_id']
        if stage == 'goal':
            if goal == 'none':
                todo.append(('goal_write', me))
            elif goal == 'draft':
                todo.append(('goal_submit', me))
            elif goal == 'submitted' and rv:
                todo.append(('goal_ok', rv))
        elif stage == 'review':
            if not conf:
                todo.append(('goal_write' if goal == 'none' else 'goal_submit', me))
            else:
                if self_n < len(conf):
                    todo.append(('self', me))
                if mgr_n < len(conf) and rv:
                    todo.append(('mgr', rv))
            for a in ps:
                if not a['done']:
                    todo.append(('peer', a['reviewer_id']))

        rows.append({
            'user_id': me, 'name': p['name'], 'emp_no': p['emp_no'],
            'dept': p['dept'], 'dept_id': p['department_id'], 'position': p['position'],
            'reviewer_id': rv, 'reviewer_name': p['reviewer_name'],
            'goal': goal, 'goals_n': len(conf), 'self_n': self_n, 'mgr_n': mgr_n,
            'peer_done': peer_done, 'peer_total': len(ps),
            'peer_left': [a['reviewer_name'] for a in ps if not a['done']],
            'todo': todo,
        })
    return rows


def _remind_recent(db, cycle_id):
    """user_id → 마지막 독촉 후 지난 시간(시간 단위)."""
    return {r['user_id']: r['h'] for r in db.execute(
        "SELECT user_id, MIN((julianday('now') - julianday(sent_at)) * 24) AS h "
        "FROM perf_reminders WHERE cycle_id=? GROUP BY user_id", (cycle_id,))}


def _stage_deadline(cycle):
    d = cycle['goal_deadline'] if cycle['stage'] == 'goal' else \
        cycle['review_deadline'] if cycle['stage'] == 'review' else None
    if not d:
        return None, None
    try:
        left = (datetime.strptime(str(d)[:10], '%Y-%m-%d').date() - date.today()).days
    except ValueError:
        return d, None
    return d, left


@app.route('/performance/cycles/<int:cycle_id>/progress')
@manager_or_admin
def performance_progress(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    view  = request.args.get('view', 'late')
    q     = (request.args.get('q') or '').strip()
    dept  = request.args.get('dept', type=int)
    scope = _progress_scope_dept()

    rows = perf_progress(db, cycle, scope)
    stage = cycle['stage']
    active = cycle['status'] == 'active' and stage in ('goal', 'review')

    n = len(rows)
    summary = {
        'total': n,
        'late':  sum(1 for r in rows if r['todo']),
        'goal':  sum(1 for r in rows if r['goal'] == 'confirmed'),
        'self':  sum(1 for r in rows if r['goals_n'] and r['self_n'] >= r['goals_n']),
        'mgr':   sum(1 for r in rows if r['goals_n'] and r['mgr_n'] >= r['goals_n']),
        'peer_done':  sum(r['peer_done'] for r in rows),
        'peer_total': sum(r['peer_total'] for r in rows),
    }
    summary['done'] = n - summary['late']

    # 부서별 진행 — 밀린 사람이 많은 순
    depts = {}
    for r in rows:
        k = r['dept_id'] or 0
        d = depts.setdefault(k, {'id': k, 'name': r['dept'] or '미지정', 'total': 0, 'late': 0})
        d['total'] += 1
        d['late']  += 1 if r['todo'] else 0
    dept_list = sorted(depts.values(), key=lambda d: (-d['late'], d['name']))

    # 받는 사람 기준 밀린 일 수 — "누가 몇 건 막고 있나"
    blockers = {}
    for r in rows:
        for kind, who in r['todo']:
            blockers.setdefault(who, {}).setdefault(kind, 0)
            blockers[who][kind] += 1
    names = {}
    if blockers:
        ids = list(blockers)
        names = {u['id']: u['name'] for u in db.execute(
            'SELECT id, name FROM users WHERE id IN (%s)' % ','.join('?' * len(ids)), ids)}
    top_blockers = sorted(
        ({'id': u, 'name': names.get(u, '—'), 'n': sum(k.values()),
          'kinds': ' · '.join(f'{PERF_TODO[x][0]} {c}' for x, c in k.items())}
         for u, k in blockers.items() if sum(k.values()) > 1),
        key=lambda b: (-b['n'], b['name']))[:6]

    def _keep(r):
        if q and q not in (r['name'] or '') and q not in (r['dept'] or ''):
            return False
        if dept is not None and (r['dept_id'] or 0) != dept:
            return False
        if view == 'late':
            return bool(r['todo'])
        if view == 'done':
            return not r['todo']
        return True
    shown = [r for r in rows if _keep(r)]

    deadline, left = _stage_deadline(cycle)
    return render_template('performance/progress.html',
                           cycle=cycle, rows=shown, summary=summary, view=view, q=q,
                           dept=dept, dept_list=dept_list, top_blockers=top_blockers,
                           recent=_remind_recent(db, cycle_id), remind_hours=PERF_REMIND_HOURS,
                           todo_label={k: v[0] for k, v in PERF_TODO.items()},
                           stage_label=CYCLE_STAGE_LABEL.get(stage, stage), active=active,
                           deadline=deadline, left=left,
                           is_admin=session.get('user_role') == 'admin',
                           active_page='performance_cycles')


@app.route('/performance/cycles/<int:cycle_id>/progress/remind', methods=['POST'])
@manager_or_admin
def performance_progress_remind(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    back  = dict(cycle_id=cycle_id, view=request.form.get('view') or 'late',
                 q=request.form.get('q') or None, dept=request.form.get('dept') or None)
    if cycle['status'] != 'active' or cycle['stage'] not in ('goal', 'review'):
        flash('독촉은 목표 수립·평가 진행 단계에서만 보낼 수 있습니다.', 'error')
        return redirect(url_for('performance_progress', **back))

    rows = perf_progress(db, cycle, _progress_scope_dept())
    picked = {int(x) for x in request.form.getlist('user_ids') if str(x).isdigit()}
    if request.form.get('scope') == 'picked':
        if not picked:
            flash('독촉할 사람을 선택해 주세요.', 'error')
            return redirect(url_for('performance_progress', **back))
        rows = [r for r in rows if r['user_id'] in picked]

    per = {}   # 받는 사람 → {kind: 건수}
    for r in rows:
        for kind, who in r['todo']:
            per.setdefault(who, {}).setdefault(kind, 0)
            per[who][kind] += 1
    if not per:
        flash('보낼 사람이 없습니다. 남은 일이 없거나 볼 수 있는 범위 밖입니다.', 'error')
        return redirect(url_for('performance_progress', **back))

    recent = _remind_recent(db, cycle_id)
    active_ids = {u['id'] for u in db.execute(
        "SELECT id FROM users WHERE status='active' AND id IN (%s)" % ','.join('?' * len(per)),
        list(per))}
    deadline, left = _stage_deadline(cycle)
    tail = ''
    if deadline:
        tail = f' 마감 {deadline}' + (f' (D-{left})' if left is not None and left >= 0 else
                                      ' (마감 지남)' if left is not None else '')

    sent = skipped = 0
    for who, kinds in per.items():
        if who not in active_ids:
            continue
        if who in recent and recent[who] < PERF_REMIND_HOURS:
            skipped += 1
            continue
        parts = []
        for kind, c in kinds.items():
            label = PERF_TODO[kind][0]
            parts.append(f'{label} {c}명' if PERF_TODO[kind][1] != 'self' else label)
        first = next(iter(kinds))
        add_notification(who, 'action', 'perf', f'{cycle["name"]} 할 일이 남았습니다',
                         '남은 일: ' + ' · '.join(parts) + '.' + tail,
                         link=PERF_TODO[first][2].format(c=cycle_id))
        db.execute('INSERT INTO perf_reminders (cycle_id, user_id, stage, items, sent_by) '
                   'VALUES (?,?,?,?,?)',
                   (cycle_id, who, cycle['stage'], ','.join(kinds), session['user_id']))
        sent += 1
    db.commit()
    if sent:
        log_audit('create', 'performance', None,
                  f'{cycle["name"]} {CYCLE_STAGE_LABEL[cycle["stage"]]} 독촉 {sent}명')
        msg = f'{sent}명에게 독촉 알림을 보냈습니다.'
        if skipped:
            msg += f' 최근 {PERF_REMIND_HOURS}시간 안에 받은 {skipped}명은 건너뛰었습니다.'
    else:
        msg = f'모두 최근 {PERF_REMIND_HOURS}시간 안에 독촉을 받아 보내지 않았습니다.'
    flash(msg, 'success' if sent else 'error')
    return redirect(url_for('performance_progress', **back))


# ── P4 캘리브레이션 회의 세팅·사전자료 ─────────────────────────
CAL_LEVELS = {0: '부문', 1: '본부', 2: '실'}
CAL_MIN_GROUP = 5          # 이보다 작은 묶음은 같은 부문의 가장 큰 회의에 합친다
CAL_ATTENDEE_ROLE = {'facilitator': '진행', 'leader': '조직장', 'reviewer': '평가자', 'manual': '추가'}


def _dept_chain_fn(db):
    """부서 id → 최상위부터 내려오는 조직 id 목록."""
    parent = {d['id']: d['parent_id'] for d in db.execute('SELECT id, parent_id FROM departments')}
    cache = {}

    def chain(did):
        if did not in cache:
            out, cur, seen = [], did, set()
            while cur and cur not in seen:
                seen.add(cur)
                out.append(cur)
                cur = parent.get(cur)
            cache[did] = out[::-1]
        return cache[did]
    return chain


def build_calibration_sessions(db, cycle, level, uid):
    """평가 대상자를 조직 단위로 묶어 회의를 만든다. 기존 회의는 지우고 새로 만든다."""
    cid = cycle['id']
    chain = _dept_chain_fn(db)
    names = {d['id']: (d['name'], d['leader_id'])
             for d in db.execute('SELECT id, name, leader_id FROM departments')}
    people = db.execute(
        'SELECT cp.user_id, cp.reviewer_id, u.department_id FROM cycle_participants cp '
        'JOIN users u ON u.id = cp.user_id WHERE cp.cycle_id=? AND cp.included=1', (cid,)).fetchall()

    groups = {}
    for p in people:
        ch = chain(p['department_id']) if p['department_id'] else []
        key = ch[min(level, len(ch) - 1)] if ch else 0
        groups.setdefault(key, []).append(p)

    # 작은 묶음 → 같은 최상위 조직 아래 가장 큰 묶음으로
    for key in sorted(groups, key=lambda k: len(groups[k])):
        if key == 0 or len(groups[key]) >= CAL_MIN_GROUP or key not in groups:
            continue
        root = chain(key)[0]
        same = [k for k in groups if k != key and k and chain(k)[0] == root]
        if same:
            big = max(same, key=lambda k: len(groups[k]))
            groups[big].extend(groups.pop(key))
    # 부서 없는 사람 → 가장 큰 회의
    if 0 in groups and len(groups) > 1:
        orphans = groups.pop(0)
        groups[max(groups, key=lambda k: len(groups[k]))].extend(orphans)

    for s in db.execute('SELECT id FROM calibration_sessions WHERE cycle_id=?', (cid,)).fetchall():
        db.execute('DELETE FROM calibration_session_attendees WHERE session_id=?', (s['id'],))
    db.execute('DELETE FROM calibration_session_members WHERE cycle_id=?', (cid,))
    db.execute('DELETE FROM calibration_sessions WHERE cycle_id=?', (cid,))

    member_ids = {p['user_id'] for p in people}
    made = 0
    for key in sorted(groups, key=lambda k: (k == 0, names.get(k, ('',))[0])):
        members = groups[key]
        name = names[key][0] if key in names else '조직 미지정'
        sid = db.execute(
            'INSERT INTO calibration_sessions (cycle_id, name, org_id, facilitator_id, created_by) '
            'VALUES (?,?,?,?,?)', (cid, f'{name} 캘리브레이션', key or None, uid, uid)).lastrowid
        for p in members:
            db.execute('INSERT INTO calibration_session_members (session_id, cycle_id, user_id) '
                       'VALUES (?,?,?)', (sid, cid, p['user_id']))
        att = [(uid, 'facilitator')]
        leader = names.get(key, (None, None))[1]
        if leader:
            att.append((leader, 'leader'))
        for rv in sorted({p['reviewer_id'] for p in members if p['reviewer_id']}):
            att.append((rv, 'reviewer'))
        for a_uid, role in att:
            db.execute('INSERT OR IGNORE INTO calibration_session_attendees (session_id, user_id, role) '
                       'VALUES (?,?,?)', (sid, a_uid, role))
        made += 1
    db.commit()
    return made, len(member_ids)


def _cal_session_or_404(db, sid):
    s = db.execute('SELECT cs.*, pc.name AS cycle_name, pc.stage, pc.status AS cycle_status, '
                   '       f.name AS facilitator_name '
                   'FROM calibration_sessions cs JOIN performance_cycles pc ON pc.id = cs.cycle_id '
                   'LEFT JOIN users f ON f.id = cs.facilitator_id WHERE cs.id=?', (sid,)).fetchone()
    if not s:
        abort(404)
    return s


def _cal_missing(r):
    """진행 행에서 아직 안 끝난 평가 목록."""
    out = []
    if not r['goals_n']:
        out.append('확정 목표 없음')
    else:
        if r['self_n'] < r['goals_n']:
            out.append('자기평가')
        if r['mgr_n'] < r['goals_n']:
            out.append('평가자 평가')
    if r['peer_total'] and r['peer_done'] < r['peer_total']:
        out.append(f'동료평가 {r["peer_total"] - r["peer_done"]}건')
    return out


def calibration_packet(db, session_row):
    """사전자료 — 회의 대상자별 점수·권고 등급·미완료 항목, 권고 분포."""
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (session_row['cycle_id'],)).fetchone()
    ids = {m['user_id'] for m in db.execute(
        'SELECT user_id FROM calibration_session_members WHERE session_id=?', (session_row['id'],))}
    prog = {r['user_id']: r for r in perf_progress(db, cycle) if r['user_id'] in ids}
    saved = {r['user_id']: r for r in db.execute(
        'SELECT user_id, final_grade, downgrade_reason, note, is_shared FROM calibration_results WHERE cycle_id=?',
        (cycle['id'],))}
    rows = []
    for u in ids:
        p = prog.get(u)
        if not p:
            continue
        c = _calc_calibration_row(db, u, cycle['id'])
        rows.append({**c, 'dept': p['dept'], 'position': p['position'], 'emp_no': p['emp_no'],
                     'reviewer_name': p['reviewer_name'], 'missing': _cal_missing(p),
                     'final_grade': saved[u]['final_grade'] if u in saved else None,
                     'final_reason': (saved[u]['downgrade_reason'] or saved[u]['note'] or '') if u in saved else '',
                     'is_shared': saved[u]['is_shared'] if u in saved else 0})
    rows.sort(key=lambda r: (r['overall'] is None, -(r['overall'] or 0), r['dept'] or '', r['name']))

    gd = get_grade_dist()
    n = len(rows)
    dist = {g: sum(1 for r in rows if r['suggested_grade'] == g) for g in 'SABCD'}
    target = {g: round(n * gd['pct'][g] / 100, 1) for g in 'SABCD'}
    summary = {
        'total': n,
        'ready': sum(1 for r in rows if not r['missing']),
        'missing': sum(1 for r in rows if r['missing']),
        'noscore': sum(1 for r in rows if r['overall'] is None),
        'anomaly': sum(1 for r in rows if r['anomaly']),
        'decided': sum(1 for r in rows if r['final_grade']),
    }
    return cycle, rows, summary, dist, target, gd


CAL_GRADE_NUM = {'S': 5, 'A': 4, 'B': 3, 'C': 2, 'D': 1}


def cal_grade_rule_error(suggested, final, reason):
    """등급 조정 규칙 — 권고보다 낮출 때 최대 1단계, 사유 필수."""
    base = CAL_GRADE_NUM.get(suggested, 3)
    gap = base - CAL_GRADE_NUM[final]
    if gap > 1:
        return f'권고 {suggested or "B"}에서 최대 1단계까지만 낮출 수 있습니다'
    if gap == 1 and not reason:
        return '낮출 때는 조정 사유가 필요합니다'
    return None


def cal_publish_state(db, cycle):
    """결과 공개 가능 여부 — 명단 대상 전원 등급 확정, 조정·평가 단계, 아직 미공개."""
    cid = cycle['id']
    total = db.execute('SELECT COUNT(*) FROM cycle_participants WHERE cycle_id=? AND included=1',
                       (cid,)).fetchone()[0]
    decided = db.execute('SELECT COUNT(*) FROM cycle_participants cp JOIN calibration_results cr '
                         'ON cr.cycle_id=cp.cycle_id AND cr.user_id=cp.user_id '
                         'WHERE cp.cycle_id=? AND cp.included=1', (cid,)).fetchone()[0]
    shared = db.execute('SELECT COUNT(*) FROM calibration_results WHERE cycle_id=? AND is_shared=1',
                        (cid,)).fetchone()[0]
    st = {'total': total, 'decided': decided, 'shared': shared, 'can': False, 'why': ''}
    if shared or cycle['stage'] in ('appeal', 'closed'):
        st['why'] = '이미 결과를 공개했습니다.'
    elif cycle['stage'] not in ('review', 'calibration'):
        st['why'] = '결과 공개는 평가 진행·HR 조정 단계에서만 할 수 있습니다.'
    elif not total:
        st['why'] = '평가 명단이 없습니다.'
    elif decided < total:
        st['why'] = f'등급이 정해지지 않은 대상자가 {total - decided}명 있습니다.'
    else:
        st['can'] = True
    return st


def _cal_can_view(db, sid):
    if session.get('user_role') == 'admin':
        return True
    return bool(db.execute('SELECT 1 FROM calibration_session_attendees WHERE session_id=? AND user_id=?',
                           (sid, session.get('user_id'))).fetchone())


@app.route('/performance/cycles/<int:cycle_id>/calibration-setup', methods=['GET', 'POST'])
@admin_required
def calibration_setup(cycle_id):
    db    = get_db()
    cycle = _roster_cycle(db, cycle_id)
    uid   = session['user_id']

    if request.method == 'POST':
        action = request.form.get('action')
        sid = request.form.get('session_id', type=int)
        srow = db.execute('SELECT * FROM calibration_sessions WHERE id=? AND cycle_id=?',
                          (sid, cycle_id)).fetchone() if sid else None

        if action == 'build':
            level = request.form.get('level', type=int)
            if level not in CAL_LEVELS:
                level = 1
            n_in = db.execute('SELECT COUNT(*) FROM cycle_participants WHERE cycle_id=? AND included=1',
                              (cycle_id,)).fetchone()[0]
            if not n_in:
                flash('평가 명단이 없습니다. 명단부터 만들어 주세요.', 'error')
            else:
                made, people = build_calibration_sessions(db, cycle, level, uid)
                log_audit('create', 'performance', None, f'{cycle["name"]} 캘리브레이션 회의 {made}개 구성')
                flash(f'{CAL_LEVELS[level]} 단위로 회의 {made}개를 만들었습니다. 대상 {people}명.', 'success')

        elif action == 'save' and srow:
            name = (request.form.get('name') or '').strip()[:60] or srow['name']
            meet_date = (request.form.get('meet_date') or '').strip() or None
            meet_time = (request.form.get('meet_time') or '').strip()[:5] or None
            location  = (request.form.get('location') or '').strip()[:80] or None
            fac = request.form.get('facilitator_id', type=int) or srow['facilitator_id']
            if meet_date:
                try:
                    datetime.strptime(meet_date, '%Y-%m-%d')
                except ValueError:
                    meet_date = None
            db.execute('UPDATE calibration_sessions SET name=?, meet_date=?, meet_time=?, location=?, '
                       'facilitator_id=? WHERE id=?', (name, meet_date, meet_time, location, fac, sid))
            if fac:
                db.execute("DELETE FROM calibration_session_attendees WHERE session_id=? AND role='facilitator'", (sid,))
                db.execute("INSERT INTO calibration_session_attendees (session_id, user_id, role) VALUES (?,?,'facilitator') "
                           "ON CONFLICT(session_id, user_id) DO UPDATE SET role='facilitator'", (sid, fac))
            db.commit()
            flash(f'{name} 정보를 저장했습니다.', 'success')

        elif action == 'add_attendee' and srow:
            au = request.form.get('user_id', type=int)
            if au and db.execute("SELECT 1 FROM users WHERE id=? AND status='active'", (au,)).fetchone():
                db.execute("INSERT OR IGNORE INTO calibration_session_attendees (session_id, user_id, role) "
                           "VALUES (?,?,'manual')", (sid, au))
                db.commit()
                flash('참석자를 추가했습니다.', 'success')

        elif action == 'remove_attendee' and srow:
            au = request.form.get('user_id', type=int)
            if au == srow['facilitator_id']:
                flash('진행자는 뺄 수 없습니다. 진행자를 먼저 바꿔 주세요.', 'error')
            else:
                db.execute('DELETE FROM calibration_session_attendees WHERE session_id=? AND user_id=?', (sid, au))
                db.commit()
                flash('참석자에서 뺐습니다.', 'success')

        elif action == 'move':
            mu = request.form.get('user_id', type=int)
            to = request.form.get('to_session', type=int)
            ok_to = db.execute('SELECT id, name FROM calibration_sessions WHERE id=? AND cycle_id=?',
                               (to, cycle_id)).fetchone()
            ok_u = db.execute('SELECT 1 FROM cycle_participants WHERE cycle_id=? AND user_id=? AND included=1',
                              (cycle_id, mu)).fetchone()
            if ok_to and ok_u:
                db.execute('INSERT INTO calibration_session_members (session_id, cycle_id, user_id) VALUES (?,?,?) '
                           'ON CONFLICT(cycle_id, user_id) DO UPDATE SET session_id=excluded.session_id',
                           (to, cycle_id, mu))
                db.commit()
                flash(f'{ok_to["name"]}(으)로 옮겼습니다.', 'success')
            else:
                flash('옮길 사람이나 회의를 찾을 수 없습니다.', 'error')

        elif action == 'notify' and srow:
            if not srow['meet_date']:
                flash('회의 날짜를 먼저 저장해 주세요.', 'error')
            else:
                att = db.execute("SELECT a.user_id FROM calibration_session_attendees a JOIN users u ON u.id=a.user_id "
                                 "WHERE a.session_id=? AND u.status='active'", (sid,)).fetchall()
                when = srow['meet_date'] + (f' {srow["meet_time"]}' if srow['meet_time'] else '')
                n_mem = db.execute('SELECT COUNT(*) FROM calibration_session_members WHERE session_id=?',
                                   (sid,)).fetchone()[0]
                for a in att:
                    add_notification(a['user_id'], 'info', 'perf', f'캘리브레이션 회의 안내 · {srow["name"]}',
                                     f'{when}' + (f' · {srow["location"]}' if srow['location'] else '') +
                                     f' · 대상 {n_mem}명. 회의 전에 사전자료를 확인해 주세요.',
                                     link=url_for('calibration_session', sid=sid))
                db.execute('UPDATE calibration_sessions SET notified_at=CURRENT_TIMESTAMP WHERE id=?', (sid,))
                db.commit()
                log_audit('create', 'performance', None, f'{srow["name"]} 참석자 안내 {len(att)}명')
                flash(f'참석자 {len(att)}명에게 회의 안내를 보냈습니다.', 'success')

        elif action == 'publish':
            pub = cal_publish_state(db, cycle)
            if not pub['can']:
                flash(pub['why'], 'error')
            else:
                ok_pub, msg = publish_calibration_results(db, cycle_id)
                flash(msg, 'success' if ok_pub else 'error')
        return redirect(url_for('calibration_setup', cycle_id=cycle_id))

    sessions = db.execute(
        'SELECT cs.*, f.name AS facilitator_name, '
        '       (SELECT COUNT(*) FROM calibration_session_members m WHERE m.session_id=cs.id) AS n '
        'FROM calibration_sessions cs LEFT JOIN users f ON f.id = cs.facilitator_id '
        'WHERE cs.cycle_id=? ORDER BY cs.meet_date IS NULL, cs.meet_date, cs.meet_time, cs.name',
        (cycle_id,)).fetchall()
    rows = perf_progress(db, cycle)
    by_user = {r['user_id']: r for r in rows}
    member_map = {m['user_id']: m['session_id'] for m in db.execute(
        'SELECT user_id, session_id FROM calibration_session_members WHERE cycle_id=?', (cycle_id,))}
    stats = {s['id']: {'missing': 0, 'decided': 0} for s in sessions}
    decided = {r['user_id'] for r in db.execute('SELECT user_id FROM calibration_results WHERE cycle_id=?', (cycle_id,))}
    for u, s_id in member_map.items():
        if s_id not in stats or u not in by_user:
            continue
        if _cal_missing(by_user[u]):
            stats[s_id]['missing'] += 1
        if u in decided:
            stats[s_id]['decided'] += 1
    attendees = {}
    for a in db.execute(
            'SELECT a.session_id, a.user_id, a.role, u.name FROM calibration_session_attendees a '
            'JOIN users u ON u.id = a.user_id JOIN calibration_sessions cs ON cs.id = a.session_id '
            "WHERE cs.cycle_id=? ORDER BY CASE a.role WHEN 'facilitator' THEN 0 WHEN 'leader' THEN 1 "
            "WHEN 'reviewer' THEN 2 ELSE 3 END, u.name", (cycle_id,)):
        attendees.setdefault(a['session_id'], []).append(a)
    unassigned = [r for r in rows if r['user_id'] not in member_map]
    people = db.execute("SELECT u.id, u.name, d.name AS dept FROM users u "
                        "LEFT JOIN departments d ON d.id = u.department_id "
                        "WHERE u.status='active' AND u.role != 'guest' ORDER BY u.name").fetchall()
    return render_template('performance/calibration_setup.html',
                           cycle=cycle, sessions=sessions, stats=stats, attendees=attendees,
                           unassigned=unassigned, roster_n=len(rows), people=people,
                           members=sorted(rows, key=lambda r: r['name']), member_map=member_map,
                           levels=CAL_LEVELS, role_label=CAL_ATTENDEE_ROLE,
                           missing_total=sum(1 for r in rows if _cal_missing(r)),
                           pub=cal_publish_state(db, cycle),
                           active_page='performance_cycles')


@app.route('/performance/calibration/sessions/<int:sid>')
@login_required
def calibration_session(sid):
    db = get_db()
    s  = _cal_session_or_404(db, sid)
    if not _cal_can_view(db, sid):
        abort(403)
    cycle, rows, summary, dist, target, gd = calibration_packet(db, s)
    view = request.args.get('view', 'all')
    shown = [r for r in rows if view != 'missing' or r['missing']]
    shown = [r for r in shown if view != 'anomaly' or r['anomaly']]

    if request.args.get('format') == 'csv':
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['이름', '사번', '조직', '직급', '평가자', '자기평가', '평가자 평가', '동료평가',
                    '종합', '권고 등급', '확정 등급', '조정 사유', '미완료', '이상 신호'])
        for r in rows:
            w.writerow([r['name'], r['emp_no'] or '', r['dept'] or '', r['position'] or '',
                        r['reviewer_name'] or '', r['self_avg'] or '', r['mgr_avg'] or '',
                        r['peer_avg'] or '', r['overall'] or '', r['suggested_grade'] or '',
                        r['final_grade'] or '', r['final_reason'], ' · '.join(r['missing']), r['anomaly'] or ''])
        log_audit('download', 'performance', None, f'{s["name"]} 사전자료 CSV ({len(rows)}명)')
        return Response('﻿' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition':
                                 f"attachment; filename*=UTF-8''calibration_{sid}.csv"})

    attendees = db.execute(
        'SELECT a.role, u.name, d.name AS dept FROM calibration_session_attendees a '
        'JOIN users u ON u.id = a.user_id LEFT JOIN departments d ON d.id = u.department_id '
        "WHERE a.session_id=? ORDER BY CASE a.role WHEN 'facilitator' THEN 0 WHEN 'leader' THEN 1 "
        "WHEN 'reviewer' THEN 2 ELSE 3 END, u.name", (sid,)).fetchall()
    return render_template('performance/calibration_session.html',
                           s=s, cycle=cycle, rows=shown, summary=summary, dist=dist, target=target,
                           gd=gd, view=view, attendees=attendees, role_label=CAL_ATTENDEE_ROLE,
                           is_admin=session.get('user_role') == 'admin',
                           active_page='performance_cycles')


def _read_upload_csv(f):
    import csv, io
    b = f.read()
    for enc in ('utf-8-sig', 'cp949'):
        try:
            text = b.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        return None
    return list(csv.DictReader(io.StringIO(text)))


@app.route('/performance/calibration/sessions/<int:sid>/results', methods=['GET', 'POST'])
@admin_required
def calibration_session_results(sid):
    """P5 — 회의 결과 일괄 입력. 화면에서 등급을 고르거나, 사전자료 CSV 에 확정 등급을 채워 올린다."""
    db = get_db()
    s  = _cal_session_or_404(db, sid)
    cycle, rows, summary, dist, target, gd = calibration_packet(db, s)
    rows.sort(key=lambda r: (r['dept'] or '', r['name']))
    locked = None
    if any(r['is_shared'] for r in rows) or cycle['stage'] in ('appeal', 'closed'):
        locked = '결과를 이미 공개해 수정할 수 없습니다. 바꿀 일이 있으면 이의신청으로 처리하세요.'
    elif cycle['stage'] not in ('review', 'calibration'):
        locked = '결과 입력은 평가 진행·HR 조정 단계에서만 할 수 있습니다.'

    vals = {r['user_id']: {'grade': r['final_grade'] or '', 'reason': r['final_reason'] or ''} for r in rows}
    errors, notice = {}, None

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if locked:
            flash(locked, 'error')
            return redirect(url_for('calibration_session_results', sid=sid))

        if action == 'upload':
            f = request.files.get('file')
            data = _read_upload_csv(f) if f and f.filename else None
            if not data or '확정 등급' not in data[0]:
                flash('사전자료 CSV 형식의 파일을 올려 주세요. "확정 등급" 칸이 필요합니다.', 'error')
                return redirect(url_for('calibration_session_results', sid=sid))
            by_no = {str(r['emp_no']): r for r in rows if r['emp_no']}
            names = {}
            for r in rows:
                names.setdefault(r['name'], []).append(r)
            filled, skipped = 0, []
            for line in data:
                no = (line.get('사번') or '').strip()
                nm = (line.get('이름') or '').strip()
                r = by_no.get(no) or (names[nm][0] if len(names.get(nm, [])) == 1 else None)
                g = (line.get('확정 등급') or '').strip().upper()
                if not g:
                    continue
                if not r:
                    skipped.append(nm or no or '?')
                    continue
                if g not in CAL_GRADE_NUM:
                    errors[r['user_id']] = f'"{g}" 는 등급이 아닙니다'
                vals[r['user_id']] = {'grade': g if g in CAL_GRADE_NUM else '',
                                      'reason': (line.get('조정 사유') or '').strip()[:200]}
                filled += 1
            notice = f'파일에서 {filled}명 등급을 불러왔습니다. 확인 후 [저장]을 눌러야 반영됩니다.'
            if skipped:
                notice += f' 이 회의에 없는 {len(skipped)}명은 건너뛰었습니다: ' + ', '.join(skipped[:5])
        else:
            picked = {}
            for r in rows:
                u = r['user_id']
                g = (request.form.get(f'grade_{u}') or '').strip().upper()
                reason = (request.form.get(f'reason_{u}') or '').strip()[:200]
                vals[u] = {'grade': g, 'reason': reason}
                if not g:
                    continue
                if g not in CAL_GRADE_NUM:
                    errors[u] = '등급을 다시 골라 주세요'
                    continue
                err = cal_grade_rule_error(r['suggested_grade'], g, reason)
                if err:
                    errors[u] = err
                else:
                    picked[u] = (g, reason)
            if not errors and gd['mode'] == 'forced' and picked:
                groups, member = grade_dist_groups(db, cycle['id'], gd,
                                                   override={u: g for u, (g, _) in picked.items()})
                over = [f'{x["label"]} ' + ', '.join(x['over']) for x in groups if x['over']]
                if over:
                    errors[0] = '강제 배분 상한 초과 — ' + ' / '.join(over[:3])
            if not errors:
                row_by = {r['user_id']: r for r in rows}
                changed = 0
                for u, (g, reason) in picked.items():
                    r = row_by[u]
                    if r['final_grade'] == g and (r['final_reason'] or '') == reason:
                        continue
                    down = CAL_GRADE_NUM[g] < CAL_GRADE_NUM.get(r['suggested_grade'], 3)
                    summary_text = generate_calibration_summary(r['name'], r['self_avg'], r['peer_avg'],
                                                                r['mgr_avg'], r['upward_avg'])
                    db.execute('''
                        INSERT INTO calibration_results
                          (cycle_id, user_id, self_avg, peer_avg, mgr_avg, upward_avg,
                           suggested_grade, final_grade, summary_text, note,
                           downgrade_reason, is_shared, decided_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)
                        ON CONFLICT(cycle_id, user_id) DO UPDATE SET
                          self_avg=excluded.self_avg, peer_avg=excluded.peer_avg,
                          mgr_avg=excluded.mgr_avg, upward_avg=excluded.upward_avg,
                          suggested_grade=excluded.suggested_grade, final_grade=excluded.final_grade,
                          summary_text=excluded.summary_text, note=excluded.note,
                          downgrade_reason=excluded.downgrade_reason,
                          decided_by=excluded.decided_by, decided_at=CURRENT_TIMESTAMP
                    ''', (cycle['id'], u, r['self_avg'], r['peer_avg'], r['mgr_avg'], r['upward_avg'],
                          r['suggested_grade'], g, summary_text,
                          None if down else (reason or None), reason if down else None,
                          session['user_id']))
                    changed += 1
                db.commit()
                if changed:
                    log_audit('update', 'performance', None, f'{s["name"]} 결과 입력 {changed}명')
                left = sum(1 for r in rows if r['user_id'] not in picked and not r['final_grade'])
                flash(f'{changed}명 등급을 저장했습니다.' + (f' 아직 {left}명 남았습니다.' if left else ''),
                      'success')
                return redirect(url_for('calibration_session_results', sid=sid))

    counts = {g: sum(1 for v in vals.values() if v['grade'] == g) for g in 'SABCD'}
    return render_template('performance/calibration_results.html',
                           s=s, cycle=cycle, rows=rows, vals=vals, errors=errors, notice=notice,
                           locked=locked, gd=gd, target=target, counts=counts,
                           filled=sum(1 for v in vals.values() if v['grade']),
                           active_page='performance_cycles')


# ── Onboarding Dashboard ────────────────────────────────────
@app.route('/me/onboarding')
@login_required
def me_onboarding():
    db  = get_db()
    uid = session['user_id']
    # 인사팀 미리보기: 입사자가 보게 될 화면을 그대로(체크는 못 함)
    preview = None
    pid = request.args.get('user', type=int)
    if pid and pid != uid and session.get('user_role') in ('admin', 'recruiter'):
        preview = db.execute('SELECT id, name FROM users WHERE id=?', (pid,)).fetchone()
        if not preview:
            abort(404)
        uid = pid

    content_tasks = workplace.onboarding_tasks(db)
    rows = db.execute("SELECT task_key, done FROM onboarding_progress WHERE user_id=?", (uid,)).fetchall()
    ckeys = {t['key'] for t in content_tasks}
    # 회사 자료의 체크리스트로 맞춘다 — 자료에 없는 항목을 이미 체크했으면(진행 기록이 사라지므로) 그대로 둔다
    if rows and content_tasks and not any(r['done'] for r in rows if r['task_key'] not in ckeys):
        for i, t in enumerate(content_tasks):
            db.execute("INSERT OR IGNORE INTO onboarding_progress (user_id, task_key, task_label, category, sort_order) "
                       "VALUES (?,?,?,?,?)", (uid, t['key'], t['label'], t.get('category') or 'general', i + 1))
            db.execute("UPDATE onboarding_progress SET task_label=?, category=?, sort_order=? WHERE user_id=? AND task_key=?",
                       (t['label'], t.get('category') or 'general', i + 1, uid, t['key']))
        db.execute("DELETE FROM onboarding_progress WHERE user_id=? AND task_key NOT IN (%s)" % ','.join('?' * len(ckeys)),
                   (uid, *ckeys))
        db.commit()
    if not rows:
        from integrations.dispatcher import _seed_onboarding_tasks
        _seed_onboarding_tasks(get_tenant_db_path(session.get('tenant_id', 1)), uid)

    plan = workplace.onboarding_plan(db, uid)
    vals = plan['values'] if plan else {}
    due = plan['due'] if plan else {}
    today = date.today()
    tasks = []
    for t in db.execute("SELECT * FROM onboarding_progress WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall():
        t = dict(t)
        t['label'] = workplace.fill(t['task_label'], vals) if vals else t['task_label']
        d = due.get(t['task_key'])
        t['due'] = d
        t['due_label'] = '%s(%s)' % (d.strftime('%m.%d'), workplace.WEEKDAY_KO[d.weekday()]) if d else ''
        # 기한 지남은 입사 두 달 안에만 — 오래 다닌 직원에게 빨간 줄을 늘어놓지 않는다
        t['late'] = bool(d and d < today and not t['done'] and (today - d).days <= 60)
        tasks.append(t)

    # 기한(며칠째까지) 순으로 묶는다 — 자료가 없으면 예전처럼 분류별
    from collections import OrderedDict
    grouped = OrderedDict()
    if due:
        for t in sorted(tasks, key=lambda t: (t['due'] is None, t['due'] or today, t['sort_order'])):
            key = t['due_label'] or '기한 없음'
            grouped.setdefault(key, {'label': key, 'tasks': []})['tasks'].append(t)
    else:
        for t in tasks:
            cat = t['category']
            grouped.setdefault(cat, {'label': workplace.TASK_CATEGORIES.get(cat, cat), 'tasks': []})['tasks'].append(t)

    total = len(tasks)
    done  = sum(1 for t in tasks if t['done'])
    me = db.execute("SELECT jira_epic_key, hire_date FROM users WHERE id=?", (uid,)).fetchone()
    buddy = plan['who']['buddy'] if plan else db.execute(
        "SELECT u.name, u.email, d.name AS dept, p.name AS pos "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "WHERE u.id=(SELECT buddy_id FROM users WHERE id=?)", (uid,)).fetchone()

    return render_template('me/onboarding.html',
        plan=plan, grouped=grouped, total=total, done=done,
        pct=int(done / total * 100) if total else 0,
        late=sum(1 for t in tasks if t['late']),
        buddy=buddy, manager=plan['who']['manager'] if plan else None,
        jira_epic_key=me['jira_epic_key'] if me else None,
        hire_date=me['hire_date'] if me else None, preview=preview, active_page='me_onboarding')


@app.route('/me/onboarding/<task_key>/done', methods=['POST'])
@login_required
def onboarding_task_done(task_key):
    uid   = session['user_id']
    is_done = request.form.get('done', '1') == '1'
    db = get_db()
    from datetime import datetime as _dt
    db.execute(
        "UPDATE onboarding_progress SET done=?, done_at=? WHERE user_id=? AND task_key=?",
        (1 if is_done else 0, _dt.now().isoformat() if is_done else None, uid, task_key)
    )
    db.commit()
    return ('', 204)


# ── Profile ─────────────────────────────────────────────────
@app.route('/me/benefits')
@login_required
def me_benefits():
    db  = get_db()
    uid = session['user_id']

    # 회사가 활성화한 복리후생 항목
    configs = db.execute(
        "SELECT * FROM benefit_configs WHERE enabled=1 ORDER BY key"
    ).fetchall()

    # 개인 오버라이드 (금액 조정)
    overrides = {
        r['benefit_key']: r['amount']
        for r in db.execute(
            "SELECT * FROM employee_benefit_overrides WHERE user_id=?", (uid,)
        ).fetchall()
    }

    # 항목별 금액 계산
    items = []
    total_monthly     = 0
    total_monthly_tax = 0
    for cfg in configs:
        key  = cfg['key']
        meta = BENEFIT_CATALOG.get(key, {})
        if not meta:
            continue
        amount = overrides.get(key, cfg['amount'] or meta.get('default_amount', 0))
        is_tax_exempt = meta.get('tax_exempt', False)
        items.append({
            'key':        key,
            'name':       meta['name'],
            'icon':       meta.get('icon', 'fa-gift'),
            'amount':     amount,
            'tax_exempt': is_tax_exempt,
            'monthly_limit': meta.get('monthly_limit'),
            'legal_basis': meta.get('legal_basis', ''),
            'description': meta.get('description', ''),
            'conditions':  meta.get('conditions'),
            'payment_type': meta.get('payment_type', 'monthly_fixed'),
        })
        if meta.get('payment_type') == 'monthly_fixed':
            total_monthly += amount
            if not is_tax_exempt:
                total_monthly_tax += amount

    # 직원 기본 정보 (급여 조회)
    user = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id=p.id '
        'WHERE u.id=?', (uid,)
    ).fetchone()

    # 최근 급여명세서에서 실제 지급된 복리후생 확인
    last_payslip = db.execute(
        "SELECT * FROM payslips WHERE user_id=? AND status='confirmed' ORDER BY year DESC, month DESC LIMIT 1",
        (uid,)
    ).fetchone()

    # 복지포인트 데이터
    from datetime import date
    this_year = date.today().year

    wp_balance = db.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM welfare_point_ledger WHERE user_id=?", (uid,)
    ).fetchone()[0]

    cfg_row = db.execute("SELECT welfare_point_annual FROM company_config LIMIT 1").fetchone()
    wp_annual_limit = int(cfg_row['welfare_point_annual']) if cfg_row and cfg_row['welfare_point_annual'] else 500000

    wp_granted_this_year = db.execute(
        "SELECT COALESCE(SUM(delta),0) FROM welfare_point_ledger "
        "WHERE user_id=? AND delta>0 AND strftime('%Y', created_at)=?",
        (uid, str(this_year))
    ).fetchone()[0]

    wp_history = db.execute(
        "SELECT * FROM welfare_point_ledger WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (uid,)
    ).fetchall()

    # 미완료 Enrollment Event
    enrollment_event = db.execute(
        "SELECT * FROM benefit_enrollment_events WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
        (uid,)
    ).fetchone()

    return render_template('me/benefits.html',
                           items=items,
                           total_monthly=total_monthly,
                           total_monthly_tax=total_monthly_tax,
                           user=user,
                           last_payslip=last_payslip,
                           wp_balance=wp_balance,
                           wp_annual_limit=wp_annual_limit,
                           wp_granted_this_year=wp_granted_this_year,
                           wp_history=wp_history,
                           enrollment_event=enrollment_event,
                           this_year=this_year,
                           active_page='me_benefits')


@app.route('/me/benefits/enrollment/<int:eid>/complete', methods=['POST'])
@login_required
def enrollment_complete(eid):
    db  = get_db()
    uid = session['user_id']
    ev  = db.execute(
        "SELECT * FROM benefit_enrollment_events WHERE id=? AND user_id=?", (eid, uid)
    ).fetchone()
    if ev:
        db.execute(
            "UPDATE benefit_enrollment_events SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (eid,)
        )
        db.commit()
        flash('복리후생 등록이 완료되었습니다.', 'success')
    return redirect(url_for('me_benefits'))


@app.route('/admin/welfare-points', methods=['GET', 'POST'])
@login_required
def admin_welfare_points():
    if session.get('user_role') != 'admin':
        return redirect(url_for('dashboard'))
    db = get_db()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'grant_all':
            # 전 직원 일괄 연간 지급
            from datetime import date
            this_year = date.today().year
            cfg = db.execute("SELECT welfare_point_annual FROM company_config LIMIT 1").fetchone()
            amount = int(cfg['welfare_point_annual']) if cfg else 500000

            employees = db.execute(
                "SELECT id FROM users WHERE status='active' AND role != 'guest'"
            ).fetchall()
            count = 0
            for emp in employees:
                uid = emp['id']
                # 올해 이미 연간 지급 받았는지 확인
                already = db.execute(
                    "SELECT id FROM welfare_point_ledger "
                    "WHERE user_id=? AND reason LIKE '%연간%' AND strftime('%Y',created_at)=?",
                    (uid, str(this_year))
                ).fetchone()
                if already:
                    continue
                current_balance = db.execute(
                    "SELECT COALESCE(SUM(delta),0) FROM welfare_point_ledger WHERE user_id=?", (uid,)
                ).fetchone()[0]
                new_balance = current_balance + amount
                db.execute(
                    "INSERT INTO welfare_point_ledger (user_id, delta, reason, balance_after) VALUES (?,?,?,?)",
                    (uid, amount, f'{this_year}년 연간 복지포인트 지급', new_balance)
                )
                add_notification(uid, 'info', 'action',
                    f'{this_year}년 복지포인트 지급',
                    f'{amount:,}원의 복지포인트가 지급되었습니다. 잔액: {new_balance:,}원',
                    url_for('me_benefits'))
                count += 1
            db.commit()
            flash(f'{count}명에게 복지포인트 {amount:,}원이 일괄 지급되었습니다.', 'success')

        elif action == 'grant_one':
            uid    = request.form.get('user_id', type=int)
            amount = request.form.get('amount', type=int)
            reason = request.form.get('reason', '').strip() or '수동 지급'
            if uid and amount:
                current = db.execute(
                    "SELECT COALESCE(SUM(delta),0) FROM welfare_point_ledger WHERE user_id=?", (uid,)
                ).fetchone()[0]
                new_balance = current + amount
                db.execute(
                    "INSERT INTO welfare_point_ledger (user_id, delta, reason, balance_after) VALUES (?,?,?,?)",
                    (uid, amount, reason, new_balance)
                )
                add_notification(uid, 'info', 'action',
                    '복지포인트 지급',
                    f'{amount:,}원의 복지포인트가 지급되었습니다. 잔액: {new_balance:,}원',
                    url_for('me_benefits'))
                db.commit()
                flash(f'복지포인트 {amount:,}원 지급 완료.', 'success')

        elif action == 'update_annual':
            amount = request.form.get('welfare_point_annual', type=int)
            if amount:
                db.execute("UPDATE company_config SET welfare_point_annual=?", (amount,))
                db.commit()
                flash(f'연간 복지포인트 기준액이 {amount:,}원으로 변경되었습니다.', 'success')

        return redirect(url_for('admin_welfare_points'))

    from datetime import date
    this_year = date.today().year
    cfg = db.execute("SELECT welfare_point_annual FROM company_config LIMIT 1").fetchone()
    wp_annual = int(cfg['welfare_point_annual']) if cfg else 500000

    # 직원별 잔액 현황
    employees = db.execute(
        "SELECT u.id, u.name, u.emp_no, d.name AS dept_name, "
        "  COALESCE((SELECT SUM(delta) FROM welfare_point_ledger WHERE user_id=u.id), 0) AS balance, "
        "  COALESCE((SELECT SUM(delta) FROM welfare_point_ledger "
        "            WHERE user_id=u.id AND delta>0 AND strftime('%Y',created_at)=?), 0) AS granted_this_year "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "WHERE u.status='active' AND u.role != 'guest' ORDER BY u.name",
        (str(this_year),)
    ).fetchall()

    not_granted = [e for e in employees if e['granted_this_year'] == 0]

    return render_template('admin/welfare_points.html',
                           employees=employees,
                           not_granted=not_granted,
                           wp_annual=wp_annual,
                           this_year=this_year,
                           active_page='admin_welfare_points')


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db    = get_db()
    uid   = session['user_id']
    user  = db.execute(
        'SELECT u.*, d.name AS dept_name, p.name AS pos_name FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id WHERE u.id=?', (uid,)
    ).fetchone()
    error = None
    msg   = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_info':
            phone              = request.form.get('phone', '').strip() or None
            address            = request.form.get('address', '').strip() or None
            emergency_name     = request.form.get('emergency_name', '').strip() or None
            emergency_phone    = request.form.get('emergency_phone', '').strip() or None
            emergency_relation = request.form.get('emergency_relation', '').strip() or None
            db.execute(
                'UPDATE users SET phone=?, address=?, emergency_name=?, emergency_phone=?, emergency_relation=? WHERE id=?',
                (phone, address, emergency_name, emergency_phone, emergency_relation, uid)
            )
            db.commit()
            # 온보딩 체크리스트 'talentcore_login' 자동 완료
            from datetime import datetime as _dt
            db.execute(
                "UPDATE onboarding_progress SET done=1, done_at=? WHERE user_id=? AND task_key='talentcore_login' AND done=0",
                (_dt.now().isoformat(), uid)
            )
            db.commit()
            msg = '정보가 저장되었습니다.'
            user = db.execute(
                'SELECT u.*, d.name AS dept_name, p.name AS pos_name FROM users u '
                'LEFT JOIN departments d ON u.department_id = d.id '
                'LEFT JOIN positions p ON u.position_id = p.id WHERE u.id=?', (uid,)
            ).fetchone()

        elif action == 'change_password':
            current_pw  = request.form.get('current_password', '')
            new_pw      = request.form.get('new_password', '')
            confirm_pw  = request.form.get('confirm_password', '')
            if not check_password_hash(user['password_hash'], current_pw):
                error = '현재 비밀번호가 올바르지 않습니다.'
            elif validate_password(new_pw):
                error = validate_password(new_pw)
            elif new_pw != confirm_pw:
                error = '새 비밀번호와 확인 비밀번호가 일치하지 않습니다.'
            else:
                db.execute(
                    'UPDATE users SET password_hash=? WHERE id=?',
                    (generate_password_hash(new_pw), uid)
                )
                db.commit()
                msg = '비밀번호가 변경되었습니다.'

    return render_template('profile.html', user=user, error=error, msg=msg,
                           active_page='profile')


# ── Recruit ─────────────────────────────────────────────────
STAGES = [
    ('review',       '서류 검토'),
    ('screening',    '리크루터 스크리닝'),
    ('inter1',       '1차 인터뷰'),
    ('kickoff',      '킥오프 미팅'),
    ('inter2',       '2차 인터뷰'),
    ('debrief',      '디브리프 미팅'),
    ('offer',        '오퍼'),
    ('accepted',     '최종 합격'),      # 후보자가 오퍼 수락
    ('rejected',     '오퍼 거절'),      # 후보자가 오퍼 거절 (후보자 의사결정)
    ('disqualified', '불합격'),         # 회사가 후보자 탈락 처리 (어느 단계에서든)
]
STAGE_MAP     = dict(STAGES)
# 진행 중인 단계 (칸반 열 기준) — 터미널 3종 제외
ACTIVE_STAGES = [s for s in STAGES if s[0] not in ('accepted', 'rejected', 'disqualified')]
# 터미널 단계 (재진입 불가)
TERMINAL_STAGES = {'accepted', 'rejected', 'disqualified'}

STAGE_COLORS = {
    'review':       '#B4B2A9',
    'screening':    '#AFA9EC',
    'inter1':       '#85B7EB',
    'kickoff':      '#97C459',
    'inter2':       '#5DCAA5',
    'debrief':      '#EF9F27',
    'offer':        '#ED93B1',
    'accepted':     '#27ae60',
    'rejected':     '#F09595',
    'disqualified': '#c0392b',
}

SOURCE_LABELS = {
    'direct':   '직접 지원',
    'referral': '내부 추천',
    'headhunt': '헤드헌팅',
    'platform': '채용 플랫폼',
    'other':    '기타',
}

ROUND_TYPE_LABEL = {
    'hr':        'HR 인터뷰',
    'technical': '기술 인터뷰',
    'culture':   '컬처 핏',
    'executive': '임원 면접',
    'other':     '기타',
}
ROUND_STATUS_LABEL = {
    'scheduled': '예정',
    'completed': '완료',
    'cancelled': '취소',
    'no_show':   '노쇼',
}
RECOMMENDATION_LABEL = {'pass': '추천', 'hold': '보류', 'fail': '불가'}

REJECTION_REASON_CODES = {
    'SKILL_MISMATCH':  '직무 역량 미달',
    'CULTURE_FIT':     '조직 문화 부적합',
    'SALARY_MISMATCH': '연봉 미합의',
    'COMMUNICATION':   '커뮤니케이션 이슈',
    'ANOTHER_OFFER':   '타사 오퍼 수락',
    'POSITION_CLOSED': '포지션 마감',
    'OVERQUALIFIED':   '과도한 경력',
    'NO_SHOW':         '면접 불참',
    'WITHDREW':        '지원자 자진 철회',
}

OFFER_STATUS_LABEL = {
    'draft':       '초안',
    'sent':        '발송됨',
    'accepted':    '수락',
    'negotiating': '협상 중',
    'rejected':    '거절',
    'expired':     '만료',
}

# 이메일 템플릿 3종 (기획서 P1 — 인터뷰 안내/합격/불합격)
EMAIL_TEMPLATES = {
    'interview_invite': {
        'label':   '면접 안내',
        'subject': '[{company}] {name}님, 면접 일정을 안내드립니다',
        'body': (
            '{name}님 안녕하세요.\n\n'
            '{company} 채용팀입니다.\n\n'
            '{posting_title} 포지션에 지원해 주셔서 감사합니다.\n'
            '서류 검토 결과, 면접에 초대하게 되었습니다.\n\n'
            '■ 면접 일정\n'
            '- 라운드: {round_type}\n'
            '- 일시: {interview_date}\n'
            '- 장소/방식: {interview_location}\n\n'
            '궁금하신 사항은 언제든지 회신 주세요.\n\n'
            '감사합니다.\n'
            '{company} 채용팀 드림'
        ),
    },
    'pass': {
        'label':   '합격 안내',
        'subject': '[{company}] {name}님, 최종 합격을 축하드립니다',
        'body': (
            '{name}님 안녕하세요.\n\n'
            '{company} 채용팀입니다.\n\n'
            '{posting_title} 포지션 최종 면접 결과,\n'
            '합격하셨음을 알려드립니다. 축하드립니다! \n\n'
            '오퍼 레터 및 입사 관련 안내는 별도로 발송해 드릴 예정입니다.\n\n'
            '감사합니다.\n'
            '{company} 채용팀 드림'
        ),
    },
    'fail': {
        'label':   '불합격 안내',
        'subject': '[{company}] {name}님, 채용 결과를 안내드립니다',
        'body': (
            '{name}님 안녕하세요.\n\n'
            '{company} 채용팀입니다.\n\n'
            '{posting_title} 포지션에 지원해 주셔서 진심으로 감사합니다.\n\n'
            '신중하게 검토한 결과, 이번에는 함께하지 못하게 되었습니다.\n'
            '귀한 시간을 내어 주신 데 깊이 감사드리며,\n'
            '앞으로의 커리어에 좋은 일들이 가득하시길 바랍니다.\n\n'
            '감사합니다.\n'
            '{company} 채용팀 드림'
        ),
    },
    'offer': {
        'label':   '오퍼 안내',
        'subject': '[{company}] {name}님께 오퍼를 제안드립니다',
        'body': (
            '{name}님 안녕하세요.\n\n'
            '{company} 채용팀입니다.\n\n'
            '{posting_title} 포지션의 최종 합격을 다시 한번 축하드립니다.\n\n'
            '아래와 같이 입사 조건을 제안드립니다:\n\n'
            '■ 제안 조건\n'
            '- 연봉: {salary}원\n'
            '- 입사 예정일: {start_date}\n'
            '- 오퍼 유효 기간: {expiry_date}까지\n\n'
            '수락 또는 문의 사항은 본 메일로 회신 주시기 바랍니다.\n\n'
            '감사합니다.\n'
            '{company} 채용팀 드림'
        ),
    },
}

def log_recruit(applicant_id, event_type, meta=None, round_id=None):
    """채용 활동 로그 기록 헬퍼"""
    import json as _json
    db = get_db()
    actor_id = session.get('user_id')
    db.execute(
        'INSERT INTO recruit_activity_logs '
        '(event_type, actor_id, applicant_id, round_id, meta) VALUES (?,?,?,?,?)',
        (event_type, actor_id, applicant_id, round_id,
         _json.dumps(meta or {}, ensure_ascii=False))
    )
    db.commit()

REQUISITION_STATUS_LABEL = {
    'draft':        '작성 중',
    'pending_dept': '결재 진행 중',
    'pending_hr':   '결재 진행 중',
    'approved':     '승인 완료',
    'rejected':     '반려',
    'posted':       'Hire로 넘김',
}

# 채용 유형 — 결재선이 갈라지는 기준. "몇 명까지 뽑아도 되나"가 아니라
# "이 채용이 돈을 새로 쓰는가"가 승인 단계를 정한다.
REQUISITION_HIRE_TYPE_LABEL = {
    'backfill':      '결원 충원',
    'new_planned':   '계획 증원',
    'new_unplanned': '계획 외 증원',
}
REQUISITION_HIRE_TYPE_HINT = {
    'backfill':      '퇴직·휴직 결원 충원 · 인건비 증가 없음',
    'new_planned':   '당해 인력계획 반영 증원',
    'new_unplanned': '인력계획 외 증원 · 인건비 증가',
}
# 결재선 설정 화면이 다루는 종류 — 요청서 3종 + 오퍼 1종.
# 요청서 작성 폼은 REQUISITION_HIRE_TYPE_LABEL 만 보므로 오퍼가 섞이지 않는다.
FLOW_KIND_LABEL = dict(REQUISITION_HIRE_TYPE_LABEL, offer_band='오퍼 밴드 초과')
FLOW_KIND_HINT = dict(
    REQUISITION_HIRE_TYPE_HINT,
    offer_band='Hire 에서 올라온 오퍼가 공고 연봉 밴드 상한을 넘을 때',
)
FLOW_ROLE_LABEL = {
    'dept_head':     '조직장',
    'upper_head':    '차상위 조직장',
    'division_head': '부문장',
    'hr':            '인사 담당',
    'exec':          '경영진 (C레벨)',
    'user':          '지정한 사람',
}
# 결재자를 '어떻게 찾는가' — 설정 화면과 요청서 미리보기에 그대로 보여 준다
FLOW_ROLE_HINT = {
    'dept_head':     '요청 부서의 조직장 · 요청자 본인이 조직장이거나 공석이면 한 단계 위 조직장',
    'upper_head':    '위 조직장의 한 단계 위 조직장 · 없으면 대표이사',
    'division_head': '요청 부서가 속한 부문의 장 · 공석이거나 본인이면 대표이사',
    'hr':            '결재 역할의 "인사 담당 결재자"',
    'exec':          '결재 역할에서 지정한 C레벨 · 미지정이거나 본인이면 대표이사',
    'user':          '지정한 한 사람',
}
ACTED_AS_LABEL = {'self': '', 'delegate': '대결', 'admin': '관리자 대리 처리'}
OPENING_STATUS_LABEL = {
    'approved': '공고 전',
    'open':     '채용 진행',
    'filled':   '충원',
    'closed':   '마감',
}

REQUISITION_EMP_TYPE_LABEL = {
    'full_time':  '정규직',
    'part_time':  '파트타임',
    'contract':   '계약직',
    'intern':     '인턴',
    'freelance':  '프리랜서',
}

# M 트랙은 L5(CL5, level=5) 이상에서만 선택 가능
REQUISITION_TRACK_LABEL = {
    'IC': 'IC (Individual Contributor)',
    'M':  'M (Manager)',
}
MANAGER_TRACK_MIN_LEVEL = 5

# M 트랙 직함 (레벨별)
M_TRACK_TITLE = {
    5: 'M1 · Team Lead',
    6: 'M2 · Engineering Manager',
    7: 'M3 · Senior Manager',
    8: 'M4 · Director',
    9: 'M5 · VP / C-Level',
}
IC_TRACK_TITLE = {
    1: 'L1 · Junior Associate',
    2: 'L2 · Associate',
    3: 'L3 · Mid-level',
    4: 'L4 · Senior',
    5: 'L5 · Staff / Tech Lead',
    6: 'L6 · Senior Staff',
    7: 'L7 · Principal',
    8: 'L8 · Distinguished',
    9: 'L9 · Fellow / CTO',
}

# ── Salary Band API ───────────────────────────────────────────────────
@app.route('/api/salary-band')
@login_required
def api_salary_band():
    """직군 + 레벨 + 트랙 → 연봉 밴드 JSON 반환."""
    db         = get_db()
    jf_id      = request.args.get('job_family_id', type=int)
    level      = request.args.get('level', type=int)
    track      = request.args.get('track', 'IC')

    if not jf_id or not level:
        return jsonify({'error': 'job_family_id and level required'}), 400

    pos = db.execute('SELECT id FROM positions WHERE level=?', (level,)).fetchone()
    if not pos:
        return jsonify({'error': 'level not found'}), 404

    band = db.execute(
        'SELECT min_salary, mid_salary, max_salary FROM salary_grades '
        'WHERE job_family_id=? AND position_id=?',
        (jf_id, pos['id'])
    ).fetchone()

    if not band:
        return jsonify({'min': 0, 'mid': 0, 'max': 0})

    mn  = band['min_salary']
    mid = band['mid_salary']
    mx  = band['max_salary']

    # M 트랙: IC 대비 +10% (Amazon SDM vs SDE 기준)
    if track == 'M' and level >= MANAGER_TRACK_MIN_LEVEL:
        mn  = int(mn  * 1.10)
        mid = int(mid * 1.10)
        mx  = int(mx  * 1.10)

    return jsonify({
        'min': mn,
        'mid': mid,
        'max': mx,
        'min_man': mn  // 10000,
        'mid_man': mid // 10000,
        'max_man': mx  // 10000,
    })

# ── Requisition 라우트 ────────────────────────────────────────────────

# ==============================================================
#  결재선 엔진 + 자리 카드
#  요청서는 "몇 명 뽑겠다"는 문서이고, 승인이 끝나면 그 수만큼
#  자리 카드(job_openings)가 떨어진다. 사람이 나가도 카드는 남는다.
#  결재 단계 수는 채용 유형이 정하고, 그 서식은 관리자가 고칠 수 있다.
# ==============================================================

DEFAULT_FLOW = {
    'backfill':      [(1, 'dept_head',     None,   '조직장 확인',      2),
                      (2, 'hr',            None,   '인사 확인',        2)],
    'new_planned':   [(1, 'dept_head',     None,   '조직장 승인',      2),
                      (2, 'hr',            None,   '인사 승인',        2)],
    'new_unplanned': [(1, 'dept_head',     None,   '조직장 승인',      2),
                      (2, 'division_head', None,   '부문장 승인',      2),
                      (3, 'exec',          'chro', '인사 승인 (CHRO)', 2),
                      (4, 'exec',          'cfo',  '예산 승인 (CFO)',  3),
                      (5, 'exec',          'ceo',  '최종 승인 (CEO)',  3)],
    # 밴드를 넘긴 오퍼 — 하이어링 매니저는 Hire 에서 이미 봤으므로 인사·대표만 본다
    'offer_band':    [(1, 'exec',          'chro', '인사 승인 (CHRO)', 2),
                      (2, 'exec',          'ceo',  '최종 승인 (CEO)',  2)],
}

# 결재를 못 하는 날로 보는 휴가 — 재택·외출·반차는 결재 가능으로 본다
AWAY_LEAVE_TYPES = ('annual', 'sick', 'maternity', 'paternity', 'parental', 'family_care',
                    'bereavement', 'military', 'compensation', 'menstrual', 'miscarriage', 'fertility')


def _flow_template(db, hire_type):
    """이 채용 유형이 밟아야 할 결재 단계 목록. 관리자가 고친 값이 우선."""
    ht = hire_type if hire_type in DEFAULT_FLOW else 'new_planned'
    rows = db.execute(
        'SELECT * FROM requisition_flow_steps WHERE hire_type=? ORDER BY step_no', (ht,)
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]
    return [{'step_no': n, 'role_kind': rk, 'exec_key': ek, 'user_id': None, 'label': lb, 'sla_days': sla}
            for n, rk, ek, lb, sla in DEFAULT_FLOW[ht]]


def _hr_admin_ids(db):
    return [r['id'] for r in db.execute("SELECT id FROM users WHERE role='admin'").fetchall()]


def _approval_roles(db):
    """C레벨·인사 담당 — key → {title, short, user_id, user_name, delegate_id, ...}"""
    try:
        rows = db.execute(
            'SELECT r.*, u.name AS user_name, u.status AS user_status, dg.name AS delegate_name '
            'FROM approval_roles r LEFT JOIN users u ON u.id=r.user_id '
            'LEFT JOIN users dg ON dg.id=r.delegate_id ORDER BY r.sort_order').fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r['key']: dict(r) for r in rows}


def _is_away(db, uid, day=None):
    """지금 결재할 수 없는 사람인가 — 퇴사했거나 오늘 휴가 중."""
    if not uid:
        return True
    u = db.execute('SELECT status FROM users WHERE id=?', (uid,)).fetchone()
    if not u or u['status'] != 'active':
        return True
    day = day or date.today().isoformat()
    try:
        return bool(db.execute(
            "SELECT 1 FROM leave_requests WHERE user_id=? AND status='approved' "
            "AND start_date<=? AND end_date>=? AND type IN (%s) LIMIT 1"
            % ','.join('?' * len(AWAY_LEAVE_TYPES)),
            (uid, day, day) + AWAY_LEAVE_TYPES).fetchone())
    except sqlite3.OperationalError:
        return False


def _org_leaders_up(db, dept_id):
    """부서에서 위로 올라가며 [(부서명, 조직장id 또는 None)] — 퇴사자가 조직장이면 공석으로 본다."""
    out, seen = [], set()
    while dept_id and dept_id not in seen:
        seen.add(dept_id)
        d = db.execute(
            'SELECT d.id, d.name, d.parent_id, d.leader_id, u.status FROM departments d '
            'LEFT JOIN users u ON u.id=d.leader_id WHERE d.id=?', (dept_id,)).fetchone()
        if not d:
            break
        out.append((d['name'], d['leader_id'] if d['leader_id'] and d['status'] == 'active' else None))
        dept_id = d['parent_id']
    return out


def _plan_flow(db, dept_id, requester_id, hire_type):
    """결재선을 실제 사람으로 푼다. 요청서 미리보기와 상신이 같은 함수를 쓴다.

    원칙 (전결·대결 규정의 최소형):
      · 본인 결재 금지 — 요청자가 그 자리 사람이면 한 단계 위로 올린다.
      · 공석이면 위로 올린다. 끝까지 없으면 대표이사, 대표이사도 없으면 시스템 관리자.
      · 같은 사람이 두 번 나오면 뒤 단계는 생략한다.
      · 결재자가 부재(휴가·퇴사)면 대결자가 대신 처리할 수 있다.
    """
    roles  = _approval_roles(db)
    ceo    = roles.get('ceo') or {}
    ceo_id = ceo.get('user_id') if ceo.get('user_status') == 'active' else None
    admins = _hr_admin_ids(db)
    chain  = _org_leaders_up(db, dept_id)

    # 요청자 기준으로 결재할 수 있는 조직장 사다리 (본인·공석 제외)
    ladder = []
    for _dname, lid in chain:
        if lid and lid != requester_id and lid not in ladder:
            ladder.append(lid)
    head_note = ''
    if chain:
        if chain[0][1] == requester_id:
            head_note = '요청자 본인이 부서장 → 상위 조직장'
        elif not chain[0][1]:
            head_note = '부서장 공석 → 상위 조직장'

    def active(uid):
        if not uid:
            return None
        r = db.execute("SELECT id FROM users WHERE id=? AND status='active'", (uid,)).fetchone()
        return r['id'] if r else None

    plan, n = [], 0
    for st in _flow_template(db, hire_type):
        kind, key = st['role_kind'], (st.get('exec_key') or '')
        uid = dlg = None
        note = ''
        if kind == 'dept_head':
            if ladder:
                uid, note = ladder[0], head_note
                dlg = ladder[1] if len(ladder) > 1 else ceo_id
            else:
                uid, note = ceo_id, '상위 조직장 없음 → 대표이사'
        elif kind == 'upper_head':
            if len(ladder) > 1:
                uid = ladder[1]
                dlg = ladder[2] if len(ladder) > 2 else ceo_id
            else:
                uid, note = ceo_id, '차상위 조직장 없음 → 대표이사'
        elif kind == 'division_head':
            root = chain[-1][1] if chain else None
            if root and root != requester_id:
                uid, dlg = root, ceo_id
            else:
                uid, note = ceo_id, ('요청자 본인이 부문장 → 대표이사' if root else '부문장 공석 → 대표이사')
        elif kind in ('exec', 'hr'):
            r = roles.get(key if kind == 'exec' else 'hr_desk') or {}
            title = r.get('short') or r.get('title') or key.upper()
            uid, dlg = active(r.get('user_id')), active(r.get('delegate_id'))
            if kind == 'hr':
                if uid == requester_id:
                    uid, dlg, note = dlg, None, '요청자 본인이 인사 담당 → 대결자'
                if not uid:
                    uid, note = (admins[0] if admins else None), '인사 담당 미지정 → 시스템 관리자'
            else:
                if not uid:
                    uid, dlg, note = ceo_id, None, '%s 미지정 → 대표이사' % title
                elif uid == requester_id and key != 'ceo':
                    uid, dlg, note = ceo_id, None, '요청자 본인이 %s → 대표이사' % title
        else:  # user
            uid = active(st.get('user_id'))
            if not uid:
                note = '지정한 사람 없음 → 시스템 관리자'
        if not uid:
            uid = admins[0] if admins else None
            note = note or '결재자를 찾지 못함 → 시스템 관리자'

        if dlg in (uid, requester_id):
            dlg = None
        p = _person_brief(db, uid)
        d = _person_brief(db, dlg)
        plan.append({
            'role_kind': kind, 'exec_key': key or None,
            'label': st.get('label') or FLOW_ROLE_LABEL.get(kind, ''),
            'sla_days': int(st.get('sla_days') or 2),
            'assignee_id': uid, 'name': p['name'], 'pos': p['pos'], 'dept': p['dept'],
            'delegate_id': dlg, 'delegate_name': d['name'],
            'note': note, 'skipped': '', 'step_no': None,
        })

    # 같은 사람이 여러 번 나오면 마지막(더 높은) 단계에서 한 번만 결재한다
    for i, item in enumerate(plan):
        if item['assignee_id'] and item['assignee_id'] == requester_id:
            item['skipped'] = '요청자 본인 결재 — 생략'
        elif item['assignee_id'] and any(x['assignee_id'] == item['assignee_id'] for x in plan[i + 1:]):
            item['skipped'] = '뒤 단계와 같은 결재자 — 생략'
        if not item['skipped']:
            n += 1
            item['step_no'] = n
    return plan


def _person_brief(db, uid):
    if not uid:
        return {'name': None, 'pos': None, 'dept': None}
    r = db.execute(
        'SELECT u.name, p.name AS pos, d.name AS dept FROM users u '
        'LEFT JOIN positions p ON p.id=u.position_id LEFT JOIN departments d ON d.id=u.department_id '
        'WHERE u.id=?', (uid,)).fetchone()
    if not r:
        return {'name': None, 'pos': None, 'dept': None}
    return {'name': r['name'], 'pos': (r['pos'] or '').replace(' — ', ' ') or None, 'dept': r['dept']}


def _step_approver_ids(db, req, step):
    """한 단계를 '누가 눌러야 하는가'를 실제 사람 목록으로 바꾼다.

    상신 때 박아 둔 결재자가 기본이고, 그 사람이 부재면 대결자도 누를 수 있다.
    """
    step = dict(step)
    if step.get('assignee_id'):
        ids = [step['assignee_id']]
        if step.get('delegate_id') and _is_away(db, step['assignee_id']):
            ids.append(step['delegate_id'])
        return ids
    # 결재선 2판 이전에 올라간 요청서 — 예전 규칙 그대로
    kind = step['role_kind']
    if kind == 'dept_head':
        ids = [r['id'] for r in db.execute(
            "SELECT id FROM users WHERE role IN ('manager','admin') "
            "AND department_id=? AND id!=?",
            (req['department_id'], req['requester_id'])).fetchall()]
        return ids or _hr_admin_ids(db)
    if kind == 'user':
        return [step['user_id']] if step.get('user_id') else _hr_admin_ids(db)
    return _hr_admin_ids(db)


def _build_requisition_flow(db, req):
    """요청서 한 건에 결재 단계들을 깔아 준다(이미 있으면 그대로 둔다). 결재자는 이 순간 고정된다."""
    if db.execute('SELECT 1 FROM requisition_approvals WHERE requisition_id=? LIMIT 1',
                  (req['id'],)).fetchone():
        return
    cum = 0
    plan = _plan_flow(db, req['department_id'], req['requester_id'],
                      req['hire_type'] if 'hire_type' in req.keys() else None)
    for st in plan:
        if st['skipped']:
            continue
        cum += st['sla_days']
        db.execute(
            'INSERT INTO requisition_approvals '
            '(requisition_id, step_no, role_kind, exec_key, label, due_at, assignee_id, delegate_id, route_note) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (req['id'], st['step_no'], st['role_kind'], st['exec_key'], st['label'],
             (datetime.now() + timedelta(days=cum)).strftime('%Y-%m-%d %H:%M:%S'),
             st['assignee_id'], st['delegate_id'], st['note'] or None))
    db.commit()


def _flow_rows(db, req_id):
    return db.execute(
        'SELECT a.*, u.name AS approver_name, s.name AS assignee_name, g.name AS delegate_name, '
        '       sp.name AS assignee_pos, sd.name AS assignee_dept '
        'FROM requisition_approvals a '
        'LEFT JOIN users u ON a.approver_id=u.id '
        'LEFT JOIN users s ON a.assignee_id=s.id '
        'LEFT JOIN positions sp ON s.position_id=sp.id '
        'LEFT JOIN departments sd ON s.department_id=sd.id '
        'LEFT JOIN users g ON a.delegate_id=g.id '
        'WHERE a.requisition_id=? ORDER BY a.step_no', (req_id,)).fetchall()


def _current_step(db, req_id):
    return db.execute(
        "SELECT * FROM requisition_approvals WHERE requisition_id=? AND status='waiting' "
        "ORDER BY step_no LIMIT 1", (req_id,)).fetchone()


def _can_act_on_step(db, req, step, uid, role):
    if step is None:
        return False
    if role == 'admin':
        return True
    return uid in _step_approver_ids(db, req, dict(step))


def _is_requisition_approver(db, req_id, uid):
    """결재선에 이름이 올라간 사람 — 부서가 달라도 요청서를 볼 수 있어야 한다."""
    return bool(db.execute(
        'SELECT 1 FROM requisition_approvals WHERE requisition_id=? AND (assignee_id=? OR delegate_id=?) LIMIT 1',
        (req_id, uid, uid)).fetchone())


def _notify_step(db, req, step):
    """지금 차례인 사람에게만 알린다."""
    for aid in _step_approver_ids(db, req, dict(step)):
        if not aid:
            continue
        add_notification(aid, 'info', 'action', '채용 요청서 결재',
                         u'"%s" 요청서가 %s 단계에서 결재를 기다립니다.'
                         % (req['title'], step['label'] or FLOW_ROLE_LABEL.get(step['role_kind'], '')),
                         link=url_for('requisition_detail', req_id=req['id']))
    db.commit()


def _parse_req_lines(f):
    """요청서 작성 화면의 레벨별 줄을 읽는다. 인원 0인 줄은 버린다.

    반환: [{'position_id','job_family_id','headcount','salary_min','salary_max'}, ...]
    """
    poss  = f.getlist('line_position_id')
    heads = f.getlist('line_headcount')
    jfs   = f.getlist('line_job_family_id')
    lo    = f.getlist('line_salary_min')
    hi    = f.getlist('line_salary_max')

    def at(lst, i, dflt=''):
        return lst[i] if i < len(lst) else dflt

    def num(v):
        try:
            return int(str(v).strip() or 0)
        except (TypeError, ValueError):
            return 0

    out = []
    for i in range(len(poss)):
        n = max(0, num(at(heads, i, 1)))
        if not poss[i] or not n:
            continue
        out.append({
            'position_id':   num(poss[i]) or None,
            'job_family_id': num(at(jfs, i)) or None,
            'headcount':     n,
            'salary_min':    num(at(lo, i)) * 10000,   # 화면은 만원, 저장은 원
            'salary_max':    num(at(hi, i)) * 10000,
        })
    return out


def _save_req_lines(db, req_id, lines):
    """레벨별 줄을 저장하고, 요청서 요약값(인원·직급·밴드)을 줄에 맞춰 맞춘다."""
    if not lines:
        return 0
    db.execute('DELETE FROM requisition_lines WHERE requisition_id=?', (req_id,))
    for i, l in enumerate(lines, start=1):
        db.execute(
            'INSERT INTO requisition_lines '
            '(requisition_id, seq, position_id, job_family_id, headcount, salary_min, salary_max) '
            'VALUES (?,?,?,?,?,?,?)',
            (req_id, i, l['position_id'], l['job_family_id'],
             l['headcount'], l['salary_min'], l['salary_max']))
    total = sum(l['headcount'] for l in lines)
    lows  = [l['salary_min'] for l in lines if l['salary_min']]
    highs = [l['salary_max'] for l in lines if l['salary_max']]
    db.execute(
        'UPDATE job_requisitions SET headcount=?, position_id=?, job_family_id=?, '
        'salary_min=?, salary_max=? WHERE id=?',
        (total, lines[0]['position_id'], lines[0]['job_family_id'],
         min(lows) if lows else 0, max(highs) if highs else 0, req_id))
    return total


def _create_openings(db, req):
    """승인 완료 → 요청한 인원수만큼 자리 카드를 뗀다.

    레벨별 줄(requisition_lines)이 있으면 줄마다 그 줄의 직급·직군·밴드를 물려준다.
    줄이 없는 옛 요청서는 요청서 값 그대로 headcount 장을 뗀다. 중복 생성은 막는다.
    """
    made = db.execute('SELECT COUNT(*) c FROM job_openings WHERE requisition_id=?',
                      (req['id'],)).fetchone()['c']
    if made:
        return 0

    rk    = req.keys()
    lines = db.execute('SELECT * FROM requisition_lines WHERE requisition_id=? ORDER BY seq',
                       (req['id'],)).fetchall()
    if lines:
        specs = [(l['position_id'], l['job_family_id'],
                  l['salary_min'] or 0, l['salary_max'] or 0)
                 for l in lines for _ in range(max(1, int(l['headcount'] or 1)))]
    else:
        n = max(1, int(req['headcount'] or 1))
        specs = [(req['position_id'],
                  req['job_family_id'] if 'job_family_id' in rk else None,
                  req['salary_min'] or 0, req['salary_max'] or 0)] * n

    for i, (pos_id, jf_id, s_min, s_max) in enumerate(specs, start=1):
        db.execute(
            'INSERT INTO job_openings '
            '(requisition_id, seq, code, title, department_id, position_id, job_family_id, '
            ' employment_type, hire_type, backfill_user_id, target_start_date, '
            ' salary_min, salary_max, status) '
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'approved')",
            (req['id'], i, 'OP-%d-%d' % (req['id'], i), req['title'],
             req['department_id'], pos_id, jf_id,
             req['employment_type'],
             (req['hire_type'] if 'hire_type' in rk else None) or 'new_planned',
             req['backfill_user_id'] if 'backfill_user_id' in rk else None,
             req['target_start_date'], s_min, s_max))
    db.commit()
    return len(specs)


@app.route('/requisitions')
@login_required
def requisition_list():
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    status_f = request.args.get('status', '')

    sql = (
        'SELECT r.*, d.name AS dept_name, p.name AS pos_name, '
        'u.name AS requester_name, da.name AS dept_approver_name, ha.name AS hr_approver_name '
        'FROM job_requisitions r '
        'LEFT JOIN departments d  ON r.department_id   = d.id '
        'LEFT JOIN positions   p  ON r.position_id     = p.id '
        'LEFT JOIN users       u  ON r.requester_id    = u.id '
        'LEFT JOIN users       da ON r.dept_approver_id = da.id '
        'LEFT JOIN users       ha ON r.hr_approver_id   = ha.id '
        'WHERE 1=1'
    )
    params = []
    on_line = ('r.id IN (SELECT requisition_id FROM requisition_approvals '
               'WHERE assignee_id=? OR delegate_id=?)')
    if role == 'manager':
        mgr_dept = session.get('dept_id') or 0
        # 본인 신청 + 같은 부서 신청 + 결재선에 이름이 오른 요청서
        sql += ' AND (r.requester_id=? OR r.department_id=? OR %s)' % on_line
        params += [uid, mgr_dept, uid, uid]
    elif role not in ('admin', 'recruiter'):
        sql += ' AND (r.requester_id=? OR %s)' % on_line
        params += [uid, uid, uid]

    if status_f:
        sql += ' AND r.status=?'
        params.append(status_f)

    sql += ' ORDER BY r.created_at DESC'
    reqs = db.execute(sql, params).fetchall()

    return render_template('hiring/requisition_list.html',
        reqs=reqs,
        status_filter=status_f,
        status_labels=REQUISITION_STATUS_LABEL,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        hire_type_labels=REQUISITION_HIRE_TYPE_LABEL,
        active_page='requisition'
    )


def _requisition_submit_for_approval(req_id, uid):
    """작성 완료 → 결재 상신. 채용 유형이 정한 단계들을 깔고 1단계에만 알린다."""
    db  = get_db()
    req = db.execute('SELECT * FROM job_requisitions WHERE id=? AND requester_id=?', (req_id, uid)).fetchone()
    if not req or req['status'] != 'draft':
        flash('처리할 수 없는 요청입니다.', 'error')
        return False

    db.execute(
        "UPDATE job_requisitions SET status='pending_dept', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req_id,))
    db.commit()

    _build_requisition_flow(db, req)
    step = _current_step(db, req_id)
    if step:
        _notify_step(db, req, step)
        flash('결재를 올렸습니다 — %s 단계부터 시작합니다.' % (step['label'] or ''), 'success')
    else:
        flash('결재를 올렸습니다.', 'success')
    return True


def _dept_leader_chain(db, dept_id):
    """부서 하나에서 면접관 두 자리를 뽑는다.

    1차 = 그 부서의 부서장(HM).
    2차 = 그 위 조직으로 한 칸씩 올라가며 처음 만나는 부서장(차상위 리더).
    부서장이 비어 있으면 그 자리는 None — 화면에서 '지정 필요'로 보인다.
    """
    if not dept_id:
        return {'hm_id': None, 'hm_name': None, 'senior_id': None, 'senior_name': None}
    row = db.execute(
        'SELECT d.id, d.parent_id, d.leader_id, l.name AS leader_name '
        'FROM departments d LEFT JOIN users l ON d.leader_id=l.id AND l.status="active" '
        'WHERE d.id=?', (dept_id,)).fetchone()
    if not row:
        return {'hm_id': None, 'hm_name': None, 'senior_id': None, 'senior_name': None}

    hm_id, hm_name = row['leader_id'], row['leader_name']
    senior_id = senior_name = None
    pid, seen = row['parent_id'], {row['id']}
    while pid and pid not in seen:
        seen.add(pid)
        up = db.execute(
            'SELECT d.id, d.parent_id, d.leader_id, l.name AS leader_name '
            'FROM departments d LEFT JOIN users l ON d.leader_id=l.id AND l.status="active" '
            'WHERE d.id=?', (pid,)).fetchone()
        if not up:
            break
        if up['leader_id'] and up['leader_id'] != hm_id:
            senior_id, senior_name = up['leader_id'], up['leader_name']
            break
        pid = up['parent_id']
    return {'hm_id': hm_id, 'hm_name': hm_name,
            'senior_id': senior_id, 'senior_name': senior_name}


@app.route('/requisitions/new', methods=['GET', 'POST'])
@login_required
def requisition_new():
    db    = get_db()
    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs   = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()
    # 백필이면 "누구 자리인가"를 반드시 적게 한다 — 이게 없으면 백필인지 증원인지 아무도 모른다
    leavers = db.execute(
        "SELECT u.id, u.name, u.status, u.termination_date, d.name AS dept_name FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "ORDER BY (u.status='active'), COALESCE(u.termination_date,'') DESC, u.name"
    ).fetchall()
    flow_preview = {ht: _flow_template(db, ht) for ht in REQUISITION_HIRE_TYPE_LABEL}
    # 2차 면접의 '협업 리더' 후보 — 조직도에 없는 정보라 요청서에서 직접 고른다
    dept_leaders = {d['id']: _dept_leader_chain(db, d['id']) for d in depts}
    collab_pool = db.execute(
        "SELECT u.id, u.name, d.name AS dept_name, p.name AS pos_name "
        "FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions   p ON u.position_id  =p.id "
        "WHERE u.status='active' ORDER BY d.name, u.name"
    ).fetchall()

    if request.method == 'POST':
        f = request.form
        rule = _start_date_rule_msg(db, _normalize_hire_date(f.get('target_start_date', '')))
        if rule:
            flash('희망 입사일: ' + rule, 'error')
            return redirect(url_for('requisition_new'))
        rid = db.execute(
            'INSERT INTO job_requisitions '
            '(title, department_id, position_id, job_family_id, track, '
            ' headcount, employment_type, reason, '
            ' required_skills, salary_min, salary_mid, salary_max, target_start_date, '
            ' hire_type, backfill_user_id, budget_note, collab_leader_id, '
            ' status, requester_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                f.get('title','').strip(),
                f.get('department_id') or None,
                f.get('position_id') or None,
                f.get('job_family_id') or None,
                f.get('track', 'IC'),
                int(f.get('headcount', 1)),
                f.get('employment_type', 'full_time'),
                f.get('reason','').strip(),
                f.get('required_skills','').strip(),
                int(f.get('salary_min') or 0),
                int(f.get('salary_mid') or 0),
                int(f.get('salary_max') or 0),
                f.get('target_start_date','').strip() or None,
                f.get('hire_type') if f.get('hire_type') in REQUISITION_HIRE_TYPE_LABEL else 'new_planned',
                (f.get('backfill_user_id') or None) if f.get('hire_type') == 'backfill' else None,
                f.get('budget_note','').strip() or None,
                f.get('collab_leader_id') or None,
                'draft',
                session['user_id'],
            )
        ).lastrowid
        # 레벨별 줄 — "L5 1명, L3 2명". 승인되면 이 줄대로 자리 카드가 떨어진다.
        _save_req_lines(db, rid, _parse_req_lines(f))
        db.commit()

        action = f.get('action', 'save')
        if action == 'submit':
            _requisition_submit_for_approval(rid, session['user_id'])
        return redirect(url_for('requisition_detail', req_id=rid))

    return render_template('hiring/requisition_form.html',
        req=None, depts=depts, poses=poses, jfs=jfs,
        leavers=leavers, collab_pool=collab_pool, dept_leaders=dept_leaders,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        hire_type_labels=REQUISITION_HIRE_TYPE_LABEL,
        hire_type_hints=REQUISITION_HIRE_TYPE_HINT,
        flow_preview=flow_preview,
        track_labels=REQUISITION_TRACK_LABEL,
        manager_track_min_level=MANAGER_TRACK_MIN_LEVEL,
        ic_track_title=IC_TRACK_TITLE,
        m_track_title=M_TRACK_TITLE,
        active_page='requisition'
    )


@app.route('/requisitions/<int:req_id>')
@login_required
def requisition_detail(req_id):
    db  = get_db()
    uid = session['user_id']
    role = session.get('user_role')

    req = db.execute(
        'SELECT r.*, d.name AS dept_name, p.name AS pos_name, p.level AS pos_level, '
        'jf.name AS jf_name, jf.code AS jf_code, '
        'u.name AS requester_name, da.name AS dept_approver_name, ha.name AS hr_approver_name, '
        'cl.name AS collab_leader_name, dl.name AS dept_leader_name '
        'FROM job_requisitions r '
        'LEFT JOIN departments d  ON r.department_id    = d.id '
        'LEFT JOIN users       cl ON r.collab_leader_id = cl.id '
        'LEFT JOIN users       dl ON d.leader_id        = dl.id '
        'LEFT JOIN positions   p  ON r.position_id      = p.id '
        'LEFT JOIN job_families jf ON r.job_family_id   = jf.id '
        'LEFT JOIN users       u  ON r.requester_id     = u.id '
        'LEFT JOIN users       da ON r.dept_approver_id = da.id '
        'LEFT JOIN users       ha ON r.hr_approver_id   = ha.id '
        'WHERE r.id=?', (req_id,)
    ).fetchone()
    if not req:
        flash('채용 요청서를 찾을 수 없습니다.', 'error')
        return redirect(url_for('requisition_list'))

    # 권한 체크: 본인 or 매니저(같은 부서) or admin/recruiter
    mgr_dept = session.get('dept_id') or 0
    if role not in ('admin', 'recruiter') and req['requester_id'] != uid \
            and not _is_requisition_approver(db, req_id, uid):
        if role != 'manager' or req['department_id'] != mgr_dept:
            flash('접근 권한이 없습니다.', 'error')
            return redirect(url_for('requisition_list'))

    posting = None
    if req['posting_id']:
        posting = db.execute('SELECT * FROM job_postings WHERE id=?', (req['posting_id'],)).fetchone()

    # 옛 요청서(결재선이 생기기 전에 올라간 것)도 화면에서 열리는 순간 단계를 깔아 준다
    if req['status'] in ('pending_dept', 'pending_hr'):
        _build_requisition_flow(db, req)
    steps = _flow_rows(db, req_id)
    step  = _current_step(db, req_id)
    # 자리 카드 — 레벨(직급)과, 그 자리를 예약해 둔 입사 예정자까지 같이 본다
    openings = db.execute(
        'SELECT o.*, u.name AS hired_name, p.name AS pos_name, p.level AS pos_level, '
        '       jf.name AS jf_name, ih.id AS hire_id, ih.name AS reserved_name, '
        '       ih.start_date AS reserved_start '
        'FROM job_openings o '
        'LEFT JOIN users u ON o.hired_user_id=u.id '
        'LEFT JOIN positions p ON o.position_id=p.id '
        'LEFT JOIN job_families jf ON o.job_family_id=jf.id '
        "LEFT JOIN incoming_hires ih ON ih.opening_id=o.id AND ih.status='waiting' "
        'WHERE o.requisition_id=? ORDER BY o.seq', (req_id,)).fetchall()
    # 충원 현황은 상태값이 아니라 카드에서 센다 — 카드가 곧 정원이다
    fill = {
        'total':    len(openings),
        'filled':   sum(1 for o in openings if o['status'] == 'filled'),
        'reserved': sum(1 for o in openings if o['status'] != 'filled' and o['reserved_name']),
    }
    fill['left'] = fill['total'] - fill['filled'] - fill['reserved']
    fill['done'] = bool(fill['total']) and fill['filled'] == fill['total']
    lines = db.execute(
        'SELECT l.*, p.name AS pos_name, p.level AS pos_level, jf.name AS jf_name '
        'FROM requisition_lines l '
        'LEFT JOIN positions p ON l.position_id=p.id '
        'LEFT JOIN job_families jf ON l.job_family_id=jf.id '
        'WHERE l.requisition_id=? ORDER BY l.seq', (req_id,)).fetchall()
    backfill_of = None
    if 'backfill_user_id' in req.keys() and req['backfill_user_id']:
        backfill_of = db.execute('SELECT id, name, termination_date FROM users WHERE id=?',
                                 (req['backfill_user_id'],)).fetchone()

    # 이 요청서 한 장에서 면접관 세 자리가 결정된다 (1차 HM · 2차 차상위 · 2차 협업 리더)
    chain = _dept_leader_chain(db, req['department_id'])

    # 지금 차례 결재자가 부재면 대결자가 누를 수 있다 — 화면에 그 사실을 보여 준다
    cur_away = bool(step and step['assignee_id'] and _is_away(db, step['assignee_id']))
    reassign_pool = []
    if role == 'admin' and step:
        reassign_pool = db.execute(
            "SELECT u.id, u.name, d.name AS dept_name, p.name AS pos_name FROM users u "
            "LEFT JOIN departments d ON u.department_id=d.id LEFT JOIN positions p ON u.position_id=p.id "
            "WHERE u.status='active' AND u.id!=? ORDER BY (u.role='employee'), d.name, u.name",
            (req['requester_id'],)).fetchall()
    hire_ready = _hire_config()['ready'] if role in ('admin', 'recruiter') else False

    return render_template('hiring/requisition_detail.html',
        req=req, posting=posting, chain=chain,
        steps=steps, cur_step=step, cur_away=cur_away,
        reassign_pool=reassign_pool, hire_ready=hire_ready,
        acted_as_labels=ACTED_AS_LABEL,
        can_act=_can_act_on_step(db, req, step, uid, role),
        openings=openings, backfill_of=backfill_of,
        lines=lines, fill=fill,
        status_labels=REQUISITION_STATUS_LABEL,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        hire_type_labels=REQUISITION_HIRE_TYPE_LABEL,
        role_labels=FLOW_ROLE_LABEL,
        opening_labels=OPENING_STATUS_LABEL,
        ic_track_title=IC_TRACK_TITLE,
        m_track_title=M_TRACK_TITLE,
        active_page='requisition'
    )


@app.route('/requisitions/<int:req_id>/submit', methods=['POST'])
@login_required
def requisition_submit(req_id):
    """작성 완료 → 부서장 승인 요청."""
    _requisition_submit_for_approval(req_id, session['user_id'])
    return redirect(url_for('requisition_detail', req_id=req_id))


@app.route('/requisitions/<int:req_id>/act', methods=['POST'])
@login_required
def requisition_act(req_id):
    """지금 차례인 결재 한 단계를 처리한다. 승인/반려 버튼은 이 하나로 모인다."""
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')

    req = db.execute('SELECT * FROM job_requisitions WHERE id=?', (req_id,)).fetchone()
    if not req or req['status'] not in ('pending_dept', 'pending_hr'):
        flash('처리할 수 없는 요청입니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    _build_requisition_flow(db, req)
    step = _current_step(db, req_id)
    if not _can_act_on_step(db, req, step, uid, role):
        flash('이 단계의 결재자가 아닙니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    action  = request.form.get('action', 'approve')
    comment = request.form.get('comment', '').strip()
    # 누가 어떤 자격으로 눌렀는가 — 본인 / 대결 / 관리자 대리
    if not step['assignee_id'] or uid == step['assignee_id']:
        acted_as = 'self'
    elif uid in _step_approver_ids(db, req, dict(step)):
        acted_as = 'delegate'
    else:
        acted_as = 'admin'

    if action != 'approve':
        db.execute(
            "UPDATE requisition_approvals SET status='rejected', approver_id=?, comment=?, acted_as=?, "
            "acted_at=CURRENT_TIMESTAMP WHERE id=?", (uid, comment, acted_as, step['id']))
        db.execute(
            "UPDATE job_requisitions SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (req_id,))
        db.commit()
        add_notification(req['requester_id'], 'info', 'action', '채용 요청서 반려',
                         u'"%s" 요청서가 %s 단계에서 반려되었습니다. 사유: %s'
                         % (req['title'], step['label'] or '', comment or '(없음)'),
                         link=url_for('requisition_detail', req_id=req_id))
        db.commit()
        flash('요청서를 반려했습니다.', 'success')
        return redirect(url_for('requisition_detail', req_id=req_id))

    db.execute(
        "UPDATE requisition_approvals SET status='approved', approver_id=?, comment=?, acted_as=?, "
        "acted_at=CURRENT_TIMESTAMP WHERE id=?", (uid, comment, acted_as, step['id']))
    # 옛 화면들이 아직 보는 칸도 같이 채워 둔다
    if step['role_kind'] == 'dept_head':
        db.execute('UPDATE job_requisitions SET dept_approver_id=?, dept_approved_at=CURRENT_TIMESTAMP '
                   'WHERE id=?', (uid, req_id))
    elif step['role_kind'] == 'hr':
        db.execute('UPDATE job_requisitions SET hr_approver_id=?, hr_approved_at=CURRENT_TIMESTAMP '
                   'WHERE id=?', (uid, req_id))
    db.commit()

    nxt = _current_step(db, req_id)
    if nxt:
        _notify_step(db, req, nxt)
        flash(u'승인했습니다. 다음은 %s 단계입니다.' % (nxt['label'] or ''), 'success')
        return redirect(url_for('requisition_detail', req_id=req_id))

    # 마지막 단계 통과 → 승인 완료 + 자리 카드 발행
    db.execute("UPDATE job_requisitions SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (req_id,))
    db.commit()
    fresh = db.execute('SELECT * FROM job_requisitions WHERE id=?', (req_id,)).fetchone()
    n = _create_openings(db, fresh)
    add_notification(req['requester_id'], 'info', 'action', '채용 요청서 최종 승인',
                     u'채용 요청 "%s" 최종 승인 — 포지션 %d건 생성' % (req['title'], n or req['headcount']),
                     link=url_for('requisition_detail', req_id=req_id))
    db.commit()
    flash(u'최종 승인 완료 — 포지션 %d건 생성' % (n or req['headcount']), 'success')
    return redirect(url_for('requisition_detail', req_id=req_id))


@app.route('/requisitions/flow-preview')
@login_required
def requisition_flow_preview():
    """요청서 작성 화면 — 부서·채용 유형을 고르는 순간 '누구의 승인이 필요한가'를 사람 이름으로."""
    db = get_db()
    ht = request.args.get('hire_type', 'new_planned')
    try:
        dept_id = int(request.args.get('dept') or 0) or None
    except ValueError:
        dept_id = None
    if not dept_id:
        return jsonify({'ok': False, 'steps': []})
    plan = _plan_flow(db, dept_id, session['user_id'], ht)
    live = [p for p in plan if not p['skipped']]
    return jsonify({'ok': True, 'steps': plan,
                    'total_days': sum(p['sla_days'] for p in live),
                    'count': len(live)})


@app.route('/requisitions/<int:req_id>/reassign', methods=['POST'])
@login_required
@admin_required
def requisition_reassign(req_id):
    """지금 차례 결재자를 바꾼다(퇴사·장기 부재·조직 개편). 사유는 기록으로 남는다."""
    db  = get_db()
    req = db.execute('SELECT * FROM job_requisitions WHERE id=?', (req_id,)).fetchone()
    step = _current_step(db, req_id) if req else None
    try:
        new_uid = int(request.form.get('user_id') or 0)
    except ValueError:
        new_uid = 0
    reason = request.form.get('reason', '').strip()
    person = db.execute("SELECT id, name FROM users WHERE id=? AND status='active'", (new_uid,)).fetchone()
    if not step or not person or not reason:
        flash('바꿀 결재자와 사유를 모두 입력하세요.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))
    if new_uid == req['requester_id']:
        flash('요청자 본인은 결재자가 될 수 없습니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))
    before = db.execute('SELECT name FROM users WHERE id=?', (step['assignee_id'],)).fetchone() \
        if step['assignee_id'] else None
    note = '%s → %s 변경 (%s · %s)' % (before['name'] if before else '미지정', person['name'],
                                    session.get('user_name') or '관리자', reason)
    db.execute('UPDATE requisition_approvals SET assignee_id=?, delegate_id=NULL, route_note=? WHERE id=?',
               (new_uid, note, step['id']))
    db.commit()
    _notify_step(db, req, db.execute('SELECT * FROM requisition_approvals WHERE id=?', (step['id'],)).fetchone())
    db.commit()
    flash('%s 단계 결재자를 %s(으)로 바꿨습니다.' % (step['label'] or '', person['name']), 'success')
    return redirect(url_for('requisition_detail', req_id=req_id))


# ── 헤드카운트(자리판) ──────────────────────────────────────────────────────────
# 조직도 위에 포지션(빈 자리)을 올린다. 새 테이블은 없다 — 모두 있는 데서 계산한다.
#   헤드카운트 = 정원 대장(상위 조직엔 하위 합계가 들어 있으므로 '자기 몫'만 뗀다)
#   찬 포지션 = 그 조직 재직자
#   빈 포지션 = 포지션 카드(공고 전·채용 진행) + 결재 중 요청서 + 아직 요청 없는 칸
# 빈 날짜를 아는 포지션만 날수를 센다. 모르는 날짜를 지어내지 않는다.
SEAT_LATE_DAYS = 90
SEAT_TAG = {
    'pending':  ('결재 중', 'wait'),
    'approved': ('공고 전', 'idle'),
    'open':     ('채용 진행', 'wait'),
    'reserved': ('입사 예정', 'done'),
    'none':     ('요청 없음', 'idle'),
}


def seat_board_data(db, today=None):
    from datetime import date as _d
    today = today or _d.today()
    fy = today.year

    def _day(v):
        try:
            return _d.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    deps = [dict(r) for r in db.execute(
        'SELECT d.id, d.name, d.parent_id, d.dept_type, d.leader_id, u.name AS leader_name '
        'FROM departments d LEFT JOIN users u ON d.leader_id=u.id ORDER BY d.id').fetchall()]
    by_id = {d['id']: d for d in deps}
    kids = {}
    for d in deps:
        kids.setdefault(d['parent_id'] if d['parent_id'] in by_id else None, []).append(d['id'])
    target = {r[0]: r[1] for r in db.execute(
        'SELECT department_id, target_count FROM department_headcount WHERE fiscal_year=?', (fy,)).fetchall()}

    leaving = {}
    for r in db.execute(
            "SELECT user_id, COALESCE(final_termination_date, requested_termination_date) AS d "
            "FROM termination_requests WHERE status IN ('submitted','under_review','approved','in_progress')"
            ).fetchall():
        if _day(r['d']) and _day(r['d']) >= today:
            leaving[r['user_id']] = r['d']

    seats = {d['id']: [] for d in deps}
    seats[None] = []
    people = db.execute(
        "SELECT u.id, u.name, u.department_id, u.termination_date, u.hire_date, "
        "p.name AS pos_name, COALESCE(p.level, 0) AS lv FROM users u "
        "LEFT JOIN positions p ON u.position_id=p.id WHERE u.status='active' "
        "ORDER BY lv DESC, u.name").fetchall()
    for u in people:
        did = u['department_id'] if u['department_id'] in by_id else None
        out = leaving.get(u['id'])
        if not out and _day(u['termination_date']) and _day(u['termination_date']) >= today:
            out = u['termination_date']
        leader = did is not None and by_id[did]['leader_id'] == u['id']
        seats[did].append({'kind': 'person', 'uid': u['id'], 'name': u['name'],
                           'sub': u['pos_name'] or '', 'leader': leader,
                           'leaving': str(out)[:10] if out else None})
    for did in seats:
        seats[did].sort(key=lambda s: not s['leader'])

    person_at = {s['uid']: s for row in seats.values() for s in row}

    def _empty(did, state, since, bf_uid=None, **kw):
        # 아직 나가지 않은 사람의 후임이면 새 칸을 만들지 않고 그 사람 칸에 붙인다
        if bf_uid in person_at:
            person_at[bf_uid]['successor'] = SEAT_TAG[state][0]
            person_at[bf_uid]['req_id'] = kw.get('req_id')
            return
        days = (today - since).days if since and since <= today else None
        seat = {'kind': 'empty', 'state': state, 'tag': SEAT_TAG[state][0], 'tone': SEAT_TAG[state][1],
                'since': since.isoformat() if since else None, 'days': days,
                'late': days is not None and days > SEAT_LATE_DAYS}
        seat.update(kw)
        seats[did if did in by_id else None].append(seat)

    for o in db.execute(
            "SELECT o.id, o.code, o.title, o.department_id, o.status, o.hire_type, o.requisition_id, "
            "o.created_at, o.backfill_user_id, p.name AS pos_name, bu.name AS backfill_name, "
            "bu.termination_date AS bf_out, "
            "ih.name AS reserved_name, ih.start_date AS reserved_start "
            "FROM job_openings o LEFT JOIN positions p ON o.position_id=p.id "
            "LEFT JOIN users bu ON o.backfill_user_id=bu.id "
            "LEFT JOIN incoming_hires ih ON ih.opening_id=o.id AND ih.status='waiting' "
            "WHERE o.status IN ('approved','open') ORDER BY o.created_at, o.seq").fetchall():
        since = _day(o['created_at'])
        if o['hire_type'] == 'backfill' and _day(o['bf_out']) and _day(o['bf_out']) < (since or today):
            since = _day(o['bf_out'])
        state = 'reserved' if o['reserved_name'] else o['status']
        _empty(o['department_id'], state, since, bf_uid=o['backfill_user_id'], code=o['code'], title=o['title'],
               sub=o['pos_name'] or '', req_id=o['requisition_id'], backfill=o['backfill_name'],
               reserved=o['reserved_name'], start=o['reserved_start'])

    for r in db.execute(
            "SELECT r.id, r.title, r.department_id, r.headcount, r.hire_type, r.created_at, r.backfill_user_id, "
            "bu.name AS backfill_name, bu.termination_date AS bf_out, "
            "(SELECT SUM(headcount) FROM requisition_lines l WHERE l.requisition_id=r.id) AS line_hc "
            "FROM job_requisitions r LEFT JOIN users bu ON r.backfill_user_id=bu.id "
            "WHERE r.status IN ('pending_dept','pending_hr') "
            "AND NOT EXISTS (SELECT 1 FROM job_openings o WHERE o.requisition_id=r.id) "
            "ORDER BY r.created_at").fetchall():
        since = _day(r['created_at'])
        if r['hire_type'] == 'backfill' and _day(r['bf_out']) and _day(r['bf_out']) < (since or today):
            since = _day(r['bf_out'])
        for _i in range(max(1, int(r['line_hc'] or r['headcount'] or 1))):
            _empty(r['department_id'], 'pending', since, bf_uid=r['backfill_user_id'], title=r['title'], sub='',
                   req_id=r['id'], backfill=r['backfill_name'])

    # 정원에서 '자기 몫' — 하위 조직 정원을 뺀 나머지
    own = {}
    for d in deps:
        if d['id'] in target:
            below = sum(target.get(k, 0) for k in kids.get(d['id'], []))
            own[d['id']] = max(0, target[d['id']] - below)

    # 요청 없는 빈 칸의 날짜 — 그 조직에서 최근에 나간 사람 순서대로 맞춘다.
    # 이미 후임 요청·카드가 있는 퇴사자는 뺀다. 나간 사람이 모자라면 날짜 없음.
    covered = {r[0] for r in db.execute(
        'SELECT backfill_user_id FROM job_openings WHERE backfill_user_id IS NOT NULL '
        'UNION SELECT backfill_user_id FROM job_requisitions WHERE backfill_user_id IS NOT NULL').fetchall()}
    leavers = {}
    for u in db.execute(
            "SELECT id, name, department_id, termination_date FROM users "
            "WHERE status='resigned' AND termination_date IS NOT NULL "
            "ORDER BY termination_date DESC").fetchall():
        if u['id'] not in covered and _day(u['termination_date']) and _day(u['termination_date']) <= today:
            leavers.setdefault(u['department_id'], []).append(u)

    nodes = []

    def _walk(pid, depth):
        tot = {'seats': 0, 'filled': 0, 'empty': 0, 'late': 0, 'over': 0}
        for did in kids.get(pid, []):
            d = by_id[did]
            row = seats[did]
            filled = sum(1 for s in row if s['kind'] == 'person')
            pipeline = len(row) - filled
            gap = (own[did] - filled - pipeline) if did in own else 0
            gone = leavers.get(did, [])
            for i in range(max(0, gap)):
                lv = gone[i] if i < len(gone) else None
                _empty(did, 'none', _day(lv['termination_date']) if lv else None,
                       backfill=lv['name'] if lv else None)
            node = {'id': did, 'name': d['name'], 'type': d['dept_type'], 'leader': d['leader_name'],
                    'depth': depth, 'target': own.get(did), 'seats': seats[did],
                    'over': max(0, -gap) if did in own else 0}
            nodes.append(node)
            sub = _walk(did, depth + 1)
            mine = {'seats': len(seats[did]), 'filled': filled,
                    'empty': len(seats[did]) - filled,
                    'late': sum(1 for s in seats[did] if s.get('late')), 'over': node['over']}
            node['own'] = mine
            node['all'] = {k: mine[k] + sub[k] for k in mine}
            for k in tot:
                tot[k] += node['all'][k]
        return tot

    total = _walk(None, 0)
    if seats[None]:
        row = seats[None]
        filled = sum(1 for s in row if s['kind'] == 'person')
        mine = {'seats': len(row), 'filled': filled, 'empty': len(row) - filled,
                'late': sum(1 for s in row if s.get('late')), 'over': 0}
        nodes.append({'id': 0, 'name': '부서 미지정', 'type': None, 'leader': None, 'depth': 0,
                      'target': None, 'seats': row, 'over': 0, 'own': mine, 'all': mine})
        for k in total:
            total[k] += mine[k]
    return {'nodes': nodes, 'total': total, 'kids': kids, 'parent': {d['id']: d['parent_id'] for d in deps}}


@app.route('/seats')
@login_required
def seat_board():
    role = session.get('user_role')
    if role not in ('admin', 'manager', 'recruiter'):
        abort(403)
    db = get_db()
    data = seat_board_data(db)
    nodes, kids = data['nodes'], data['kids']

    def _subtree(root):
        out, stack = set(), [root]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(kids.get(cur, []))
        return out

    scope = None
    if role == 'manager':
        scope = _subtree(session.get('dept_id') or -1)
    dept_f = request.args.get('dept', type=int)
    if dept_f:
        sub = _subtree(dept_f)
        scope = sub if scope is None else (scope & sub)
    view = request.args.get('view', 'all')
    if view not in ('all', 'empty', 'late'):
        view = 'all'

    shown = [n for n in nodes if scope is None or n['id'] in scope]
    if scope is not None:
        roots = [n for n in shown if data['parent'].get(n['id']) not in scope]
        total = {k: sum(n['all'][k] for n in roots) for k in data['total']}
        base = min((n['depth'] for n in shown), default=0)
    else:
        total, base = data['total'], 0
    if view == 'empty':
        shown = [n for n in shown if n['all']['empty']]
    elif view == 'late':
        shown = [n for n in shown if n['all']['late']]

    dept_opts = [n for n in nodes if n['id'] and (role != 'manager' or n['id'] in _subtree(session.get('dept_id') or -1))]
    return render_template('hiring/seat_board.html',
        nodes=shown, total=total, base=base, view=view, dept_f=dept_f, dept_opts=dept_opts,
        dept_type_label=DEPT_TYPE_LABEL, late_days=SEAT_LATE_DAYS,
        can_request=True, active_page='seats')


# ── 자리 대장 ────────────────────────────────────────────────────────
@app.route('/openings')
@login_required
def opening_board():
    """열려 있는 자리를 한 장씩 세는 화면. 부서 총원이 아니라 '자리'가 단위다."""
    db     = get_db()
    role   = session.get('user_role')
    status = request.args.get('status', 'live')

    sql = ('SELECT o.*, d.name AS dept_name, p.name AS pos_name, p.level AS pos_level, '
           'r.requester_id, ru.name AS requester_name, bu.name AS backfill_name, '
           'hu.name AS hired_name, ih.name AS reserved_name, ih.start_date AS reserved_start '
           'FROM job_openings o '
           'LEFT JOIN departments d ON o.department_id=d.id '
           'LEFT JOIN positions   p ON o.position_id=p.id '
           'LEFT JOIN job_requisitions r ON o.requisition_id=r.id '
           'LEFT JOIN users ru ON r.requester_id=ru.id '
           'LEFT JOIN users bu ON o.backfill_user_id=bu.id '
           'LEFT JOIN users hu ON o.hired_user_id=hu.id '
           "LEFT JOIN incoming_hires ih ON ih.opening_id=o.id AND ih.status='waiting' WHERE 1=1")
    params = []
    if role not in ('admin', 'recruiter'):
        sql += ' AND o.department_id=?'
        params.append(session.get('dept_id') or 0)
    if status == 'live':
        sql += " AND o.status IN ('approved','open')"
    elif status in OPENING_STATUS_LABEL:
        sql += ' AND o.status=?'
        params.append(status)
    sql += ' ORDER BY o.created_at DESC, o.seq'
    rows = db.execute(sql, params).fetchall()

    counts = {r['status']: r['c'] for r in db.execute(
        'SELECT status, COUNT(*) c FROM job_openings GROUP BY status').fetchall()}

    return render_template('hiring/opening_board.html',
        rows=rows, counts=counts, status_filter=status,
        opening_labels=OPENING_STATUS_LABEL,
        hire_type_labels=REQUISITION_HIRE_TYPE_LABEL,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        can_edit=(role in ('admin', 'recruiter')),
        # Hire 주소·열쇠가 없으면 '보내기'는 아예 나타나지 않는다.
        # 눌러도 안 되는 버튼을 보여주는 게 제일 나쁘다.
        hire_ready=_hire_config()['ready'],
        active_page='opening'
    )


@app.route('/openings/<int:op_id>/close', methods=['POST'])
@recruiter_or_admin
def opening_close(op_id):
    reason = request.form.get('reason', '').strip()
    db = get_db()
    db.execute("UPDATE job_openings SET status='closed', close_reason=?, "
               "closed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
               "WHERE id=? AND status IN ('approved','open')", (reason, op_id))
    db.commit()
    flash('포지션을 마감했습니다.', 'success')
    return redirect(request.referrer or url_for('opening_board'))


@app.route('/openings/<int:op_id>/reopen', methods=['POST'])
@recruiter_or_admin
def opening_reopen(op_id):
    db = get_db()
    db.execute("UPDATE job_openings SET status='approved', close_reason=NULL, closed_at=NULL, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='closed'", (op_id,))
    db.commit()
    flash('포지션을 재개했습니다.', 'success')
    return redirect(request.referrer or url_for('opening_board'))


# ── Hire(Cadence) 연동 ───────────────────────────────────────────────
# 승인된 자리 카드를 Hire 로 넘긴다. 방향은 한쪽뿐이다 —
# 이쪽(TalentCore)이 밀고, Hire 는 받기만 한다. 조직·정원·결재는 여기 것이고
# 전형 설계·후보자 관리는 Hire 것이라는 경계를 지키기 위해서다.
#
# 자리 카드 여러 장 → 공고 한 건. 같은 직무 3명을 뽑을 때 보드를 3개로
# 쪼개면 지원자를 어느 보드에 넣을지 매번 골라야 한다(Greenhouse·Workday 도
# 요청과 공고를 분리하고 자리 여러 개를 공고 하나에 매단다).

def _hire_config():
    """Hire 주소·토큰. 관리자가 /settings/hire 에서 넣는다."""
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM company_settings WHERE key IN ('hire_url','hire_token')"
    ).fetchall()
    cfg = {r['key']: (r['value'] or '').strip() for r in rows}
    url = cfg.get('hire_url', '').rstrip('/')
    token = cfg.get('hire_token', '')
    return {'url': url, 'token': token, 'ready': bool(url and token)}


@app.route('/settings/hire', methods=['GET', 'POST'])
@admin_required
def hire_settings():
    """Hire 가 어디에 있고 어떤 열쇠로 여는지 — 관리자가 한 번 넣어두면 끝난다."""
    db = get_db()
    if request.method == 'POST':
        url   = request.form.get('hire_url', '').strip().rstrip('/')
        token = request.form.get('hire_token', '').strip()
        auto  = '1' if request.form.get('auto_contract') else '0'
        if url and not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        for k, v in (('hire_url', url), ('hire_token', token), ('auto_contract', auto)):
            db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
        db.commit()
        flash('Hire 연동 정보 저장 완료' if url and token
              else '저장 완료 · 주소와 연동 토큰 모두 등록 시 공고 등록 가능', 'success')
        return redirect(url_for('hire_settings'))

    cfg = _hire_config()
    linked = db.execute(
        "SELECT COUNT(*) c FROM job_openings WHERE external_ref IS NOT NULL AND external_ref<>''"
    ).fetchone()['c']
    auto_contract = _auto_contract_enabled(db)
    ignited = db.execute(
        "SELECT COUNT(*) c FROM job_openings WHERE status='filled' AND hired_user_id IS NOT NULL"
    ).fetchone()['c']
    return render_template('hiring/hire_settings.html',
        cfg=cfg, linked=linked, auto_contract=auto_contract, ignited=ignited,
        active_page='hiresettings')


@app.route('/openings/push', methods=['POST'])
@recruiter_or_admin
def opening_push():
    """고른 자리 카드를 Hire 공고 한 건으로 만든다."""
    db  = get_db()
    cfg = _hire_config()
    back = request.referrer or url_for('opening_board')

    if not cfg['ready']:
        flash('Hire 주소·연동 토큰 미등록 (설정 > Hire 연동)', 'error')
        return redirect(back)

    try:
        op_ids = [int(x) for x in request.form.getlist('op_ids')]
    except ValueError:
        op_ids = []
    if not op_ids:
        flash('포지션을 1건 이상 선택하세요.', 'error')
        return redirect(back)

    q = ','.join('?' * len(op_ids))
    rows = db.execute(
        'SELECT o.*, d.name AS dept_name, p.name AS position_name FROM job_openings o '
        'LEFT JOIN departments d ON o.department_id=d.id '
        'LEFT JOIN positions p ON o.position_id=p.id '
        'WHERE o.id IN (%s) ORDER BY o.seq' % q, op_ids).fetchall()

    if len(rows) != len(op_ids):
        flash('존재하지 않는 포지션이 포함되어 있습니다. 새로 고침 후 다시 선택하세요.', 'error')
        return redirect(back)
    # 이미 넘어간 자리를 또 보내면 Hire 에 같은 공고가 두 개 생긴다.
    dup = [r for r in rows if r['external_ref']]
    if dup:
        flash('이미 Hire 공고에 등록된 포지션 — %s' % ', '.join(r['code'] for r in dup), 'error')
        return redirect(back)
    bad = [r for r in rows if r['status'] != 'approved']
    if bad:
        flash('공고 등록 불가 포지션 — %s' % ', '.join(r['code'] for r in bad), 'error')
        return redirect(back)
    # 직무·부서·밴드가 다른 자리를 한 공고에 묶으면 공고 내용이 거짓말이 된다.
    req_ids = {r['requisition_id'] for r in rows}
    if len(req_ids) != 1:
        flash('동일 채용 요청의 포지션끼리만 공고 1건으로 등록할 수 있습니다.', 'error')
        return redirect(back)

    req = db.execute('SELECT * FROM job_requisitions WHERE id=?', (rows[0]['requisition_id'],)).fetchone()
    if not req:
        flash('원본 요청서를 찾을 수 없습니다.', 'error')
        return redirect(back)

    first = rows[0]

    # 면접관 세 자리 — 요청서가 이미 알고 있는 사람들이다.
    #   1차 = 그 부서의 부서장(HM)
    #   2차 = 차상위 리더 + 요청서에서 고른 협업 리더 (이어서 60+60분)
    # 이걸 안 보내면 Hire 는 공고를 '면접관 없음'으로 열고, 후보자가 인터뷰
    # 단계에 서는 순간 조율이 통째로 막힌다. 사람 확인은 사번으로 한다 —
    # 동명이인이 있는 회사에서 이름만 보내면 엉뚱한 사람에게 면접이 잡힌다.
    chain = _dept_leader_chain(db, first['department_id'] or req['department_id'])

    def _who(uid):
        if not uid:
            return None
        u = db.execute('SELECT emp_no, name FROM users WHERE id=? AND status="active"',
                       (uid,)).fetchone()
        if not u:
            return None
        return {'emp_no': u['emp_no'] or '', 'name': u['name'] or ''}

    panel = {
        'hm':     _who(chain['hm_id']),
        'upper':  _who(chain['senior_id']),
        'collab': _who(req['collab_leader_id']),
    }

    jd_parts = []
    if req['reason']:
        jd_parts.append('[채용 배경]\n' + req['reason'])
    if req['required_skills']:
        jd_parts.append('[필요 역량]\n' + req['required_skills'])

    payload = {
        'title':        first['title'] or req['title'],
        # 직급은 자리 카드가 들고 있는 값이다(L4 — Senior). 공고 제목과는 다르다 —
        # 이걸 안 보내면 Hire 의 오퍼 초안이 '직급' 칸에 공고 제목을 넣어 버린다.
        'level':        first['position_name'] or '',
        'dept':         first['dept_name'] or '',
        'emp':          REQUISITION_EMP_TYPE_LABEL.get(
                            first['employment_type'] or req['employment_type'], '정규직'),
        # Hire 의 밴드 단위는 만원이다. 여기 저장 단위는 원.
        'band_lo':      int((first['salary_min'] or req['salary_min'] or 0) // 10000),
        'band_hi':      int((first['salary_max'] or req['salary_max'] or 0) // 10000),
        'jd':           '\n\n'.join(jd_parts),
        'target_start': first['target_start_date'] or req['target_start_date'] or '',
        'hire_type':    REQUISITION_HIRE_TYPE_LABEL.get(req['hire_type'], ''),
        'req_ref':      'REQ-%d' % req['id'],
        'openings':     [{'id': r['id'], 'code': r['code']} for r in rows],
        'team':         first['dept_name'] or '',
        'hiring_manager': (panel['hm'] or {}).get('name', ''),
        'panel':        panel,
    }

    body = json.dumps(payload).encode('utf-8')
    hreq = urllib.request.Request(
        cfg['url'] + '/api/openings', data=body,
        headers={'Content-Type': 'application/json', 'X-API-Token': cfg['token']})
    try:
        with urllib.request.urlopen(hreq, timeout=15) as resp:
            out = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail, err = '', {}
        try:
            err = json.loads(e.read().decode('utf-8')) or {}
            detail = err.get('message') or err.get('error') or ''
        except Exception:
            pass
        if e.code == 409 and err.get('error') == 'already-linked' and err.get('position_id'):
            # 지난번에 보냈는데 응답을 못 받은 경우 — Hire 에 이미 공고가 있으니 그 번호로 연결만 한다
            out = {'ok': True, 'position_id': err['position_id']}
        else:
            flash('Hire 가 받지 않았습니다 (%s). %s' % (e.code, detail), 'error')
            return redirect(back)
    except Exception as e:
        # 승인·포지션 카드는 이미 저장돼 있다 — 보내기만 실패했으니 Hire 를 켜고 다시 누르면 된다
        reason = getattr(e, 'reason', e)
        if isinstance(reason, ConnectionRefusedError) or 'refused' in str(reason).lower() or '10061' in str(reason):
            why = 'Hire 서버가 꺼져 있습니다'
        elif 'timed out' in str(reason).lower() or isinstance(reason, TimeoutError):
            why = 'Hire 서버가 응답하지 않습니다'
        else:
            why = '주소를 찾을 수 없습니다'
        flash('Hire(%s)에 보내지 못했습니다 — %s. 포지션은 그대로 남아 있으니 Hire가 켜진 뒤 다시 보내세요. '
              '주소는 설정 > Hire 연동에서 바꿉니다.' % (cfg['url'], why), 'error')
        return redirect(back)

    pid = (out or {}).get('position_id')
    if not (out or {}).get('ok') or not pid:
        flash('Hire 가 공고 번호를 돌려주지 않았습니다. 잠시 뒤 다시 시도해 주세요.', 'error')
        return redirect(back)

    for r in rows:
        db.execute("UPDATE job_openings SET status='open', external_ref=?, "
                   "opened_at=COALESCE(opened_at, CURRENT_TIMESTAMP), "
                   "updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid, r['id']))
    # 이 요청서에서 아직 안 넘어간 자리가 없으면 요청서 자체를 '넘김'으로 닫는다.
    left = db.execute("SELECT COUNT(*) c FROM job_openings "
                      "WHERE requisition_id=? AND status='approved'", (req['id'],)).fetchone()['c']
    if not left:
        db.execute("UPDATE job_requisitions SET status='posted', updated_at=CURRENT_TIMESTAMP "
                   "WHERE id=? AND status='approved'", (req['id'],))
    if req['requester_id']:
        add_notification(req['requester_id'], 'info', 'action', '채용 공고 개설',
                         u'채용 요청 "%s" 포지션 %d건 Hire 공고 등록' % (req['title'], len(rows)),
                         link=url_for('requisition_detail', req_id=req['id']))
    db.commit()

    flash(u'포지션 %d건 Hire 공고 %s 등록 완료' % (len(rows), pid), 'success')
    return redirect(back)


# ══════════════════════════════════════════════════════════════════════
#  오퍼 밴드 초과 결재 — Hire 에서 올라온다
#  공고에 적힌 연봉 밴드 상한을 넘는 오퍼는 회사가 한 번 더 본다.
#  결재선을 푸는 기계(_plan_flow)는 요청서와 같은 것을 쓰고, 표만 따로 둔다.
#  Hire 는 결재 건 번호만 들고 있다가 화면을 그릴 때마다 상태를 물어본다.
# ══════════════════════════════════════════════════════════════════════
OFFER_APPROVAL_STATUS_LABEL = {
    'pending': '결재 중', 'approved': '승인', 'rejected': '반려', 'cancelled': '취소',
}


def _offer_notify(db, uid, title, content, link=None):
    """add_notification 과 같은 일 — 다만 연결을 넘겨 받는다(API 는 세션이 없다)."""
    if not uid:
        return
    db.execute(
        'INSERT INTO notifications (user_id, type, category, title, content, link) '
        'VALUES (?,?,?,?,?,?)', (uid, 'info', 'action', title, content, link))


def _build_offer_flow(db, off):
    """오퍼 한 건에 결재 단계를 깔아 준다(이미 있으면 그대로). 결재자는 이 순간 고정된다."""
    if db.execute('SELECT 1 FROM offer_approval_steps WHERE offer_id=? LIMIT 1',
                  (off['id'],)).fetchone():
        return
    cum = 0
    for st in _plan_flow(db, off['department_id'], off['requester_id'], 'offer_band'):
        if st['skipped']:
            continue
        cum += st['sla_days']
        db.execute(
            'INSERT INTO offer_approval_steps '
            '(offer_id, step_no, role_kind, exec_key, label, due_at, assignee_id, delegate_id, route_note) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (off['id'], st['step_no'], st['role_kind'], st['exec_key'], st['label'],
             (datetime.now() + timedelta(days=cum)).strftime('%Y-%m-%d %H:%M:%S'),
             st['assignee_id'], st['delegate_id'], st['note'] or None))
    db.commit()


def _offer_steps(db, oid):
    return db.execute(
        'SELECT a.*, u.name AS approver_name, s.name AS assignee_name, g.name AS delegate_name, '
        '       sp.name AS assignee_pos, sd.name AS assignee_dept '
        'FROM offer_approval_steps a '
        'LEFT JOIN users u ON a.approver_id=u.id '
        'LEFT JOIN users s ON a.assignee_id=s.id '
        'LEFT JOIN positions sp ON s.position_id=sp.id '
        'LEFT JOIN departments sd ON s.department_id=sd.id '
        'LEFT JOIN users g ON a.delegate_id=g.id '
        'WHERE a.offer_id=? ORDER BY a.step_no', (oid,)).fetchall()


def _offer_current_step(db, oid):
    return db.execute(
        "SELECT * FROM offer_approval_steps WHERE offer_id=? AND status='waiting' "
        "ORDER BY step_no LIMIT 1", (oid,)).fetchone()


def _can_act_on_offer_step(db, off, step, uid, role):
    if step is None or off['status'] != 'pending':
        return False
    if role == 'admin':
        return True
    return uid in _step_approver_ids(db, off, dict(step))


def _is_offer_approver(db, oid, uid):
    """결재선에 이름이 올라간 사람 — 부서가 달라도 이 건을 볼 수 있어야 한다."""
    return bool(db.execute(
        'SELECT 1 FROM offer_approval_steps WHERE offer_id=? AND (assignee_id=? OR delegate_id=?) LIMIT 1',
        (oid, uid, uid)).fetchone())


def _notify_offer_step(db, off, step):
    """지금 차례인 사람에게만 알린다."""
    for aid in _step_approver_ids(db, off, dict(step)):
        _offer_notify(db, aid, '오퍼 결재',
                      u'%s 님 오퍼(%s)가 %s 단계에서 결재를 기다립니다.'
                      % (off['cand_name'], off['position_title'] or '직무 미기재',
                         step['label'] or FLOW_ROLE_LABEL.get(step['role_kind'], '')),
                      link=url_for('offer_approval_detail', oid=off['id']))
    db.commit()


def _offer_state(db, off):
    """Hire 가 물어볼 때 내려줄 요약 — 상태·단계·지금 차례인 사람."""
    steps = _offer_steps(db, off['id'])
    cur   = _offer_current_step(db, off['id']) if off['status'] == 'pending' else None
    rej   = next((s for s in steps if s['status'] == 'rejected'), None)
    return {
        'ok': True, 'id': off['id'], 'ref': off['ext_ref'], 'status': off['status'],
        'status_label': OFFER_APPROVAL_STATUS_LABEL.get(off['status'], off['status']),
        'base': off['base'], 'sign': off['sign'],
        'band_hi': off['band_hi'], 'decided_at': off['decided_at'],
        'reject_reason': (rej['comment'] if rej else None),
        'reject_by': (rej['approver_name'] if rej else None),
        'current': ({'step_no': cur['step_no'], 'label': cur['label'],
                     'name': _person_brief(db, cur['assignee_id'])['name'],
                     'due_at': cur['due_at']} if cur else None),
        'steps': [{'step_no': s['step_no'], 'label': s['label'], 'status': s['status'],
                   'name': s['assignee_name'], 'pos': s['assignee_pos'],
                   'acted_at': s['acted_at'], 'comment': s['comment'],
                   'by': s['approver_name']} for s in steps],
    }


def _offer_latest(db, ref):
    return db.execute(
        'SELECT * FROM offer_approvals WHERE ext_ref=? ORDER BY id DESC LIMIT 1', (ref,)).fetchone()


# ── 오퍼 결재함 (화면) ───────────────────────────────────────────────
@app.route('/offers/approvals')
@login_required
def offer_approvals():
    """밴드를 넘긴 오퍼만 여기로 온다. 채용 담당·관리자는 전부, 나머지는 자기 결재 건만."""
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    rows = db.execute(
        'SELECT o.*, '
        " (SELECT s.label FROM offer_approval_steps s "
        "   WHERE s.offer_id=o.id AND s.status='waiting' ORDER BY s.step_no LIMIT 1) AS cur_label, "
        " (SELECT s.assignee_id FROM offer_approval_steps s "
        "   WHERE s.offer_id=o.id AND s.status='waiting' ORDER BY s.step_no LIMIT 1) AS cur_assignee, "
        " (SELECT u.name FROM offer_approval_steps s LEFT JOIN users u ON u.id=s.assignee_id "
        "   WHERE s.offer_id=o.id AND s.status='waiting' ORDER BY s.step_no LIMIT 1) AS cur_name "
        'FROM offer_approvals o ORDER BY o.created_at DESC LIMIT 300').fetchall()

    wide = role in ('admin', 'recruiter')
    offers, mine = [], 0
    for r in rows:
        if not wide and not _is_offer_approver(db, r['id'], uid):
            continue
        turn = r['status'] == 'pending' and (role == 'admin' or r['cur_assignee'] == uid)
        offers.append({'r': r, 'turn': turn})
        if turn:
            mine += 1
    return render_template('hiring/offer_approvals.html',
        offers=offers, my_turn=mine,
        status_labels=OFFER_APPROVAL_STATUS_LABEL,
        active_page='offerappr')


@app.route('/offers/approvals/<int:oid>')
@login_required
def offer_approval_detail(oid):
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    off  = db.execute('SELECT * FROM offer_approvals WHERE id=?', (oid,)).fetchone()
    if not off:
        abort(404)
    if role not in ('admin', 'recruiter') and not _is_offer_approver(db, oid, uid):
        abort(403)
    steps = _offer_steps(db, oid)
    cur   = _offer_current_step(db, oid) if off['status'] == 'pending' else None
    return render_template('hiring/offer_approval_detail.html',
        off=off, steps=steps, cur_step=cur,
        can_act=_can_act_on_offer_step(db, off, cur, uid, role),
        status_labels=OFFER_APPROVAL_STATUS_LABEL,
        role_labels=FLOW_ROLE_LABEL,
        acted_labels=ACTED_AS_LABEL,
        active_page='offerappr')


@app.route('/offers/approvals/<int:oid>/act', methods=['POST'])
@login_required
def offer_approval_act(oid):
    """지금 차례인 한 단계를 처리한다. 요청서 결재와 같은 규칙."""
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    off  = db.execute('SELECT * FROM offer_approvals WHERE id=?', (oid,)).fetchone()
    if not off or off['status'] != 'pending':
        flash('처리할 수 없는 결재입니다.', 'error')
        return redirect(url_for('offer_approvals'))

    step = _offer_current_step(db, oid)
    if not _can_act_on_offer_step(db, off, step, uid, role):
        flash('이 단계의 결재자가 아닙니다.', 'error')
        return redirect(url_for('offer_approval_detail', oid=oid))

    action  = request.form.get('action', 'approve')
    comment = request.form.get('comment', '').strip()
    if not step['assignee_id'] or uid == step['assignee_id']:
        acted_as = 'self'
    elif uid in _step_approver_ids(db, off, dict(step)):
        acted_as = 'delegate'
    else:
        acted_as = 'admin'

    if action != 'approve':
        if not comment:
            flash('반려 사유를 적어 주세요.', 'error')
            return redirect(url_for('offer_approval_detail', oid=oid))
        db.execute(
            "UPDATE offer_approval_steps SET status='rejected', approver_id=?, comment=?, acted_as=?, "
            "acted_at=CURRENT_TIMESTAMP WHERE id=?", (uid, comment, acted_as, step['id']))
        db.execute("UPDATE offer_approvals SET status='rejected', decided_at=CURRENT_TIMESTAMP "
                   "WHERE id=?", (oid,))
        _offer_notify(db, off['requester_id'], '오퍼 반려',
                      u'%s 님 오퍼가 %s 단계에서 반려되었습니다. 사유: %s'
                      % (off['cand_name'], step['label'] or '', comment),
                      link=url_for('offer_approval_detail', oid=oid))
        db.commit()
        flash('반려했습니다. Hire 오퍼 화면에 사유가 그대로 보입니다.', 'success')
        return redirect(url_for('offer_approval_detail', oid=oid))

    db.execute(
        "UPDATE offer_approval_steps SET status='approved', approver_id=?, comment=?, acted_as=?, "
        "acted_at=CURRENT_TIMESTAMP WHERE id=?", (uid, comment, acted_as, step['id']))
    db.commit()

    nxt = _offer_current_step(db, oid)
    if nxt:
        _notify_offer_step(db, off, nxt)
        flash(u'승인했습니다. 다음은 %s 단계입니다.' % (nxt['label'] or ''), 'success')
        return redirect(url_for('offer_approval_detail', oid=oid))

    db.execute("UPDATE offer_approvals SET status='approved', decided_at=CURRENT_TIMESTAMP WHERE id=?",
               (oid,))
    _offer_notify(db, off['requester_id'], '오퍼 최종 승인',
                  u'%s 님 오퍼가 최종 승인되었습니다. Hire 에서 발송할 수 있습니다.' % off['cand_name'],
                  link=url_for('offer_approval_detail', oid=oid))
    db.commit()
    flash('최종 승인했습니다. Hire 에서 오퍼를 보낼 수 있습니다.', 'success')
    return redirect(url_for('offer_approval_detail', oid=oid))


# ── 오퍼 결재 API (Hire 전용) ────────────────────────────────────────
def _offer_api_db():
    """토큰을 확인하고 그 회사의 DB 연결을 연다. (연결, 내가 닫아야 하나) 를 돌려준다."""
    tenant = get_tenant_by_api_token(request.headers.get('X-API-Token', ''))
    if not tenant:
        return None, False
    if tenant['id'] == session.get('tenant_id', 1):
        return get_db(), False
    conn = sqlite3.connect(get_tenant_db_path(tenant['id']))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn, True


@app.route('/api/offers/approval', methods=['POST'])
def offer_approval_create_api():
    """Hire 가 밴드를 넘긴 오퍼를 결재로 올린다.

    인증: X-API-Token (/api/hires · /api/openings 와 같은 열쇠)
    JSON {ref, cand_name, position_title, opening_code, department_name, level,
          base, sign, band_lo, band_hi, start_date, requester_core_id, requester_name, note}
    금액은 만원 단위(Hire 화면과 같은 단위)로 받는다.
    같은 ref 로 이미 결재 중인 건이 있으면 새로 만들지 않고 그것을 돌려준다.
    """
    db, own = _offer_api_db()
    if db is None:
        return {'ok': False, 'error': 'invalid token'}, 401
    try:
        j    = request.get_json(silent=True) or {}
        ref  = str(j.get('ref') or '').strip()[:120]
        name = str(j.get('cand_name') or '').strip()[:60]
        if not ref or not name:
            return {'ok': False, 'error': 'ref · cand_name 이 필요합니다'}, 400

        def num(k):
            try:
                return max(0, int(j.get(k) or 0))
            except (TypeError, ValueError):
                return 0

        prev = _offer_latest(db, ref)
        # 결재 중이면 그 건을, 이미 승인된 건과 금액이 같으면 그 승인을 돌려준다.
        # (금액을 고쳐 다시 올리면 새 결재를 태운다)
        if prev and (prev['status'] == 'pending'
                     or (prev['status'] == 'approved'
                         and (prev['base'] or 0) == num('base')
                         and (prev['sign'] or 0) == num('sign'))):
            return _offer_state(db, prev), 200

        # 부서 — 공고 번호로 역추적하고, 없으면 부서 이름으로 맞춰 본다
        dept_id, dept_name = None, str(j.get('department_name') or '').strip()[:60]
        code = str(j.get('opening_code') or '').strip()[:40]
        if code:
            r = db.execute('SELECT department_id FROM job_openings WHERE code=? ORDER BY id DESC LIMIT 1',
                           (code,)).fetchone()
            if r:
                dept_id = r['department_id']
        if not dept_id and dept_name:
            r = db.execute('SELECT id FROM departments WHERE name=? LIMIT 1', (dept_name,)).fetchone()
            if r:
                dept_id = r['id']
        if dept_id and not dept_name:
            r = db.execute('SELECT name FROM departments WHERE id=?', (dept_id,)).fetchone()
            dept_name = r['name'] if r else ''

        req_id = None
        try:
            cand = int(j.get('requester_core_id') or 0)
        except (TypeError, ValueError):
            cand = 0
        if cand:
            r = db.execute("SELECT id FROM users WHERE id=? AND status='active'", (cand,)).fetchone()
            req_id = r['id'] if r else None

        cur = db.execute(
            'INSERT INTO offer_approvals '
            '(ext_ref, cand_name, position_title, department_id, department_name, opening_code, '
            ' level, base, sign, band_lo, band_hi, start_date, requester_id, requester_name, note) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ref, name, str(j.get('position_title') or '').strip()[:120] or None,
             dept_id, dept_name or None, code or None,
             str(j.get('level') or '').strip()[:20] or None,
             num('base'), num('sign'), num('band_lo'), num('band_hi'),
             str(j.get('start_date') or '').strip()[:10] or None,
             req_id, str(j.get('requester_name') or '').strip()[:40] or None,
             str(j.get('note') or '').strip()[:500] or None))
        db.commit()
        off = db.execute('SELECT * FROM offer_approvals WHERE id=?', (cur.lastrowid,)).fetchone()
        _build_offer_flow(db, off)
        step = _offer_current_step(db, off['id'])
        if step:
            _notify_offer_step(db, off, step)
        else:
            # 결재자를 한 명도 못 찾았다 — 사람이 없는데 승인된 척하면 안 된다
            db.execute("UPDATE offer_approvals SET status='cancelled', decided_at=CURRENT_TIMESTAMP "
                       "WHERE id=?", (off['id'],))
            db.commit()
            return {'ok': False,
                    'error': '결재자를 찾지 못했습니다 — TalentCore 결재선 설정을 확인해 주세요'}, 409
        return _offer_state(db, off), 201
    finally:
        if own:
            db.close()


@app.route('/api/offers/approval', methods=['GET'])
def offer_approval_state_api():
    """이 후보자 오퍼가 지금 어디까지 왔나. ?ref=<Hire 후보자 id>"""
    db, own = _offer_api_db()
    if db is None:
        return {'ok': False, 'error': 'invalid token'}, 401
    try:
        ref = (request.args.get('ref') or '').strip()[:120]
        if not ref:
            return {'ok': False, 'error': 'ref 가 필요합니다'}, 400
        off = _offer_latest(db, ref)
        if not off:
            return {'ok': True, 'status': 'none'}, 200
        return _offer_state(db, off), 200
    finally:
        if own:
            db.close()


@app.route('/api/offers/approval/cancel', methods=['POST'])
def offer_approval_cancel_api():
    """Hire 에서 오퍼를 초안으로 되돌렸다 — 결재함에서 내린다. JSON {ref}"""
    db, own = _offer_api_db()
    if db is None:
        return {'ok': False, 'error': 'invalid token'}, 401
    try:
        ref = str((request.get_json(silent=True) or {}).get('ref') or '').strip()[:120]
        if not ref:
            return {'ok': False, 'error': 'ref 가 필요합니다'}, 400
        off = _offer_latest(db, ref)
        if not off:
            return {'ok': True, 'status': 'none'}, 200
        if off['status'] == 'pending':
            db.execute("UPDATE offer_approvals SET status='cancelled', decided_at=CURRENT_TIMESTAMP "
                       "WHERE id=?", (off['id'],))
            db.commit()
            off = db.execute('SELECT * FROM offer_approvals WHERE id=?', (off['id'],)).fetchone()
        return _offer_state(db, off), 200
    finally:
        if own:
            db.close()


# ── 오퍼레터 문안 ─────────────────────────────────
# 오퍼레터의 뼈대(처우·입사일·회신 기한)는 Hire 가 그린다. 회사마다 달라지는
# 말 — 인사말·복리후생·서명자 — 만 여기서 정한다. 회사 정보는 TalentCore 것이고,
# 같은 문장이 계약서·안내문에도 쓰이기 때문이다.
OFFER_LETTER_DEFAULTS = {
    'offer_letter_greeting': u'함께 일하게 되어 기쁩니다. 아래와 같이 입사 조건을 안내드립니다.',
    'offer_letter_benefits': u'',            # 한 줄에 하나
    'offer_letter_signer': u'',              # 비우면 Hire 가 채용 담당자 이름으로 적는다
    'offer_letter_signer_title': u'',
    'offer_letter_reply_days': u'7',
    'offer_letter_vest_years': u'4',
    'offer_letter_vest_cliff': u'12',
    'offer_letter_equity_note': u'스톡옵션은 비상장 주식을 살 수 있는 권리이며, 미래 가치는 보장되지 않습니다.',
}


def _offer_letter_settings(db):
    """저장된 문안 + 기본값. 없는 값은 기본값으로 채운다."""
    rows = db.execute(
        "SELECT key, value FROM company_settings WHERE key LIKE 'offer_letter_%'").fetchall()
    cfg = dict(OFFER_LETTER_DEFAULTS)
    for r in rows:
        if r['key'] in cfg and (r['value'] or '').strip():
            cfg[r['key']] = r['value']
    return cfg


def _offer_letter_int(cfg, key, lo, hi, dflt):
    try:
        n = int(str(cfg.get(key) or '').strip())
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, n))


@app.route('/settings/offer-letter', methods=['GET', 'POST'])
@admin_required
def offer_letter_settings():
    """오퍼레터에 들어갈 회사 말 — 관리자가 한 번 정해두면 모든 오퍼에 같이 나간다."""
    db = get_db()
    if request.method == 'POST':
        f = request.form
        vals = {
            'offer_letter_greeting': f.get('greeting', '').strip()[:600],
            'offer_letter_benefits': f.get('benefits', '').strip()[:2000],
            'offer_letter_signer': f.get('signer', '').strip()[:60],
            'offer_letter_signer_title': f.get('signer_title', '').strip()[:60],
            'offer_letter_reply_days': str(max(1, min(30, int(f.get('reply_days') or 7)))
                                           if (f.get('reply_days') or '7').isdigit() else 7),
            'offer_letter_vest_years': str(max(1, min(10, int(f.get('vest_years') or 4)))
                                           if (f.get('vest_years') or '4').isdigit() else 4),
            'offer_letter_vest_cliff': str(max(0, min(36, int(f.get('vest_cliff') or 12)))
                                           if (f.get('vest_cliff') or '12').isdigit() else 12),
            'offer_letter_equity_note': f.get('equity_note', '').strip()[:400],
        }
        for k, v in vals.items():
            db.execute('INSERT INTO company_settings (key,value) VALUES (?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
        db.commit()
        flash('오퍼레터 문안을 저장했습니다.', 'success')
        return redirect(url_for('offer_letter_settings'))

    cfg = _offer_letter_settings(db)
    return render_template('hiring/offer_letter.html',
                           cfg=cfg, company=get_company_info(),
                           hire=_hire_config(), active_page='offerletter')


@app.route('/api/offers/letter', methods=['GET'])
def offer_letter_api():
    """Hire 가 오퍼레터를 그릴 때 읽어가는 회사 문안. 인증: X-API-Token"""
    db, own = _offer_api_db()
    if db is None:
        return {'ok': False, 'error': 'invalid token'}, 401
    try:
        cfg = _offer_letter_settings(db)
        co = db.execute("SELECT key, value FROM company_settings WHERE key IN ('name','ceo','address')").fetchall()
        info = {r['key']: (r['value'] or '').strip() for r in co}
        benefits = [x.strip() for x in (cfg['offer_letter_benefits'] or '').splitlines() if x.strip()]
        return {
            'ok': True,
            'company': {
                'name': info.get('name') or _COMPANY_DEFAULTS.get('name', ''),
                'ceo': info.get('ceo', ''),
                'address': info.get('address', ''),
            },
            'greeting': cfg['offer_letter_greeting'],
            'benefits': benefits,
            'signer': cfg['offer_letter_signer'],
            'signer_title': cfg['offer_letter_signer_title'],
            'reply_days': _offer_letter_int(cfg, 'offer_letter_reply_days', 1, 30, 7),
            'equity': {
                'vest_years': _offer_letter_int(cfg, 'offer_letter_vest_years', 1, 10, 4),
                'cliff_months': _offer_letter_int(cfg, 'offer_letter_vest_cliff', 0, 36, 12),
                'note': cfg['offer_letter_equity_note'],
            },
        }, 200
    finally:
        if own:
            db.close()


# ── 결재선 서식 관리 ─────────────────────────────────────────────────
@app.route('/settings/requisition-flow', methods=['GET', 'POST'])
@admin_required
def requisition_flow_settings():
    """채용 유형마다 누가 몇 단계로 보는지 — 관리자가 여기서 고친다."""
    db = get_db()
    if request.method == 'POST':
        f = request.form
        # C레벨·인사 담당 결재자와 대결자
        for key in _approval_roles(db):
            u  = f.get('role_%s_user' % key) or None
            dg = f.get('role_%s_delegate' % key) or None
            if u and dg and u == dg:
                dg = None
            db.execute('UPDATE approval_roles SET user_id=?, delegate_id=? WHERE key=?', (u, dg, key))
        db.execute('DELETE FROM requisition_flow_steps')
        for ht in FLOW_KIND_LABEL:
            step_no = 0
            for i in range(1, 7):
                kind = f.get('%s_kind_%d' % (ht, i), '')
                if kind not in FLOW_ROLE_LABEL:
                    continue
                step_no += 1
                try:
                    sla = max(1, int(f.get('%s_sla_%d' % (ht, i)) or 2))
                except ValueError:
                    sla = 2
                ek = f.get('%s_exec_%d' % (ht, i)) or 'ceo'
                db.execute(
                    'INSERT INTO requisition_flow_steps '
                    '(hire_type, step_no, role_kind, exec_key, user_id, label, sla_days) VALUES (?,?,?,?,?,?,?)',
                    (ht, step_no, kind, ek if kind == 'exec' else None,
                     (f.get('%s_user_%d' % (ht, i)) or None) if kind == 'user' else None,
                     f.get('%s_label_%d' % (ht, i), '').strip() or FLOW_ROLE_LABEL[kind],
                     sla))
        db.commit()
        flash('결재선 저장 완료 · 진행 중인 요청서는 기존 결재선 유지', 'success')
        return redirect(url_for('requisition_flow_settings'))

    flows = {ht: _flow_template(db, ht) for ht in FLOW_KIND_LABEL}
    people = db.execute(
        "SELECT u.id, u.name, p.name AS pos_name, d.name AS dept_name FROM users u "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "WHERE u.status='active' ORDER BY u.name").fetchall()
    roles = _approval_roles(db)
    return render_template('hiring/requisition_flow.html',
        flows=flows, people=people,
        exec_roles=roles,
        hire_type_labels=FLOW_KIND_LABEL,
        hire_type_hints=FLOW_KIND_HINT,
        role_labels=FLOW_ROLE_LABEL,
        role_hints=FLOW_ROLE_HINT,
        active_page='reqflow'
    )


# ── 회사·건물 (회의실·온보딩 V1) ─────────────────────────────────────────
# 건물·층·회의실·온보딩 자료는 GPT 등에서 만든 JSON 을 붙여 넣어 한 번에 넣는다.
# 검사 → 미리보기 → 저장 두 단계. 입사일 요일·오리엔테이션 고정 예약 규칙도 여기서.
@app.route('/settings/workplace', methods=['GET', 'POST'])
@admin_required
def workplace_settings():
    db = get_db()
    preview, raw = None, ''
    if request.method == 'POST':
        act = request.form.get('action', '')
        if act == 'rules':
            f = request.form
            wds = sorted({int(w) for w in f.getlist('start_weekdays') if w.isdigit() and int(w) < 7})
            o_s, o_e = f.get('orientation_start', ''), f.get('orientation_end', '')
            room_code = f.get('orientation_room', '')
            try:
                ok_time = workplace.hm(o_s) < workplace.hm(o_e)
            except (ValueError, IndexError):
                ok_time = False
            try:
                rel = max(0, min(14, int(f.get('orientation_release_days') or 2)))
            except ValueError:
                rel = 2
            if not wds:
                flash('입사 요일을 하나 이상 골라 주세요', 'error')
            elif not ok_time:
                flash('오리엔테이션 종료 시간이 시작 시간보다 늦어야 합니다', 'error')
            elif not workplace.room(db, room_code):
                flash('오리엔테이션 방을 골라 주세요', 'error')
            else:
                vals = {'start_weekdays': ','.join(map(str, wds)), 'orientation_room': room_code,
                        'orientation_start': o_s, 'orientation_end': o_e,
                        'orientation_release_days': str(rel)}
                for k, v in vals.items():
                    db.execute('INSERT INTO company_settings (key, value) VALUES (?, ?) '
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
                db.commit()
                log_audit('update', 'document', detail='workplace_rules ' + json.dumps(vals, ensure_ascii=False))
                flash('입사일·오리엔테이션 규칙 저장 완료', 'success')
            return redirect(url_for('workplace_settings'))
        if act in ('check', 'import'):
            raw = request.form.get('data', '')
            try:
                data = workplace.parse_blobs(raw)
            except ValueError as e:
                preview = {'fatal': str(e)}
            else:
                errors, warnings, summary = workplace.validate(db, data)
                preview = {'errors': errors, 'warnings': warnings, 'summary': summary}
                if act == 'import' and not errors:
                    workplace.import_data(db, data, session.get('user_id'))
                    db.commit()
                    log_audit('update', 'document', detail='workplace_import ' + json.dumps(summary, ensure_ascii=False))
                    parts = []
                    if summary['rooms']:
                        parts.append('층 %d · 회의실 %d' % (summary['floors'], summary['rooms']))
                    if summary['has_onboarding']:
                        parts.append('온보딩 할 일 %d' % summary['tasks'])
                    flash('저장 완료 — ' + ' · '.join(parts) + (' · 확인 필요 %d건' % len(warnings) if warnings else ''), 'success')
                    return redirect(url_for('workplace_settings'))

    st = workplace.settings(db)
    today = date.today()
    fl = workplace.floors(db)
    rms = workplace.rooms(db)
    starts = workplace.next_start_dates(db, today, 6, st)
    upcoming = [{'date': d, 'wd': workplace.WEEKDAY_KO[d.weekday()], 'people': workplace.hires_on(db, d),
                 'released': (d - today).days <= st['release_days'] and not workplace.hires_on(db, d)}
                for d in starts]
    next_year_holidays = db.execute('SELECT COUNT(*) FROM public_holidays WHERE year=?', (today.year + 1,)).fetchone()[0]
    return render_template('workplace/settings.html',
        st=st, sites=workplace.sites(db), floors=fl, rooms=rms,
        room_types=workplace.ROOM_TYPES, bookable_label=workplace.BOOKABLE_LABEL,
        company=get_company_info(), onboarding=workplace.onboarding_content(db),
        upcoming=upcoming, weekday_ko=workplace.WEEKDAY_KO,
        next_year_holidays=next_year_holidays, next_year=today.year + 1,
        preview=preview, raw=raw, active_page='workplace')


def _rooms_redirect(d=None, floor=None):
    return redirect(url_for('rooms', date=d or request.form.get('date') or None,
                            floor=floor or request.form.get('floor') or None))


@app.route('/rooms')
@login_required
def rooms():
    """회의실 예약 — 층 배치도 + 30분 칸 시간표 + 내 예약."""
    db = get_db()
    uid, role = session.get('user_id'), session.get('user_role')
    fl = workplace.floors(db)
    if not fl:
        return render_template('workplace/rooms.html', empty=True, active_page='rooms')
    today = date.today()
    now = datetime.now()
    day = workplace.to_date(request.args.get('date')) or today
    fkey = request.args.get('floor', '')
    cur = next((f for f in fl if '%s-%s' % (f['site_code'], f['floor']) == fkey), None)
    if not cur:
        me = db.execute('SELECT department_id FROM users WHERE id=?', (uid,)).fetchone()
        cur = (workplace.dept_floor(db, me['department_id']) if me else None) or fl[0]
    all_rooms = workplace.rooms(db)
    frooms = [r for r in all_rooms if r['site_code'] == cur['site_code'] and r['floor'] == cur['floor']]
    is_hr = workplace.is_hr_member(db, uid, role)
    st = workplace.settings(db)

    by_room = {r['code']: [] for r in frooms}
    for b in workplace.bookings_between(db, day, day):
        if b['room_code'] in by_room:
            b['mine'] = b['user_id'] == uid
            if b['kind'] == 'interview' and not is_hr and not b['mine']:
                b['title'] = '면접'
            by_room[b['room_code']].append(b)
    for blk in workplace.orientation_blocks(db, day, day, today=today, st=st):
        if blk['room_code'] in by_room:
            blk['mine'] = False
            if not is_hr:
                blk['title'] = '신규 입사자 오리엔테이션'
            by_room[blk['room_code']].append(blk)

    slots = []
    t = workplace.hm(workplace.GRID_START)
    while t < workplace.hm(workplace.GRID_END):
        slots.append(workplace.fmt_hm(t))
        t += workplace.SLOT_MIN
    now_hm = now.strftime('%H:%M') if day == today else None

    rows, status = [], {}
    for r in frooms:
        items = sorted(by_room[r['code']], key=lambda b: b['start_at'])
        for b in items:
            b['col'] = (workplace.hm(b['start_at'][11:16]) - workplace.hm(workplace.GRID_START)) // workplace.SLOT_MIN + 1
            b['span'] = max(1, (workplace.hm(b['end_at'][11:16]) - workplace.hm(b['start_at'][11:16])) // workplace.SLOT_MIN)
        taken = set()
        for b in items:
            taken.update(range(b['col'], b['col'] + b['span']))
        ok = workplace.can_book(db, r, uid, role)
        rows.append({'room': r, 'items': items, 'taken': sorted(taken), 'can': ok})
        if day < today:
            continue
        if now_hm:
            busy = next((b for b in items if b['start_at'][11:16] <= now_hm < b['end_at'][11:16]), None)
            nxt = next((b for b in items if b['start_at'][11:16] > now_hm), None)
            if busy:
                status[r['code']] = {'kind': 'busy', 'text': '사용 중 ~%s' % busy['end_at'][11:16]}
            elif nxt:
                status[r['code']] = {'kind': 'free', 'text': '비어 있음 · %s부터 예약' % nxt['start_at'][11:16]}
            else:
                status[r['code']] = {'kind': 'free', 'text': '오늘 비어 있음'}
        else:
            status[r['code']] = {'kind': 'lock' if items else 'free',
                                 'text': ('예약 %d건' % len(items)) if items else '종일 비어 있음'}
        if not ok:
            status[r['code']]['text'] = workplace.BOOKABLE_LABEL[r['bookable_by']] + ' 전용 · ' + status[r['code']]['text']

    mine = [dict(b) for b in db.execute(
        "SELECT b.*, r.name AS room_name, r.floor FROM room_bookings b JOIN workplace_rooms r ON r.code=b.room_code "
        "WHERE b.user_id=? AND b.status='active' AND b.end_at >= ? ORDER BY b.start_at LIMIT 30",
        (uid, now.strftime('%Y-%m-%d %H:%M')))]
    for b in mine:
        d = workplace.to_date(b['start_at'])
        b['label'] = '%s (%s) %s~%s' % (d.strftime('%m.%d'), workplace.WEEKDAY_KO[d.weekday()], b['start_at'][11:16], b['end_at'][11:16])

    return render_template('workplace/rooms.html', empty=False,
        floors=fl, cur=cur, cur_key='%s-%s' % (cur['site_code'], cur['floor']), rooms=frooms, rows=rows,
        status=status, slots=slots, day=day, today=today, now_hm=now_hm,
        prev_day=day - timedelta(days=1), next_day=day + timedelta(days=1),
        weekday=workplace.WEEKDAY_KO[day.weekday()], mine=mine, bookable_label=workplace.BOOKABLE_LABEL,
        is_start_day=workplace.is_start_day(db, day, st), st=st,
        grid_start=workplace.GRID_START, grid_end=workplace.GRID_END, active_page='rooms')


@app.route('/rooms/book', methods=['POST'])
@login_required
def room_book():
    db = get_db()
    f = request.form
    d = f.get('date', '')
    bid, err, _hit = workplace.create_booking(
        db, f.get('room', ''), f.get('title', ''),
        '%s %s' % (d, f.get('start', '')), '%s %s' % (d, f.get('end', '')),
        user_id=session.get('user_id'), role=session.get('user_role'),
        attendees=f.get('attendees') if (f.get('attendees') or '').isdigit() else 0,
        note=f.get('note', ''), booked_by=session.get('user_name', ''))
    if err:
        flash(err, 'error')
    else:
        db.commit()
        rm = workplace.room(db, f.get('room'))
        flash('%s %s %s~%s 예약 완료' % (rm['name'], d[5:].replace('-', '.'), f.get('start'), f.get('end')), 'success')
    return _rooms_redirect(d)


@app.route('/rooms/bookings/<int:bid>/cancel', methods=['POST'])
@login_required
def room_cancel(bid):
    db = get_db()
    b = db.execute("SELECT * FROM room_bookings WHERE id=? AND status='active'", (bid,)).fetchone()
    if not b:
        flash('이미 취소되었거나 없는 예약입니다', 'error')
    elif b['user_id'] != session.get('user_id') and session.get('user_role') != 'admin':
        flash('본인 예약만 취소할 수 있습니다', 'error')
    else:
        db.execute("UPDATE room_bookings SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (bid,))
        db.commit()
        flash('예약을 취소했습니다 — %s %s' % (b['title'], b['start_at'][5:].replace('-', '.')), 'success')
    return _rooms_redirect(b['start_at'][:10] if b else None)


@app.route('/rooms/bookings/<int:bid>.ics')
@login_required
def room_ics(bid):
    db = get_db()
    b = db.execute('SELECT * FROM room_bookings WHERE id=?', (bid,)).fetchone()
    if not b or (b['user_id'] != session.get('user_id') and session.get('user_role') != 'admin'):
        abort(404)
    rm = workplace.room(db, b['room_code'])
    site = next((s for s in workplace.sites(db) if s['code'] == rm['site_code']), None)
    return Response(workplace.ics_text(dict(b), rm, site), mimetype='text/calendar',
                    headers={'Content-Disposition': 'attachment; filename="booking-%d.ics"' % bid})


@app.route('/settings/workplace/export')
@admin_required
def workplace_export():
    body = json.dumps(workplace.export_data(get_db()), ensure_ascii=False, indent=2)
    return Response(body, mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename="workplace.json"'})


@app.route('/recruit/dashboard')
@retired_ats
@recruiter_or_admin
def recruit_dashboard():
    db = get_db()

    # ── 퍼널: 진행 중 단계별 인원수 ──────────────────────────────
    stage_counts_raw = db.execute(
        "SELECT stage, COUNT(*) AS cnt FROM applicants GROUP BY stage"
    ).fetchall()
    stage_cnt = {r['stage']: r['cnt'] for r in stage_counts_raw}

    funnel = []
    prev_cnt = None
    for stage_key, stage_label in ACTIVE_STAGES:
        cnt = stage_cnt.get(stage_key, 0)
        conv = round(cnt / prev_cnt * 100, 1) if prev_cnt and prev_cnt > 0 else None
        funnel.append({'key': stage_key, 'label': stage_label, 'count': cnt, 'conv': conv})
        prev_cnt = cnt

    # ── 합격/불합격 집계 ─────────────────────────────────────────
    total       = sum(r['cnt'] for r in stage_counts_raw)
    accepted    = stage_cnt.get('accepted', 0)
    rejected    = stage_cnt.get('rejected', 0)
    disqualified = stage_cnt.get('disqualified', 0)
    in_progress = total - accepted - rejected - disqualified

    # ── Time-to-Fill: 공고별 게시→합격 평균 소요일 ───────────────
    ttf_rows = db.execute(
        """
        SELECT jp.title,
               COUNT(a.id)                                          AS hired_cnt,
               ROUND(AVG(
                   (julianday(o.created_at) - julianday(jp.created_at))
               ), 1)                                                AS avg_days
        FROM job_postings jp
        JOIN applicants a  ON a.posting_id = jp.id AND a.stage IN ('accepted','hired')
        JOIN offers     o  ON o.applicant_id = a.id AND o.status IN ('accepted','sent')
        GROUP BY jp.id
        ORDER BY avg_days ASC
        LIMIT 10
        """
    ).fetchall()

    # ── 소스별 합격률 ─────────────────────────────────────────────
    source_rows = db.execute(
        """
        SELECT source,
               COUNT(*)                                                   AS total,
               SUM(CASE WHEN stage IN ('accepted','hired') THEN 1 ELSE 0 END) AS hired
        FROM applicants
        GROUP BY source
        ORDER BY total DESC
        """
    ).fetchall()
    source_data = []
    for r in source_rows:
        rate = round(r['hired'] / r['total'] * 100, 1) if r['total'] > 0 else 0
        source_data.append({
            'source': SOURCE_LABELS.get(r['source'], r['source']),
            'total':  r['total'],
            'hired':  r['hired'],
            'rate':   rate,
        })

    # ── 월별 신규 지원자 추이 (최근 6개월) ──────────────────────
    monthly_rows = db.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS ym, COUNT(*) AS cnt
        FROM applicants
        WHERE created_at >= date('now', '-6 months')
        GROUP BY ym
        ORDER BY ym
        """
    ).fetchall()
    monthly = [{'ym': r['ym'], 'cnt': r['cnt']} for r in monthly_rows]

    # ── 공고별 지원자 수 Top 5 ───────────────────────────────────
    top_postings = db.execute(
        """
        SELECT jp.title, COUNT(a.id) AS cnt,
               SUM(CASE WHEN a.stage='accepted' THEN 1 ELSE 0 END) AS hired
        FROM job_postings jp
        LEFT JOIN applicants a ON a.posting_id = jp.id
        GROUP BY jp.id
        ORDER BY cnt DESC
        LIMIT 5
        """
    ).fetchall()

    return render_template('recruit/dashboard.html',
                           funnel=funnel,
                           total=total, accepted=accepted,
                           rejected=rejected, disqualified=disqualified,
                           in_progress=in_progress,
                           ttf_rows=ttf_rows,
                           source_data=source_data,
                           monthly=monthly,
                           top_postings=top_postings,
                           active_page='recruit_dashboard')


@app.route('/recruit/postings')
@retired_ats
@recruiter_or_admin
def recruit_postings():
    db     = get_db()
    status = request.args.get('status', '')
    sql    = (
        'SELECT jp.*, d.name AS dept_name, p.name AS pos_name, '
        'u.name AS created_by_name, COUNT(a.id) AS applicant_count '
        'FROM job_postings jp '
        'LEFT JOIN departments d ON jp.department_id = d.id '
        'LEFT JOIN positions   p ON jp.position_id   = p.id '
        'LEFT JOIN users       u ON jp.created_by    = u.id '
        'LEFT JOIN applicants  a ON jp.id = a.posting_id '
    )
    params = []
    if status in ('draft', 'open', 'closed'):
        sql += 'WHERE jp.status = ? '
        params.append(status)
    sql += 'GROUP BY jp.id ORDER BY jp.created_at DESC'
    postings = db.execute(sql, params).fetchall()
    return render_template('recruit/postings.html',
                           postings=postings, status=status,
                           active_page='recruit')

@app.route('/recruit/postings/new', methods=['GET', 'POST'])
@retired_ats
@recruiter_or_admin
def recruit_posting_new():
    db    = get_db()
    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    error = None

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        dept_id  = request.form.get('department_id') or None
        pos_id   = request.form.get('position_id') or None
        desc     = request.form.get('description', '').strip() or None
        reqs     = request.form.get('requirements', '').strip() or None
        status   = request.form.get('status', 'open')
        deadline = request.form.get('deadline') or None

        if not title:
            error = '공고 제목은 필수입니다.'
        elif status not in ('draft', 'open', 'closed'):
            error = '올바르지 않은 공고 상태입니다.'
        else:
            db.execute(
                'INSERT INTO job_postings '
                '(title, department_id, position_id, description, requirements, status, deadline, created_by) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (title, dept_id, pos_id, desc, reqs, status, deadline, session['user_id'])
            )
            db.commit()
            return redirect(url_for('recruit_postings'))

    return render_template('recruit/posting_form.html',
                           mode='new', posting=None, depts=depts, poses=poses, error=error,
                           active_page='recruit')

@app.route('/recruit/postings/<int:posting_id>', methods=['GET', 'POST'])
@retired_ats
@recruiter_or_admin
def recruit_posting_detail(posting_id):
    db      = get_db()
    posting = db.execute(
        'SELECT jp.*, d.name AS dept_name, p.name AS pos_name '
        'FROM job_postings jp '
        'LEFT JOIN departments d ON jp.department_id = d.id '
        'LEFT JOIN positions   p ON jp.position_id   = p.id '
        'WHERE jp.id = ?', (posting_id,)
    ).fetchone()
    if not posting:
        abort(404)

    error = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_applicant':
            name   = request.form.get('name', '').strip()
            email  = request.form.get('email', '').strip()
            phone  = request.form.get('phone', '').strip() or None
            source = request.form.get('source', 'direct')
            note   = request.form.get('resume_note', '').strip() or None
            if not name or not email:
                error = '지원자 이름과 이메일은 필수입니다.'
            elif source not in SOURCE_LABELS:
                error = '올바르지 않은 지원 경로입니다.'
            else:
                db.execute(
                    'INSERT INTO applicants (posting_id, name, email, phone, source, resume_note) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (posting_id, name, email, phone, source, note)
                )
                db.commit()
        elif action == 'close':
            db.execute("UPDATE job_postings SET status='closed' WHERE id=?", (posting_id,))
            db.commit()
        elif action == 'reopen':
            db.execute("UPDATE job_postings SET status='open' WHERE id=?", (posting_id,))
            db.commit()
        return redirect(url_for('recruit_posting_detail', posting_id=posting_id))

    applicants = db.execute(
        'SELECT * FROM applicants WHERE posting_id=? ORDER BY created_at DESC',
        (posting_id,)
    ).fetchall()
    return render_template('recruit/posting_detail.html',
                           posting=posting, applicants=applicants,
                           stage_map=STAGE_MAP, source_labels=SOURCE_LABELS,
                           error=error,
                           active_page='recruit')

@app.route('/recruit/postings/<int:posting_id>/edit', methods=['GET', 'POST'])
@retired_ats
@recruiter_or_admin
def recruit_posting_edit(posting_id):
    db      = get_db()
    posting = db.execute('SELECT * FROM job_postings WHERE id=?', (posting_id,)).fetchone()
    if not posting:
        abort(404)
    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    error = None

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        dept_id  = request.form.get('department_id') or None
        pos_id   = request.form.get('position_id') or None
        desc     = request.form.get('description', '').strip() or None
        reqs     = request.form.get('requirements', '').strip() or None
        status   = request.form.get('status', 'open')
        deadline = request.form.get('deadline') or None

        if not title:
            error = '공고 제목은 필수입니다.'
        elif status not in ('draft', 'open', 'closed'):
            error = '올바르지 않은 공고 상태입니다.'
        else:
            db.execute(
                'UPDATE job_postings SET title=?, department_id=?, position_id=?, '
                'description=?, requirements=?, status=?, deadline=? WHERE id=?',
                (title, dept_id, pos_id, desc, reqs, status, deadline, posting_id)
            )
            db.commit()
            return redirect(url_for('recruit_posting_detail', posting_id=posting_id))

    return render_template('recruit/posting_form.html',
                           mode='edit', posting=posting, depts=depts, poses=poses, error=error,
                           active_page='recruit')

@app.route('/recruit/pipeline')
@retired_ats
@recruiter_or_admin
def recruit_pipeline():
    db         = get_db()
    posting_id = request.args.get('posting', type=int)
    postings   = db.execute(
        "SELECT jp.*, d.name AS dept_name, "
        "r.name AS recruiter_name, hm.name AS hiring_manager_name, co.name AS coordinator_name "
        "FROM job_postings jp "
        "LEFT JOIN departments d ON jp.department_id = d.id "
        "LEFT JOIN users r  ON jp.recruiter_id = r.id "
        "LEFT JOIN users hm ON jp.hiring_manager_id = hm.id "
        "LEFT JOIN users co ON jp.coordinator_id = co.id "
        "WHERE jp.status != 'draft' ORDER BY jp.created_at DESC"
    ).fetchall()

    if not posting_id and postings:
        posting_id = postings[0]['id']

    current_posting = None
    pipeline = {stage: [] for stage, _ in STAGES}

    if posting_id:
        current_posting = next((p for p in postings if p['id'] == posting_id), None)
        applicants = db.execute(
            'SELECT a.*, '
            '(julianday("now") - julianday(a.created_at)) AS days_in_pipeline '
            'FROM applicants a '
            'WHERE a.posting_id = ? '
            'ORDER BY a.created_at DESC',
            (posting_id,)
        ).fetchall()
        for a in applicants:
            stage = a['stage']
            if stage in pipeline:
                pipeline[stage].append(dict(a))

    return render_template('recruit/pipeline.html',
                           pipeline=pipeline, stages=STAGES,
                           stage_map=STAGE_MAP,
                           stage_colors=STAGE_COLORS,
                           active_stages=ACTIVE_STAGES,
                           source_labels=SOURCE_LABELS,
                           rejection_reason_codes=REJECTION_REASON_CODES,
                           postings=postings,
                           posting_id=posting_id,
                           current_posting=current_posting,
                           active_page='recruit_pipeline')


@app.route('/recruit/applicants/<int:applicant_id>/stage', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_stage_update(applicant_id):
    """AJAX — 드래그앤드롭 단계 변경"""
    data      = request.get_json(force=True)
    new_stage = data.get('stage', '')
    if new_stage not in STAGE_MAP:
        return jsonify({'ok': False, 'error': 'invalid stage'}), 400
    # 불합격 처리는 전용 라우트로만 허용 (사유 코드 필수)
    if new_stage == 'disqualified':
        return jsonify({'ok': False, 'error': 'use disqualify endpoint'}), 400
    # 오퍼 거절은 오퍼 단계에서만 허용
    if new_stage == 'rejected':
        return jsonify({'ok': False, 'error': 'use disqualify endpoint'}), 400
    db = get_db()
    applicant = db.execute('SELECT * FROM applicants WHERE id=?', (applicant_id,)).fetchone()
    if not applicant:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    old_stage = applicant['stage']
    if old_stage in TERMINAL_STAGES:
        return jsonify({'ok': False, 'error': '터미널 단계에서는 이동할 수 없습니다.'}), 400
    db.execute('UPDATE applicants SET stage=? WHERE id=?', (new_stage, applicant_id))
    db.execute(
        'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?,?,?,?)',
        (applicant_id, new_stage, f'{STAGE_MAP.get(old_stage, old_stage)} → {STAGE_MAP[new_stage]}', session['user_id'])
    )
    log_recruit(applicant_id, 'stage_changed', {'from': old_stage, 'to': new_stage})
    db.commit()
    return jsonify({'ok': True, 'stage': new_stage, 'label': STAGE_MAP[new_stage]})


def _save_recruit_email(db, applicant_id, email_type, recipient, subject, body):
    """채용 이메일 발송 이력 저장 헬퍼"""
    db.execute(
        'INSERT INTO recruit_emails (applicant_id, email_type, recipient, subject, body, sent_by) '
        'VALUES (?,?,?,?,?,?)',
        (applicant_id, email_type, recipient, subject, body, session.get('user_id'))
    )


def _render_email_template(tpl_key, context):
    """이메일 템플릿 렌더링 헬퍼"""
    tpl = EMAIL_TEMPLATES.get(tpl_key, {})
    company = os.environ.get('COMPANY_NAME', 'TalentCore')
    ctx = {'company': company, **context}
    subject = tpl.get('subject', '').format_map(ctx)
    body    = tpl.get('body', '').format_map(ctx)
    return subject, body


@app.route('/recruit/applicants/<int:applicant_id>/disqualify', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_disqualify(applicant_id):
    """불합격 처리 — 어느 단계에서든, 사유 코드 기록 + 이메일 발송 옵션"""
    db        = get_db()
    applicant = db.execute(
        'SELECT a.*, jp.title AS posting_title FROM applicants a '
        'JOIN job_postings jp ON a.posting_id = jp.id WHERE a.id=?', (applicant_id,)
    ).fetchone()
    if not applicant:
        abort(404)
    if applicant['stage'] in TERMINAL_STAGES:
        flash('이미 처리 완료된 후보자입니다.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    reason_code  = request.form.get('reason_code', '').strip()
    note         = request.form.get('note', '').strip() or None
    send_email   = request.form.get('send_email') == '1'
    email_body   = request.form.get('email_body', '').strip()
    from_stage   = applicant['stage']

    if not reason_code:
        flash('불합격 사유 코드를 선택해주세요.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    db.execute(
        'UPDATE applicants SET stage=?, disqualified_from=?, disqualify_reason=? WHERE id=?',
        ('disqualified', from_stage, reason_code, applicant_id)
    )
    db.execute(
        'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?,?,?,?)',
        (applicant_id, 'disqualified',
         f'[불합격] {STAGE_MAP.get(from_stage, from_stage)} 단계 · 사유: {REJECTION_REASON_CODES.get(reason_code, reason_code)}' + (f' · {note}' if note else ''),
         session['user_id'])
    )
    log_recruit(applicant_id, 'disqualified',
                {'from_stage': from_stage, 'reason_code': reason_code, 'note': note})

    if send_email and applicant['email']:
        subject, body = _render_email_template('fail', {
            'name': applicant['name'], 'posting_title': applicant['posting_title']
        })
        if email_body:
            body = email_body
        _save_recruit_email(db, applicant_id, 'fail', applicant['email'], subject, body)

    db.commit()
    msg = f'{applicant["name"]} 님이 불합격 처리됐습니다.'
    if send_email and applicant['email']:
        msg += ' (이메일 발송 기록 저장됨)'
    flash(msg, 'info')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))


@app.route('/recruit/applicants/<int:applicant_id>/offer-reject', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_offer_reject(applicant_id):
    """오퍼 거절 처리 — offer 단계 후보자에 한해"""
    db        = get_db()
    applicant = db.execute('SELECT * FROM applicants WHERE id=?', (applicant_id,)).fetchone()
    if not applicant:
        abort(404)
    if applicant['stage'] != 'offer':
        flash('오퍼 단계의 후보자에게만 오퍼 거절 처리가 가능합니다.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    note = request.form.get('note', '').strip() or None
    db.execute('UPDATE applicants SET stage=? WHERE id=?', ('rejected', applicant_id))
    db.execute(
        'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?,?,?,?)',
        (applicant_id, 'rejected', note or '후보자가 오퍼를 거절했습니다.', session['user_id'])
    )
    # 오퍼 상태도 rejected로 동기화
    db.execute(
        "UPDATE offers SET status='rejected', responded_at=CURRENT_TIMESTAMP "
        "WHERE applicant_id=? AND status IN ('sent','negotiating')", (applicant_id,)
    )
    log_recruit(applicant_id, 'offer_rejected', {'note': note})
    db.commit()
    # Slack DM: HR 전체 + 담당 리크루터에게 오퍼 거절 알림
    from integrations.dispatcher import notify_slack_multi
    _hr_rows = db.execute("SELECT email, name FROM users WHERE role='admin' AND status='active'").fetchall()
    _targets = [(r['email'], r['name']) for r in _hr_rows if r['email']]
    notify_slack_multi(
        _targets,
        f"[TalentCore] 오퍼 거절 알림\n"
        f"지원자 {applicant['name']}님이 오퍼를 거절했습니다.\n"
        f"사유: {note or '미입력'}\n"
        f"후속 조치(재공고/파이프라인 재검토)가 필요합니다.",
        '오퍼 거절'
    )
    flash(f'{applicant["name"]} 님이 오퍼를 거절했습니다.', 'info')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))


@app.route('/recruit/applicants/<int:applicant_id>/hire', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_hire(applicant_id):
    """입사 확정 — 오퍼 데이터로 직원 레코드 자동 생성 + 온보딩 파이프라인 가동"""
    db = get_db()

    # 지원자 + 공고 + 오퍼 데이터 한 번에 조회
    applicant = db.execute(
        '''SELECT a.*,
                  jp.department_id, jp.title AS posting_title,
                  jr.position_id, jr.job_family_id,
                  o.salary AS offer_salary, o.start_date AS offer_start_date, o.id AS offer_id
           FROM applicants a
           JOIN job_postings jp ON a.posting_id = jp.id
           LEFT JOIN job_requisitions jr ON jp.requisition_id = jr.id
           LEFT JOIN offers o ON o.applicant_id = a.id
                             AND o.status IN ('sent','negotiating','accepted')
           WHERE a.id=?
           ORDER BY o.id DESC LIMIT 1''',
        (applicant_id,)
    ).fetchone()

    if not applicant:
        abort(404)
    if applicant['stage'] not in ('offer', 'accepted'):
        flash('오퍼 단계의 후보자에게만 입사 확정이 가능합니다.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    # 이미 직원으로 전환된 경우 방지
    if applicant['hired_employee_id']:
        flash('이미 직원으로 등록된 지원자입니다.', 'warning')
        return redirect(url_for('employee_detail', emp_id=applicant['hired_employee_id']))

    name       = applicant['name']
    email      = applicant['email'] or ''
    phone      = applicant['phone'] or ''
    dept_id    = applicant['department_id']
    pos_id     = applicant['position_id']
    jf_id      = applicant['job_family_id']
    base_salary= applicant['offer_salary'] or 0
    hire_date  = applicant['offer_start_date'] or date.today().isoformat()

    # 이메일 중복 체크
    if email and db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
        flash(f'이미 등록된 이메일입니다: {email}', 'danger')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    # 임시 비밀번호 (첫 로그인 시 변경 안내)
    import secrets as _sec
    from werkzeug.security import generate_password_hash as _gph
    temp_pw   = _sec.token_urlsafe(8)
    pw_hash   = _gph(temp_pw)

    # 사번 자동 생성
    last_emp_no = db.execute("SELECT emp_no FROM users WHERE emp_no IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if last_emp_no and last_emp_no['emp_no']:
        try:
            next_no = int(last_emp_no['emp_no'].replace('TC-', '')) + 1
        except Exception:
            next_no = 1001
    else:
        next_no = 1001
    emp_no = f'TC-{next_no:05d}'

    # 직원 레코드 자동 생성
    cur = db.execute(
        '''INSERT INTO users
           (name, email, phone, password_hash, role, department_id, position_id,
            job_family_id, hire_date, employment_type, status, emp_no)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (name, email, phone, pw_hash, 'employee',
         dept_id, pos_id, jf_id, hire_date,
         'full_time', 'active', emp_no)
    )
    new_user_id = cur.lastrowid

    # 급여 초기 등록 (오퍼 급여 기준)
    if base_salary:
        today = date.today()
        db.execute(
            'INSERT OR IGNORE INTO payslips (user_id, year, month, base_salary, gross_pay, net_pay) VALUES (?,?,?,?,?,?)',
            (new_user_id, today.year, today.month, base_salary, base_salary, int(base_salary * 0.897))
        )

    # 지원자 상태 업데이트
    db.execute('UPDATE applicants SET stage=?, hired_employee_id=? WHERE id=?',
               ('accepted', new_user_id, applicant_id))
    db.execute(
        'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?,?,?,?)',
        (applicant_id, 'accepted', f'입사 확정 — 직원 자동 등록 (ID:{new_user_id}, 사번:{emp_no})', session['user_id'])
    )

    # 오퍼 상태 동기화 + hired_employee_id 연결
    if applicant['offer_id']:
        db.execute(
            "UPDATE offers SET status='accepted', responded_at=CURRENT_TIMESTAMP, hired_employee_id=? WHERE id=?",
            (new_user_id, applicant['offer_id'])
        )

    log_recruit(applicant_id, 'hired', {'employee_id': new_user_id, 'emp_no': emp_no})
    db.commit()

    # 입사자에게 알림
    add_notification(
        new_user_id, 'info', 'onboarding', '환영합니다!',
        f'TalentCore 임시 비밀번호: {temp_pw} — 첫 로그인 후 변경해주세요.',
        url_for('me_onboarding')
    )
    # HR 담당자에게 알림
    add_notification(
        session['user_id'], 'action', 'action', f'{name} 직원 등록 완료',
        f'{emp_no} — 버디·스케줄 배정을 완료해주세요.',
        url_for('employee_detail', emp_id=new_user_id)
    )

    # 온보딩 파이프라인 가동 (Jira 에픽 + Slack + 이메일 + 체크리스트)
    try:
        from integrations.dispatcher import on_employee_created
        _dr = db.execute('SELECT name FROM departments WHERE id=?', (dept_id,)).fetchone()
        _pr = db.execute('SELECT name FROM positions WHERE id=?', (pos_id,)).fetchone()
        dept_name = _dr['name'] if _dr else ''
        pos_name  = _pr['name'] if _pr else ''
        on_employee_created({
            'id': new_user_id, 'name': name, 'email': email,
            'dept': dept_name, 'pos': pos_name,
            'hire_date': hire_date,
        }, db_path=get_tenant_db_path(session.get('tenant_id', 1)))
    except Exception as e:
        app.logger.warning(f'recruit_hire integration error: {e}')

    flash(f'{name}({emp_no}) 입사 확정 완료! Jira·Slack·온보딩 체크리스트가 자동으로 준비됩니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=new_user_id))


# ── 오퍼 관리 ─────────────────────────────────────────────────────────────────

@app.route('/recruit/applicants/<int:applicant_id>/offers', methods=['GET', 'POST'])
@retired_ats
@recruiter_or_admin
def recruit_offers(applicant_id):
    """오퍼 목록 + 생성"""
    db        = get_db()
    applicant = db.execute(
        'SELECT a.*, jp.title AS posting_title, jp.id AS jp_id, '
        'jp.salary_min, jp.salary_max, '
        'jr.job_family_id, jr.job_level, jr.track, jr.salary_mid AS req_salary_mid '
        'FROM applicants a '
        'JOIN job_postings jp ON a.posting_id = jp.id '
        'LEFT JOIN job_requisitions jr ON jp.requisition_id = jr.id '
        'WHERE a.id=?', (applicant_id,)
    ).fetchone()
    if not applicant:
        abort(404)

    if request.method == 'POST':
        def _int(key):
            v = request.form.get(key, '').replace(',', '').strip()
            return int(v) if v else None

        salary       = _int('salary')
        bonus_pct    = _int('bonus_pct') or 20
        equity_type  = request.form.get('equity_type', 'rsu')
        if equity_type not in ('rsu', 'stock_option', 'none'):
            equity_type = 'rsu'
        rsu_total    = _int('rsu_total') or 0
        rsu_vest_yrs = _int('rsu_vest_years') or 4
        option_qty   = _int('option_qty') or 0
        strike_price = _int('strike_price') or 0
        if equity_type == 'stock_option':
            rsu_total = 0
        elif equity_type == 'rsu':
            option_qty = strike_price = 0
        else:
            rsu_total = option_qty = strike_price = 0
        signing      = _int('signing_bonus') or 0
        start_date   = request.form.get('start_date') or None
        expiry_date  = request.form.get('expiry_date') or None
        location     = request.form.get('location', '서울 강남')
        wfh_days     = _int('wfh_days') or 2
        job_level    = request.form.get('job_level') or (applicant['job_level'] if applicant['job_level'] else None)
        track        = request.form.get('track') or (applicant['track'] if applicant['track'] else 'IC')
        signer       = request.form.get('company_signer', '')
        signer_title = request.form.get('company_signer_title', 'Chief People Officer')
        action       = request.form.get('action', 'draft')
        status       = 'sent' if action == 'send' else 'draft'

        offer_id = db.execute(
            'INSERT INTO offers (applicant_id, posting_id, status, salary, bonus_pct, '
            'rsu_total, rsu_vest_years, signing_bonus, equity_type, option_qty, strike_price, '
            'start_date, expiry_date, '
            'location, wfh_days, job_level, track, company_signer, company_signer_title, '
            'sent_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (applicant_id, applicant['posting_id'], status, salary, bonus_pct,
             rsu_total, rsu_vest_yrs, signing, equity_type, option_qty, strike_price,
             start_date, expiry_date,
             location, wfh_days, job_level, track, signer, signer_title,
             'CURRENT_TIMESTAMP' if action == 'send' else None,
             session['user_id'])
        ).lastrowid

        if action == 'send' and applicant['email']:
            subject, body = _render_email_template('offer', {
                'name': applicant['name'],
                'posting_title': applicant['posting_title'],
                'salary': f'{salary:,}' if salary else '협의',
                'start_date': start_date or '협의',
                'expiry_date': expiry_date or '협의',
            })
            _save_recruit_email(db, applicant_id, 'offer', applicant['email'], subject, body)
            db.execute("UPDATE offers SET sent_at=CURRENT_TIMESTAMP WHERE id=?", (offer_id,))

        log_recruit(applicant_id, 'offer_created', {'offer_id': offer_id, 'status': status})
        db.commit()
        flash('오퍼가 생성됐습니다.' + (' (이메일 기록 저장)' if action == 'send' else ''), 'success')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id) + '#offers')

    # GET — 오퍼 관리는 지원자 상세의 오퍼 탭에서 진행 (전용 페이지 없음)
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id) + '#offers')


@app.route('/recruit/offers/<int:offer_id>/update', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_offer_update(offer_id):
    """오퍼 레터 인라인 편집 저장 (AJAX)"""
    db    = get_db()
    offer = db.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
    if not offer:
        return jsonify({'error': 'not found'}), 404

    data  = request.get_json(silent=True) or {}

    def _safe_int(v):
        try:
            return int(str(v).replace(',', '').strip()) if v is not None and str(v).strip() else None
        except (ValueError, TypeError):
            return None

    fields = {}
    for key in ('salary', 'bonus_pct', 'rsu_total', 'rsu_vest_years', 'signing_bonus', 'wfh_days',
                'option_qty', 'strike_price'):
        if key in data:
            fields[key] = _safe_int(data[key])
    for key in ('start_date', 'expiry_date', 'location', 'job_level', 'track',
                'company_signer', 'company_signer_title', 'body'):
        if key in data:
            fields[key] = str(data[key]).strip() or None
    if data.get('equity_type') in ('rsu', 'stock_option', 'none'):
        fields['equity_type'] = data['equity_type']

    if not fields:
        return jsonify({'ok': True, 'msg': 'no changes'})

    set_clause = ', '.join(f'{k}=?' for k in fields)
    db.execute(f'UPDATE offers SET {set_clause} WHERE id=?', list(fields.values()) + [offer_id])
    db.commit()
    return jsonify({'ok': True})


@app.route('/recruit/offers/<int:offer_id>/letter')
@retired_ats
@recruiter_or_admin
def recruit_offer_letter(offer_id):
    """오퍼 레터 페이지 (인라인 편집 + 인쇄)"""
    db    = get_db()
    offer = db.execute(
        'SELECT o.*, a.name AS applicant_name, a.email AS applicant_email, '
        'jp.title AS posting_title, u.name AS created_by_name, '
        'jf.name AS job_family_name '
        'FROM offers o '
        'JOIN applicants a ON o.applicant_id = a.id '
        'JOIN job_postings jp ON o.posting_id = jp.id '
        'LEFT JOIN users u ON o.created_by = u.id '
        'LEFT JOIN job_requisitions jr ON jp.requisition_id = jr.id '
        'LEFT JOIN job_families jf ON jr.job_family_id = jf.id '
        'WHERE o.id=?', (offer_id,)
    ).fetchone()
    if not offer:
        abort(404)

    # 연봉 밴드 (슬라이더용) — salary_grades는 position_id 기반
    band = None
    jl = offer['job_level'] or ''
    # job_level이 없으면 요청서에서 가져오기
    if not jl:
        req = db.execute(
            'SELECT jr.job_level, jr.job_family_id FROM job_postings jp '
            'LEFT JOIN job_requisitions jr ON jp.requisition_id = jr.id '
            'WHERE jp.id=?', (offer['posting_id'],)
        ).fetchone()
        if req:
            jl = req['job_level'] or ''
    level_part = jl[2:] if jl.startswith('CL') else (jl[1:] if jl else '')
    if level_part.isdigit():
        level_num = int(level_part)
        band = db.execute(
            'SELECT sg.min_salary, sg.mid_salary, sg.max_salary '
            'FROM salary_grades sg '
            'JOIN positions p ON sg.position_id = p.id '
            'JOIN job_postings jp ON sg.job_family_id = ('
            '  SELECT jr2.job_family_id FROM job_requisitions jr2 WHERE jr2.id = jp.requisition_id'
            ') '
            'WHERE jp.id=? AND p.level=? LIMIT 1',
            (offer['posting_id'], level_num)
        ).fetchone()
        # fallback: 요청서의 salary_min/mid/max 직접 사용
        if not band:
            req2 = db.execute(
                'SELECT jr.salary_min, jr.salary_mid, jr.salary_max '
                'FROM job_postings jp '
                'JOIN job_requisitions jr ON jp.requisition_id = jr.id '
                'WHERE jp.id=?', (offer['posting_id'],)
            ).fetchone()
            if req2 and req2['salary_mid']:
                band = req2

    company = os.environ.get('COMPANY_NAME', 'TalentCore')
    return render_template('recruit/offer_letter.html',
                           offer=offer, company=company,
                           band=band,
                           offer_status_label=OFFER_STATUS_LABEL)


@app.route('/recruit/offers/<int:offer_id>/send', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_offer_send(offer_id):
    """오퍼 발송 처리"""
    db    = get_db()
    offer = db.execute(
        'SELECT o.*, a.name AS applicant_name, a.email AS applicant_email, '
        'jp.title AS posting_title FROM offers o '
        'JOIN applicants a ON o.applicant_id = a.id '
        'JOIN job_postings jp ON o.posting_id = jp.id WHERE o.id=?', (offer_id,)
    ).fetchone()
    if not offer:
        abort(404)
    db.execute(
        "UPDATE offers SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (offer_id,)
    )
    if offer['applicant_email']:
        subject, body = _render_email_template('offer', {
            'name': offer['applicant_name'],
            'posting_title': offer['posting_title'],
            'salary': f'{int(offer["salary"]):,}' if offer['salary'] else '협의',
            'start_date': offer['start_date'] or '협의',
            'expiry_date': offer['expiry_date'] or '협의',
        })
        _save_recruit_email(db, offer['applicant_id'], 'offer',
                            offer['applicant_email'], subject, body)
    log_recruit(offer['applicant_id'], 'offer_sent', {'offer_id': offer_id})
    db.commit()
    # Slack DM: 채용 담당자에게 오퍼 발송 완료 알림
    _sender = db.execute('SELECT email, name FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if _sender and _sender['email']:
        from integrations.dispatcher import notify_slack
        _sal = f"{int(offer['salary']):,}" if offer.get('salary') else '협의'
        notify_slack(
            _sender['email'],
            f"[TalentCore] 오퍼 발송 완료\n"
            f"지원자: {offer['applicant_name']}\n"
            f"포지션: {offer['posting_title']}\n"
            f"연봉: {_sal}원\n"
            f"입사예정: {offer.get('start_date') or '협의'}",
            '오퍼 발송',
            name=_sender['name']
        )
    flash('오퍼가 발송 처리됐습니다. (이메일 기록 저장)', 'success')
    return redirect(url_for('recruit_applicant_detail', applicant_id=offer['applicant_id']))


# ── 이메일 발송 이력 / 미리보기 ───────────────────────────────────────────────

@app.route('/recruit/applicants/<int:applicant_id>/emails')
@retired_ats
@recruiter_or_admin
def recruit_email_logs(applicant_id):
    """발송 이메일 이력 JSON (상세 페이지 탭용 AJAX)"""
    db   = get_db()
    logs = db.execute(
        'SELECT e.*, u.name AS sent_by_name FROM recruit_emails e '
        'LEFT JOIN users u ON e.sent_by = u.id '
        'WHERE e.applicant_id=? ORDER BY e.sent_at DESC', (applicant_id,)
    ).fetchall()
    return jsonify([dict(r) for r in logs])


@app.route('/recruit/applicants/<int:applicant_id>/email-send', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_email_send(applicant_id):
    """이메일 작성 모달에서 커스텀 이메일 발송 이력 저장"""
    db = get_db()
    ap = db.execute('SELECT * FROM applicants WHERE id=?', (applicant_id,)).fetchone()
    if not ap:
        return jsonify({'error': 'not found'}), 404
    recipient = request.form.get('recipient', ap['email'])
    subject   = request.form.get('subject', '').strip()
    body      = request.form.get('body', '').strip()
    if not subject or not body:
        return jsonify({'error': '제목과 본문을 입력하세요.'}), 400
    _save_recruit_email(db, applicant_id, 'custom', recipient, subject, body)
    log_recruit(applicant_id, 'email_sent', {'type': 'custom', 'subject': subject})
    db.commit()
    return jsonify({'ok': True})


@app.route('/recruit/email-preview', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_email_preview():
    """이메일 템플릿 미리보기 JSON"""
    tpl_key = request.json.get('type', 'fail')
    context = request.json.get('context', {})
    subject, body = _render_email_template(tpl_key, context)
    return jsonify({'subject': subject, 'body': body})


@app.route('/recruit/rounds/<int:round_id>/notes/add', methods=['POST'])
@retired_ats
@login_required
def recruit_round_note_add(round_id):
    """면접 라운드 빠른 메모 추가"""
    db      = get_db()
    rnd     = db.execute('SELECT * FROM interview_rounds WHERE id=?', (round_id,)).fetchone()
    if not rnd:
        abort(404)
    content = request.form.get('content', '').strip()
    if not content:
        flash('내용을 입력해주세요.', 'warning')
        return redirect(request.referrer or url_for('recruit_applicant_detail', applicant_id=rnd['applicant_id']))
    db.execute(
        'INSERT INTO interview_round_notes (round_id, author_id, content) VALUES (?,?,?)',
        (round_id, session['user_id'], content)
    )
    db.commit()
    return redirect(url_for('recruit_applicant_detail', applicant_id=rnd['applicant_id']) + '#interviews')


@app.route('/recruit/applicants/<int:applicant_id>/panel')
@retired_ats
@recruiter_or_admin
def recruit_applicant_panel(applicant_id):
    """슬라이드인 패널용 JSON"""
    import json as _json
    db = get_db()
    a = db.execute(
        'SELECT a.*, jp.title AS posting_title, jp.id AS posting_id '
        'FROM applicants a JOIN job_postings jp ON a.posting_id = jp.id '
        'WHERE a.id=?', (applicant_id,)
    ).fetchone()
    if not a:
        return jsonify({'ok': False}), 404

    rounds = db.execute(
        'SELECT r.*, '
        '(SELECT COUNT(*) FROM interview_feedback f WHERE f.round_id = r.id) AS feedback_count, '
        '(SELECT AVG(f.score_overall) FROM interview_feedback f WHERE f.round_id = r.id) AS avg_score '
        'FROM interview_rounds r WHERE r.applicant_id=? ORDER BY r.round_no',
        (applicant_id,)
    ).fetchall()

    logs = db.execute(
        'SELECT l.stage, l.note, l.created_at, u.name AS changed_by_name '
        'FROM applicant_logs l JOIN users u ON l.changed_by = u.id '
        'WHERE l.applicant_id=? ORDER BY l.created_at DESC LIMIT 10',
        (applicant_id,)
    ).fetchall()

    return jsonify({
        'ok': True,
        'applicant': {
            'id':          a['id'],
            'name':        a['name'],
            'email':       a['email'],
            'phone':       a['phone'] or '',
            'source':      SOURCE_LABELS.get(a['source'], a['source']),
            'stage':       a['stage'],
            'stage_label': STAGE_MAP.get(a['stage'], a['stage']),
            'resume_note': a['resume_note'] or '',
            'created_at':  a['created_at'][:10],
            'days':        int(db.execute(
                'SELECT julianday("now") - julianday(created_at) FROM applicants WHERE id=?',
                (applicant_id,)).fetchone()[0] or 0),
            'posting_title': a['posting_title'],
            'posting_id':    a['posting_id'],
        },
        'rounds': [{'round_no': r['round_no'],
                    'round_type': ROUND_TYPE_LABEL.get(r['round_type'], r['round_type']),
                    'status': ROUND_STATUS_LABEL.get(r['status'], r['status']),
                    'scheduled_at': (r['scheduled_at'] or '')[:16],
                    'feedback_count': r['feedback_count'],
                    'avg_score': round(r['avg_score'], 1) if r['avg_score'] else None}
                   for r in rounds],
        'logs': [{'stage': STAGE_MAP.get(l['stage'], l['stage']),
                  'note': l['note'] or '',
                  'created_at': l['created_at'][:16],
                  'changed_by': l['changed_by_name']}
                 for l in logs],
    })

@app.route('/recruit/applicants/<int:applicant_id>', methods=['GET', 'POST'])
@retired_ats
@recruiter_or_admin
def recruit_applicant_detail(applicant_id):
    db        = get_db()
    applicant = db.execute(
        'SELECT a.*, jp.title AS posting_title, jp.id AS posting_id, '
        'jp.salary_min, jp.salary_max, '
        'jr.job_level, jr.track, jr.salary_mid AS req_salary_mid, jr.job_family_id '
        'FROM applicants a '
        'JOIN job_postings jp ON a.posting_id = jp.id '
        'LEFT JOIN job_requisitions jr ON jp.requisition_id = jr.id '
        'WHERE a.id=?', (applicant_id,)
    ).fetchone()
    if not applicant:
        abort(404)

    if request.method == 'POST':
        new_stage = request.form.get('stage', '')
        note      = request.form.get('note', '').strip() or None
        reason_code = request.form.get('reason_code', '').strip() or None
        if new_stage in STAGE_MAP:
            db.execute('UPDATE applicants SET stage=? WHERE id=?', (new_stage, applicant_id))
            db.execute(
                'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?, ?, ?, ?)',
                (applicant_id, new_stage, note, session['user_id'])
            )
            meta = {'stage': new_stage, 'note': note}
            if reason_code:
                meta['reason_code'] = reason_code
            log_recruit(applicant_id, 'stage_changed', meta)
            db.commit()
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    # 면접 라운드 + 인터뷰어 + 피드백
    rounds = db.execute(
        'SELECT r.*, u.name AS created_by_name '
        'FROM interview_rounds r JOIN users u ON r.created_by = u.id '
        'WHERE r.applicant_id=? ORDER BY r.round_no', (applicant_id,)
    ).fetchall()

    rounds_data = []
    for r in rounds:
        interviewers = db.execute(
            'SELECT ii.*, u.name AS interviewer_name, u.email AS interviewer_email '
            'FROM interview_interviewers ii JOIN users u ON ii.interviewer_id = u.id '
            'WHERE ii.round_id=?', (r['id'],)
        ).fetchall()
        feedbacks = db.execute(
            'SELECT f.*, u.name AS interviewer_name '
            'FROM interview_feedback f JOIN users u ON f.interviewer_id = u.id '
            'WHERE f.round_id=? ORDER BY f.submitted_at', (r['id'],)
        ).fetchall()
        # 라운드별 빠른 메모
        round_notes = db.execute(
            'SELECT n.*, u.name AS author_name '
            'FROM interview_round_notes n JOIN users u ON n.author_id = u.id '
            'WHERE n.round_id=? ORDER BY n.created_at', (r['id'],)
        ).fetchall()
        # 인터뷰어 누적 면접 시간 (분)
        for iv in interviewers:
            total_min = db.execute(
                'SELECT COALESCE(SUM(r2.actual_min), 0) '
                'FROM interview_interviewers ii2 '
                'JOIN interview_rounds r2 ON ii2.round_id = r2.id '
                'WHERE ii2.interviewer_id=? AND r2.status="completed"',
                (iv['interviewer_id'],)
            ).fetchone()[0]
            # sqlite Row는 immutable이므로 dict로 변환
        feedbacks_list = [dict(f) for f in feedbacks]
        interviewers_list = [dict(iv) for iv in interviewers]
        # 피드백 제출한 인터뷰어 set
        submitted_ids = {f['interviewer_id'] for f in feedbacks_list}
        for iv in interviewers_list:
            iv['feedback_submitted'] = iv['interviewer_id'] in submitted_ids
        rounds_data.append({
            'round': dict(r),
            'interviewers': interviewers_list,
            'feedbacks': feedbacks_list,
            'notes': [dict(n) for n in round_notes],
        })

    # 채용 전체 활동 로그
    activity_logs = db.execute(
        'SELECT l.*, u.name AS actor_name '
        'FROM recruit_activity_logs l LEFT JOIN users u ON l.actor_id = u.id '
        'WHERE l.applicant_id=? ORDER BY l.created_at DESC',
        (applicant_id,)
    ).fetchall()

    # 단계 변경 로그 (기존)
    stage_logs = db.execute(
        'SELECT l.*, u.name AS changed_by_name '
        'FROM applicant_logs l JOIN users u ON l.changed_by = u.id '
        'WHERE l.applicant_id=? ORDER BY l.created_at DESC',
        (applicant_id,)
    ).fetchall()

    # 인터뷰어 후보 (admin/manager/recruiter)
    interviewers_all = db.execute(
        "SELECT id, name, email FROM users WHERE role IN ('admin','manager','recruiter') "
        "AND role != 'guest' ORDER BY name"
    ).fetchall()

    # 제출 서류 목록
    documents = db.execute(
        'SELECT d.*, u.name AS uploader_name '
        'FROM applicant_documents d LEFT JOIN users u ON d.uploaded_by = u.id '
        'WHERE d.applicant_id=? ORDER BY d.uploaded_at DESC', (applicant_id,)
    ).fetchall()

    # 오퍼 목록
    offers = db.execute(
        'SELECT o.*, u.name AS created_by_name FROM offers o '
        'LEFT JOIN users u ON o.created_by = u.id '
        'WHERE o.applicant_id=? ORDER BY o.created_at DESC', (applicant_id,)
    ).fetchall()

    # 이메일 발송 이력
    email_logs = db.execute(
        'SELECT e.*, u.name AS sent_by_name FROM recruit_emails e '
        'LEFT JOIN users u ON e.sent_by = u.id '
        'WHERE e.applicant_id=? ORDER BY e.sent_at DESC', (applicant_id,)
    ).fetchall()

    return render_template('recruit/applicant_detail.html',
                           applicant=applicant,
                           rounds_data=rounds_data,
                           activity_logs=activity_logs,
                           stage_logs=stage_logs,
                           interviewers_all=interviewers_all,
                           documents=documents,
                           doc_type_label=DOC_TYPE_LABEL,
                           offers=offers,
                           email_logs=email_logs,
                           offer_status_label=OFFER_STATUS_LABEL,
                           email_templates=EMAIL_TEMPLATES,
                           stages=STAGES, stage_map=STAGE_MAP,
                           round_type_label=ROUND_TYPE_LABEL,
                           round_status_label=ROUND_STATUS_LABEL,
                           recommendation_label=RECOMMENDATION_LABEL,
                           rejection_reason_codes=REJECTION_REASON_CODES,
                           source_labels=SOURCE_LABELS,
                           active_page='recruit')


@app.route('/recruit/applicants/<int:applicant_id>/documents/upload', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_doc_upload(applicant_id):
    db = get_db()
    f = request.files.get('file')
    doc_type = request.form.get('doc_type', 'resume')
    if not f or not f.filename:
        flash('파일을 선택해주세요.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))
    if not allowed_file(f.filename):
        flash('허용되지 않는 파일 형식입니다.', 'danger')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))
    content = f.read()
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        flash(f'파일 크기는 {MAX_FILE_SIZE_MB}MB 이하여야 합니다.', 'danger')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))
    ext = f.filename.rsplit('.', 1)[1].lower()
    stored_name = f'{uuid.uuid4().hex}.{ext}'
    save_path = os.path.join(UPLOAD_FOLDER, stored_name)
    with open(save_path, 'wb') as out:
        out.write(content)
    db.execute(
        'INSERT INTO applicant_documents (applicant_id, doc_type, original_name, stored_name, file_size, uploaded_by) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (applicant_id, doc_type, f.filename, stored_name, len(content), session['user_id'])
    )
    log_recruit(applicant_id, 'document_uploaded', {'doc_type': doc_type, 'file': f.filename})
    db.commit()
    flash('서류가 업로드됐습니다.', 'success')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))


@app.route('/recruit/documents/<int:doc_id>/file')
@retired_ats
@login_required
def recruit_doc_file(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM applicant_documents WHERE id=?', (doc_id,)).fetchone()
    if not doc:
        abort(404)
    from flask import send_from_directory
    return send_from_directory(UPLOAD_FOLDER, doc['stored_name'],
                               download_name=doc['original_name'])


@app.route('/recruit/documents/<int:doc_id>/delete', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_doc_delete(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM applicant_documents WHERE id=?', (doc_id,)).fetchone()
    if not doc:
        abort(404)
    applicant_id = doc['applicant_id']
    try:
        os.remove(os.path.join(UPLOAD_FOLDER, doc['stored_name']))
    except OSError:
        pass
    db.execute('DELETE FROM applicant_documents WHERE id=?', (doc_id,))
    db.commit()
    flash('서류가 삭제됐습니다.', 'info')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))


@app.route('/recruit/applicants/<int:applicant_id>/rounds/new', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_round_new(applicant_id):
    db = get_db()
    applicant = db.execute('SELECT id FROM applicants WHERE id=?', (applicant_id,)).fetchone()
    if not applicant:
        abort(404)
    round_no      = request.form.get('round_no', '1')
    round_type    = request.form.get('round_type', 'technical')
    scheduled_at  = request.form.get('scheduled_at', '').strip() or None
    planned_min   = request.form.get('planned_min', '60')
    location_type = request.form.get('location_type', 'video')
    meet_link     = request.form.get('meet_link', '').strip() or None
    try:
        round_no    = int(round_no)
        planned_min = int(planned_min)
    except ValueError:
        abort(400)
    if round_type not in ROUND_TYPE_LABEL:
        abort(400)
    cur = db.execute(
        'INSERT INTO interview_rounds '
        '(applicant_id, round_no, round_type, scheduled_at, planned_min, '
        ' location_type, meet_link, created_by) VALUES (?,?,?,?,?,?,?,?)',
        (applicant_id, round_no, round_type, scheduled_at, planned_min,
         location_type, meet_link, session['user_id'])
    )
    round_id = cur.lastrowid
    db.commit()
    log_recruit(applicant_id, 'round_created', {
        'round_no': round_no, 'round_type': round_type,
        'scheduled_at': scheduled_at, 'planned_min': planned_min
    }, round_id=round_id)
    flash(f'{round_no}차 면접 라운드가 생성되었습니다.', 'success')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id) + '#interviews')


@app.route('/recruit/rounds/<int:round_id>/interviewers', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_round_assign_interviewer(round_id):
    db = get_db()
    r = db.execute('SELECT * FROM interview_rounds WHERE id=?', (round_id,)).fetchone()
    if not r:
        abort(404)
    interviewer_id = request.form.get('interviewer_id', type=int)
    is_required    = 1 if request.form.get('is_required') else 0
    if not interviewer_id:
        abort(400)
    try:
        db.execute(
            'INSERT INTO interview_interviewers '
            '(round_id, interviewer_id, is_required, assigned_by) VALUES (?,?,?,?)',
            (round_id, interviewer_id, is_required, session['user_id'])
        )
        db.commit()
        iv = db.execute('SELECT name, email FROM users WHERE id=?', (interviewer_id,)).fetchone()
        log_recruit(r['applicant_id'], 'interviewer_assigned',
                    {'interviewer_id': interviewer_id,
                     'interviewer_name': iv['name'] if iv else ''},
                    round_id=round_id)
        _ap_info = db.execute('SELECT name FROM applicants WHERE id=?', (r['applicant_id'],)).fetchone()
        add_notification(
            interviewer_id, 'action', 'recruit',
            f'{r["round_no"]}차 면접 인터뷰어 배정',
            f'{_ap_info["name"] if _ap_info else "지원자"} — {r["scheduled_at"] or "일정 미정"}',
            url_for('recruit_applicant_detail', applicant_id=r['applicant_id'])
        )
        # Slack DM
        if iv and iv['email']:
            from integrations.dispatcher import notify_slack
            _sched = r['scheduled_at'] or '일정 미정'
            _ap_nm = _ap_info['name'] if _ap_info else '지원자'
            notify_slack(
                iv['email'],
                f"[TalentCore] 면접 배정 알림\n"
                f"{r['round_no']}차 면접 인터뷰어로 배정됐습니다.\n"
                f"지원자: {_ap_nm}\n"
                f"일정: {_sched} ({r['planned_min']}분)\n"
                f"TalentCore에서 지원자 정보를 확인하세요.",
                '면접 배정',
                name=iv['name']
            )
    except Exception:
        flash('이미 배정된 인터뷰어입니다.', 'error')
    return redirect(url_for('recruit_applicant_detail', applicant_id=r['applicant_id']) + '#interviews')


@app.route('/recruit/rounds/<int:round_id>/interviewers/<int:interviewer_id>/remove', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_round_remove_interviewer(round_id, interviewer_id):
    db = get_db()
    r = db.execute('SELECT * FROM interview_rounds WHERE id=?', (round_id,)).fetchone()
    if not r:
        abort(404)
    db.execute('DELETE FROM interview_interviewers WHERE round_id=? AND interviewer_id=?',
               (round_id, interviewer_id))
    db.commit()
    log_recruit(r['applicant_id'], 'interviewer_removed',
                {'interviewer_id': interviewer_id}, round_id=round_id)
    flash('인터뷰어가 제거되었습니다.', 'success')
    return redirect(url_for('recruit_applicant_detail', applicant_id=r['applicant_id']) + '#interviews')


@app.route('/recruit/rounds/<int:round_id>/complete', methods=['POST'])
@retired_ats
@recruiter_or_admin
def recruit_round_complete(round_id):
    db = get_db()
    r = db.execute('SELECT * FROM interview_rounds WHERE id=?', (round_id,)).fetchone()
    if not r:
        abort(404)
    actual_start = request.form.get('actual_start', '').strip() or None
    actual_end   = request.form.get('actual_end', '').strip() or None
    actual_min   = request.form.get('actual_min', type=int)
    status       = request.form.get('status', 'completed')
    if status not in ('completed', 'cancelled', 'no_show'):
        status = 'completed'
    db.execute(
        'UPDATE interview_rounds SET status=?, actual_start_at=?, actual_end_at=?, '
        'actual_min=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
        (status, actual_start, actual_end, actual_min, round_id)
    )
    db.commit()
    log_recruit(r['applicant_id'], 'round_status_changed',
                {'status': status, 'actual_min': actual_min}, round_id=round_id)
    flash(f'면접 상태가 "{ROUND_STATUS_LABEL.get(status, status)}"으로 업데이트되었습니다.', 'success')
    return redirect(url_for('recruit_applicant_detail', applicant_id=r['applicant_id']) + '#interviews')


@app.route('/recruit/rounds/<int:round_id>/feedback', methods=['GET', 'POST'])
@retired_ats
@login_required
def recruit_round_feedback(round_id):
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role', '')
    r = db.execute(
        'SELECT ir.*, a.name AS applicant_name, jp.title AS posting_title '
        'FROM interview_rounds ir '
        'JOIN applicants a ON ir.applicant_id = a.id '
        'JOIN job_postings jp ON a.posting_id = jp.id '
        'WHERE ir.id=?', (round_id,)
    ).fetchone()
    if not r:
        abort(404)
    # 배정된 인터뷰어 또는 admin/recruiter만 접근 가능
    is_assigned = db.execute(
        'SELECT id FROM interview_interviewers WHERE round_id=? AND interviewer_id=?',
        (round_id, uid)
    ).fetchone()
    if not is_assigned and role not in ('admin', 'recruiter'):
        flash('면접 피드백 권한이 없습니다.', 'error')
        return redirect(url_for('dashboard'))

    existing = db.execute(
        'SELECT * FROM interview_feedback WHERE round_id=? AND interviewer_id=?',
        (round_id, uid)
    ).fetchone()

    if request.method == 'POST':
        recommendation = request.form.get('recommendation', '')
        if recommendation not in ('pass', 'hold', 'fail'):
            flash('추천 여부를 선택해주세요.', 'error')
            return redirect(request.url)

        def _int(key):
            try:
                v = int(request.form.get(key, 0))
                return v if 1 <= v <= 5 else None
            except (ValueError, TypeError):
                return None

        strengths  = request.form.get('strengths', '').strip() or None
        concerns   = request.form.get('concerns', '').strip() or None
        notes      = request.form.get('interview_notes', '').strip() or None
        edit_reason = request.form.get('edit_reason', '').strip() or None

        if existing:
            db.execute(
                'UPDATE interview_feedback SET recommendation=?, '
                'score_technical=?, score_communication=?, score_culture_fit=?, '
                'score_growth=?, score_overall=?, strengths=?, concerns=?, '
                'interview_notes=?, is_edited=1, edit_reason=?, '
                'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                (recommendation, _int('score_technical'), _int('score_communication'),
                 _int('score_culture_fit'), _int('score_growth'), _int('score_overall'),
                 strengths, concerns, notes, edit_reason, existing['id'])
            )
            event = 'feedback_edited'
        else:
            db.execute(
                'INSERT INTO interview_feedback '
                '(round_id, interviewer_id, recommendation, score_technical, '
                ' score_communication, score_culture_fit, score_growth, score_overall, '
                ' strengths, concerns, interview_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (round_id, uid, recommendation, _int('score_technical'),
                 _int('score_communication'), _int('score_culture_fit'),
                 _int('score_growth'), _int('score_overall'), strengths, concerns, notes)
            )
            event = 'feedback_submitted'

        db.commit()
        log_recruit(r['applicant_id'], event, {
            'recommendation': recommendation,
            'scores': {
                'technical': _int('score_technical'),
                'communication': _int('score_communication'),
                'culture_fit': _int('score_culture_fit'),
                'growth': _int('score_growth'),
                'overall': _int('score_overall'),
            },
            'is_edited': bool(existing),
            'edit_reason': edit_reason,
        }, round_id=round_id)
        flash('피드백이 저장되었습니다.', 'success')
        return redirect(url_for('recruit_applicant_detail', applicant_id=r['applicant_id']) + '#interviews')

    return render_template('recruit/feedback_form.html',
                           round=r, existing=existing,
                           recommendation_label=RECOMMENDATION_LABEL,
                           active_page='recruit')


# ── C2: 1:1 면담 · 피드백 · 목표 체크인 ─────────────────────────────
# 흐름: 목표 체크인(직원) → 1:1 안건·메모·액션(매니저↔직원, 다음 면담으로 이월)
#       → 수시 피드백·요청 → 팀원 평가 화면 참고 패널에 같은 기간 기록이 모인다.
ONE_ON_ONE_CADENCES = (7, 14, 30)
CHECKIN_STATUS_LABEL = {'on_track': '순항', 'at_risk': '주의', 'off_track': '위험'}
CHECKIN_STATUS_CLS = {'on_track': 'done', 'at_risk': 'wait', 'off_track': 'late'}
FEEDBACK_KIND_LABEL = {'praise': '칭찬', 'suggest': '개선 제안', 'response': '요청 응답'}
ONE_ON_ONE_STATUS_LABEL = {'scheduled': '예정', 'done': '완료', 'cancelled': '취소'}


def get_perf_culture(cfg=None):
    """1:1 권장 주기(일)·칭찬 전사 공개 허용 — 회사 설정."""
    cfg = cfg or get_company_config()
    try:
        cadence = int(cfg.get('one_on_one_cadence_days') or 14)
    except (TypeError, ValueError):
        cadence = 14
    if cadence not in ONE_ON_ONE_CADENCES:
        cadence = 14
    pp = cfg.get('feedback_public_praise')
    return {'cadence': cadence, 'public_praise': True if pp is None else bool(int(pp)),
            'cadence_label': {7: '매주', 14: '격주', 30: '매월'}[cadence]}


def save_perf_culture(db, f):
    """초기 설정·회사 설정 공통 저장 (성과 섹션이 함께 제출될 때만)."""
    if 'one_on_one_cadence_days' not in f:
        return
    try:
        cadence = int(f.get('one_on_one_cadence_days'))
    except (TypeError, ValueError):
        cadence = 14
    if cadence not in ONE_ON_ONE_CADENCES:
        cadence = 14
    db.execute('UPDATE company_config SET one_on_one_cadence_days=?, feedback_public_praise=? WHERE id=1',
               (cadence, 1 if f.get('feedback_public_praise') else 0))


def _safe_next(default):
    nxt = request.form.get('next') or ''
    return nxt if nxt.startswith('/') and not nxt.startswith('//') else default


def _parse_when(s):
    s = (s or '').strip().replace('T', ' ')[:16]
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M').strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return None


def _c2_is_manager_of(emp, uid, role, dept_id):
    """1:1·피드백 요청 권한 — 보고 라인(manager_id) 또는 같은 부서의 매니저."""
    if not emp or emp['id'] == uid:
        return False
    if emp['manager_id'] == uid:
        return True
    return role == 'manager' and bool(dept_id) and emp['department_id'] == int(dept_id)


def _c2_reports(db, uid, role, dept_id):
    return db.execute(
        "SELECT u.id, u.name, u.manager_id, u.department_id, d.name AS dept_name, p.name AS pos_name "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "WHERE u.status='active' AND u.id != ? AND u.role NOT IN ('admin','guest') "
        "AND (u.manager_id=? OR (? = 'manager' AND u.manager_id IS NULL AND u.department_id=?)) "
        "ORDER BY u.name", (uid, uid, role, int(dept_id or 0) or -1)
    ).fetchall()


def _fb_visible(uid, role, dept_id, public_praise=True):
    """피드백 열람 범위 — 작성자·받는 사람(매니저 전용 제외)·받는 사람의 매니저·요청자·HR 관리자."""
    if role == 'admin':
        return '1=1', []
    sql = ("(f.from_id=? OR (f.to_id=? AND f.visibility!='manager') OR t.manager_id=? "
           "OR f.request_id IN (SELECT id FROM feedback_requests WHERE requester_id=?)")
    args = [uid, uid, uid, uid]
    if public_praise:
        sql += " OR f.visibility='public'"
    if role == 'manager' and dept_id:
        sql += " OR (t.department_id=? AND f.to_id != ?)"
        args += [int(dept_id), uid]
    return sql + ')', args


FB_SELECT = ("SELECT f.*, fu.name AS from_name, t.name AS to_name, td.name AS to_dept, "
             "fr.question AS req_question "
             "FROM feedback f JOIN users fu ON f.from_id=fu.id JOIN users t ON f.to_id=t.id "
             "LEFT JOIN departments td ON t.department_id=td.id "
             "LEFT JOIN feedback_requests fr ON f.request_id=fr.id ")


def _latest_checkins(db, goal_ids):
    if not goal_ids:
        return {}
    q = ','.join('?' * len(goal_ids))
    rows = db.execute(
        f"SELECT gc.* FROM goal_checkins gc WHERE gc.id IN "
        f"(SELECT MAX(id) FROM goal_checkins WHERE goal_id IN ({q}) GROUP BY goal_id)", list(goal_ids)
    ).fetchall()
    return {r['goal_id']: r for r in rows}


def _c2_team_rows(db, uid, role, dept_id, pc):
    """매니저 기준 팀원별 1:1 주기·액션·피드백·목표 체크인 현황."""
    today = date.today()
    cut90 = (today - timedelta(days=90)).isoformat()
    rows = []
    for r in _c2_reports(db, uid, role, dept_id):
        last = db.execute("SELECT MAX(scheduled_at) FROM one_on_ones WHERE employee_id=? AND manager_id=? "
                          "AND status='done'", (r['id'], uid)).fetchone()[0]
        nxt = db.execute("SELECT id, scheduled_at FROM one_on_ones WHERE employee_id=? AND manager_id=? "
                         "AND status='scheduled' ORDER BY scheduled_at LIMIT 1", (r['id'], uid)).fetchone()
        open_actions = db.execute(
            "SELECT COUNT(*) FROM one_on_one_actions a JOIN one_on_ones o ON a.meeting_id=o.id "
            "WHERE o.employee_id=? AND o.manager_id=? AND a.status='open'", (r['id'], uid)).fetchone()[0]
        fb_n = db.execute("SELECT COUNT(*) FROM feedback WHERE to_id=? AND created_at>=?",
                          (r['id'], cut90)).fetchone()[0]
        goal_ids = [g['id'] for g in db.execute(
            "SELECT g.id FROM performance_goals g JOIN performance_cycles c ON g.cycle_id=c.id "
            "WHERE g.user_id=? AND c.status='active'", (r['id'],)).fetchall()]
        risk_n = sum(1 for ck in _latest_checkins(db, goal_ids).values() if ck['status'] != 'on_track')
        days = (today - date.fromisoformat(last[:10])).days if last else None
        if nxt:
            state = 'wait' if nxt['scheduled_at'][:10] < today.isoformat() else ''
        else:
            state = 'late' if (days is None or days > pc['cadence']) else ''
        rows.append({'id': r['id'], 'name': r['name'], 'dept_name': r['dept_name'], 'pos_name': r['pos_name'],
                     'last': last, 'days': days, 'next': nxt, 'open_actions': open_actions,
                     'fb_n': fb_n, 'risk_n': risk_n, 'state': state})
    return rows


def _c2_todo(db, uid, role, dept_id, pc, with_team=True):
    """홈 인박스용 — 임박 1:1, 주기 초과 팀원, 응답 대기 피드백 요청."""
    items = []
    ad = perf_addons(db)
    soon = (date.today() + timedelta(days=2)).isoformat() + ' 23:59'
    for m in [] if not ad['one_on_one'] else db.execute(
            "SELECT o.id, o.scheduled_at, o.manager_id, mu.name AS mname, eu.name AS ename "
            "FROM one_on_ones o JOIN users mu ON o.manager_id=mu.id JOIN users eu ON o.employee_id=eu.id "
            "WHERE (o.manager_id=? OR o.employee_id=?) AND o.status='scheduled' AND o.scheduled_at<=? "
            "ORDER BY o.scheduled_at LIMIT 3", (uid, uid, soon)).fetchall():
        other = m['ename'] if m['manager_id'] == uid else m['mname']
        items.append({'id': m['id'], 'category': 'one_on_one', 'title': f"{other} — 1:1 면담",
                      'sub': m['scheduled_at'], 'link': url_for('one_on_one', mid=m['id'])})
    if with_team and ad['one_on_one']:
        late = [r for r in _c2_team_rows(db, uid, role, dept_id, pc) if r['state'] == 'late']
        if late:
            names = ', '.join(r['name'] for r in late[:3]) + (f" 외 {len(late) - 3}명" if len(late) > 3 else '')
            items.append({'id': 0, 'category': 'one_on_one', 'title': f"1:1 주기 초과 팀원 {len(late)}명",
                          'sub': f"권장 {pc['cadence']}일 · {names}", 'link': url_for('one_on_ones')})
    for q in [] if not ad['feedback'] else db.execute(
            "SELECT fr.id, fr.due_date, ru.name AS rname, su.name AS sname, fr.subject_id, fr.requester_id "
            "FROM feedback_requests fr JOIN users ru ON fr.requester_id=ru.id JOIN users su ON fr.subject_id=su.id "
            "WHERE fr.responder_id=? AND fr.status='pending' ORDER BY fr.id LIMIT 3", (uid,)).fetchall():
        about = '본인' if q['subject_id'] == q['requester_id'] else q['sname']
        items.append({'id': q['id'], 'category': 'feedback', 'title': f"{q['rname']} — 피드백 요청",
                      'sub': f"대상 {about}" + (f" · 기한 {q['due_date']}" if q['due_date'] else ''),
                      'link': url_for('feedback_home', tab='requests')})
    return items


def _c2_member_context(db, emp_uid, cycle, viewer_uid, role, dept_id):
    """팀원 평가 화면 참고 패널 — 사이클 기간의 1:1·피드백·체크인."""
    start = (cycle['start_date'] or '0000-01-01')[:10]
    end = (cycle['end_date'] or '9999-12-31')[:10] + ' 23:59:59'
    oo_count = db.execute("SELECT COUNT(*) FROM one_on_ones WHERE employee_id=? AND status='done' "
                          "AND scheduled_at BETWEEN ? AND ?", (emp_uid, start, end)).fetchone()[0]
    oo_last = db.execute("SELECT MAX(scheduled_at) FROM one_on_ones WHERE employee_id=? AND status='done'",
                         (emp_uid,)).fetchone()[0]
    oo_next = db.execute("SELECT id, scheduled_at, manager_id FROM one_on_ones WHERE employee_id=? "
                         "AND status='scheduled' ORDER BY scheduled_at LIMIT 1", (emp_uid,)).fetchone()
    open_actions = db.execute(
        "SELECT COUNT(*) FROM one_on_one_actions a JOIN one_on_ones o ON a.meeting_id=o.id "
        "WHERE o.employee_id=? AND a.status='open'", (emp_uid,)).fetchone()[0]
    pc = get_perf_culture()
    vis_sql, vis_args = _fb_visible(viewer_uid, role, dept_id, pc['public_praise'])
    fb = db.execute(FB_SELECT + f"WHERE f.to_id=? AND f.created_at BETWEEN ? AND ? AND {vis_sql} "
                    "ORDER BY f.id DESC", [emp_uid, start, end] + vis_args).fetchall()
    goals = db.execute("SELECT id, title FROM performance_goals WHERE cycle_id=? AND user_id=?",
                       (cycle['id'], emp_uid)).fetchall()
    cks = _latest_checkins(db, [g['id'] for g in goals])
    risk = [{'title': g['title'], 'ck': cks[g['id']]} for g in goals
            if g['id'] in cks and cks[g['id']]['status'] != 'on_track']
    emp = db.execute('SELECT id, manager_id, department_id FROM users WHERE id=?', (emp_uid,)).fetchone()
    return {'oo_count': oo_count, 'oo_last': oo_last, 'open_actions': open_actions,
            'oo_next': oo_next if oo_next and oo_next['manager_id'] == viewer_uid else None,
            'can_1on1': _c2_is_manager_of(emp, viewer_uid, role, dept_id),
            'fb_counts': {k: sum(1 for f in fb if f['kind'] == k) for k in FEEDBACK_KIND_LABEL},
            'fb_recent': fb[:3], 'risk_goals': risk, 'checked_in': len(cks), 'goal_n': len(goals)}


@app.route('/performance/one-on-ones', methods=['GET', 'POST'])
@login_required
def one_on_ones():
    db = get_db()
    uid, role = session['user_id'], session.get('user_role')
    dept_id = int(session.get('dept_id') or 0)
    pc = get_perf_culture()

    if request.method == 'POST':
        when = _parse_when(request.form.get('scheduled_at'))
        topic = (request.form.get('topic') or '').strip()[:500]
        if not when:
            flash('면담 일시를 입력하세요.', 'error')
            return redirect(url_for('one_on_ones'))
        emp_id = request.form.get('employee_id', type=int)
        if emp_id:
            emp = db.execute("SELECT id, name, manager_id, department_id FROM users WHERE id=? AND status='active'",
                             (emp_id,)).fetchone()
            if not _c2_is_manager_of(emp, uid, role, dept_id):
                abort(403)
            mgr_id, emp_uid, other = uid, emp['id'], emp['id']
        else:
            me = db.execute('SELECT manager_id FROM users WHERE id=?', (uid,)).fetchone()
            if not me or not me['manager_id']:
                flash('보고 라인에 매니저가 지정되지 않아 1:1을 잡을 수 없습니다.', 'error')
                return redirect(url_for('one_on_ones'))
            mgr_id, emp_uid, other = me['manager_id'], uid, me['manager_id']
        dup = db.execute("SELECT id FROM one_on_ones WHERE manager_id=? AND employee_id=? AND status='scheduled'",
                         (mgr_id, emp_uid)).fetchone()
        if dup:
            flash('예정된 1:1이 이미 있어 해당 면담으로 이동했습니다.', 'warning')
            return redirect(url_for('one_on_one', mid=dup['id']))
        mid = db.execute('INSERT INTO one_on_ones (manager_id, employee_id, scheduled_at) VALUES (?,?,?)',
                         (mgr_id, emp_uid, when)).lastrowid
        if topic:
            db.execute('INSERT INTO one_on_one_topics (meeting_id, author_id, content) VALUES (?,?,?)',
                       (mid, uid, topic))
        db.commit()
        add_notification(other, 'info', 'perf', '1:1 면담 예정',
                         f"{session.get('user_name')} · {when}", url_for('one_on_one', mid=mid))
        return redirect(url_for('one_on_one', mid=mid))

    today = date.today()
    meetings = db.execute(
        "SELECT o.*, mu.name AS manager_name, eu.name AS employee_name, "
        "(SELECT COUNT(*) FROM one_on_one_topics t WHERE t.meeting_id=o.id) AS topic_n, "
        "(SELECT COUNT(*) FROM one_on_one_actions a WHERE a.meeting_id=o.id AND a.status='open') AS open_n "
        "FROM one_on_ones o JOIN users mu ON o.manager_id=mu.id JOIN users eu ON o.employee_id=eu.id "
        "WHERE o.manager_id=? OR o.employee_id=? "
        "ORDER BY CASE o.status WHEN 'scheduled' THEN 0 ELSE 1 END, "
        "CASE o.status WHEN 'scheduled' THEN o.scheduled_at END ASC, o.scheduled_at DESC LIMIT 40",
        (uid, uid)).fetchall()
    my_mgr = db.execute('SELECT m.id, m.name FROM users u JOIN users m ON u.manager_id=m.id WHERE u.id=?',
                        (uid,)).fetchone()
    team = _c2_team_rows(db, uid, role, dept_id, pc)

    managers = []
    if role == 'admin':
        cut = (today - timedelta(days=pc['cadence'])).isoformat()
        cut90 = (today - timedelta(days=90)).isoformat()
        done90 = dict(db.execute("SELECT manager_id, COUNT(*) FROM one_on_ones WHERE status='done' "
                                 "AND scheduled_at>=? GROUP BY manager_id", (cut90,)).fetchall())
        covered = dict(db.execute(
            "SELECT o.manager_id, COUNT(DISTINCT o.employee_id) FROM one_on_ones o "
            "JOIN users u ON u.id=o.employee_id AND u.manager_id=o.manager_id "
            "WHERE (o.status='done' AND o.scheduled_at>=?) OR (o.status='scheduled' AND o.scheduled_at>=?) "
            "GROUP BY o.manager_id", (cut, today.isoformat())).fetchall())
        last = dict(db.execute("SELECT manager_id, MAX(scheduled_at) FROM one_on_ones WHERE status='done' "
                               "GROUP BY manager_id").fetchall())
        open_a = dict(db.execute("SELECT o.manager_id, COUNT(*) FROM one_on_one_actions a "
                                 "JOIN one_on_ones o ON a.meeting_id=o.id WHERE a.status='open' "
                                 "GROUP BY o.manager_id").fetchall())
        for m in db.execute(
                "SELECT m.id, m.name, d.name AS dept_name, COUNT(u.id) AS reports FROM users u "
                "JOIN users m ON u.manager_id=m.id LEFT JOIN departments d ON m.department_id=d.id "
                "WHERE u.status='active' AND m.status='active' GROUP BY m.id "
                "ORDER BY d.name, m.name").fetchall():
            cov = covered.get(m['id'], 0)
            managers.append({'id': m['id'], 'name': m['name'], 'dept_name': m['dept_name'],
                             'reports': m['reports'], 'covered': cov, 'done90': done90.get(m['id'], 0),
                             'last': last.get(m['id']), 'open_actions': open_a.get(m['id'], 0),
                             'state': 'late' if cov * 2 < m['reports'] else ('done' if cov >= m['reports'] else '')})

    return render_template('performance/one_on_ones.html',
                           pc=pc, meetings=meetings, my_mgr=my_mgr, team=team, managers=managers,
                           pre_emp=request.args.get('emp', type=int),
                           default_when=(datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT10:00'),
                           status_label=ONE_ON_ONE_STATUS_LABEL, active_page='one_on_ones')


@app.route('/performance/one-on-ones/<int:mid>', methods=['GET', 'POST'])
@login_required
def one_on_one(mid):
    db = get_db()
    uid, role = session['user_id'], session.get('user_role')
    dept_id = int(session.get('dept_id') or 0)
    m = db.execute(
        "SELECT o.*, mu.name AS manager_name, eu.name AS employee_name, d.name AS emp_dept, p.name AS emp_pos "
        "FROM one_on_ones o JOIN users mu ON o.manager_id=mu.id JOIN users eu ON o.employee_id=eu.id "
        "LEFT JOIN departments d ON eu.department_id=d.id LEFT JOIN positions p ON eu.position_id=p.id "
        "WHERE o.id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    if uid not in (m['manager_id'], m['employee_id']):
        abort(403)   # 1:1 내용은 참여자만 — HR 관리자는 목록 화면에서 진행 현황만 본다
    is_mgr = uid == m['manager_id']
    other_id = m['employee_id'] if is_mgr else m['manager_id']
    pair = (m['manager_id'], m['employee_id'])
    pc = get_perf_culture()

    if request.method == 'POST':
        a = request.form.get('action', '')
        content = (request.form.get('content') or '').strip()
        item_id = request.form.get('item_id', type=int)
        go = mid
        anchor = ''
        if a == 'topic_add' and content:
            db.execute('INSERT INTO one_on_one_topics (meeting_id, author_id, content) VALUES (?,?,?)',
                       (mid, uid, content[:500]))
            anchor = '#topics'
        elif a == 'topic_toggle' and item_id:
            db.execute('UPDATE one_on_one_topics SET is_done=1-is_done WHERE id=? AND meeting_id=?', (item_id, mid))
            anchor = '#topics'
        elif a == 'topic_del' and item_id:
            db.execute('DELETE FROM one_on_one_topics WHERE id=? AND meeting_id=? AND author_id=?',
                       (item_id, mid, uid))
            anchor = '#topics'
        elif a == 'notes':
            db.execute('UPDATE one_on_ones SET notes=? WHERE id=?', (content[:10000], mid))
            flash('공유 메모 저장', 'success')
            anchor = '#notes'
        elif a == 'private':
            db.execute('INSERT INTO one_on_one_private_notes (meeting_id, user_id, content, updated_at) '
                       'VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(meeting_id, user_id) '
                       'DO UPDATE SET content=excluded.content, updated_at=CURRENT_TIMESTAMP',
                       (mid, uid, content[:10000]))
            flash('비공개 메모 저장', 'success')
            anchor = '#notes'
        elif a == 'action_add' and content:
            owner = request.form.get('owner_id', type=int)
            owner = owner if owner in pair else m['employee_id']
            due = (request.form.get('due_date') or '')[:10] or None
            db.execute('INSERT INTO one_on_one_actions (meeting_id, content, owner_id, due_date) VALUES (?,?,?,?)',
                       (mid, content[:500], owner, due))
            if owner != uid:
                add_notification(owner, 'action', 'perf', '1:1 액션 아이템',
                                 content[:80] + (f" · 기한 {due}" if due else ''), url_for('one_on_one', mid=mid))
            anchor = '#actions'
        elif a == 'action_toggle' and item_id:
            db.execute("UPDATE one_on_one_actions SET status=CASE status WHEN 'open' THEN 'done' ELSE 'open' END "
                       "WHERE id=? AND meeting_id IN (SELECT id FROM one_on_ones WHERE manager_id=? AND employee_id=?)",
                       (item_id,) + pair)
            anchor = '#actions'
        elif a == 'reschedule' and m['status'] == 'scheduled':
            when = _parse_when(request.form.get('scheduled_at'))
            if when:
                db.execute('UPDATE one_on_ones SET scheduled_at=? WHERE id=?', (when, mid))
                add_notification(other_id, 'info', 'perf', '1:1 일정 변경', f"{session.get('user_name')} · {when}",
                                 url_for('one_on_one', mid=mid))
        elif a == 'cancel' and m['status'] == 'scheduled':
            db.execute("UPDATE one_on_ones SET status='cancelled' WHERE id=?", (mid,))
            add_notification(other_id, 'info', 'perf', '1:1 면담 취소',
                             f"{session.get('user_name')} · {m['scheduled_at']}", url_for('one_on_one', mid=mid))
        elif a == 'complete' and m['status'] == 'scheduled':
            db.execute("UPDATE one_on_ones SET status='done', done_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
            nxt = _parse_when(request.form.get('next_at'))
            if nxt:
                go = db.execute('INSERT INTO one_on_ones (manager_id, employee_id, scheduled_at) VALUES (?,?,?)',
                                pair + (nxt,)).lastrowid
                add_notification(other_id, 'info', 'perf', '다음 1:1 면담 예정',
                                 f"{session.get('user_name')} · {nxt}", url_for('one_on_one', mid=go))
                flash(f'면담 완료 · 다음 1:1 {nxt} 예정 · 미완료 액션은 다음 면담에 이어서 표시', 'success')
            else:
                flash('면담 완료', 'success')
        db.commit()
        return redirect(url_for('one_on_one', mid=go) + anchor)

    topics = db.execute("SELECT t.*, u.name AS author_name FROM one_on_one_topics t JOIN users u ON t.author_id=u.id "
                        "WHERE t.meeting_id=? ORDER BY t.is_done, t.id", (mid,)).fetchall()
    actions = db.execute("SELECT a.*, u.name AS owner_name FROM one_on_one_actions a "
                         "LEFT JOIN users u ON a.owner_id=u.id WHERE a.meeting_id=? "
                         "ORDER BY CASE a.status WHEN 'open' THEN 0 ELSE 1 END, a.id", (mid,)).fetchall()
    carry = db.execute("SELECT a.*, u.name AS owner_name, o.scheduled_at AS meeting_at FROM one_on_one_actions a "
                       "JOIN one_on_ones o ON a.meeting_id=o.id LEFT JOIN users u ON a.owner_id=u.id "
                       "WHERE o.manager_id=? AND o.employee_id=? AND o.id != ? AND a.status='open' "
                       "ORDER BY o.scheduled_at, a.id", pair + (mid,)).fetchall()
    history = db.execute("SELECT o.id, o.scheduled_at, o.status, "
                         "(SELECT COUNT(*) FROM one_on_one_topics t WHERE t.meeting_id=o.id) AS topic_n "
                         "FROM one_on_ones o WHERE o.manager_id=? AND o.employee_id=? AND o.id != ? "
                         "ORDER BY o.scheduled_at DESC LIMIT 8", pair + (mid,)).fetchall()
    private = db.execute('SELECT content, updated_at FROM one_on_one_private_notes WHERE meeting_id=? AND user_id=?',
                         (mid, uid)).fetchone()
    goals = db.execute("SELECT g.id, g.title, g.progress, g.weight, c.name AS cycle_name "
                       "FROM performance_goals g JOIN performance_cycles c ON g.cycle_id=c.id "
                       "WHERE g.user_id=? AND c.status='active' ORDER BY g.created_at", (m['employee_id'],)).fetchall()
    cks = _latest_checkins(db, [g['id'] for g in goals])
    vis_sql, vis_args = _fb_visible(uid, role, dept_id, pc['public_praise'])
    cut90 = (date.today() - timedelta(days=90)).isoformat()
    feedback_rows = db.execute(FB_SELECT + f"WHERE f.to_id=? AND f.created_at>=? AND {vis_sql} ORDER BY f.id DESC LIMIT 6",
                               [m['employee_id'], cut90] + vis_args).fetchall()
    try:
        base_dt = datetime.strptime(m['scheduled_at'][:16], '%Y-%m-%d %H:%M')
    except ValueError:
        base_dt = datetime.now()
    next_default = (base_dt + timedelta(days=pc['cadence'])).strftime('%Y-%m-%dT%H:%M')
    return render_template('performance/one_on_one.html',
                           m=m, is_mgr=is_mgr, topics=topics, actions=actions, carry=carry, history=history,
                           private=private, goals=goals, cks=cks, feedback_rows=feedback_rows, pc=pc,
                           next_default=next_default, today_iso=date.today().isoformat(), when_value=m['scheduled_at'][:16].replace(' ', 'T'),
                           status_label=ONE_ON_ONE_STATUS_LABEL, ck_label=CHECKIN_STATUS_LABEL,
                           ck_cls=CHECKIN_STATUS_CLS, kind_label=FEEDBACK_KIND_LABEL,
                           active_page='one_on_ones')


@app.route('/performance/feedback')
@login_required
def feedback_home():
    db = get_db()
    uid, role = session['user_id'], session.get('user_role')
    dept_id = int(session.get('dept_id') or 0)
    pc = get_perf_culture()
    reports = _c2_reports(db, uid, role, dept_id)
    tabs = [('received', '받은 피드백'), ('sent', '보낸 피드백'), ('requests', '요청')]
    if pc['public_praise']:
        tabs.append(('praise', '칭찬 피드'))
    if reports or role == 'admin':
        tabs.append(('team', '전사' if role == 'admin' else '팀'))
    tab = request.args.get('tab', 'received')
    if tab not in dict(tabs):
        tab = 'received'
    vis_sql, vis_args = _fb_visible(uid, role, dept_id, pc['public_praise'])
    emp_filter = request.args.get('emp', type=int)
    filter_emp = None

    rows, incoming, outgoing = [], [], []
    if tab == 'received':
        rows = db.execute(FB_SELECT + "WHERE f.to_id=? AND f.visibility!='manager' ORDER BY f.id DESC LIMIT 100",
                          (uid,)).fetchall()
    elif tab == 'sent':
        rows = db.execute(FB_SELECT + "WHERE f.from_id=? ORDER BY f.id DESC LIMIT 100", (uid,)).fetchall()
    elif tab == 'praise':
        rows = db.execute(FB_SELECT + "WHERE f.visibility='public' AND f.kind='praise' ORDER BY f.id DESC LIMIT 60"
                          ).fetchall()
    elif tab == 'team':
        where, args = f"f.to_id != ? AND {vis_sql}", [uid] + vis_args
        if role != 'admin':
            ids = [r['id'] for r in reports] or [-1]
            where += f" AND f.to_id IN ({','.join('?' * len(ids))})"
            args += ids
        if emp_filter:
            filter_emp = db.execute('SELECT id, name, manager_id, department_id FROM users WHERE id=?',
                                    (emp_filter,)).fetchone()
            if not filter_emp or (role != 'admin' and not _c2_is_manager_of(filter_emp, uid, role, dept_id)):
                abort(403)
            where += ' AND f.to_id=?'
            args.append(emp_filter)
        rows = db.execute(FB_SELECT + f"WHERE {where} ORDER BY f.id DESC LIMIT 150", args).fetchall()
    else:
        incoming = db.execute(
            "SELECT fr.*, ru.name AS requester_name, su.name AS subject_name FROM feedback_requests fr "
            "JOIN users ru ON fr.requester_id=ru.id JOIN users su ON fr.subject_id=su.id "
            "WHERE fr.responder_id=? ORDER BY CASE fr.status WHEN 'pending' THEN 0 ELSE 1 END, fr.id DESC LIMIT 60",
            (uid,)).fetchall()
        outgoing = db.execute(
            "SELECT fr.*, pu.name AS responder_name, su.name AS subject_name, f.content AS answer "
            "FROM feedback_requests fr JOIN users pu ON fr.responder_id=pu.id JOIN users su ON fr.subject_id=su.id "
            "LEFT JOIN feedback f ON fr.feedback_id=f.id "
            "WHERE fr.requester_id=? ORDER BY fr.id DESC LIMIT 60", (uid,)).fetchall()

    cut90 = (date.today() - timedelta(days=90)).isoformat()
    sums = {
        'received': db.execute("SELECT COUNT(*) FROM feedback WHERE to_id=? AND visibility!='manager' AND created_at>=?",
                               (uid, cut90)).fetchone()[0],
        'sent': db.execute('SELECT COUNT(*) FROM feedback WHERE from_id=? AND created_at>=?', (uid, cut90)).fetchone()[0],
        'to_answer': db.execute("SELECT COUNT(*) FROM feedback_requests WHERE responder_id=? AND status='pending'",
                                (uid,)).fetchone()[0],
        'waiting': db.execute("SELECT COUNT(*) FROM feedback_requests WHERE requester_id=? AND status='pending'",
                              (uid,)).fetchone()[0],
    }
    people = db.execute("SELECT u.id, u.name, d.name AS dept_name FROM users u "
                        "LEFT JOIN departments d ON u.department_id=d.id "
                        "WHERE u.status='active' AND u.id != ? AND u.role != 'guest' ORDER BY u.name", (uid,)).fetchall()
    return render_template('performance/feedback.html',
                           tab=tab, tabs=tabs, rows=rows, incoming=incoming, outgoing=outgoing, sums=sums,
                           people=people, reports=reports, pc=pc, filter_emp=filter_emp,
                           pre_to=request.args.get('to', type=int), pre_subject=request.args.get('subject', type=int),
                           kind_label=FEEDBACK_KIND_LABEL, active_page='feedback')


@app.route('/performance/feedback/give', methods=['POST'])
@login_required
def feedback_give():
    db = get_db()
    uid = session['user_id']
    pc = get_perf_culture()
    to_id = request.form.get('to_id', type=int)
    kind = request.form.get('kind') if request.form.get('kind') in ('praise', 'suggest') else 'praise'
    content = (request.form.get('content') or '').strip()[:2000]
    back = _safe_next(url_for('feedback_home', tab='sent'))
    target = db.execute("SELECT id, name FROM users WHERE id=? AND status='active'", (to_id,)).fetchone()
    if not target or target['id'] == uid:
        flash('받는 사람을 선택하세요.', 'error')
        return redirect(back)
    if len(content) < 2:
        flash('피드백 내용을 입력하세요.', 'error')
        return redirect(back)
    vis = 'public' if (kind == 'praise' and pc['public_praise'] and request.form.get('public')) else 'private'
    db.execute('INSERT INTO feedback (from_id, to_id, kind, content, visibility) VALUES (?,?,?,?,?)',
               (uid, to_id, kind, content, vis))
    db.commit()
    add_notification(to_id, 'info', 'perf', f"피드백 수신 · {FEEDBACK_KIND_LABEL[kind]}",
                     f"{session.get('user_name')} · {content[:60]}", url_for('feedback_home', tab='received'))
    flash(f"{target['name']}에게 {FEEDBACK_KIND_LABEL[kind]} 전달" + (' · 칭찬 피드 공개' if vis == 'public' else ''),
          'success')
    return redirect(back)


@app.route('/performance/feedback/request', methods=['POST'])
@login_required
def feedback_request_new():
    db = get_db()
    uid, role = session['user_id'], session.get('user_role')
    dept_id = int(session.get('dept_id') or 0)
    back = _safe_next(url_for('feedback_home', tab='requests'))
    subject_id = request.form.get('subject_id', type=int) or uid
    responder_id = request.form.get('responder_id', type=int)
    question = (request.form.get('question') or '').strip()[:500]
    due = (request.form.get('due_date') or '')[:10] or None
    if subject_id != uid:
        subj = db.execute('SELECT id, manager_id, department_id FROM users WHERE id=?', (subject_id,)).fetchone()
        if not _c2_is_manager_of(subj, uid, role, dept_id):
            abort(403)
    responder = db.execute("SELECT id, name FROM users WHERE id=? AND status='active'", (responder_id,)).fetchone()
    if not responder or responder['id'] == uid:
        flash('의견을 줄 사람을 선택하세요.', 'error')
        return redirect(back)
    if len(question) < 2:
        flash('요청 내용을 입력하세요.', 'error')
        return redirect(back)
    dup = db.execute("SELECT id FROM feedback_requests WHERE requester_id=? AND subject_id=? AND responder_id=? "
                     "AND status='pending'", (uid, subject_id, responder_id)).fetchone()
    if dup:
        flash('같은 사람에게 보낸 응답 대기 요청이 있습니다.', 'warning')
        return redirect(back)
    db.execute('INSERT INTO feedback_requests (requester_id, subject_id, responder_id, question, due_date) '
               'VALUES (?,?,?,?,?)', (uid, subject_id, responder_id, question, due))
    db.commit()
    subj_name = '본인' if subject_id == uid else db.execute('SELECT name FROM users WHERE id=?',
                                                              (subject_id,)).fetchone()['name']
    add_notification(responder_id, 'action', 'perf', '피드백 요청',
                     f"{session.get('user_name')} · 대상 {subj_name}" + (f" · 기한 {due}" if due else ''),
                     url_for('feedback_home', tab='requests'))
    flash(f"{responder['name']}에게 피드백 요청", 'success')
    return redirect(back)


@app.route('/performance/feedback/requests/<int:rid>', methods=['POST'])
@login_required
def feedback_request_respond(rid):
    db = get_db()
    uid = session['user_id']
    r = db.execute('SELECT * FROM feedback_requests WHERE id=?', (rid,)).fetchone()
    if not r:
        abort(404)
    if r['responder_id'] != uid:
        abort(403)
    if r['status'] != 'pending':
        flash('이미 처리된 요청입니다.', 'warning')
        return redirect(url_for('feedback_home', tab='requests'))
    if request.form.get('action') == 'decline':
        db.execute("UPDATE feedback_requests SET status='declined' WHERE id=?", (rid,))
        db.commit()
        add_notification(r['requester_id'], 'info', 'perf', '피드백 요청 거절', session.get('user_name'),
                         url_for('feedback_home', tab='requests'))
        return redirect(url_for('feedback_home', tab='requests'))
    content = (request.form.get('content') or '').strip()[:2000]
    if len(content) < 2:
        flash('응답 내용을 입력하세요.', 'error')
        return redirect(url_for('feedback_home', tab='requests'))
    # 본인이 요청한 피드백은 본인이 보고, 매니저가 팀원에 대해 요청한 의견은 매니저만 본다
    vis = 'private' if r['requester_id'] == r['subject_id'] else 'manager'
    fid = db.execute("INSERT INTO feedback (from_id, to_id, kind, content, visibility, request_id) "
                     "VALUES (?,?,'response',?,?,?)", (uid, r['subject_id'], content, vis, rid)).lastrowid
    db.execute("UPDATE feedback_requests SET status='done', feedback_id=? WHERE id=?", (fid, rid))
    db.commit()
    add_notification(r['requester_id'], 'info', 'perf', '피드백 요청 응답', f"{session.get('user_name')} · {content[:60]}",
                     url_for('feedback_home', tab='requests'))
    flash('응답 전달', 'success')
    return redirect(url_for('feedback_home', tab='requests'))


# ── Performance: Progress & Self-Review ─────────────────────
@app.route('/performance/goals/<int:goal_id>/progress', methods=['POST'])
@login_required
def performance_goal_progress(goal_id):
    db   = get_db()
    uid  = session['user_id']
    goal = db.execute(
        'SELECT g.user_id, g.cycle_id, g.title, c.stage FROM performance_goals g '
        'JOIN performance_cycles c ON g.cycle_id=c.id WHERE g.id=?', (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)
    if goal['user_id'] != uid:
        abort(403)
    if goal['stage'] not in ('goal', 'progress', 'review'):
        flash('평가 조정이 시작된 이후에는 진행률을 수정할 수 없습니다.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))
    try:
        progress = max(0, min(100, int(request.form.get('progress', 0))))
    except (ValueError, TypeError):
        progress = 0
    status = request.form.get('status', 'on_track')
    status = status if status in CHECKIN_STATUS_LABEL else 'on_track'
    comment = (request.form.get('comment') or '').strip()[:500] or None
    db.execute('UPDATE performance_goals SET progress=? WHERE id=?', (progress, goal_id))
    db.execute('INSERT INTO goal_checkins (goal_id, user_id, progress, status, comment) VALUES (?,?,?,?,?)',
               (goal_id, uid, progress, status, comment))
    db.commit()
    if status != 'on_track':
        mgr = db.execute('SELECT manager_id FROM users WHERE id=?', (uid,)).fetchone()
        if mgr and mgr['manager_id']:
            add_notification(mgr['manager_id'], 'action', 'perf',
                             f"목표 체크인 {CHECKIN_STATUS_LABEL[status]} · {session.get('user_name')}",
                             f"{goal['title'][:40]} · {progress}%" + (f" · {comment[:40]}" if comment else ''),
                             url_for('performance_team_member', emp_uid=uid, cycle=goal['cycle_id']))
    flash(f"체크인 기록 · {progress}% · {CHECKIN_STATUS_LABEL[status]}", 'success')
    return redirect(url_for('performance', cycle=goal['cycle_id']))


@app.route('/performance/goals/<int:goal_id>/self-review', methods=['GET', 'POST'])
@login_required
def performance_self_review(goal_id):
    db   = get_db()
    uid  = session['user_id']
    goal = db.execute(
        'SELECT g.*, c.name AS cycle_name, c.stage AS cycle_stage FROM performance_goals g '
        'JOIN performance_cycles c ON g.cycle_id = c.id WHERE g.id=?',
        (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)
    if goal['user_id'] != uid:
        abort(403)
    if goal['cycle_stage'] != 'review':
        flash('자기평가는 "평가 진행" 단계에서만 작성할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))
    if goal['approval_status'] != 'confirmed':
        flash('팀장이 목표를 확정한 후에 자기평가를 작성할 수 있습니다.', 'error')
        return redirect(url_for('performance', cycle=goal['cycle_id']))
    error = None
    if request.method == 'POST':
        try:
            score = int(request.form.get('self_score', 0))
        except (ValueError, TypeError):
            score = 0
        comment = request.form.get('self_comment', '').strip() or None
        if not (1 <= score <= 5):
            error = '자기평가 점수는 1~5점 사이여야 합니다.'
        else:
            db.execute(
                'UPDATE performance_goals SET self_score=?, self_comment=? WHERE id=?',
                (score, comment, goal_id)
            )
            db.commit()
            return redirect(url_for('performance'))
    return render_template('performance/self_review.html',
                           goal=goal, error=error, score_labels=SCORE_LABELS,
                           active_page='performance')


# ── Attendance (통합) ────────────────────────────────────────
@app.route('/attendance/home')
@login_required
def attendance_home():
    import calendar as cal_mod
    import json as _json
    from datetime import date, timedelta

    db    = get_db()
    uid   = session['user_id']
    role  = session.get('user_role', 'employee')
    today = date.today()

    # ── 공통: 연차 계산 (단일 소스) ──────────────────────────────
    _bal = get_leave_balance(db, uid)
    total_leave = _bal['total']
    used_leave  = _bal['used']

    # ── TAB: 홈 ──────────────────────────────────────────────────
    checkin = db.execute(
        'SELECT * FROM checkins WHERE user_id=? AND date=?',
        (uid, today.isoformat())
    ).fetchone()

    # 미체크아웃 경고 (전날 체크인만 있는 경우)
    yesterday = (today - timedelta(days=1)).isoformat()
    unclosed  = db.execute(
        "SELECT * FROM checkins WHERE user_id=? AND date=? "
        "AND check_in IS NOT NULL AND (check_out IS NULL OR check_out='')",
        (uid, yesterday)
    ).fetchone()

    first_day = today.replace(day=1)
    last_day  = date(today.year + (today.month // 12), (today.month % 12) + 1, 1) - timedelta(days=1)

    checkins_month = db.execute(
        'SELECT * FROM checkins WHERE user_id=? AND date>=? AND date<=? ORDER BY date',
        (uid, first_day.isoformat(), last_day.isoformat())
    ).fetchall()

    holiday_rows   = db.execute(
        'SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?',
        (first_day.isoformat(), last_day.isoformat())
    ).fetchall()
    month_holidays = {h['date'] for h in holiday_rows}

    base_row    = db.execute(
        'SELECT base_salary FROM employee_salary WHERE user_id=? ORDER BY updated_at DESC LIMIT 1',
        (uid,)
    ).fetchone()
    base_salary = base_row['base_salary'] if base_row else 0

    month_regular_min = month_overtime_min = month_night_min = total_extra_pay_amount = 0
    for c in checkins_month:
        month_regular_min  += c['regular_min']
        month_overtime_min += c['overtime_min']
        month_night_min    += c['night_min']
        is_h = c['date'] in month_holidays
        res  = calc_extra_pay(c['overtime_min'], c['night_min'], base_salary,
                              is_holiday=is_h,
                              holiday_regular_min=c['regular_min'] if is_h else 0)
        total_extra_pay_amount += res['total_extra_pay']

    extra_pay    = {'total_extra_pay': total_extra_pay_amount}
    weekly_hours = calc_weekly_hours(db, uid, today.isoformat())

    # ── TAB: 휴가 ────────────────────────────────────────────────
    all_requests = db.execute(
        'SELECT r.*, u.name AS approver_name '
        'FROM leave_requests r '
        'LEFT JOIN users u ON r.approver_id = u.id '
        'WHERE r.user_id=? ORDER BY r.created_at DESC',
        (uid,)
    ).fetchall()

    annual_remain = round(total_leave - float(used_leave), 1)
    pending_leave = db.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE user_id=? AND status='pending'",
        (uid,)
    ).fetchone()[0]

    year         = today.year
    special_used = {}
    for lt in ('maternity','paternity','parental','family_care',
               'bereavement','military','compensation'):
        row = db.execute(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE user_id=? AND type=? AND status!='cancelled' "
            "AND strftime('%Y',start_date)=?",
            (uid, lt, str(year))
        ).fetchone()
        special_used[lt] = row[0]

    # ── TAB: 캘린더 ──────────────────────────────────────────────
    raw_month = request.args.get('month', today.strftime('%Y-%m'))
    try:
        cal_y, cal_m = int(raw_month[:4]), int(raw_month[5:7])
        if not (1 <= cal_m <= 12): raise ValueError
    except (ValueError, IndexError):
        cal_y, cal_m = today.year, today.month

    prev_m = date(cal_y, cal_m, 1) - timedelta(days=1)
    next_m = date(cal_y, cal_m, cal_mod.monthrange(cal_y, cal_m)[1]) + timedelta(days=1)

    # 해당 월 휴가 이벤트 — 본인 + 같은 부서 팀원
    CAL_COLOR = {
        'annual':  ('#dbeafe','#1e40af'), 'half_am': ('#dbeafe','#1e40af'),
        'half_pm': ('#dbeafe','#1e40af'), 'sick':    ('#ffedd5','#c2410c'),
        'outing':  ('#f5f3ff','#7c3aed'), 'maternity':('#fce7f3','#9d174d'),
        'parental':('#f0fdf4','#166534'), 'paternity':('#ede9fe','#6d28d9'),
    }
    my_dept = db.execute('SELECT department_id FROM users WHERE id=?', (uid,)).fetchone()
    dept_id_for_cal = my_dept['department_id'] if my_dept else None

    cal_month_start = date(cal_y, cal_m, 1).isoformat()
    cal_month_end   = date(cal_y, cal_m, cal_mod.monthrange(cal_y, cal_m)[1]).isoformat()

    if dept_id_for_cal:
        cal_reqs = db.execute(
            "SELECT r.*, u.name AS user_name FROM leave_requests r "
            "JOIN users u ON r.user_id=u.id "
            "WHERE r.status='approved' "
            "  AND r.start_date <= ? AND r.end_date >= ? "
            "  AND (u.department_id=? OR r.user_id=?) "
            "ORDER BY r.start_date",
            (cal_month_end, cal_month_start, dept_id_for_cal, uid)
        ).fetchall()
    else:
        cal_reqs = db.execute(
            "SELECT r.*, u.name AS user_name FROM leave_requests r "
            "JOIN users u ON r.user_id=u.id "
            "WHERE r.status='approved' AND r.user_id=? "
            "  AND r.start_date <= ? AND r.end_date >= ? "
            "ORDER BY r.start_date",
            (uid, cal_month_end, cal_month_start)
        ).fetchall()

    events_by_date = {}
    for r in cal_reqs:
        try:
            sd = date.fromisoformat(r['start_date'])
            ed = date.fromisoformat(r['end_date'])
        except ValueError:
            continue
        cur = sd
        while cur <= ed:
            k = cur.isoformat()
            bg, tc = CAL_COLOR.get(r['type'], ('#f1f5f9','#475569'))
            is_mine = (r['user_id'] == uid)
            events_by_date.setdefault(k, []).append({
                'name':       r['user_name'],
                'type':       LEAVE_LABELS.get(r['type'], r['type']),
                'color':      bg if is_mine else '#f3e8ff',
                'text_color': tc if is_mine else '#7c3aed',
                'is_mine':    is_mine,
            })
            cur += timedelta(days=1)

    # 오늘 부재 목록
    today_absent = []
    for r in cal_reqs:
        try:
            if r['start_date'] <= today.isoformat() <= r['end_date']:
                today_absent.append({
                    'name': r['user_name'],
                    'type': LEAVE_LABELS.get(r['type'], r['type']),
                    'is_mine': r['user_id'] == uid,
                })
        except Exception:
            pass

    # 이달 팀 부재 일정 (오늘 이후)
    upcoming_absent = []
    seen = set()
    for r in cal_reqs:
        key_ua = (r['user_id'], r['start_date'])
        if key_ua in seen:
            continue
        seen.add(key_ua)
        upcoming_absent.append({
            'name': r['user_name'],
            'type': LEAVE_LABELS.get(r['type'], r['type']),
            'start': r['start_date'],
            'end':   r['end_date'],
            'is_mine': r['user_id'] == uid,
        })

    # 해당 월 체크인 날짜 집합
    cal_checkins = {r['date'] for r in db.execute(
        'SELECT date FROM checkins WHERE user_id=? AND date BETWEEN ? AND ? AND check_in IS NOT NULL',
        (uid, cal_month_start, cal_month_end)
    ).fetchall()}

    holiday_dates = {h['date'] for h in holiday_rows}
    first_cell = date(cal_y, cal_m, 1)
    offset     = (first_cell.weekday() + 1) % 7   # 일요일 시작
    start_cell = first_cell - timedelta(days=offset)
    calendar_cells = []
    cur = start_cell
    for _ in range(42):
        calendar_cells.append({
            'date':          cur.isoformat(),
            'day':           cur.day,
            'current_month': cur.month == cal_m,
            'is_today':      cur == today,
            'is_holiday':    cur.isoformat() in holiday_dates,
            'worked':        cur.isoformat() in cal_checkins,
            'events':        events_by_date.get(cur.isoformat(), [])[:3],
        })
        cur += timedelta(days=1)

    # ── TAB: 승인 (매니저/Admin) ─────────────────────────────────
    approval_reqs   = []
    reviewed_reqs   = []
    pending_count   = 0
    reviewed_count  = 0
    depts           = []

    if role in ('admin', 'manager'):
        depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
        dept_f = request.args.get('dept', '')
        apv_status = request.args.get('apv_status', 'pending')

        sql = (
            'SELECT r.*, u.name AS user_name, u.department_id, '
            'u.manager_id AS user_manager_id, '
            'd.name AS dept_name, p.name AS pos_name '
            'FROM leave_requests r '
            'JOIN users u ON r.user_id=u.id '
            'LEFT JOIN departments d ON u.department_id=d.id '
            'LEFT JOIN positions   p ON u.position_id=p.id '
            'WHERE r.status=?'
        )
        params = [apv_status]
        if dept_f:
            sql += ' AND u.department_id=?'; params.append(dept_f)
        if role == 'manager':
            mgr_dept = session.get('dept_id') or 0
            cur_uid  = session.get('user_id')
            # 같은 부서 직원 OR 직속 부하 매니저(manager_id=나) 모두 표시
            sql += ' AND (u.department_id=? OR u.manager_id=?)'
            params += [mgr_dept, cur_uid]
        sql += ' ORDER BY r.created_at DESC'

        approval_reqs  = db.execute(sql, params).fetchall()
        pending_count  = db.execute("SELECT COUNT(*) FROM leave_requests WHERE status='pending'").fetchone()[0]
        reviewed_count = db.execute("SELECT COUNT(*) FROM leave_requests WHERE status='reviewed'").fetchone()[0]

    # ── OT 신청 목록 ──────────────────────────────────────────────
    my_ot_requests = db.execute(
        'SELECT o.*, u.name AS approver_name '
        'FROM overtime_requests o '
        'LEFT JOIN users u ON o.approver_id=u.id '
        'WHERE o.user_id=? ORDER BY o.date DESC LIMIT 20',
        (uid,)
    ).fetchall()

    # 매니저/Admin: 팀 OT 승인 대기
    ot_pending_list = []
    if role in ('admin', 'manager'):
        ot_sql = (
            'SELECT o.*, u.name AS user_name, d.name AS dept_name '
            'FROM overtime_requests o '
            'JOIN users u ON o.user_id=u.id '
            'LEFT JOIN departments d ON u.department_id=d.id '
        )
        ot_params = []
        if role == 'manager':
            # 매니저는 본인 검토 몫(pending)만 — 검토 완료(reviewed) 건은 HR 대기 상태
            mgr_dept = session.get('dept_id') or 0
            cur_uid  = session.get('user_id')
            ot_sql  += "WHERE o.status='pending' AND (u.department_id=? OR u.manager_id=?)"
            ot_params += [mgr_dept, cur_uid]
        else:
            # HR(admin)은 매니저 검토 완료 건까지 함께 확인
            ot_sql += "WHERE o.status IN ('pending','reviewed')"
        ot_sql += ' ORDER BY o.date DESC'
        ot_pending_list = db.execute(ot_sql, ot_params).fetchall()

    # ── 개인 월간 리포트 (전월 vs 이번 달) ───────────────────────
    def _month_stats(y, m):
        import calendar as _cal
        fd = date(y, m, 1).isoformat()
        ld = date(y, m, _cal.monthrange(y, m)[1]).isoformat()
        rows = db.execute(
            'SELECT regular_min, overtime_min, night_min, check_in '
            'FROM checkins WHERE user_id=? AND date>=? AND date<=? AND check_in IS NOT NULL',
            (uid, fd, ld)
        ).fetchall()
        work_days    = len(rows)
        total_reg    = sum(r['regular_min']  for r in rows)
        total_ot     = sum(r['overtime_min'] for r in rows)
        total_night  = sum(r['night_min']    for r in rows)
        return dict(work_days=work_days, total_reg=total_reg,
                    total_ot=total_ot, total_night=total_night)

    this_stats = _month_stats(today.year, today.month)
    prev_month_d = (today.replace(day=1) - timedelta(days=1))
    prev_stats   = _month_stats(prev_month_d.year, prev_month_d.month)

    def _diff(cur, prv):
        return cur - prv

    monthly_report = dict(
        this_month=f'{today.year}년 {today.month}월',
        work_days=this_stats['work_days'],
        work_days_diff=_diff(this_stats['work_days'], prev_stats['work_days']),
        total_hours=round(this_stats['total_reg'] / 60, 1),
        total_hours_diff=round(_diff(this_stats['total_reg'], prev_stats['total_reg']) / 60, 1),
        ot_hours=round(this_stats['total_ot'] / 60, 1),
        ot_hours_diff=round(_diff(this_stats['total_ot'], prev_stats['total_ot']) / 60, 1),
        night_hours=round(this_stats['total_night'] / 60, 1),
        night_hours_diff=round(_diff(this_stats['total_night'], prev_stats['total_night']) / 60, 1),
    )

    # ── 최소 11시간 휴식 미준수 감지 ────────────────────────────
    min_rest_violations = []
    recent_days = db.execute(
        'SELECT date, check_in, check_out FROM checkins '
        'WHERE user_id=? AND date>=? AND check_in IS NOT NULL AND check_out IS NOT NULL '
        'ORDER BY date DESC LIMIT 14',
        (uid, (today - timedelta(days=14)).isoformat())
    ).fetchall()
    for i in range(len(recent_days) - 1):
        try:
            prev_co = recent_days[i+1]['check_out']
            curr_ci = recent_days[i]['check_in']
            if prev_co and curr_ci:
                from datetime import datetime as _dt
                t1 = _dt.fromisoformat(prev_co)
                t2 = _dt.fromisoformat(curr_ci)
                gap_hours = (t2 - t1).total_seconds() / 3600
                if gap_hours < 11:
                    min_rest_violations.append({
                        'date': recent_days[i]['date'],
                        'gap_hours': round(gap_hours, 1),
                    })
        except Exception:
            pass

    active_tab = request.args.get('tab', 'home')

    return render_template('attendance/home.html',
        # 공통
        today=today, labels=LEAVE_LABELS,
        total_leave=total_leave, used_leave=float(used_leave),
        remain_leave=total_leave - float(used_leave),
        annual_remain=annual_remain,
        # 홈 탭
        checkin=checkin, unclosed=unclosed,
        checkins_month=checkins_month,
        month_regular_min=month_regular_min,
        month_overtime_min=month_overtime_min,
        month_night_min=month_night_min,
        extra_pay=extra_pay,
        weekly_hours=weekly_hours,
        monthly_report=monthly_report,
        min_rest_violations=min_rest_violations,
        # OT 탭
        my_ot_requests=my_ot_requests,
        ot_pending_list=ot_pending_list,
        # 휴가 탭
        all_requests=all_requests, leave_meta=LEAVE_META,
        leave_meta_json=_json.dumps({k: {
            'label': v.get('label',''), 'icon': v.get('icon','fa-calendar'),
            'approval_flow': v.get('approval_flow','manager_only'),
            'approval_hr_threshold': v.get('approval_hr_threshold', None),
            'law': v.get('law',''), 'pay_info': v.get('pay_info',''),
            'desc': v.get('desc',''), 'requires_docs': v.get('requires_docs', False),
            'docs_note': v.get('docs_note',''), 'deduct': v.get('deduct','none'),
            'max_days': v.get('max_days', None), 'fixed_days': v.get('fixed_days', None),
        } for k, v in LEAVE_META.items() if k not in ('remote','outing')}),
        pending_leave=pending_leave,
        special_used=_json.dumps(special_used),
        # 캘린더 탭
        calendar_cells=calendar_cells,
        cal_year=cal_y, cal_month=cal_m,
        prev_month=prev_m.strftime('%Y-%m'),
        next_month=next_m.strftime('%Y-%m'),
        today_absent=today_absent,
        upcoming_absent=upcoming_absent,
        # 승인 탭
        approval_reqs=approval_reqs,
        reviewed_reqs=reviewed_reqs,
        pending_count=pending_count,
        reviewed_count=reviewed_count,
        depts=depts,
        # 탭 상태
        active_tab=active_tab,
        active_page='attendance_home'
    )


@app.route('/attendance/overtime/new', methods=['POST'])
@login_required
def overtime_new():
    """연장근무 사전/사후 신청."""
    from datetime import datetime as _dt, date as _date
    db   = get_db()
    uid  = session['user_id']
    d    = request.form
    date_val  = d.get('date','').strip()
    ot_start  = d.get('ot_start','').strip()
    ot_end    = d.get('ot_end','').strip()
    reason    = d.get('reason','').strip()
    req_type  = d.get('request_type','pre')

    if not date_val or not ot_start or not ot_end:
        flash('날짜와 시간을 모두 입력해주세요.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))

    try:
        t1   = _dt.fromisoformat(f'{date_val}T{ot_start}')
        t2   = _dt.fromisoformat(f'{date_val}T{ot_end}')
        now  = _dt.now()
        today_str = _date.today().isoformat()

        if t2 <= t1:
            flash('종료 시간이 시작 시간보다 늦어야 합니다.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))

        if req_type == 'pre':
            # 사전 승인: 시작 시간이 현재 시각 이후여야 함
            if t1 <= now:
                flash('사전 승인은 현재 시각 이후의 시간만 신청 가능합니다.', 'error')
                return redirect(url_for('attendance_home', tab='ot'))
        else:
            # 사후 신고: 종료 시간이 현재 시각 이전이어야 함
            if t2 > now:
                flash('사후 신고는 이미 종료된 시간에 대해서만 신청 가능합니다.', 'error')
                return redirect(url_for('attendance_home', tab='ot'))

        ot_minutes = int((t2 - t1).total_seconds() / 60)
    except ValueError:
        flash('시간 형식이 올바르지 않습니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))

    db.execute(
        'INSERT INTO overtime_requests (user_id, date, ot_start, ot_end, ot_minutes, reason, request_type) '
        'VALUES (?,?,?,?,?,?,?)',
        (uid, date_val, ot_start, ot_end, ot_minutes, reason, req_type)
    )
    db.commit()
    flash('연장근로 신청이 접수되었습니다.', 'success')
    return redirect(url_for('attendance_home', tab='ot'))


@app.route('/attendance/overtime/<int:ot_id>/approve', methods=['POST'])
@manager_or_admin
def overtime_approve(ot_id):
    db   = get_db()
    uid  = session.get('user_id')
    role = session.get('user_role')
    row = db.execute(
        'SELECT o.*, u.department_id, u.manager_id AS user_manager_id, u.name AS user_name '
        'FROM overtime_requests o JOIN users u ON o.user_id = u.id WHERE o.id=?', (ot_id,)
    ).fetchone()
    if not row:
        abort(404)

    _chain = get_approval_chain(db, 'overtime')
    approval_flow = _chain if _chain in ('manager_only', 'manager_hr') else 'manager_only'

    # 매니저 승인 단계
    if role == 'manager':
        if row['status'] != 'pending':
            flash('매니저 검토가 불가능한 상태입니다.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))
        if row['user_id'] == uid:
            flash('본인의 신청은 직접 승인할 수 없습니다.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))
        # 권한 검증: 같은 부서원 OR 직속 부하
        mgr_dept = session.get('dept_id') or 0
        is_direct_report = (row['user_manager_id'] == uid)
        same_dept = (mgr_dept != 0 and row['department_id'] == mgr_dept)
        if not is_direct_report and not same_dept:
            abort(403)

        if approval_flow == 'manager_only':
            db.execute(
                "UPDATE overtime_requests SET status='approved', approver_id=?, approved_at=CURRENT_TIMESTAMP, "
                "manager_id=?, manager_approved_at=CURRENT_TIMESTAMP WHERE id=?",
                (uid, uid, ot_id)
            )
            db.commit()
            add_notification(
                row['user_id'], 'info', 'overtime', 'OT 신청 승인',
                f'{row["date"]} OT 신청({row["ot_minutes"]}분)이 승인되었습니다.',
                url_for('attendance_home', tab='ot')
            )
            flash('OT 신청을 승인했습니다.', 'success')
        else:
            # manager_hr: 검토 완료 → HR 대기
            db.execute(
                "UPDATE overtime_requests SET status='reviewed', manager_id=?, manager_approved_at=CURRENT_TIMESTAMP WHERE id=?",
                (uid, ot_id)
            )
            db.commit()
            admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
            for admin in admins:
                add_notification(
                    admin['id'], 'action', 'overtime',
                    f"[HR 최종 승인 필요] 연장근로 — {row['user_name']}",
                    '매니저 검토가 완료됐습니다. HR 최종 승인이 필요합니다.',
                    url_for('approvals_hub')
                )
            flash('매니저 검토 완료. HR 최종 승인 대기 중입니다.', 'success')

    # HR(Admin) 최종 승인 단계
    elif role == 'admin':
        if approval_flow == 'manager_hr' and row['status'] == 'pending':
            flash('이 설정은 매니저 검토가 먼저 완료되어야 합니다. 담당 매니저에게 먼저 검토를 요청하세요.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))
        if row['status'] not in ('pending', 'reviewed'):
            flash('처리할 수 없는 신청입니다.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))
        db.execute(
            "UPDATE overtime_requests SET status='approved', hr_id=?, hr_approved_at=CURRENT_TIMESTAMP, "
            "approver_id=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, uid, ot_id)
        )
        db.commit()
        add_notification(
            row['user_id'], 'info', 'overtime', 'OT 신청 최종 승인',
            f'{row["date"]} OT 신청({row["ot_minutes"]}분)이 HR 최종 승인되었습니다.',
            url_for('attendance_home', tab='ot')
        )
        flash('HR 최종 승인이 완료됐습니다.', 'success')

    return redirect(url_for('attendance_home', tab='ot'))


@app.route('/attendance/overtime/<int:ot_id>/reject', methods=['POST'])
@manager_or_admin
def overtime_reject(ot_id):
    db   = get_db()
    uid  = session.get('user_id')
    role = session.get('user_role')
    row = db.execute(
        'SELECT o.*, u.department_id, u.manager_id AS user_manager_id '
        'FROM overtime_requests o JOIN users u ON o.user_id = u.id WHERE o.id=?', (ot_id,)
    ).fetchone()
    if not row:
        abort(404)
    if row['status'] not in ('pending', 'reviewed'):
        flash('처리할 수 없는 신청입니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))

    if role == 'manager':
        if row['user_id'] == uid:
            flash('본인의 신청은 직접 반려할 수 없습니다.', 'error')
            return redirect(url_for('attendance_home', tab='ot'))
        mgr_dept = session.get('dept_id') or 0
        is_direct_report = (row['user_manager_id'] == uid)
        same_dept = (mgr_dept != 0 and row['department_id'] == mgr_dept)
        if not is_direct_report and not same_dept:
            abort(403)

    reason = request.form.get('reject_reason', '')
    db.execute(
        "UPDATE overtime_requests SET status='rejected', approver_id=?, approved_at=CURRENT_TIMESTAMP, "
        "reject_reason=? WHERE id=?",
        (uid, reason, ot_id)
    )
    db.commit()
    add_notification(
        row['user_id'], 'info', 'overtime', 'OT 신청 반려',
        f'{row["date"]} OT 신청이 반려되었습니다. 사유: {reason}',
        url_for('attendance_home', tab='ot')
    )
    flash('OT 신청을 반려했습니다.', 'success')
    return redirect(url_for('attendance_home', tab='ot'))


@app.route('/attendance/leave-carryover', methods=['POST'])
@login_required
def leave_carryover():
    """연차 이월 계산 — Admin 전용. 전년도 잔여연차를 이번 연도로 이월."""
    if session.get('user_role') != 'admin':
        flash('관리자만 실행할 수 있습니다.', 'error')
        return redirect(url_for('attendance_home'))
    from datetime import date
    db   = get_db()
    year = date.today().year
    carry_max = 10  # 이월 최대 일수

    cfg = db.execute('SELECT carry_over_max FROM company_config WHERE id=1').fetchone()
    if cfg and cfg['carry_over_max']:
        carry_max = cfg['carry_over_max']

    employees = db.execute(
        "SELECT id, hire_date FROM users WHERE role != 'guest' AND (termination_date IS NULL OR termination_date='')"
    ).fetchall()

    processed = 0
    for emp in employees:
        # 전년도 잔여 = 단일 소스 계산 (전년 이월분·병가 정책 포함)
        prev = get_leave_balance(db, emp['id'], year=year - 1)
        carry_amt = min(max(0, prev['remaining']), carry_max)

        db.execute(
            'INSERT INTO leave_balances (user_id, year, total_days, used_days, carry_over_days, carry_over_max) '
            'VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(user_id, year) DO UPDATE SET '
            '  total_days=excluded.total_days, used_days=excluded.used_days, '
            '  carry_over_days=excluded.carry_over_days, updated_at=CURRENT_TIMESTAMP',
            (emp['id'], year, prev['base'] + carry_amt, prev['used'], carry_amt, carry_max)
        )
        processed += 1

    db.commit()
    flash(f'{processed}명 연차 이월 처리 완료 (최대 {carry_max}일)', 'success')
    return redirect(url_for('attendance_home'))


@app.route('/attendance/remote', methods=['POST'])
@login_required
def attendance_remote():
    """재택근무 토글 — 오늘 체크인 레코드의 is_remote를 반전."""
    from datetime import date
    db    = get_db()
    uid   = session['user_id']
    today = date.today().isoformat()

    row = db.execute(
        'SELECT id, is_remote FROM checkins WHERE user_id=? AND date=?', (uid, today)
    ).fetchone()

    if row:
        new_val = 0 if row['is_remote'] else 1
        db.execute('UPDATE checkins SET is_remote=? WHERE id=?', (new_val, row['id']))
    else:
        # 체크인 없어도 재택 표시만 등록 (check_in 없는 레코드)
        db.execute(
            'INSERT INTO checkins (user_id, date, is_remote) VALUES (?,?,1) '
            'ON CONFLICT(user_id, date) DO UPDATE SET is_remote=1',
            (uid, today)
        )
    db.commit()
    return redirect(url_for('attendance_home'))


WORK_TYPES = {
    'standard':   '일반근무',
    'flex':       '선택근로제 (§52)',
    'elastic':    '탄력근로제 (§51)',
    'autonomous': '재량근로제 (§58)',
}

BLOCK_TYPES = {
    'office': '오피스 근무',
    'remote': '재택 근무',
    'lunch':  '점심시간',
}


def _week_monday(d):
    """주어진 date의 해당 주 월요일 반환"""
    from datetime import timedelta
    return d - timedelta(days=d.weekday())


@app.route('/attendance/checkin', methods=['POST'])
@login_required
def do_checkin():
    from datetime import date, datetime
    db    = get_db()
    uid   = session['user_id']
    today = date.today().isoformat()
    now   = datetime.now().strftime('%H:%M')

    schedule = get_user_schedule(db, uid, today)
    status   = judge_attendance(now, schedule)
    sched_id = schedule['id'] if schedule else None

    db.execute(
        'INSERT INTO checkins (user_id, date, check_in, attendance_status, schedule_id) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(user_id, date) DO UPDATE SET check_in=excluded.check_in, '
        'attendance_status=excluded.attendance_status, schedule_id=excluded.schedule_id',
        (uid, today, now, status, sched_id)
    )
    db.commit()

    if status == 'late':
        sched_name = schedule.get('name', '') if schedule else ''
        work_start = schedule.get('work_start', '') if schedule else ''
        flash(f'지각 처리됐습니다. (기준 출근 시각: {work_start})', 'warning')

    return redirect(url_for('attendance_home'))


@app.route('/attendance/checkout', methods=['POST'])
@login_required
def do_checkout():
    from datetime import date, datetime
    db    = get_db()
    uid   = session['user_id']
    today = date.today().isoformat()
    now   = datetime.now().strftime('%H:%M')
    row = db.execute(
        'SELECT id, check_in FROM checkins WHERE user_id=? AND date=?', (uid, today)
    ).fetchone()
    if row:
        check_in_time = row['check_in'] or '09:00'
        # 체크인과 동일 시각이면 0분 처리 (잘못된 버튼 클릭 방지)
        if check_in_time == now:
            flash('체크인과 동일한 시각입니다. 퇴근 시 다시 눌러주세요.', 'warning')
            return redirect(url_for('attendance_home'))

        hrs = calc_day_hours(today, check_in_time, now)
        is_holiday = bool(db.execute(
            'SELECT 1 FROM public_holidays WHERE date=?', (today,)
        ).fetchone())
        holiday_min = hrs['regular_min'] + hrs['overtime_min'] if is_holiday else 0
        schedule     = get_user_schedule(db, uid, today)
        early        = judge_early_leave(now, schedule)
        cur_status   = db.execute(
            'SELECT attendance_status FROM checkins WHERE user_id=? AND date=?', (uid, today)
        ).fetchone()
        new_status = cur_status['attendance_status'] if cur_status else 'present'
        if early and new_status not in ('late',):
            new_status = 'early_leave'

        db.execute(
            'UPDATE checkins '
            'SET check_out=?, regular_min=?, overtime_min=?, night_min=?, holiday_min=?, break_min=?, attendance_status=? '
            'WHERE user_id=? AND date=?',
            (now,
             hrs['regular_min'], hrs['overtime_min'],
             hrs['night_min'],   holiday_min,
             hrs['break_min'],   new_status,
             uid, today)
        )
        db.commit()

        if early:
            we = schedule.get('work_end', '') if schedule else ''
            flash(f'조퇴 처리됐습니다. (기준 퇴근 시각: {we})', 'warning')

        # ── 주 52시간 실시간 체크 (근로기준법 §53) ────────────────
        weekly = calc_weekly_hours(db, uid, today)
        if weekly['is_violation']:
            # 직속 매니저 + 모든 Admin에게 위반 알림
            emp = db.execute(
                'SELECT name, manager_id FROM users WHERE id=?', (uid,)
            ).fetchone()
            if emp and emp['manager_id']:
                add_notification(
                    emp['manager_id'], 'action', 'overtime',
                    f'주 52시간 초과: {emp["name"]}',
                    f'{emp["name"]}님이 이번 주 {weekly["total_h"]}시간 근무했습니다 '
                    f'(법정 한도 초과 {weekly["over_h"]}시간).',
                    url_for('overtime_monitor')
                )
            admins = db.execute(
                "SELECT id FROM users WHERE role='admin'"
            ).fetchall()
            for admin in admins:
                add_notification(
                    admin['id'], 'action', 'overtime',
                    f'주 52시간 초과 감지',
                    f'{emp["name"] if emp else uid}님 이번 주 {weekly["total_h"]}h '
                    f'(초과 {weekly["over_h"]}h).',
                    url_for('overtime_monitor')
                )
            flash(
                f'주 52시간 초과! 이번 주 총 {weekly["total_h"]}시간 근무 '
                f'(법정 한도 초과 {weekly["over_h"]}시간). HR 담당자에게 자동 알림이 발송됐습니다.',
                'error'
            )
        elif weekly['is_warning']:
            flash(
                f'이번 주 {weekly["total_h"]}시간 근무 중 — '
                f'주 52시간 한도까지 {weekly["remain_h"]}시간 남았습니다.',
                'warning'
            )

    return redirect(url_for('attendance_home'))


# ── Peer Review & Calibration ────────────────────────────────

UPWARD_QUESTIONS = [
    '매니저는 나의 성장을 위한 구체적인 피드백을 제공한다.',
    '매니저는 불필요하게 세부 사항을 통제하지 않는다 (마이크로매니징 없음).',
    '매니저는 팀 목표와 우선순위를 명확하게 전달한다.',
    '매니저는 나를 한 사람으로서 배려한다.',
    '전반적으로 이 매니저와 계속 일하고 싶다.',
]


def _calc_upward_avg(row):
    """upward review 행에서 5개 질문 평균 반환"""
    scores = [row[f'q{i}_score'] for i in range(1, 6) if row[f'q{i}_score'] is not None]
    return round(sum(scores) / len(scores), 2) if scores else None


def generate_calibration_summary(name, self_avg, peer_avg, mgr_avg, upward_avg):
    """규칙 기반 캘리브레이션 요약 텍스트 생성"""
    scores = {k: v for k, v in {
        '자기평가': self_avg, '동료평가': peer_avg, '매니저평가': mgr_avg
    }.items() if v is not None}

    if not scores:
        return '아직 평가 데이터가 없습니다.'

    overall = sum(scores.values()) / len(scores)

    # 등급 결정
    if overall >= 4.5:
        grade, label = 'S', '탁월'
    elif overall >= 3.5:
        grade, label = 'A', '우수'
    elif overall >= 2.5:
        grade, label = 'B', '양호'
    elif overall >= 1.5:
        grade, label = 'C', '개선필요'
    else:
        grade, label = 'D', '미흡'

    parts = [f'{name}의 종합 평균은 {overall:.2f}점으로 {label}({grade}) 등급에 해당합니다.']

    # 일관성 분석
    if len(scores) >= 2:
        rng = max(scores.values()) - min(scores.values())
        if rng <= 0.5:
            parts.append('3가지 평가 간 일관성이 높습니다.')
        elif rng <= 1.0:
            parts.append('평가 간 소폭의 차이가 있습니다.')
        else:
            parts.append('평가 간 상당한 편차가 있어 추가 논의가 필요합니다.')

    # 자기평가 vs 매니저평가 갭
    if self_avg is not None and mgr_avg is not None:
        gap = self_avg - mgr_avg
        if gap >= 1.0:
            parts.append('자기평가가 매니저평가보다 1점 이상 높습니다 (자기인식 과잉 경향 확인 필요).')
        elif gap <= -1.0:
            parts.append('매니저평가가 자기평가보다 1점 이상 높습니다 (겸손한 자기평가).')

    # 동료평가 특이점
    if peer_avg is not None and mgr_avg is not None:
        if peer_avg < mgr_avg - 0.7:
            parts.append('동료평가가 매니저평가보다 낮습니다 — 협업/커뮤니케이션 측면을 확인하세요.')
        elif peer_avg > mgr_avg + 0.7:
            parts.append('동료들의 평가가 매니저평가보다 높습니다.')

    # 매니저 upward
    if upward_avg is not None:
        if upward_avg >= 4.0:
            parts.append(f'팀원 피드백 평균 {upward_avg:.1f}점 — 높은 리더십 만족도를 보입니다.')
        elif upward_avg < 3.0:
            parts.append(f'팀원 피드백 평균 {upward_avg:.1f}점 — 리더십 개선이 필요합니다.')

    parts.append(f'캘리브레이션 권고 등급: {grade}')
    return ' '.join(parts)


def publish_calibration_results(db, cid):
    """직원에게 공개 = 이의신청 단계 시작 (등급 공개 + 7일 이의기간). (성공 여부, 메시지)"""
    gd = get_grade_dist()
    if gd['mode'] == 'forced':
        groups, _m = grade_dist_groups(db, cid, gd)
        issues = [f'{x["label"]} ' + ', '.join(x['over'] + x['under'])
                  for x in groups if x['over'] or x['under']]
        if issues:
            return False, '강제 배분 미충족으로 공개 불가 — ' + ' / '.join(issues[:4])
    appeal_until = (date.today() + timedelta(days=7)).isoformat()
    db.execute('UPDATE calibration_results SET is_shared=1 WHERE cycle_id=?', (cid,))
    db.execute("UPDATE performance_cycles SET stage='appeal', appeal_until=? WHERE id=?",
               (appeal_until, cid))
    db.commit()
    shared_rows = db.execute(
        'SELECT user_id, final_grade FROM calibration_results WHERE cycle_id=? AND is_shared=1', (cid,)
    ).fetchall()
    for r in shared_rows:
        add_notification(
            r['user_id'], 'info', 'perf',
            '성과 평가 결과가 공개되었습니다',
            f'이번 주기 최종 등급: {r["final_grade"]}등급 · 이의신청은 {appeal_until}까지 1회 가능합니다.',
            link='/performance?tab=result'
        )
    count = len(shared_rows)
    log_audit('update', 'performance', None,
              f'평가 결과 공개 + 이의신청 기간 시작 (~{appeal_until}, {count}명)')
    return True, f'{count}명의 평가 결과가 공개되었습니다. 이의신청 기간: {appeal_until}까지.'


@app.route('/performance/calibration', methods=['GET', 'POST'])
@admin_required
def calibration():
    db   = get_db()
    cycles = db.execute("SELECT * FROM performance_cycles ORDER BY start_date DESC").fetchall()
    active_cycle = next((c for c in cycles if c['status'] == 'active'), None)
    try:
        selected_cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        selected_cycle_id = 0
    selected_cycle = next((c for c in cycles if c['id'] == selected_cycle_id), active_cycle)
    cycle_id = selected_cycle['id'] if selected_cycle else 0

    if request.method == 'POST':
        action = request.form.get('action')

        # 개별 등급 확정
        if action == 'confirm':
            if selected_cycle and selected_cycle['stage'] not in ('calibration', 'review'):
                flash('등급 확정은 "HR 조정" 단계에서만 가능합니다. 주기 관리에서 단계를 변경하세요.', 'error')
                return redirect(url_for('calibration', cycle=cycle_id))
            GRADE_NUM = {'S': 5, 'A': 4, 'B': 3, 'C': 2, 'D': 1}
            uid              = int(request.form.get('user_id'))
            final_grade      = request.form.get('final_grade')
            note             = request.form.get('note', '').strip() or None
            downgrade_reason = request.form.get('downgrade_reason', '').strip() or None
            cid              = int(request.form.get('cycle_id'))
            try:
                potential_score = int(request.form.get('potential_score', 0)) or None
            except (ValueError, TypeError):
                potential_score = None
            if final_grade not in ('S', 'A', 'B', 'C', 'D'):
                flash('올바른 등급을 선택하세요.', 'error')
            else:
                # 집계값 다시 계산
                row = _calc_calibration_row(db, uid, cid)
                suggested = row['suggested_grade']

                # 다운그레이드 검사 (최대 1단계, 사유 필수)
                if GRADE_NUM.get(final_grade, 3) < GRADE_NUM.get(suggested, 3):
                    gap = GRADE_NUM[suggested] - GRADE_NUM[final_grade]
                    if gap > 1:
                        flash(
                            f'등급을 {gap}단계 낮출 수 없습니다. '
                            f'권고 등급({suggested})에서 최대 1단계까지만 조정 가능합니다.',
                            'error'
                        )
                        return redirect(url_for('calibration', cycle=cycle_id))
                    if not downgrade_reason:
                        flash('등급을 낮출 경우 반드시 조정 사유를 입력해야 합니다.', 'error')
                        return redirect(url_for('calibration', cycle=cycle_id))

                gd = get_grade_dist()
                if gd['mode'] == 'forced' and final_grade in ('S', 'A'):
                    groups, member = grade_dist_groups(db, cid, gd, override=(uid, final_grade))
                    grp = next((x for x in groups if x['key'] == member.get(uid)), None)
                    if grp and grp['dist'][final_grade] > grp['cap'][final_grade]:
                        flash(f'강제 배분 초과 — {grp["label"]} {final_grade}등급 상한 {grp["cap"][final_grade]}명 '
                              f'({grp["total"]}명 × {gd["pct"][final_grade]}%)', 'error')
                        return redirect(url_for('calibration', cycle=cycle_id, dept=request.args.get('dept') or None))

                summary = generate_calibration_summary(
                    row['name'], row['self_avg'], row['peer_avg'],
                    row['mgr_avg'], row['upward_avg']
                )
                db.execute('''
                    INSERT INTO calibration_results
                      (cycle_id, user_id, self_avg, peer_avg, mgr_avg, upward_avg,
                       suggested_grade, final_grade, summary_text, note,
                       downgrade_reason, potential_score, is_shared, decided_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    ON CONFLICT(cycle_id, user_id) DO UPDATE SET
                      self_avg=excluded.self_avg, peer_avg=excluded.peer_avg,
                      mgr_avg=excluded.mgr_avg, upward_avg=excluded.upward_avg,
                      suggested_grade=excluded.suggested_grade,
                      final_grade=excluded.final_grade,
                      summary_text=excluded.summary_text,
                      note=excluded.note,
                      downgrade_reason=excluded.downgrade_reason,
                      potential_score=COALESCE(excluded.potential_score, potential_score),
                      decided_by=excluded.decided_by,
                      decided_at=CURRENT_TIMESTAMP
                ''', (cid, uid,
                      row['self_avg'], row['peer_avg'], row['mgr_avg'], row['upward_avg'],
                      suggested, final_grade, summary, note, downgrade_reason,
                      potential_score, session['user_id']))
                db.commit()
                log_audit('update', 'performance', uid,
                          f'캘리브레이션 등급 확정 ({final_grade}' + (f', 하향사유: {downgrade_reason}' if downgrade_reason else '') + ')')
                flash('등급이 확정되었습니다.', 'success')

        # 직원에게 공개 = 이의신청 단계 시작 (등급 공개 + 7일 이의기간)
        elif action == 'publish':
            ok_pub, msg = publish_calibration_results(db, int(request.form.get('cycle_id')))
            flash(msg, 'success' if ok_pub else 'error')

        return redirect(url_for('calibration', cycle=cycle_id))

    # GET — 전 직원 집계 (R1-C: 팀 단위 진행)
    try:
        selected_dept = int(request.args.get('dept', 0))
    except (ValueError, TypeError):
        selected_dept = 0

    all_rows = []
    if cycle_id:
        emps = db.execute(
            "SELECT u.id, u.name, u.department_id, d.name dept_name FROM users u "
            "LEFT JOIN departments d ON u.department_id=d.id "
            "WHERE u.status='active' AND u.role NOT IN ('admin','guest') "
            "ORDER BY d.name, u.name"
        ).fetchall()

        for emp in emps:
            row = _calc_calibration_row(db, emp['id'], cycle_id)
            row['dept_id'] = emp['department_id'] or 0
            # 기존 확정 결과
            saved = db.execute(
                'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=?',
                (cycle_id, emp['id'])
            ).fetchone()
            row['confirmed']        = saved is not None
            row['final_grade']      = saved['final_grade'] if saved else row['suggested_grade']
            row['is_shared']        = saved['is_shared'] if saved else 0
            row['note']             = saved['note'] if saved else ''
            row['potential_score']  = saved['potential_score'] if saved else None
            row['downgrade_reason'] = saved['downgrade_reason'] if saved else None
            all_rows.append(row)

    # 전체 분포 집계 (공개 조건은 전 직원 기준 유지)
    grade_dist = {'S':0,'A':0,'B':0,'C':0,'D':0}
    confirmed_count = 0
    for r in all_rows:
        if r['confirmed']:
            confirmed_count += 1
            grade_dist[r['final_grade']] = grade_dist.get(r['final_grade'], 0) + 1

    total_count = len(all_rows)
    publish_ready = confirmed_count > 0 and confirmed_count == total_count

    # 부서(팀)별 진행 현황 — 좌측 패널
    dept_groups = {}
    for r in all_rows:
        g = dept_groups.setdefault(r['dept_id'], {
            'dept_id': r['dept_id'],
            'dept_name': r['dept_name'] if r['dept_name'] != '—' else '부서 미지정',
            'total': 0, 'confirmed': 0,
            'dist': {'S':0,'A':0,'B':0,'C':0,'D':0},
        })
        g['total'] += 1
        if r['confirmed']:
            g['confirmed'] += 1
            g['dist'][r['final_grade']] = g['dist'].get(r['final_grade'], 0) + 1
    dept_list = sorted(dept_groups.values(), key=lambda d: d['dept_name'])

    # 선택 부서 상세 — 종합점수 내림차순 (미산출은 뒤로)
    rows = []
    dept_info = None
    if selected_dept and selected_dept in dept_groups:
        dept_info = dept_groups[selected_dept]
        rows = sorted(
            (r for r in all_rows if r['dept_id'] == selected_dept),
            key=lambda r: (r['overall'] is None, -(r['overall'] or 0), r['name'])
        )

    active_acr = db.execute(
        "SELECT id FROM compensation_review_cycles WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # 이의신청 현황
    appeal_pending = db.execute(
        "SELECT COUNT(*) FROM grade_appeals WHERE cycle_id=? AND status='pending'", (cycle_id,)
    ).fetchone()[0] if cycle_id else 0

    # 등급 배분 기준 (회사 설정) — 적용 단위별 상한·하한
    grade_dist_cfg = get_grade_dist()
    target_dist = grade_dist_cfg['pct']
    dist_groups = grade_dist_groups(db, cycle_id, grade_dist_cfg)[0] if cycle_id else []
    dist_blocked = grade_dist_cfg['mode'] == 'forced' and any(x['over'] or x['under'] for x in dist_groups)
    anomaly_count = sum(1 for r in all_rows if r['anomaly'])

    return render_template('performance/calibration.html',
                           anomaly_count=anomaly_count,
                           cycles=cycles, selected_cycle=selected_cycle,
                           cycle_id=cycle_id, rows=rows,
                           dept_list=dept_list, selected_dept=selected_dept,
                           dept_info=dept_info, target_dist=target_dist,
                           grade_dist_cfg=grade_dist_cfg, dist_groups=dist_groups,
                           dist_blocked=dist_blocked,
                           grade_dist=grade_dist, confirmed_count=confirmed_count,
                           total_count=total_count, publish_ready=publish_ready,
                           active_acr=active_acr,
                           appeal_pending=appeal_pending,
                           form_basis=form_basis(cycle_form(selected_cycle)) if selected_cycle else '',
                           cycle_stage_label=CYCLE_STAGE_LABEL,
                           active_page='performance')


def _calc_calibration_row(db, user_id, cycle_id):
    """직원 한 명의 캘리브레이션 집계값 계산"""
    user = db.execute(
        'SELECT u.id, u.name, d.name dept_name FROM users u '
        'LEFT JOIN departments d ON u.department_id=d.id WHERE u.id=?', (user_id,)
    ).fetchone()

    # 자기평가 평균 (목표별 self_score 가중 평균)
    self_rows = db.execute(
        'SELECT self_score, weight FROM performance_goals '
        'WHERE user_id=? AND cycle_id=? AND self_score IS NOT NULL',
        (user_id, cycle_id)
    ).fetchall()
    if self_rows:
        total_w = sum(r['weight'] for r in self_rows)
        self_avg = round(sum(r['self_score'] * r['weight'] for r in self_rows) / total_w, 2) if total_w else None
    else:
        self_avg = None

    # 매니저 평가 평균 (performance_reviews)
    mgr_rows = db.execute(
        'SELECT pr.score FROM performance_reviews pr '
        'JOIN performance_goals g ON pr.goal_id=g.id '
        'WHERE g.user_id=? AND g.cycle_id=?',
        (user_id, cycle_id)
    ).fetchall()
    mgr_avg = round(sum(r['score'] for r in mgr_rows) / len(mgr_rows), 2) if mgr_rows else None

    # 동료 평가 평균 (peer_reviews type=peer)
    peer_rows = db.execute(
        'SELECT score FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND review_type=\'peer\'',
        (cycle_id, user_id)
    ).fetchall()
    peer_avg = round(sum(r['score'] for r in peer_rows) / len(peer_rows), 2) if peer_rows else None

    # 상향 평가 평균 (문항 점수 q1~q5 평균의 평균)
    upward_rows = db.execute(
        'SELECT * FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND review_type=\'upward\'',
        (cycle_id, user_id)
    ).fetchall()
    _uv = [v for v in (_calc_upward_avg(r) for r in upward_rows) if v is not None]
    upward_avg = round(sum(_uv) / len(_uv), 2) if _uv else None

    # 종합 점수 — 기존 주기: 있는 것만 단순 평균 / 양식 주기: 업적·역량 × 평가자 비중
    cyc = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    form = cycle_form(cyc)
    goal_score = comp_score = None
    if form['legacy']:
        scores = [s for s in [self_avg, peer_avg, mgr_avg] if s is not None]
        overall = round(sum(scores) / len(scores), 2) if scores else None
    else:
        def _wavg(pairs):
            pairs = [(v, w) for v, w in pairs if v is not None and w]
            tw = sum(w for _, w in pairs)
            return round(sum(v * w for v, w in pairs) / tw, 2) if tw else None
        self_goal = self_avg
        mgr_goal = _wavg([(r['m'], r['weight']) for r in db.execute(
            'SELECT g.weight, AVG(pr.score) AS m FROM performance_goals g '
            'JOIN performance_reviews pr ON pr.goal_id=g.id WHERE g.user_id=? AND g.cycle_id=? GROUP BY g.id',
            (user_id, cycle_id)).fetchall()])
        keys = {c['key'] for c in form['competencies']}
        per = {}
        for r in db.execute('SELECT comp_key, rater_type, AVG(score) AS m FROM competency_scores '
                            'WHERE cycle_id=? AND user_id=? GROUP BY comp_key, rater_type',
                            (cycle_id, user_id)).fetchall():
            if r['comp_key'] in keys:
                per.setdefault(r['rater_type'], []).append(r['m'])
        self_comp = round(sum(per['self']) / len(per['self']), 2) if per.get('self') else None
        mgr_comp = round(sum(per['manager']) / len(per['manager']), 2) if per.get('manager') else None
        gw, cw = form['goal_weight'], form['comp_weight']
        self_avg = _wavg([(self_goal, gw), (self_comp, cw)])
        mgr_avg = _wavg([(mgr_goal, gw), (mgr_comp, cw)])
        overall = _wavg([(self_avg, form['self_weight']), (peer_avg, form['peer_weight']),
                         (mgr_avg, form['mgr_weight'])])
        goal_score = _wavg([(self_goal, form['self_weight']), (mgr_goal, form['mgr_weight'])])
        comp_score = _wavg([(self_comp, form['self_weight']), (mgr_comp, form['mgr_weight'])]) if cw else None

    # 등급 산출
    if overall is None:
        suggested = None
    elif overall >= 4.5: suggested = 'S'
    elif overall >= 3.5: suggested = 'A'
    elif overall >= 2.5: suggested = 'B'
    elif overall >= 1.5: suggested = 'C'
    else:                suggested = 'D'

    # 이상 감지 (자기평가 vs 매니저평가 차이 1.5 이상)
    anomaly = None
    if self_avg and mgr_avg and abs(self_avg - mgr_avg) >= 1.5:
        if self_avg > mgr_avg:
            anomaly = '자기평가가 매니저평가보다 현저히 높음'
        else:
            anomaly = '매니저평가가 자기평가보다 현저히 높음'

    return {
        'user_id': user_id,
        'name': user['name'],
        'dept_name': user['dept_name'] or '—',
        'self_avg': self_avg,
        'peer_avg': peer_avg,
        'mgr_avg': mgr_avg,
        'upward_avg': upward_avg,
        'overall': overall,
        'goal_score': goal_score,
        'comp_score': comp_score,
        'form_legacy': form['legacy'],
        'suggested_grade': suggested,
        'anomaly': anomaly,
    }


# ──────────────────────────────────────────────
# v1.1.0 — 등급 이의신청 (Phase C-10, saas_plan.md §4 ⑩)
# ──────────────────────────────────────────────
@app.route('/performance/appeal', methods=['POST'])
@login_required
def performance_appeal_new():
    """직원 — 등급 이의신청 (주기당 1회, 이의기간 내)"""
    db  = get_db()
    uid = session['user_id']
    cycle_id = request.form.get('cycle_id', type=int)
    reason   = request.form.get('reason', '').strip()

    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    result = db.execute(
        'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=? AND is_shared=1',
        (cycle_id, uid)
    ).fetchone()

    if cycle['stage'] != 'appeal' or not cycle['appeal_until'] or str(date.today()) > cycle['appeal_until']:
        flash('이의신청 기간이 아닙니다.', 'error')
    elif not result:
        flash('공개된 평가 결과가 없습니다.', 'error')
    elif not reason or len(reason) < 10:
        flash('이의신청 사유를 10자 이상 구체적으로 작성해 주세요.', 'error')
    elif db.execute('SELECT id FROM grade_appeals WHERE cycle_id=? AND user_id=?', (cycle_id, uid)).fetchone():
        flash('이의신청은 주기당 1회만 가능합니다.', 'error')
    else:
        db.execute(
            'INSERT INTO grade_appeals (cycle_id, user_id, reason, old_grade) VALUES (?,?,?,?)',
            (cycle_id, uid, reason, result['final_grade'])
        )
        db.commit()
        # 직속 매니저 + HR(admin)에게 알림
        mgr = db.execute('SELECT manager_id FROM users WHERE id=?', (uid,)).fetchone()
        targets = {r['id'] for r in db.execute(
            "SELECT id FROM users WHERE role='admin' AND status='active'").fetchall()}
        if mgr and mgr['manager_id']:
            targets.add(mgr['manager_id'])
        for t in targets:
            add_notification(
                t, 'action', 'perf',
                '등급 이의신청 접수',
                f'{session.get("user_name","직원")}님이 {cycle["name"]} 등급({result["final_grade"]})에 이의를 신청했습니다.',
                link='/performance/appeals?cycle=%d' % cycle_id
            )
        log_audit('create', 'performance', uid, f'등급 이의신청 접수 ({cycle["name"]}, 현재 등급 {result["final_grade"]})')
        flash('이의신청이 접수되었습니다. 팀장/HR 재검토 결과를 알림으로 안내드립니다.', 'success')
    return redirect(url_for('performance', cycle=cycle_id, tab='result'))


@app.route('/performance/appeals', methods=['GET', 'POST'])
@manager_or_admin
def performance_appeals():
    """팀장/HR — 이의신청 목록 + 재검토 처리 (1회)"""
    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']

    if request.method == 'POST':
        appeal_id = request.form.get('appeal_id', type=int)
        action    = request.form.get('action')            # accept | reject
        response  = request.form.get('response', '').strip()
        new_grade = request.form.get('new_grade', '').strip()

        appeal = db.execute(
            'SELECT ga.*, u.name AS user_name, u.manager_id, c.name AS cycle_name '
            'FROM grade_appeals ga JOIN users u ON ga.user_id=u.id '
            'JOIN performance_cycles c ON ga.cycle_id=c.id WHERE ga.id=?',
            (appeal_id,)
        ).fetchone()
        if not appeal:
            abort(404)
        if role != 'admin' and appeal['manager_id'] != uid:
            abort(403)
        if appeal['status'] != 'pending':
            flash('이미 처리된 이의신청입니다. 재검토는 1회만 가능합니다.', 'error')
            return redirect(url_for('performance_appeals', cycle=appeal['cycle_id']))
        if not response:
            flash('재검토 의견을 입력해야 합니다.', 'error')
            return redirect(url_for('performance_appeals', cycle=appeal['cycle_id']))

        if action == 'accept':
            if new_grade not in ('S', 'A', 'B', 'C', 'D'):
                flash('조정 등급을 선택하세요.', 'error')
                return redirect(url_for('performance_appeals', cycle=appeal['cycle_id']))
            db.execute(
                "UPDATE grade_appeals SET status='accepted', new_grade=?, response=?, "
                "resolved_by=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_grade, response, uid, appeal_id)
            )
            db.execute(
                'UPDATE calibration_results SET final_grade=?, note=COALESCE(note,\'\') || ? '
                'WHERE cycle_id=? AND user_id=?',
                (new_grade, f' [이의신청 재검토로 {appeal["old_grade"]}→{new_grade} 조정]',
                 appeal['cycle_id'], appeal['user_id'])
            )
            db.commit()
            add_notification(
                appeal['user_id'], 'info', 'perf',
                '이의신청이 인용되었습니다',
                f'{appeal["cycle_name"]} 등급이 {appeal["old_grade"]} → {new_grade}(으)로 조정되었습니다. 의견: {response}',
                link='/performance?cycle=%d&tab=result' % appeal['cycle_id']
            )
            log_audit('update', 'performance', appeal['user_id'],
                      f'이의신청 인용 — 등급 {appeal["old_grade"]}→{new_grade} ({response})')
            flash(f'{appeal["user_name"]}님의 이의신청을 인용했습니다. ({appeal["old_grade"]}→{new_grade})', 'success')
        else:
            db.execute(
                "UPDATE grade_appeals SET status='rejected', response=?, "
                "resolved_by=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                (response, uid, appeal_id)
            )
            db.commit()
            add_notification(
                appeal['user_id'], 'info', 'perf',
                '이의신청 재검토 결과 안내',
                f'{appeal["cycle_name"]} 등급({appeal["old_grade"]})이 유지되었습니다. 의견: {response}',
                link='/performance?cycle=%d&tab=result' % appeal['cycle_id']
            )
            log_audit('update', 'performance', appeal['user_id'],
                      f'이의신청 기각 — 등급 {appeal["old_grade"]} 유지 ({response})')
            flash(f'{appeal["user_name"]}님의 이의신청을 기각 처리했습니다.', 'success')
        return redirect(url_for('performance_appeals', cycle=appeal['cycle_id']))

    # GET — 목록
    cycles = db.execute('SELECT * FROM performance_cycles ORDER BY start_date DESC').fetchall()
    try:
        selected_cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        selected_cycle_id = 0
    selected_cycle = next(
        (c for c in cycles if c['id'] == selected_cycle_id),
        next((c for c in cycles if c['stage'] in ('appeal', 'closed')), None)
    )
    cycle_id = selected_cycle['id'] if selected_cycle else 0

    appeals = []
    if cycle_id:
        q = ('SELECT ga.*, u.name AS user_name, u.manager_id, d.name AS dept_name, '
             'r.name AS resolver_name '
             'FROM grade_appeals ga '
             'JOIN users u ON ga.user_id=u.id '
             'LEFT JOIN departments d ON u.department_id=d.id '
             'LEFT JOIN users r ON ga.resolved_by=r.id '
             'WHERE ga.cycle_id=? ')
        params = [cycle_id]
        if role != 'admin':
            q += 'AND u.manager_id=? '
            params.append(uid)
        q += "ORDER BY CASE ga.status WHEN 'pending' THEN 0 ELSE 1 END, ga.created_at DESC"
        appeals = db.execute(q, params).fetchall()

    return render_template('performance/appeals.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           cycle_id=cycle_id, appeals=appeals,
                           cycle_stage_label=CYCLE_STAGE_LABEL,
                           active_page='performance')


# ──────────────────────────────────────────────
# v0.49 — Talent Card
# ──────────────────────────────────────────────
@app.route('/performance/talent-card/<int:user_id>')
@login_required
def talent_card(user_id):
    db   = get_db()
    role = session['user_role']
    uid  = session['user_id']

    # 본인이거나 매니저/어드민만 접근
    if uid != user_id and role not in ('manager', 'admin'):
        abort(403)

    from payroll_utils import calc_compa_ratio

    emp = db.execute(
        '''SELECT u.*, d.name dept_name, p.name position_name, jf.name job_family_name
           FROM users u
           LEFT JOIN departments d  ON u.department_id = d.id
           LEFT JOIN positions   p  ON u.position_id   = p.id
           LEFT JOIN job_families jf ON u.job_family_id = jf.id
           WHERE u.id = ?''', (user_id,)
    ).fetchone()
    if not emp:
        abort(404)

    # 사이클별 성과 등급 히스토리
    grade_history = db.execute(
        '''SELECT pc.name cycle_name, pc.start_date, cr.final_grade,
                  cr.suggested_grade, cr.potential_score,
                  cr.self_avg, cr.peer_avg, cr.mgr_avg, cr.is_shared,
                  cr.id cr_id,
                  cr.retention_risk, cr.loss_impact, cr.achievable_level
           FROM calibration_results cr
           JOIN performance_cycles  pc ON cr.cycle_id = pc.id
           WHERE cr.user_id = ?
           ORDER BY pc.start_date DESC''', (user_id,)
    ).fetchall()

    # 가장 최근 캘리브레이션 결과
    latest = grade_history[0] if grade_history else None

    # ── Flight Risk 자동 감지 ──────────────────────────────────────────────
    flight_risk = False
    flight_risk_reasons = []
    # 1) Compa-ratio < 0.85 (급여 밴드 하단)
    sal_row = db.execute(
        '''SELECT s.base_salary, sg.mid_salary
           FROM employee_salary s
           LEFT JOIN salary_grades sg ON sg.position_id = ? AND sg.job_family_id = ?
           WHERE s.user_id = ?''',
        (emp['position_id'], emp['job_family_id'], user_id)
    ).fetchone()
    if sal_row and sal_row['mid_salary']:
        ratio = calc_compa_ratio(sal_row['base_salary'], sal_row['mid_salary'])
        if ratio < 0.85:
            flight_risk_reasons.append(f'급여 밴드 하단 (Compa {ratio:.2f})')
    # 2) 최근 성과 C 이하
    if latest and latest['final_grade'] in ('C', 'D'):
        flight_risk_reasons.append(f'성과 등급 {latest["final_grade"]}')
    # 3) 승진 이력 2년 이상 없음
    recent_promotion = db.execute(
        '''SELECT id FROM personnel_actions
           WHERE user_id = ? AND action_type = 'promotion'
             AND applied_at >= date('now', '-2 years')
           LIMIT 1''', (user_id,)
    ).fetchone()
    hire_date = emp['hire_date'] if 'hire_date' in emp.keys() else None
    if not recent_promotion:
        if hire_date:
            from datetime import date, datetime
            try:
                hd = datetime.strptime(hire_date[:10], '%Y-%m-%d').date()
                if (date.today() - hd).days > 730:
                    flight_risk_reasons.append('2년 이상 미승진')
            except Exception:
                pass
    if len(flight_risk_reasons) >= 2:
        flight_risk = True

    # 직급 목록 (Achievable Level 선택용)
    positions_list = db.execute('SELECT id, name FROM positions ORDER BY level').fetchall()

    # 9박스 위치 계산
    # X축: potential_score (1=Low, 2=Mid, 3=High)
    # Y축: final_grade 숫자 → S=3, A=2, B=1 (상위 3단계), C/D=0 영역
    GRADE_TO_Y = {'S': 3, 'A': 2, 'B': 1, 'C': 0, 'D': 0}
    box_pos = None
    if latest and latest['final_grade'] and latest['potential_score']:
        y = GRADE_TO_Y.get(latest['final_grade'], 1)
        x = latest['potential_score']  # 1~3
        box_pos = (y, x)  # (row 1~3, col 1~3)

    # 현재 목표 진행률
    active_cycle = db.execute(
        "SELECT * FROM performance_cycles WHERE status='active' ORDER BY start_date DESC LIMIT 1"
    ).fetchone()
    goals = []
    if active_cycle:
        goals = db.execute(
            '''SELECT title, progress, weight, self_score
               FROM performance_goals
               WHERE user_id=? AND cycle_id=?
               ORDER BY weight DESC''',
            (user_id, active_cycle['id'])
        ).fetchall()

    # 후계자 계획 — 이 직원이 후보로 올라간 포지션
    succession_as_candidate = db.execute(
        '''SELECT sp.*, u.name incumbent_name
           FROM succession_plans sp
           LEFT JOIN users u ON sp.incumbent_id = u.id
           WHERE sp.candidate_id = ?
           ORDER BY sp.created_at DESC''', (user_id,)
    ).fetchall()

    return render_template(
        'performance/talent_card.html',
        emp=emp,
        grade_history=grade_history,
        latest=latest,
        box_pos=box_pos,
        goals=goals,
        active_cycle=active_cycle,
        succession_as_candidate=succession_as_candidate,
        flight_risk=flight_risk,
        flight_risk_reasons=flight_risk_reasons,
        positions_list=positions_list,
        active_page='performance',
    )


@app.route('/performance/talent-card/<int:user_id>/talent-flags', methods=['POST'])
@login_required
def talent_card_flags(user_id):
    role = session['user_role']
    if role not in ('manager', 'admin'):
        abort(403)
    db = get_db()
    cr_id       = request.form.get('cr_id')
    retention   = request.form.get('retention_risk')
    loss        = request.form.get('loss_impact')
    achievable  = request.form.get('achievable_level')
    if cr_id:
        db.execute(
            '''UPDATE calibration_results
               SET retention_risk=?, loss_impact=?, achievable_level=?
               WHERE id=? AND user_id=?''',
            (retention or None, loss or None, achievable or None, cr_id, user_id)
        )
        db.commit()
        flash('Talent 평가 항목이 저장됐습니다.', 'success')
    else:
        flash('저장할 캘리브레이션 결과가 없습니다.', 'warning')
    return redirect(url_for('talent_card', user_id=user_id))


# ──────────────────────────────────────────────
# v0.50 — 목표 템플릿
# ──────────────────────────────────────────────
@app.route('/performance/goal-templates', methods=['GET', 'POST'])
@login_required
def goal_templates():
    db   = get_db()
    role = session['user_role']

    if request.method == 'POST':
        if role not in ('admin', 'manager'):
            abort(403)
        action = request.form.get('action')

        if action == 'add':
            title    = request.form.get('title', '').strip()
            desc     = request.form.get('description', '').strip() or None
            category = request.form.get('category', '개인')
            weight   = int(request.form.get('weight', 20))
            if not title:
                flash('목표명은 필수입니다.', 'error')
            else:
                db.execute(
                    'INSERT INTO goal_templates (title, description, category, weight, created_by) VALUES (?,?,?,?,?)',
                    (title, desc, category, weight, session['user_id'])
                )
                db.commit()
                flash('템플릿이 추가되었습니다.', 'success')

        elif action == 'toggle':
            tid = int(request.form.get('template_id'))
            db.execute(
                'UPDATE goal_templates SET is_active = 1 - is_active WHERE id=?', (tid,)
            )
            db.commit()

        elif action == 'delete':
            if role != 'admin':
                abort(403)
            tid = int(request.form.get('template_id'))
            db.execute('DELETE FROM goal_templates WHERE id=?', (tid,))
            db.commit()
            flash('삭제되었습니다.', 'success')

        return redirect(url_for('goal_templates'))

    templates = db.execute(
        '''SELECT gt.*, u.name creator_name
           FROM goal_templates gt
           LEFT JOIN users u ON gt.created_by = u.id
           ORDER BY gt.is_active DESC, gt.category, gt.title'''
    ).fetchall()

    return render_template('performance/goal_templates.html',
                           templates=templates,
                           active_page='performance')


@app.route('/performance/goal-templates/<int:tid>/json')
@login_required
def goal_template_json(tid):
    """goal_form에서 AJAX로 템플릿 내용 불러오기"""
    db  = get_db()
    row = db.execute(
        'SELECT * FROM goal_templates WHERE id=? AND is_active=1', (tid,)
    ).fetchone()
    if not row:
        return {'error': 'not found'}, 404
    return {
        'title':       row['title'],
        'description': row['description'] or '',
        'weight':      row['weight'],
    }


# ──────────────────────────────────────────────
# v0.49 — 후계자 계획
# ──────────────────────────────────────────────
@app.route('/performance/succession', methods=['GET', 'POST'])
@login_required
def succession():
    db   = get_db()
    role = session['user_role']
    if role not in ('manager', 'admin'):
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            pos_title    = request.form.get('position_title', '').strip()
            incumbent_id = request.form.get('incumbent_id') or None
            candidate_id = request.form.get('candidate_id')
            readiness    = request.form.get('readiness', 'ready_1y')
            note         = request.form.get('note', '').strip() or None

            if not pos_title or not candidate_id:
                flash('포지션명과 후보자는 필수입니다.', 'error')
            else:
                db.execute(
                    '''INSERT INTO succession_plans
                       (position_title, incumbent_id, candidate_id, readiness, note, created_by)
                       VALUES (?,?,?,?,?,?)''',
                    (pos_title, incumbent_id, candidate_id, readiness, note, session['user_id'])
                )
                db.commit()
                flash('승계 계획이 추가되었습니다.', 'success')

        elif action == 'delete':
            sp_id = int(request.form.get('sp_id'))
            db.execute('DELETE FROM succession_plans WHERE id=?', (sp_id,))
            db.commit()
            flash('삭제되었습니다.', 'success')

        return redirect(url_for('succession'))

    # 포지션별 그룹핑
    plans = db.execute(
        '''SELECT sp.*,
                  uc.name candidate_name, uc.role candidate_role,
                  ui.name incumbent_name,
                  d.name  dept_name
           FROM succession_plans sp
           JOIN users uc ON sp.candidate_id = uc.id
           LEFT JOIN users ui ON sp.incumbent_id = ui.id
           LEFT JOIN departments d ON uc.department_id = d.id
           ORDER BY sp.position_title, sp.readiness''',
    ).fetchall()

    # 포지션별 그룹화
    from collections import OrderedDict
    grouped = OrderedDict()
    for p in plans:
        key = p['position_title']
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(p)

    # 후보 선택용 직원 목록
    employees = db.execute(
        "SELECT id, name, role FROM users WHERE status='active' AND role NOT IN ('guest') ORDER BY name"
    ).fetchall()

    READINESS_LABELS = {
        'ready_now': '즉시 가능',
        'ready_1y':  '1년 내',
        'ready_2y':  '2년 내',
        'long_term': '장기 육성',
    }

    return render_template(
        'performance/succession.html',
        grouped=grouped,
        employees=employees,
        readiness_labels=READINESS_LABELS,
        active_page='succession',
    )


@app.route('/performance/peer')
@login_required
def peer_reviews_page():
    db    = get_db()
    uid   = session['user_id']
    role  = session['user_role']

    cycles = db.execute(
        "SELECT * FROM performance_cycles ORDER BY start_date DESC"
    ).fetchall()
    active_cycle = next((c for c in cycles if c['status'] == 'active'), None)

    try:
        selected_cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        selected_cycle_id = 0
    selected_cycle = next(
        (c for c in cycles if c['id'] == selected_cycle_id), active_cycle
    )
    cycle_id = selected_cycle['id'] if selected_cycle else 0

    # 내가 작성해야 할 다면평가 (배정된 것)
    my_assignments = []
    if cycle_id:
        rows = db.execute(
            'SELECT pa.*, u.name AS reviewee_name, u.id AS reviewee_id, '
            "pr.id AS done_id "
            'FROM peer_assignments pa '
            'JOIN users u ON pa.reviewee_id = u.id '
            "LEFT JOIN peer_reviews pr ON pr.cycle_id=pa.cycle_id "
            "  AND pr.reviewee_id=pa.reviewee_id AND pr.reviewer_id=pa.reviewer_id "
            "  AND pr.review_type='peer' "
            'WHERE pa.cycle_id=? AND pa.reviewer_id=?',
            (cycle_id, uid)
        ).fetchall()
        my_assignments = rows

    # 내가 작성해야 할 매니저 평가 (같은 부서 매니저 목록)
    upward_targets = []
    if cycle_id:
        my_dept = int(session.get('dept_id') or 0)
        if my_dept and role == 'employee':
            mgrs = db.execute(
                "SELECT u.id, u.name FROM users u "
                "WHERE u.department_id=? AND u.role='manager' AND u.status='active'",
                (my_dept,)
            ).fetchall()
            for mgr in mgrs:
                done = db.execute(
                    "SELECT id FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? "
                    "AND reviewer_id=? AND review_type='upward'",
                    (cycle_id, mgr['id'], uid)
                ).fetchone()
                upward_targets.append({'id': mgr['id'], 'name': mgr['name'], 'done': done is not None})

    # 내가 받은 다면평가 결과 (익명성: 3명 이상일 때만 공개)
    received_peer = []
    peer_count = 0
    peer_threshold_met = False
    if cycle_id:
        rows = db.execute(
            "SELECT pr.*, u.name AS reviewer_name "
            "FROM peer_reviews pr JOIN users u ON pr.reviewer_id = u.id "
            "WHERE pr.cycle_id=? AND pr.reviewee_id=? AND pr.review_type='peer' "
            "ORDER BY pr.created_at DESC",
            (cycle_id, uid)
        ).fetchall()
        peer_count = len(rows)
        if peer_count >= 3:
            received_peer = rows
            peer_threshold_met = True

    # 내가 받은 매니저 평가 결과 (매니저인 경우, 익명 — 3명 이상일 때만)
    received_upward = None
    upward_count = 0
    if cycle_id and role in ('manager', 'admin'):
        rows = db.execute(
            "SELECT * FROM peer_reviews "
            "WHERE cycle_id=? AND reviewee_id=? AND review_type='upward'",
            (cycle_id, uid)
        ).fetchall()
        upward_count = len(rows)
        if upward_count >= 3:
            avgs = {}
            for i in range(1, 6):
                vals = [r[f'q{i}_score'] for r in rows if r[f'q{i}_score'] is not None]
                avgs[f'q{i}'] = round(sum(vals) / len(vals), 1) if vals else None
            comments = [r['comment'] for r in rows if r['comment']]
            received_upward = {'avgs': avgs, 'comments': comments, 'count': upward_count}

    return render_template('performance/peer_reviews.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           my_assignments=my_assignments,
                           upward_targets=upward_targets,
                           received_peer=received_peer,
                           peer_count=peer_count,
                           peer_threshold_met=peer_threshold_met,
                           received_upward=received_upward,
                           upward_count=upward_count,
                           upward_questions=cycle_form(selected_cycle)['upward_questions'] if selected_cycle else UPWARD_QUESTIONS,
                           peer_prompts=cycle_form(selected_cycle)['peer_prompts'] if selected_cycle else DEFAULT_REVIEW_FORM['peer_prompts'],
                           active_page='peer')


@app.route('/performance/peer/write/<int:reviewee_id>', methods=['GET', 'POST'])
@login_required
def peer_review_write(reviewee_id):
    db    = get_db()
    uid   = session['user_id']
    role  = session['user_role']

    try:
        cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        cycle_id = 0

    review_type = request.args.get('type', 'peer')
    if review_type not in ('peer', 'upward'):
        review_type = 'peer'

    if not cycle_id:
        abort(400)
    if reviewee_id == uid:
        abort(403)

    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    reviewee = db.execute('SELECT id, name, role FROM users WHERE id=?', (reviewee_id,)).fetchone()
    if not cycle or not reviewee:
        abort(404)
    if not cycle['include_peer']:
        flash('이번 주기는 다면평가가 포함되지 않았습니다.', 'error')
        return redirect(url_for('performance', cycle=cycle_id))
    if cycle['stage'] != 'review':
        flash('다면평가는 "평가 진행" 단계에서만 작성할 수 있습니다.', 'error')
        return redirect(url_for('peer_reviews_page', cycle=cycle_id))

    # upward 평가는 employee만, 같은 부서의 manager만 대상
    if review_type == 'upward':
        if role != 'employee':
            abort(403)
        my_dept = int(session.get('dept_id') or 0)
        mgr_dept = db.execute(
            'SELECT department_id FROM users WHERE id=?', (reviewee_id,)
        ).fetchone()
        if not mgr_dept or mgr_dept['department_id'] != my_dept:
            abort(403)
        if reviewee['role'] not in ('manager', 'admin'):
            abort(403)

    # peer 평가는 배정된 경우만
    if review_type == 'peer':
        assigned = db.execute(
            'SELECT id FROM peer_assignments WHERE cycle_id=? AND reviewee_id=? AND reviewer_id=?',
            (cycle_id, reviewee_id, uid)
        ).fetchone()
        if not assigned:
            abort(403)

    existing = db.execute(
        'SELECT * FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND reviewer_id=? AND review_type=?',
        (cycle_id, reviewee_id, uid, review_type)
    ).fetchone()
    error = None
    form = cycle_form(cycle)
    prompts = [dict(pr, col=PEER_PROMPT_COLS[i]) for i, pr in enumerate(form['peer_prompts'])]
    questions = form['upward_questions']

    if request.method == 'POST':
        if review_type == 'peer':
            try:
                score = int(request.form.get('score', 0))
            except (ValueError, TypeError):
                score = 0
            texts = {pr['col']: (request.form.get(pr['col'], '').strip()[:2000] or None) if pr['label'] else None
                     for pr in prompts}
            strength, improvement, comment = texts['strength'], texts['improvement'], texts['comment']
            missing = [pr['label'] for pr in prompts if pr['label'] and not texts[pr['col']]]
            if not (1 <= score <= 5):
                error = '점수를 선택해주세요.'
            elif missing:
                error = f'{missing[0]} 항목을 입력해주세요.'
            else:
                db.execute(
                    'INSERT INTO peer_reviews '
                    '(cycle_id, reviewee_id, reviewer_id, review_type, score, strength, improvement, comment) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(cycle_id, reviewee_id, reviewer_id, review_type) DO UPDATE SET '
                    'score=excluded.score, strength=excluded.strength, '
                    'improvement=excluded.improvement, comment=excluded.comment, '
                    'created_at=CURRENT_TIMESTAMP',
                    (cycle_id, reviewee_id, uid, 'peer', score, strength, improvement, comment)
                )
                db.commit()
                return redirect(url_for('peer_reviews_page', cycle=cycle_id))
        else:  # upward
            q_scores = []
            for i in range(1, len(questions) + 1):
                try:
                    v = int(request.form.get(f'q{i}', 0))
                except (ValueError, TypeError):
                    v = 0
                q_scores.append(v)
            comment = request.form.get('comment', '').strip() or None
            answered = q_scores
            q_scores = q_scores + [None] * (5 - len(q_scores))
            if any(not (1 <= s <= 5) for s in answered):
                error = '모든 항목에 점수를 선택해주세요.'
            else:
                db.execute(
                    'INSERT INTO peer_reviews '
                    '(cycle_id, reviewee_id, reviewer_id, review_type, '
                    'q1_score, q2_score, q3_score, q4_score, q5_score, comment) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(cycle_id, reviewee_id, reviewer_id, review_type) DO UPDATE SET '
                    'q1_score=excluded.q1_score, q2_score=excluded.q2_score, '
                    'q3_score=excluded.q3_score, q4_score=excluded.q4_score, '
                    'q5_score=excluded.q5_score, comment=excluded.comment, '
                    'created_at=CURRENT_TIMESTAMP',
                    (cycle_id, reviewee_id, uid, 'upward', *q_scores, comment)
                )
                db.commit()
                return redirect(url_for('peer_reviews_page', cycle=cycle_id))

    # 참고 패널 — 평가 대상자의 이번 주기 확정 목표 (R1-B)
    reviewee_goals = db.execute(
        "SELECT title, category, weight, progress FROM performance_goals "
        "WHERE cycle_id=? AND user_id=? AND approval_status='confirmed' "
        "ORDER BY weight DESC",
        (cycle_id, reviewee_id)
    ).fetchall()

    return render_template('performance/peer_write.html',
                           cycle=cycle, reviewee=reviewee,
                           review_type=review_type, existing=existing,
                           upward_questions=questions, peer_prompts=prompts, error=error,
                           reviewee_goals=reviewee_goals,
                           active_page='peer')


# ── P2 다면평가 배정 ──────────────────────────────────────────

def peer_range(db=None):
    """한 사람이 받을 동료평가 인원 (최소, 최대). 기본 3~5명."""
    lo, hi = 3, 5
    try:
        row = (db or get_db()).execute(
            'SELECT peer_min, peer_max FROM company_config WHERE id=1').fetchone()
        if row:
            if row['peer_min'] is not None:
                lo = int(row['peer_min'])
            if row['peer_max'] is not None:
                hi = int(row['peer_max'])
    except Exception:
        pass
    return lo, max(lo, hi)


def _peer_targets(db, cycle_id):
    """P1 평가 명단에서 대상인 사람들."""
    return db.execute(
        'SELECT cp.user_id, cp.reviewer_id, u.name, u.department_id, u.emp_no, '
        '       d.name AS dept, p.name AS position '
        'FROM cycle_participants cp '
        'JOIN users u ON u.id = cp.user_id '
        'LEFT JOIN departments d ON d.id = u.department_id '
        'LEFT JOIN positions   p ON p.id = u.position_id '
        'WHERE cp.cycle_id=? AND cp.included=1 ORDER BY d.name, u.name', (cycle_id,)).fetchall()


def _peer_tier_fn(db):
    """두 사람이 조직상 얼마나 가까운지 — 0 같은 부서, 1 같은 상위 조직, 클수록 멀다."""
    parent = {d['id']: d['parent_id']
              for d in db.execute('SELECT id, parent_id FROM departments')}

    def chain(did):
        out, cur, seen = [], did, set()
        while cur and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = parent.get(cur)
        return out

    cache = {}

    def tier(a, b):
        key = (a, b)
        if key not in cache:
            up, v = set(chain(b)), 9
            for i, node in enumerate(chain(a)):
                if node in up:
                    v = i
                    break
            cache[key] = v
        return cache[key]
    return tier


def _peer_state(db, cycle_id):
    """대상자별 평가자 목록과, 평가자별로 써야 할 건수."""
    rows = db.execute(
        'SELECT pa.id, pa.reviewee_id, pa.reviewer_id, pa.source, '
        '       u.name AS reviewer_name, d.name AS reviewer_dept '
        'FROM peer_assignments pa '
        'JOIN users u ON u.id = pa.reviewer_id '
        'LEFT JOIN departments d ON d.id = u.department_id '
        'WHERE pa.cycle_id=? ORDER BY u.name', (cycle_id,)).fetchall()
    by, load = {}, {}
    for r in rows:
        by.setdefault(r['reviewee_id'], []).append(r)
        load[r['reviewer_id']] = load.get(r['reviewer_id'], 0) + 1
    return by, load


def suggest_peer_assignments(db, cycle):
    """평가자가 모자란 대상자에게 최소 인원까지 채운다.
    같은 부서 → 같은 상위 조직 → 전사 순으로 고르고, 이미 많이 맡은 사람은 뒤로 민다.
    본인과 본인의 평가자(매니저)는 빼고, 평가 대상자 안에서만 고른다."""
    lo, _hi = peer_range(db)
    targets = _peer_targets(db, cycle['id'])
    if not targets:
        return 0
    info = {t['user_id']: t for t in targets}
    by, load = _peer_state(db, cycle['id'])
    have = {uid: {r['reviewer_id'] for r in by.get(uid, [])} for uid in info}
    tier = _peer_tier_fn(db)

    added = 0
    for t in sorted(targets, key=lambda x: (len(have[x['user_id']]), x['user_id'])):
        me   = t['user_id']
        need = lo - len(have[me])
        if need <= 0:
            continue
        mgr   = t['reviewer_id']
        cands = [c for c in info if c != me and c != mgr and c not in have[me]]
        cands.sort(key=lambda c: (tier(t['department_id'], info[c]['department_id']),
                                  load.get(c, 0), c))
        for c in cands[:need]:
            cur = db.execute(
                'INSERT OR IGNORE INTO peer_assignments '
                '(cycle_id, reviewee_id, reviewer_id, source) VALUES (?,?,?,?)',
                (cycle['id'], me, c, 'auto'))
            if cur.rowcount:
                have[me].add(c)
                load[c] = load.get(c, 0) + 1
                added += 1
    db.commit()
    return added


@app.route('/performance/peer/assignments', methods=['GET', 'POST'])
@manager_or_admin
def peer_assignments():
    db       = get_db()
    is_admin = session['user_role'] == 'admin'
    my_dept  = int(session.get('dept_id') or 0)

    cycles = db.execute('SELECT * FROM performance_cycles ORDER BY start_date DESC').fetchall()
    active = next((c for c in cycles if c['status'] == 'active'), None)
    try:
        sel_id = int(request.values.get('cycle') or request.form.get('cycle_id') or 0)
    except (ValueError, TypeError):
        sel_id = 0
    cycle    = next((c for c in cycles if c['id'] == sel_id), active)
    cycle_id = cycle['id'] if cycle else 0

    view = request.values.get('view') or 'all'
    q    = (request.values.get('q') or '').strip()
    edit_id = request.values.get('edit', type=int)

    def _back(**kw):
        args = {'cycle': cycle_id or None, 'view': view, 'q': q or None,
                'edit': edit_id or None}
        args.update(kw)
        return redirect(url_for('peer_assignments', **args))

    lo, hi = peer_range(db)

    if request.method == 'POST':
        action = request.form.get('action', '')
        if not cycle:
            flash('평가 주기를 먼저 고르세요.', 'error')
            return _back()
        if not cycle['include_peer']:
            flash('이 주기는 다면평가를 쓰지 않는 주기입니다.', 'error')
            return _back()
        locked = bool(cycle['peer_locked_at'])

        if action == 'lock':
            if not is_admin:
                abort(403)
            if locked:
                db.execute('UPDATE performance_cycles SET peer_locked_at=NULL WHERE id=?', (cycle_id,))
                db.commit()
                flash('배정 확정을 풀었습니다.', 'success')
                return _back()
            targets = _peer_targets(db, cycle_id)
            if not targets:
                flash('평가 대상이 없습니다. 평가 명단을 먼저 만들어 주세요.', 'error')
                return _back()
            by, _ = _peer_state(db, cycle_id)
            short = [t for t in targets if len(by.get(t['user_id'], [])) < lo]
            if short:
                flash(f'평가자가 {lo}명에 못 미치는 대상 {len(short)}명이 있습니다. 먼저 채워 주세요.', 'error')
                return _back(view='short', edit=None)
            db.execute('UPDATE performance_cycles SET peer_locked_at=CURRENT_TIMESTAMP WHERE id=?',
                       (cycle_id,))
            db.commit()
            flash(f'대상 {len(targets)}명의 다면평가 배정을 확정했습니다.', 'success')
            return _back(edit=None)

        if locked:
            flash('배정이 확정되어 바꿀 수 없습니다.', 'error')
            return _back()

        if action == 'suggest':
            if not is_admin:
                abort(403)
            n = suggest_peer_assignments(db, cycle)
            flash(f'{n}건을 자동으로 배정했습니다.' if n
                  else '더 채울 곳이 없습니다.', 'success')
        elif action == 'clear_auto':
            if not is_admin:
                abort(403)
            n = db.execute("DELETE FROM peer_assignments WHERE cycle_id=? AND source='auto'",
                           (cycle_id,)).rowcount
            db.commit()
            flash(f'자동 배정 {n}건을 지웠습니다. 직접 넣은 배정은 그대로입니다.', 'success')
        elif action == 'add':
            rv  = request.form.get('reviewee_id', type=int) or 0
            rr  = request.form.get('reviewer_id', type=int) or 0
            tgt = db.execute(
                'SELECT cp.user_id, u.department_id FROM cycle_participants cp '
                'JOIN users u ON u.id = cp.user_id '
                'WHERE cp.cycle_id=? AND cp.user_id=? AND cp.included=1', (cycle_id, rv)).fetchone()
            ok = db.execute(
                'SELECT 1 FROM cycle_participants WHERE cycle_id=? AND user_id=? AND included=1',
                (cycle_id, rr)).fetchone()
            if not tgt:
                flash('평가 대상이 아닌 사람입니다.', 'error')
            elif not is_admin and tgt['department_id'] != my_dept:
                flash('내 부서 직원만 배정할 수 있습니다.', 'error')
            elif not rr or rr == rv or not ok:
                flash('평가자를 골라 주세요. 본인과 평가 대상이 아닌 사람은 고를 수 없습니다.', 'error')
            elif db.execute('SELECT COUNT(*) FROM peer_assignments WHERE cycle_id=? AND reviewee_id=?',
                            (cycle_id, rv)).fetchone()[0] >= hi:
                flash(f'한 사람에게 붙일 수 있는 평가자는 {hi}명까지입니다.', 'error')
            else:
                cur = db.execute(
                    'INSERT OR IGNORE INTO peer_assignments '
                    '(cycle_id, reviewee_id, reviewer_id, source) VALUES (?,?,?,?)',
                    (cycle_id, rv, rr, 'manual'))
                db.commit()
                flash('평가자를 넣었습니다.' if cur.rowcount else '이미 배정된 평가자입니다.',
                      'success' if cur.rowcount else 'error')
        elif action == 'remove':
            aid = request.form.get('assign_id', type=int) or 0
            row = db.execute(
                'SELECT pa.id, pa.reviewee_id, pa.reviewer_id, u.department_id '
                'FROM peer_assignments pa JOIN users u ON u.id = pa.reviewee_id '
                'WHERE pa.id=? AND pa.cycle_id=?', (aid, cycle_id)).fetchone()
            written = row and db.execute(
                "SELECT 1 FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND reviewer_id=? "
                "AND review_type='peer'", (cycle_id, row['reviewee_id'], row['reviewer_id'])).fetchone()
            if not row:
                flash('없는 배정입니다.', 'error')
            elif not is_admin and row['department_id'] != my_dept:
                flash('내 부서 직원만 배정할 수 있습니다.', 'error')
            elif written:
                flash('이미 평가를 쓴 사람은 뺄 수 없습니다.', 'error')
            else:
                db.execute('DELETE FROM peer_assignments WHERE id=?', (row['id'],))
                db.commit()
                flash('평가자를 뺐습니다.', 'success')
        else:
            flash('알 수 없는 작업입니다.', 'error')
        return _back()

    # ── 화면 ─────────────────────────────────────────────
    roster_n = db.execute('SELECT COUNT(*) FROM cycle_participants WHERE cycle_id=?',
                          (cycle_id,)).fetchone()[0] if cycle_id else 0
    all_targets = _peer_targets(db, cycle_id) if cycle_id else []
    by, load    = _peer_state(db, cycle_id) if cycle_id else ({}, {})
    written = set()
    if cycle_id:
        written = {(r['reviewee_id'], r['reviewer_id']) for r in db.execute(
            "SELECT reviewee_id, reviewer_id FROM peer_reviews "
            "WHERE cycle_id=? AND review_type='peer'", (cycle_id,))}

    mine = all_targets if is_admin else [t for t in all_targets if t['department_id'] == my_dept]
    rows = []
    for t in mine:
        revs = by.get(t['user_id'], [])
        rows.append({'u': t, 'revs': revs, 'n': len(revs),
                     'state': 'short' if len(revs) < lo else ('over' if len(revs) > hi else 'ok')})

    summary = {
        'total':   len(rows),
        'ok':      sum(1 for r in rows if r['state'] == 'ok'),
        'short':   sum(1 for r in rows if r['state'] == 'short'),
        'over':    sum(1 for r in rows if r['state'] == 'over'),
        'assigns': sum(r['n'] for r in rows),
        'auto':    sum(1 for r in rows for a in r['revs'] if a['source'] == 'auto'),
    }
    shown = [r for r in rows
             if (view == 'all' or r['state'] == view)
             and (not q or q in (r['u']['name'] or '') or q in (r['u']['dept'] or ''))]

    # 한 사람 편집 — 후보를 가까운 조직·적게 맡은 순으로 보여준다
    edit = None
    if edit_id and cycle and not cycle['peer_locked_at']:
        cur = next((r for r in rows if r['u']['user_id'] == edit_id), None)
        if cur:
            tier  = _peer_tier_fn(db)
            taken = {a['reviewer_id'] for a in cur['revs']}
            taken.update({edit_id, cur['u']['reviewer_id']})
            pool  = [c for c in all_targets if c['user_id'] not in taken]
            pool.sort(key=lambda c: (tier(cur['u']['department_id'], c['department_id']),
                                     load.get(c['user_id'], 0), c['name'] or ''))
            edit = {'row': cur, 'top': pool[:8], 'pool': pool}

    # 많이 맡은 평가자 — 한쪽에 몰렸는지 확인용
    top_load = sorted(load.items(), key=lambda kv: -kv[1])[:5]
    names    = {t['user_id']: t['name'] for t in all_targets}
    top_load = [{'name': names.get(u, ''), 'n': n} for u, n in top_load if n > lo]

    return render_template('performance/peer_assignments.html',
                           cycles=cycles, cycle=cycle, cycle_id=cycle_id,
                           rows=shown, summary=summary, view=view, q=q,
                           edit=edit, load=load, written=written,
                           peer_min=lo, peer_max=hi, roster_n=roster_n,
                           top_load=top_load, is_admin=is_admin,
                           active_page='peer_assignments')


# ── Export (Excel 내보내기) ──────────────────────────────────
from export_utils import (make_wb, write_header, write_row, auto_width,
                           freeze_header, to_response, apply_number_format,
                           KRW_FORMAT, NUM_FORMAT)
import urllib.parse


ANALYTICS_AREAS = [('people', '인력'), ('attend', '근태'), ('pay', '보상'), ('exit', '퇴직')]
ANALYTICS_LEVELS = {'late': '위험', 'wait': '주의', 'ref': '참고', 'done': '정상'}


def _analytics_issues(sm, org_rows, grade_gender, leave_util,
                      ot_violations, ot_warnings, outliers, dept_compa_avg):
    """분야별 숫자를 훑어 '확인 필요 항목'을 만든다.

    화면은 이 목록만 그린다 — 판단 기준을 한 곳에 모아 두어야
    나중에 회사마다 기준을 바꿀 때 한 자리만 고치면 된다.
    """
    out = []

    def add(key, area, level, title, sub, facts, bars=None, link=None, note=None):
        out.append({'key': key, 'area': area, 'level': level,
                    'label': ANALYTICS_LEVELS[level], 'title': title, 'sub': sub,
                    'facts': facts, 'bars': bars, 'link': link, 'note': note})

    # ── 연차 사용률 ──
    pct, pace = sm['leave_pct'], sm['leave_pace']
    lv = 'late' if pct < pace - 25 else 'wait' if pct < pace - 10 else 'done'
    add('leave', 'attend', lv, '연차 사용률 %s%%' % pct,
        '전사 · 권장 페이스 %s%%' % pace,
        [('전사 사용률', '%s%%' % pct), ('권장 페이스', '%s%%' % pace),
         ('차이', '%+.1f%%p' % (pct - pace))],
        bars={'title': '연차를 가장 적게 쓴 조직 10곳', 'unit': '%', 'max': 100, 'ref': pace,
              'ref_label': '오늘 기준 권장 페이스 %s%%' % pace,
              'rows': [(r['dept'], r['pct'])
                       for r in sorted(leave_util, key=lambda x: x['pct'])[:10]]},
        link=('근태 · 연차 열기', '/leave/admin'),
        note='권장 페이스는 1–12월을 고르게 쓴다고 보았을 때 오늘까지의 비율입니다.')

    # ── 주 52시간 ──
    nv, nw = len({r['user_id'] for r in ot_violations}), len({r['user_id'] for r in ot_warnings})
    lv = 'late' if nv else 'wait' if nw else 'done'
    add('h52', 'attend', lv,
        '주 52시간 초과 %d명' % nv if nv else ('경고 구간 %d명' % nw if nw else '주 52시간 초과 없음'),
        '최근 8주 · 경고 구간 %d명' % nw,
        [('초과', '%d명' % nv), ('경고 구간', '%d명' % nw), ('기간', '최근 8주')],
        link=('근태 열기', '/attendance/admin'))

    # ── 결원 ──
    gap, target = sm['hc_gap'], sm['hc_target']
    gap_pct = round(gap * 100.0 / target, 1) if target else 0
    lv = 'wait' if target and gap_pct >= 5 else 'ref' if gap else 'done'
    add('vacancy', 'people', lv,
        '정원 대비 결원 %d명' % gap if target else '정원이 아직 없습니다',
        ('정원 %d · 현원 %d' % (target, sm['total_active'])) if target
        else '초기 설정에서 부서별 정원을 넣으면 결원을 봅니다',
        [('정원', '%d명' % target), ('현원', '%d명' % sm['total_active']),
         ('결원', '%d명' % gap), ('12개월 입사', '%d명' % sm['hires_12m'])],
        bars={'title': '조직별 결원 (하위 조직 기준)', 'unit': '명',
              'max': max([r['gap'] for r in org_rows if r['leaf'] and r['gap']] + [1]),
              'rows': sorted([(r['name'], r['gap']) for r in org_rows
                              if r['leaf'] and r['gap'] and r['gap'] > 0],
                             key=lambda x: -x[1])[:10]},
        link=('정원 대장 열기', '/admin/setup'))

    # ── 직급별 여성 비율 ──
    fa = sm['female_pct']
    lows = [g for g in grade_gender if g['f_pct'] is not None and g['f_pct'] <= fa - 15]
    lv = 'wait' if lows else 'ref'
    worst = min(lows, key=lambda g: g['f_pct']) if lows else None
    add('grade_f', 'people', lv,
        ('%s 여성 비율 %d%%' % (worst['grade'], worst['f_pct'])) if worst
        else '직급별 여성 비율 쏠림 없음',
        ('%d / %d명 · 전사 %d%%' % (worst['f'], worst['cnt'], fa)) if worst
        else '전사 %d%% 기준 ±15%%p 안' % fa,
        [('전사 여성 비율', '%d%%' % fa), ('직급 수', '%d개' % len(grade_gender)),
         ('전사 대비 낮은 직급', '%d개' % len(lows))],
        bars={'title': '직급별 여성 비율', 'unit': '%', 'max': 100, 'ref': fa,
              'ref_label': '전사 여성 비율 %d%%' % fa,
              'rows': [(g['grade'], g['f_pct']) for g in grade_gender if g['f_pct'] is not None]},
        note='3명 미만 직급은 개인이 드러나므로 비율을 내지 않습니다.')

    # ── 기준급 대비 낮은 급여 ──
    low_pay = [e for e in outliers if e['compa_ratio'] and e['compa_ratio'] < 0.85]
    lv = 'wait' if low_pay else 'done'
    add('compa', 'pay', lv,
        '기준 대비 85%% 미만 %d명' % len(low_pay) if low_pay else '기준 대비 낮은 급여 없음',
        '전체 이상치 %d명(±15%%)' % len(outliers),
        [('85% 미만', '%d명' % len(low_pay)), ('전체 이상치', '%d명' % len(outliers))],
        link=('보상 분석 열기', '/compensation/analysis'))

    # ── 부서 간 급여 수준 차이 ──
    dept_pay = {d: v for d, v in dept_compa_avg.items() if d != '미지정'}
    if len(dept_pay) >= 2:
        lo = min(dept_pay.items(), key=lambda kv: kv[1])
        hi = max(dept_pay.items(), key=lambda kv: kv[1])
        spread = round((hi[1] - lo[1]) * 100, 1)
        lv = 'wait' if spread >= 15 else 'ref'
        add('dept_pay', 'pay', lv, '부서 간 급여 수준 차이 %s%%p' % spread,
            '%s %d%% · %s %d%%' % (lo[0], round(lo[1] * 100), hi[0], round(hi[1] * 100)),
            [('가장 낮은 부서', '%s %d%%' % (lo[0], round(lo[1] * 100))),
             ('가장 높은 부서', '%s %d%%' % (hi[0], round(hi[1] * 100))),
             ('차이', '%s%%p' % spread)],
            bars={'title': '부서별 평균 Compa-Ratio', 'unit': '%', 'max': 130,
                  'rows': sorted([(d, round(v * 100)) for d, v in dept_pay.items()],
                                 key=lambda x: x[1])},
            link=('보상 분석 열기', '/compensation/analysis'))

    # ── 최근 12개월 퇴사 ──
    ex, tr = sm['exits_12m'], sm['turnover_12m']
    lv = 'ref' if ex == 0 else 'late' if tr >= 15 else 'wait' if tr >= 10 else 'done'
    add('exit12', 'exit', lv,
        '12개월 퇴사 %d명' % ex, '이직률 %s%%' % tr,
        [('12개월 퇴사', '%d명' % ex), ('이직률', '%s%%' % tr),
         ('재직', '%d명' % sm['total_active']), ('12개월 입사', '%d명' % sm['hires_12m'])],
        note='퇴사자가 없으면 퇴직 분석에 쓸 표본도 없습니다.' if ex == 0 else None)

    order = {'late': 0, 'wait': 1, 'ref': 2, 'done': 3}
    out.sort(key=lambda i: order[i['level']])
    return out


@app.route('/analytics')
@admin_required
def people_analytics():
    db    = get_db()
    today = date.today()

    # ── 1. Headcount by department ──────────────────
    dept_headcount = db.execute(
        "SELECT d.name AS dept, COUNT(u.id) AS cnt "
        "FROM departments d LEFT JOIN users u ON u.department_id=d.id AND u.status='active' "
        "GROUP BY d.id, d.name ORDER BY cnt DESC LIMIT 12"
    ).fetchall()

    # ── 2. Headcount by position (grade) ───────────
    grade_headcount = db.execute(
        "SELECT p.name AS grade, COUNT(u.id) AS cnt "
        "FROM positions p LEFT JOIN users u ON u.position_id=p.id AND u.status='active' "
        "GROUP BY p.id, p.name ORDER BY p.level ASC LIMIT 10"
    ).fetchall()

    # ── 3. Monthly turnover (최근 12개월) ───────────
    monthly_turnover = db.execute(
        "SELECT strftime('%Y-%m', termination_date) AS ym, COUNT(*) AS cnt "
        "FROM users WHERE termination_date IS NOT NULL "
        "AND termination_date >= date('now','-12 months') "
        "GROUP BY ym ORDER BY ym ASC"
    ).fetchall()

    # ── 4. Leave utilization by dept (연차 단일 소스 기반 — 하드코딩 15일 제거) ──
    _emps = db.execute(
        "SELECT id, department_id FROM users WHERE status='active' AND department_id IS NOT NULL"
    ).fetchall()
    _dept_pcts = {}
    for e in _emps:
        b = get_leave_balance(db, e['id'])
        if b['total'] > 0:
            _dept_pcts.setdefault(e['department_id'], []).append(b['used'] * 100.0 / b['total'])
    _dept_names = {d['id']: d['name'] for d in db.execute('SELECT id, name FROM departments').fetchall()}
    leave_util = sorted(
        [{'dept': _dept_names.get(k, '—'), 'pct': round(sum(v) / len(v), 1)}
         for k, v in _dept_pcts.items() if v],
        key=lambda x: x['pct'], reverse=True
    )

    # ── 5. Compa-ratio distribution ─────────────────
    # Compa-ratio = 실제 기본급 / 해당 직급·직군 기준 연봉 × 100
    compa_rows = db.execute(
        "SELECT u.name, p.name AS grade, jf.name AS job_family, "
        "  es.base_salary, sg.annual_salary AS grade_salary, "
        "  ROUND(es.base_salary * 12.0 / NULLIF(sg.annual_salary,0) * 100, 1) AS compa_ratio "
        "FROM users u "
        "JOIN employee_salary es ON es.user_id=u.id "
        "JOIN positions p ON p.id=u.position_id "
        "LEFT JOIN job_families jf ON jf.id=u.job_family_id "
        "LEFT JOIN salary_grades sg ON sg.position_id=u.position_id AND sg.job_family_id=u.job_family_id "
        "WHERE u.status='active' AND sg.annual_salary IS NOT NULL "
        "ORDER BY compa_ratio DESC LIMIT 20"
    ).fetchall()

    # ── 7. 핵심 요약 지표 ───────────────────────────
    total_active = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    total_resigned = db.execute("SELECT COUNT(*) FROM users WHERE status='resigned'").fetchone()[0]
    turnover_rate = round(total_resigned / (total_active + total_resigned) * 100, 1) if (total_active + total_resigned) > 0 else 0
    avg_tenure_row = db.execute(
        "SELECT AVG((julianday('now') - julianday(hire_date)) / 365.25) AS avg_tenure "
        "FROM users WHERE status='active' AND hire_date IS NOT NULL"
    ).fetchone()
    avg_tenure = round(avg_tenure_row['avg_tenure'] or 0, 1)
    open_reqs = db.execute("SELECT COUNT(*) FROM job_postings WHERE status='open'").fetchone()[0]

    # export 탭용 추가 데이터
    cycles      = db.execute('SELECT id, name FROM performance_cycles ORDER BY id DESC').fetchall()
    today_year  = today.year
    today_month = today.month

    # ── v0.53 Pay Equity 데이터 ──────────────────────
    pay_equity = get_pay_equity_data(db)
    # 이상치: compa_ratio < 0.85 or > 1.15
    outliers = [e for e in pay_equity if e['compa_ratio'] and
                (e['compa_ratio'] < 0.85 or e['compa_ratio'] > 1.15)]

    # 부서별 평균 Compa-Ratio
    dept_compa = {}
    for e in pay_equity:
        if e['compa_ratio']:
            dept_compa.setdefault(e['dept_name'] or '미지정', []).append(e['compa_ratio'])
    dept_compa_avg = {d: round(sum(v)/len(v), 3) for d, v in dept_compa.items()}

    # 상여 배수 설정
    bonus_configs = {r['grade']: r['bonus_months']
                     for r in db.execute('SELECT grade, bonus_months FROM grade_bonus_config').fetchall()}

    # ── 52h 모니터링 데이터 ───────────────────────────────────────────
    import json as _json
    from datetime import timedelta as _td
    eight_ago = (today - _td(weeks=8)).isoformat()
    ot_rows = db.execute(
        """SELECT u.id AS user_id, u.name, u.emp_no, d.name AS dept_name,
                  date(c.date, '-' || ((cast(strftime('%w', c.date) AS INTEGER) + 6) % 7) || ' days') AS week_start,
                  SUM(c.regular_min + c.overtime_min) AS total_min,
                  SUM(c.overtime_min) AS ot_min
           FROM checkins c JOIN users u ON c.user_id=u.id
           LEFT JOIN departments d ON u.department_id=d.id
           WHERE u.status='active' AND c.date >= ?
           GROUP BY u.id, week_start ORDER BY total_min DESC""",
        (eight_ago,)
    ).fetchall()
    ot_violations, ot_warnings, ot_safe_count = [], [], 0
    for r in ot_rows:
        e = dict(r)
        e['total_h'] = round(e['total_min'] / 60, 1)
        e['over_h']  = round(max(0, e['total_min'] - WEEKLY_TOTAL_MAX) / 60, 1)
        if e['total_min'] > WEEKLY_TOTAL_MAX:
            ot_violations.append(e)
        elif e['total_min'] >= WEEKLY_WARNING:
            ot_warnings.append(e)
        else:
            ot_safe_count += 1
    flagged_ids = {r['user_id'] for r in ot_violations + ot_warnings}
    trend_rows = db.execute(
        """SELECT u.id AS user_id, u.name,
                  date(c.date, '-' || ((cast(strftime('%w', c.date) AS INTEGER) + 6) % 7) || ' days') AS week_start,
                  SUM(c.regular_min + c.overtime_min) AS total_min
           FROM checkins c JOIN users u ON c.user_id=u.id
           WHERE u.status='active' AND c.date >= ?
           GROUP BY u.id, week_start ORDER BY u.id, week_start""",
        ((today - _td(weeks=4)).isoformat(),)
    ).fetchall()
    ot_chart = {}
    for r in trend_rows:
        if r['user_id'] not in flagged_ids:
            continue
        uid = r['user_id']
        if uid not in ot_chart:
            ot_chart[uid] = {'name': r['name'], 'weeks': [], 'hours': []}
        ot_chart[uid]['weeks'].append(r['week_start'])
        ot_chart[uid]['hours'].append(round(r['total_min'] / 60, 1))
    ot_chart_json = _json.dumps(list(ot_chart.values()))

    # ── 퇴직자 분석 데이터 ────────────────────────────────────────────
    # 최근 24개월 완료된 퇴직 요청 기준
    attrition_rows = db.execute(
        "SELECT tr.*, u.name AS emp_name, u.hire_date, u.gender, "
        "  d.name AS dept_name, p.name AS pos_name, "
        "  m.name AS manager_name "
        "FROM termination_requests tr "
        "JOIN users u ON tr.user_id = u.id "
        "LEFT JOIN departments d ON u.department_id = d.id "
        "LEFT JOIN positions p ON u.position_id = p.id "
        "LEFT JOIN users m ON u.manager_id = m.id "
        "WHERE tr.status = 'completed' "
        "AND tr.final_termination_date >= date('now', '-24 months') "
        "ORDER BY tr.final_termination_date DESC"
    ).fetchall()

    # 이탈 원인 분포
    reason_dist = {}
    for r in attrition_rows:
        cat = r['exit_reason_category'] or r['reason_code'] or 'other'
        reason_dist[cat] = reason_dist.get(cat, 0) + 1

    # 아쉬운 퇴직 비율
    total_attrition = len(attrition_rows)
    regrettable_count = sum(1 for r in attrition_rows if r['is_regrettable'])
    regrettable_rate = round(regrettable_count / total_attrition * 100, 1) if total_attrition else 0

    # 재채용 가능 인원
    rehire_count = sum(1 for r in attrition_rows if r['is_rehire_eligible'])

    # 매니저별 이탈 수 (상위 5명)
    mgr_attrition = {}
    for r in attrition_rows:
        mgr = r['manager_name'] or '(매니저 없음)'
        mgr_attrition[mgr] = mgr_attrition.get(mgr, 0) + 1
    mgr_attrition_top = sorted(mgr_attrition.items(), key=lambda x: x[1], reverse=True)[:5]

    # 부서별 이탈 수
    dept_attrition = {}
    for r in attrition_rows:
        dept = r['dept_name'] or '미지정'
        dept_attrition[dept] = dept_attrition.get(dept, 0) + 1

    # 월별 이탈 추이 (최근 12개월, termination_requests 기준)
    monthly_attrition = db.execute(
        "SELECT strftime('%Y-%m', final_termination_date) AS ym, COUNT(*) AS cnt "
        "FROM termination_requests WHERE status='completed' "
        "AND final_termination_date >= date('now', '-12 months') "
        "GROUP BY ym ORDER BY ym ASC"
    ).fetchall()

    # 근속기간별 이탈 분포
    tenure_dist = {'1년 미만': 0, '1-2년': 0, '2-3년': 0, '3-5년': 0, '5년 이상': 0}
    for r in attrition_rows:
        if r['hire_date'] and r['final_termination_date']:
            from datetime import datetime as _dt
            hd = _dt.strptime(r['hire_date'], '%Y-%m-%d').date()
            td = _dt.strptime(r['final_termination_date'], '%Y-%m-%d').date()
            months = (td.year - hd.year) * 12 + (td.month - hd.month)
            if months < 12:       tenure_dist['1년 미만'] += 1
            elif months < 24:     tenure_dist['1-2년'] += 1
            elif months < 36:     tenure_dist['2-3년'] += 1
            elif months < 60:     tenure_dist['3-5년'] += 1
            else:                 tenure_dist['5년 이상'] += 1

    import json as _json2
    attrition_data = {
        'total': total_attrition,
        'regrettable_count': regrettable_count,
        'regrettable_rate': regrettable_rate,
        'rehire_count': rehire_count,
        'reason_dist': reason_dist,
        'mgr_attrition_top': mgr_attrition_top,
        'dept_attrition': dept_attrition,
        'monthly_labels': [r['ym'] for r in monthly_attrition],
        'monthly_counts': [r['cnt'] for r in monthly_attrition],
        'tenure_dist': tenure_dist,
        'reason_labels': EXIT_REASON_CATEGORY_LABEL,
    }


    # ── 8. 조직별 현원·정원(결원) ─────────────────────
    # 정원 대장은 상위 조직(부문·본부)에 하위까지 합친 숫자가 들어 있다.
    # 그래서 현원도 하위 조직까지 굴려서 같은 눈높이로 비교한다.
    _fy = today.year
    _target = {r[0]: r[1] for r in db.execute(
        'SELECT department_id, target_count FROM department_headcount WHERE fiscal_year=?', (_fy,)).fetchall()}
    _deps = db.execute('SELECT id, name, parent_id FROM departments ORDER BY id').fetchall()
    _parent = {d['id']: d['parent_id'] for d in _deps}
    _dname = {d['id']: d['name'] for d in _deps}
    _kids = {}
    for d in _deps:
        _kids.setdefault(d['parent_id'], []).append(d['id'])

    def _chain(did):
        """자기 자신 + 상위 조직들 (순환 방어)."""
        out, seen, cur = [], set(), did
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = _parent.get(cur)
        return out

    _cut12 = (today - timedelta(days=365)).isoformat()
    _hc, _fe, _h12 = {}, {}, {}
    for u in db.execute(
        "SELECT department_id, gender, hire_date FROM users "
        "WHERE status='active' AND department_id IS NOT NULL").fetchall():
        for did in _chain(u['department_id']):
            _hc[did] = _hc.get(did, 0) + 1
            if u['gender'] == 'F':
                _fe[did] = _fe.get(did, 0) + 1
            if u['hire_date'] and u['hire_date'] >= _cut12:
                _h12[did] = _h12.get(did, 0) + 1

    org_rows = []

    def _walk(did, depth):
        hc, t = _hc.get(did, 0), _target.get(did)
        if hc or t:
            org_rows.append({
                'id': did, 'name': _dname.get(did, '—'),
                'depth': depth, 'hc': hc, 'target': t,
                'gap': (t - hc) if t is not None else None,
                'f_pct': round(_fe.get(did, 0) * 100.0 / hc) if hc >= 3 else None,
                'h12': _h12.get(did, 0),
                'leaf': did not in _kids,
            })
        for k in _kids.get(did, []):
            _walk(k, depth + 1)

    for _root in _kids.get(None, []):
        _walk(_root, 0)

    # 상위에 정원이 있으면 하위 정원은 그 안에 이미 포함 → 맨 위 것만 더한다
    hc_target_total = sum(t for did, t in _target.items()
                          if _parent.get(did) not in _target)
    hc_gap_total = max(hc_target_total - total_active, 0) if hc_target_total else 0

    # ── 9. 직급별 성별 구성 ───────────────────────────
    grade_gender = []
    for r in db.execute(
        "SELECT p.name AS grade, p.level, COUNT(u.id) AS cnt, "
        "  SUM(CASE WHEN u.gender='F' THEN 1 ELSE 0 END) AS f "
        "FROM positions p JOIN users u ON u.position_id=p.id AND u.status='active' "
        "GROUP BY p.id ORDER BY p.level ASC").fetchall():
        grade_gender.append({'grade': r['grade'], 'cnt': r['cnt'], 'f': r['f'] or 0,
                             'f_pct': round((r['f'] or 0) * 100.0 / r['cnt']) if r['cnt'] >= 3 else None})
    _f_all = db.execute("SELECT COUNT(*) FROM users WHERE status='active' AND gender='F'").fetchone()[0]
    female_pct = round(_f_all * 100.0 / total_active) if total_active else 0

    # ── 10. 근속·연령 분포 ────────────────────────────
    tenure_buckets = [('1년 미만', 0), ('1–3년', 0), ('3–5년', 0), ('5–10년', 0), ('10년 이상', 0)]
    tenure_dist_active = dict(tenure_buckets)
    _ages = []
    for r in db.execute(
        "SELECT hire_date, birth_date FROM users WHERE status='active'").fetchall():
        if r['hire_date']:
            try:
                y = (today - date.fromisoformat(r['hire_date'])).days / 365.25
            except ValueError:
                y = None
            if y is not None:
                k = ('1년 미만' if y < 1 else '1–3년' if y < 3 else '3–5년' if y < 5
                     else '5–10년' if y < 10 else '10년 이상')
                tenure_dist_active[k] += 1
        if r['birth_date']:
            try:
                _ages.append((today - date.fromisoformat(r['birth_date'])).days / 365.25)
            except ValueError:
                pass
    tenure_dist_active = [(k, tenure_dist_active[k]) for k, _ in tenure_buckets]
    avg_age = round(sum(_ages) / len(_ages), 1) if _ages else None

    # ── 11. 연차 사용률(전사) · 권장 페이스 ────────────
    _lv_tot = _lv_used = 0
    for e in _emps:
        b = get_leave_balance(db, e['id'])
        _lv_tot += b['total']
        _lv_used += b['used']
    leave_pct_all = round(_lv_used * 100.0 / _lv_tot, 1) if _lv_tot else 0.0
    leave_pace = round(today.timetuple().tm_yday * 100.0 / 365)

    # ── 12. 최근 12개월 입·퇴사 ───────────────────────
    hires_12m = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND hire_date >= date('now','-12 months')"
    ).fetchone()[0]
    exits_12m = db.execute(
        "SELECT COUNT(*) FROM users WHERE termination_date IS NOT NULL "
        "AND termination_date >= date('now','-12 months')").fetchone()[0]
    turnover_12m = round(exits_12m * 100.0 / total_active, 1) if total_active else 0.0

    summary = {
        'total_active': total_active, 'hc_target': hc_target_total, 'hc_gap': hc_gap_total,
        'female_pct': female_pct, 'avg_tenure': avg_tenure, 'avg_age': avg_age,
        'hires_12m': hires_12m, 'exits_12m': exits_12m, 'turnover_12m': turnover_12m,
        'leave_pct': leave_pct_all, 'leave_pace': leave_pace,
    }

    issues = _analytics_issues(summary, org_rows, grade_gender, leave_util,
                               ot_violations, ot_warnings, outliers, dept_compa_avg)

    return render_template('analytics/index.html',
        active_page='analytics',
        total_active=total_active, turnover_rate=turnover_rate,
        avg_tenure=avg_tenure, open_reqs=open_reqs,
        dept_headcount=dept_headcount, grade_headcount=grade_headcount,
        monthly_turnover=monthly_turnover, leave_util=leave_util,
        compa_rows=compa_rows, org_rows=org_rows, grade_gender=grade_gender,
        tenure_dist_active=tenure_dist_active, summary=summary, issues=issues,
        cycles=cycles, today_year=today_year, today_month=today_month,
        pay_equity=pay_equity, outliers=outliers, dept_compa_avg=dept_compa_avg,
        bonus_configs=bonus_configs,
        ot_violations=ot_violations, ot_warnings=ot_warnings,
        ot_safe_count=ot_safe_count, ot_chart_json=ot_chart_json,
        attrition_data=attrition_data,
        report_sources=REPORT_SOURCES,
    )


@app.route('/export')
@admin_required
def export_hub():
    db     = get_db()
    cycles = db.execute('SELECT id, name FROM performance_cycles ORDER BY id DESC').fetchall()
    today  = date.today()
    # 은퇴한 구 ATS에 지원자가 남아 있을 때만 '보관 데이터' 칸을 띄운다 (T6)
    try:
        legacy_applicants = db.execute('SELECT COUNT(*) FROM applicants').fetchone()[0]
    except sqlite3.OperationalError:
        legacy_applicants = 0
    return render_template('export/hub.html', active_page='export',
                           cycles=cycles,
                           legacy_applicants=legacy_applicants,
                           today_year=today.year,
                           today_month=today.month)


@app.route('/export/employees')
@admin_required
def export_employees():
    db   = get_db()
    rows = db.execute(
        "SELECT u.id, u.emp_no, u.name, u.email, "
        "       d.name dept, p.name pos, jf.name jf, "
        "       u.employment_type, u.role, u.status, "
        "       u.hire_date, u.birth_date, u.phone, "
        "       u.termination_date, u.termination_reason, "
        "       mgr.name manager_name, "
        "       es.base_salary, "
        "       ROUND((JULIANDAY('now') - JULIANDAY(u.hire_date)) / 365.25, 1) years_of_service, "
        "       cr.final_grade last_grade, "
        "       es.updated_at last_salary_change, "
        "       u.marital_status, u.gender "
        "FROM users u "
        "LEFT JOIN departments d      ON u.department_id = d.id "
        "LEFT JOIN positions   p      ON u.position_id   = p.id "
        "LEFT JOIN job_families jf    ON u.job_family_id = jf.id "
        "LEFT JOIN users mgr          ON u.manager_id    = mgr.id "
        "LEFT JOIN employee_salary es ON u.id = es.user_id "
        "LEFT JOIN calibration_results cr "
        "  ON cr.user_id = u.id "
        "  AND cr.decided_at = ("
        "    SELECT MAX(decided_at) FROM calibration_results WHERE user_id = u.id"
        "  ) "
        "WHERE u.role != 'guest' ORDER BY d.name, u.name"
    ).fetchall()

    # 부양가족 집계
    dep_summary = {}
    for d in db.execute(
        "SELECT user_id, relation, COUNT(*) cnt FROM employee_dependents GROUP BY user_id, relation"
    ).fetchall():
        uid = d['user_id']
        if uid not in dep_summary:
            dep_summary[uid] = {'spouse': 0, 'child': 0, 'parent': 0, 'other': 0, 'total': 0}
        rel = d['relation'] if d['relation'] in ('spouse', 'child', 'parent') else 'other'
        dep_summary[uid][rel] += d['cnt']
        dep_summary[uid]['total'] += d['cnt']

    wb, ws = make_wb("직원 명단")
    headers = [
        '사번', '이름', '이메일', '부서', '직위', '직군',
        '고용형태', '역할', '재직상태',
        '입사일', '생년월일', '연락처',
        '퇴사일', '퇴사사유',
        '직속상관', '기본급(월)', '근속연수(년)',
        '최근성과등급', '최근급여변경일',
        '혼인상태', '성별', '부양가족 합계', '배우자', '자녀', '부모'
    ]
    write_header(ws, headers)

    EMP_TYPE_KO = {'full_time':'정규직','part_time':'시간제','contract':'계약직','intern':'인턴'}
    STATUS_KO   = {'active':'재직','inactive':'휴직','resigned':'퇴직'}
    ROLE_KO     = {'admin':'관리자','manager':'매니저','employee':'직원','recruiter':'채용담당'}
    MARITAL_KO  = {'single':'미혼','married':'기혼','divorced':'이혼','widowed':'사별'}
    GENDER_KO   = {'M':'남','F':'여','other':'기타'}
    am = {i: 'center' for i in range(1, 26)}
    am.update({2:'left', 3:'left', 4:'left', 5:'left', 6:'left', 13:'left', 15:'left'})

    for i, r in enumerate(rows, 2):
        ds = dep_summary.get(r['id'] if 'id' in r.keys() else 0, {})
        write_row(ws, i, [
            r['emp_no'] or f"TC-{r['name']}",
            r['name'], r['email'],
            r['dept'] or '', r['pos'] or '', r['jf'] or '',
            EMP_TYPE_KO.get(r['employment_type'], r['employment_type'] or ''),
            ROLE_KO.get(r['role'], r['role']),
            STATUS_KO.get(r['status'], r['status']),
            r['hire_date'] or '', r['birth_date'] or '', r['phone'] or '',
            r['termination_date'] or '', r['termination_reason'] or '',
            r['manager_name'] or '',
            r['base_salary'] or '',
            r['years_of_service'] or '',
            r['last_grade'] or '',
            (r['last_salary_change'] or '')[:10],
            MARITAL_KO.get(r['marital_status'] if 'marital_status' in r.keys() else '', ''),
            GENDER_KO.get(r['gender'] if 'gender' in r.keys() else '', ''),
            ds.get('total', 0), ds.get('spouse', 0), ds.get('child', 0), ds.get('parent', 0),
        ], align_map=am)

    apply_number_format(ws, 16, 2, len(rows) + 1, KRW_FORMAT)
    auto_width(ws)
    freeze_header(ws)
    fname = urllib.parse.quote("직원명단_Workday형식.xlsx")
    return to_response(wb, fname)


@app.route('/export/payroll')
@admin_required
def export_payroll():
    db    = get_db()
    year  = request.args.get('year',  date.today().year,  type=int)
    month = request.args.get('month', date.today().month, type=int)
    rows  = db.execute(
        'SELECT u.name, d.name dept, p.name pos, '
        '       ps.base_salary, ps.meal_allowance, ps.transport_allowance, ps.overtime_pay, '
        '       ps.gross_pay, ps.national_pension, ps.health_insurance, ps.long_term_care, '
        '       ps.employment_insurance, ps.income_tax, ps.local_income_tax, '
        '       ps.total_deduction, ps.net_pay '
        'FROM payslips ps '
        'JOIN users u ON ps.user_id=u.id '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'LEFT JOIN positions   p ON u.position_id=p.id '
        'WHERE ps.year=? AND ps.month=? ORDER BY d.name, u.name',
        (year, month)
    ).fetchall()

    wb, ws = make_wb(f"{year}년 {month}월 급여")
    headers = ['이름','부서','직위','기본급','식대','교통비','연장근로수당',
               '총지급액','국민연금','건강보험','장기요양','고용보험',
               '소득세','지방소득세','총공제액','실수령액']
    write_header(ws, headers)
    krw_cols = list(range(4, 17))  # 4~16열 통화 포맷
    am = {i: ('right' if i >= 4 else 'left') for i in range(1, 17)}
    totals = [0] * 13

    for i, r in enumerate(rows, 2):
        vals = [r['name'], r['dept'] or '', r['pos'] or '',
                r['base_salary'], r['meal_allowance'], r['transport_allowance'], r['overtime_pay'],
                r['gross_pay'], r['national_pension'], r['health_insurance'], r['long_term_care'],
                r['employment_insurance'], r['income_tax'], r['local_income_tax'],
                r['total_deduction'], r['net_pay']]
        write_row(ws, i, vals, align_map=am)
        for j, v in enumerate(vals[3:], 0):
            totals[j] += (v or 0)

    # 합계 행
    total_row = len(rows) + 2
    write_row(ws, total_row,
              ['합계', '', ''] + totals,
              total=True, align_map=am)

    for col in krw_cols:
        apply_number_format(ws, col, 2, total_row, KRW_FORMAT)
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote(f"{year}년{month}월_급여내역.xlsx")
    return to_response(wb, fname)


@app.route('/export/payroll/annual')
@admin_required
def export_payroll_annual():
    db   = get_db()
    year = request.args.get('year', date.today().year, type=int)
    rows = db.execute(
        'SELECT u.name, d.name dept, '
        '       SUM(ps.gross_pay) gross, SUM(ps.net_pay) net, '
        '       SUM(ps.income_tax) itax, SUM(ps.local_income_tax) ltax, '
        '       SUM(ps.national_pension) pension, '
        '       SUM(ps.health_insurance) health, '
        '       SUM(ps.employment_insurance) emp_ins, '
        '       COUNT(*) months '
        'FROM payslips ps JOIN users u ON ps.user_id=u.id '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'WHERE ps.year=? GROUP BY ps.user_id ORDER BY d.name, u.name',
        (year,)
    ).fetchall()

    wb, ws = make_wb(f"{year}년 연간 급여")
    headers = ['이름','부서','연간총지급액','연간실수령액','소득세','지방소득세',
               '국민연금','건강보험','고용보험','급여지급월수']
    write_header(ws, headers)
    am = {i: ('right' if i >= 3 else 'left') for i in range(1, 11)}
    am[10] = 'center'
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['name'], r['dept'] or '',
            r['gross'], r['net'], r['itax'], r['ltax'],
            r['pension'], r['health'], r['emp_ins'], r['months']
        ], align_map=am)
    for col in range(3, 10):
        apply_number_format(ws, col, 2, len(rows) + 1, KRW_FORMAT)
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote(f"{year}년_연간급여요약.xlsx")
    return to_response(wb, fname)


@app.route('/export/attendance')
@admin_required
def export_attendance():
    db    = get_db()
    year  = request.args.get('year',  date.today().year,  type=int)
    month = request.args.get('month', 0, type=int)  # 0 = 전체

    if month:
        rows = db.execute(
            'SELECT u.name, d.name dept, lr.type, lr.start_date, lr.end_date, '
            '       lr.days, lr.status, lr.reason, lr.created_at '
            'FROM leave_requests lr JOIN users u ON lr.user_id=u.id '
            'LEFT JOIN departments d ON u.department_id=d.id '
            "WHERE strftime('%Y', lr.start_date)=? AND strftime('%m', lr.start_date)=? "
            'ORDER BY d.name, u.name, lr.start_date',
            (str(year), f"{month:02d}")
        ).fetchall()
        sheet_name = f"{year}년 {month}월 근태"
        fname = urllib.parse.quote(f"{year}년{month}월_근태내역.xlsx")
    else:
        rows = db.execute(
            'SELECT u.name, d.name dept, lr.type, lr.start_date, lr.end_date, '
            '       lr.days, lr.status, lr.reason, lr.created_at '
            'FROM leave_requests lr JOIN users u ON lr.user_id=u.id '
            'LEFT JOIN departments d ON u.department_id=d.id '
            "WHERE strftime('%Y', lr.start_date)=? "
            'ORDER BY d.name, u.name, lr.start_date',
            (str(year),)
        ).fetchall()
        sheet_name = f"{year}년 전체 근태"
        fname = urllib.parse.quote(f"{year}년_근태내역.xlsx")

    TYPE_KO   = {'annual':'연차','half_am':'반차(오전)','half_pm':'반차(오후)',
                 'sick':'병가','remote':'재택근무','outing':'외근'}
    STATUS_KO = {'pending':'대기','approved':'승인','rejected':'반려','cancelled':'취소'}

    wb, ws = make_wb(sheet_name)
    headers = ['이름','부서','신청유형','시작일','종료일','일수','상태','사유','신청일시']
    write_header(ws, headers)
    am = {i: ('center' if i in (3,4,5,6,7) else 'left') for i in range(1, 10)}
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['name'], r['dept'] or '',
            TYPE_KO.get(r['type'], r['type']),
            r['start_date'], r['end_date'], r['days'],
            STATUS_KO.get(r['status'], r['status']),
            r['reason'] or '', r['created_at'] or '',
        ], align_map=am)
    auto_width(ws); freeze_header(ws)
    return to_response(wb, fname)


@app.route('/report/builder')
@admin_required
def report_builder():
    return redirect(url_for('people_analytics', tab='report'))


@app.route('/report/preview', methods=['POST'])
@admin_required
def report_preview():
    import json as _json
    data      = request.get_json(force=True)
    fields    = data.get('fields', [])
    filters   = data.get('filters', {})
    if not fields:
        return jsonify({'error': '필드를 1개 이상 선택하세요.'}), 400
    try:
        sql, params, col_labels = build_report_query(fields, filters, limit=200)
        db   = get_db()
        rows = db.execute(sql, params).fetchall()
        return jsonify({
            'columns': col_labels,
            'rows':    [dict(r) for r in rows],
            'total':   len(rows),
            'sql_hint': f"-- {len(rows)}행 반환 (최대 200행 미리보기)" ,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/report/export', methods=['POST'])
@admin_required
def report_export():
    import json as _json
    data    = request.get_json(force=True)
    fields  = data.get('fields', [])
    filters = data.get('filters', {})
    if not fields:
        return jsonify({'error': '필드를 1개 이상 선택하세요.'}), 400
    sql, params, col_labels = build_report_query(fields, filters, limit=None)
    db   = get_db()
    rows = db.execute(sql, params).fetchall()

    wb, ws = make_wb('커스텀 리포트')
    write_header(ws, col_labels)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, list(r))
    auto_width(ws)
    freeze_header(ws)

    import urllib.parse as _up
    fname = _up.quote('HR_커스텀리포트.xlsx')
    return to_response(wb, fname)


@app.route('/export/checkins')
@admin_required
def export_checkins():
    db    = get_db()
    year  = request.args.get('year',  date.today().year,  type=int)
    month = request.args.get('month', date.today().month, type=int)

    rows = db.execute(
        '''SELECT u.emp_no, u.name, d.name AS dept,
                  c.check_in, c.check_out,
                  c.regular_min, c.overtime_min, c.night_min, c.holiday_min, c.break_min,
                  c.attendance_status,
                  ws.name AS schedule_name
           FROM checkins c
           JOIN users u ON c.user_id = u.id
           LEFT JOIN departments d ON u.department_id = d.id
           LEFT JOIN work_schedules ws ON c.schedule_id = ws.id
           WHERE strftime('%Y', c.check_in) = ?
             AND strftime('%m', c.check_in) = ?
           ORDER BY d.name, u.name, c.check_in''',
        (str(year), f"{month:02d}")
    ).fetchall()

    STATUS_KO = {
        'present':'정상', 'late':'지각', 'early_leave':'조퇴',
        'absent':'결근', 'on_leave':'휴가', 'holiday':'공휴일', 'remote':'재택',
    }

    sheet_name = f"{year}년 {month}월 출퇴근"
    fname = urllib.parse.quote(f"{year}년{month}월_출퇴근기록.xlsx")
    wb, ws_sheet = make_wb(sheet_name)
    headers = [
        '사번', '이름', '부서', '출근일시', '퇴근일시',
        '정규(분)', '연장(분)', '야간(분)', '휴일(분)', '휴게(분)',
        '정규(시간)', '연장(시간)', '야간(시간)',
        '출결상태', '근무제',
    ]
    write_header(ws_sheet, headers)
    am = {i: ('center' if i >= 5 else 'left') for i in range(1, 16)}
    for i, r in enumerate(rows, 2):
        reg_h  = round((r['regular_min']  or 0) / 60, 2)
        ot_h   = round((r['overtime_min'] or 0) / 60, 2)
        ngt_h  = round((r['night_min']    or 0) / 60, 2)
        write_row(ws_sheet, i, [
            r['emp_no'] or '', r['name'], r['dept'] or '',
            r['check_in'] or '', r['check_out'] or '',
            r['regular_min'] or 0, r['overtime_min'] or 0,
            r['night_min'] or 0,   r['holiday_min'] or 0,
            r['break_min'] or 0,
            reg_h, ot_h, ngt_h,
            STATUS_KO.get(r['attendance_status'], r['attendance_status'] or ''),
            r['schedule_name'] or '',
        ], align_map=am)
    auto_width(ws_sheet)
    freeze_header(ws_sheet)
    return to_response(wb, fname)


@app.route('/export/performance')
@admin_required
def export_performance():
    db       = get_db()
    cycle_id = request.args.get('cycle_id', type=int)
    cycles   = db.execute('SELECT id, name FROM performance_cycles ORDER BY id DESC').fetchall()

    if not cycle_id and cycles:
        cycle_id = cycles[0]['id']

    rows = db.execute(
        'SELECT u.name, d.name dept, pc.name cycle, '
        '       pg.category, pg.title, pg.weight, pg.progress, '
        '       pg.self_score, pg.self_comment, pg.status '
        'FROM performance_goals pg '
        'JOIN users u ON pg.user_id=u.id '
        'JOIN performance_cycles pc ON pg.cycle_id=pc.id '
        'LEFT JOIN departments d ON u.department_id=d.id '
        'WHERE pg.cycle_id=? ORDER BY d.name, u.name, pg.id',
        (cycle_id,)
    ).fetchall() if cycle_id else []

    cycle_name = next((c['name'] for c in cycles if c['id'] == cycle_id), '전체')
    wb, ws = make_wb(f"성과목표 {cycle_name}")
    headers = ['이름','부서','평가주기','구분','목표제목','가중치(%)','진행률(%)','자기평가점수','자기평가의견','상태']
    write_header(ws, headers)
    am = {i: ('center' if i in (3,4,6,7,8) else 'left') for i in range(1, 11)}
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['name'], r['dept'] or '', r['cycle'],
            r['category'], r['title'], r['weight'], r['progress'],
            r['self_score'] or '', r['self_comment'] or '',
            '완료' if r['status'] == 'completed' else '진행중',
        ], align_map=am)
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote(f"성과목표_{cycle_name}.xlsx")
    return to_response(wb, fname)


@app.route('/export/applicants')
@admin_required
def export_applicants():
    db   = get_db()
    rows = db.execute(
        'SELECT jp.title posting, a.name, a.email, a.phone, a.source, '
        '       a.stage, a.resume_note, a.created_at '
        'FROM applicants a JOIN job_postings jp ON a.posting_id=jp.id '
        'ORDER BY jp.title, a.created_at'
    ).fetchall()

    STAGE_KO = {'applied':'지원','screening':'서류검토','interview1':'1차면접',
                'interview2':'2차면접','final':'최종면접','offered':'처우협의',
                'hired':'채용','rejected':'불합격'}

    wb, ws = make_wb("지원자 현황")
    headers = ['공고명','지원자명','이메일','전화번호','채널','전형단계','이력서 메모','지원일시']
    write_header(ws, headers)
    am = {i: ('center' if i in (6,) else 'left') for i in range(1, 9)}
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['posting'], r['name'], r['email'], r['phone'] or '',
            r['source'] or '', STAGE_KO.get(r['stage'], r['stage']),
            r['resume_note'] or '', r['created_at'] or '',
        ], align_map=am)
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("지원자현황.xlsx")
    return to_response(wb, fname)


# ── 추가 Export 라우트 ───────────────────────────────────────

@app.route('/export/calibration')
@admin_required
def export_calibration():
    db = get_db()
    cycle_id = request.args.get('cycle_id', '')
    where = 'WHERE cr.cycle_id = ?' if cycle_id else ''
    params = (cycle_id,) if cycle_id else ()
    rows = db.execute(
        f'''SELECT pc.name cycle_name,
               u.name emp_name, u.emp_no, d.name dept, p.name position,
               cr.suggested_grade, cr.final_grade, cr.downgrade_reason,
               cr.self_avg, cr.peer_avg, cr.mgr_avg,
               cr.potential_score, cr.retention_risk, cr.loss_impact,
               cr.achievable_level, cr.is_shared, cr.decided_at
        FROM calibration_results cr
        JOIN users u ON u.id = cr.user_id
        JOIN performance_cycles pc ON pc.id = cr.cycle_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN positions p ON p.id = u.position_id
        {where}
        ORDER BY pc.id DESC, d.name, u.name''', params
    ).fetchall()

    wb, ws = make_wb("성과 캘리브레이션")
    headers = ['평가주기','직원명','사번','부서','직위',
               '권고등급','최종등급','하향사유',
               '자기평가평균','다면평가평균','매니저평가평균',
               '잠재력','이탈위험(Retention Risk)','이탈임팩트(Loss Impact)',
               '달성가능레벨','직원공개','확정일']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['cycle_name'], r['emp_name'], r['emp_no'] or '',
            r['dept'] or '', r['position'] or '',
            r['suggested_grade'] or '', r['final_grade'] or '',
            r['downgrade_reason'] or '',
            round(r['self_avg'], 2) if r['self_avg'] else '',
            round(r['peer_avg'], 2) if r['peer_avg'] else '',
            round(r['mgr_avg'], 2) if r['mgr_avg'] else '',
            r['potential_score'] or '', r['retention_risk'] or '',
            r['loss_impact'] or '', r['achievable_level'] or '',
            '공개' if r['is_shared'] else '비공개',
            r['decided_at'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("캘리브레이션결과.xlsx")
    return to_response(wb, fname)


@app.route('/export/salary-history')
@admin_required
def export_salary_history():
    db = get_db()
    year = request.args.get('year', date.today().year)
    rows = db.execute(
        '''SELECT sh.changed_at, u.name emp_name, u.emp_no, d.name dept, p.name position,
               sh.old_base_salary, sh.new_base_salary,
               sh.old_base_salary - sh.new_base_salary change_amt,
               sh.reason, cb.name changed_by_name
        FROM salary_history sh
        JOIN users u ON u.id = sh.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN positions p ON p.id = u.position_id
        LEFT JOIN users cb ON cb.id = sh.changed_by
        WHERE strftime('%Y', sh.changed_at) = ?
        ORDER BY sh.changed_at DESC''', (str(year),)
    ).fetchall()

    wb, ws = make_wb("급여변경이력")
    headers = ['변경일시','직원명','사번','부서','직위',
               '변경전 기본급','변경후 기본급','변경액','변경사유','처리자']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        delta = (r['new_base_salary'] or 0) - (r['old_base_salary'] or 0)
        write_row(ws, i, [
            r['changed_at'] or '', r['emp_name'], r['emp_no'] or '',
            r['dept'] or '', r['position'] or '',
            r['old_base_salary'] or 0, r['new_base_salary'] or 0,
            delta, r['reason'] or '', r['changed_by_name'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote(f"급여변경이력_{year}.xlsx")
    return to_response(wb, fname)


@app.route('/export/performance-reviews')
@admin_required
def export_performance_reviews():
    db = get_db()
    cycle_id = request.args.get('cycle_id', '')
    where = 'WHERE pg.cycle_id = ?' if cycle_id else ''
    params = (cycle_id,) if cycle_id else ()
    rows = db.execute(
        f'''SELECT pc.name cycle_name,
               u.name emp_name, u.emp_no, d.name dept,
               pg.title goal_title, pg.weight,
               pr.score, pr.comment, rv.name reviewer_name,
               pr.created_at
        FROM performance_reviews pr
        JOIN performance_goals pg ON pg.id = pr.goal_id
        JOIN performance_cycles pc ON pc.id = pg.cycle_id
        JOIN users u ON u.id = pg.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN users rv ON rv.id = pr.reviewer_id
        {where}
        ORDER BY pc.id DESC, u.name, pg.id''', params
    ).fetchall()

    wb, ws = make_wb("매니저 평가")
    headers = ['평가주기','직원명','사번','부서','목표명','가중치(%)','점수','코멘트','평가자','평가일시']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['cycle_name'], r['emp_name'], r['emp_no'] or '',
            r['dept'] or '', r['goal_title'] or '',
            r['weight'] or 0, r['score'] or 0,
            r['comment'] or '', r['reviewer_name'] or '',
            r['created_at'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("매니저평가.xlsx")
    return to_response(wb, fname)


@app.route('/export/peer-reviews')
@admin_required
def export_peer_reviews():
    db = get_db()
    cycle_id = request.args.get('cycle_id', '')
    where = 'WHERE pr.cycle_id = ?' if cycle_id else ''
    params = (cycle_id,) if cycle_id else ()
    rows = db.execute(
        f'''SELECT pc.name cycle_name,
               u.name reviewee_name, u.emp_no, d.name dept,
               pr.review_type, pr.score,
               pr.q1_score, pr.q2_score, pr.q3_score, pr.q4_score, pr.q5_score,
               pr.strength, pr.improvement, pr.comment,
               pr.created_at
        FROM peer_reviews pr
        JOIN performance_cycles pc ON pc.id = pr.cycle_id
        JOIN users u ON u.id = pr.reviewee_id
        LEFT JOIN departments d ON d.id = u.department_id
        {where}
        ORDER BY pc.id DESC, u.name''', params
    ).fetchall()

    wb, ws = make_wb("다면평가")
    headers = ['평가주기','피평가자','사번','부서','유형','종합점수',
               'Q1','Q2','Q3','Q4','Q5',
               '잘하는 점(Continue)','개선할 점(Stop)','시작할 점(Start)','일시']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['cycle_name'], r['reviewee_name'], r['emp_no'] or '',
            r['dept'] or '', r['review_type'] or '',
            r['score'] or 0,
            r['q1_score'] or '', r['q2_score'] or '',
            r['q3_score'] or '', r['q4_score'] or '', r['q5_score'] or '',
            r['strength'] or '', r['improvement'] or '',
            r['comment'] or '', r['created_at'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("다면평가.xlsx")
    return to_response(wb, fname)


@app.route('/export/welfare-points')
@admin_required
def export_welfare_points():
    db = get_db()
    year = request.args.get('year', date.today().year)
    rows = db.execute(
        '''SELECT strftime('%Y-%m-%d', wl.created_at) dt,
               u.name emp_name, u.emp_no, d.name dept,
               wl.delta, wl.balance_after, wl.reason
        FROM welfare_point_ledger wl
        JOIN users u ON u.id = wl.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE strftime('%Y', wl.created_at) = ?
        ORDER BY wl.created_at DESC''', (str(year),)
    ).fetchall()

    wb, ws = make_wb("복지포인트이력")
    headers = ['일자','직원명','사번','부서','증감 포인트','잔액','사유']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['dt'] or '', r['emp_name'], r['emp_no'] or '',
            r['dept'] or '', r['delta'] or 0,
            r['balance_after'] or 0, r['reason'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote(f"복지포인트이력_{year}.xlsx")
    return to_response(wb, fname)


@app.route('/export/life-events')
@admin_required
def export_life_events():
    db = get_db()
    rows = db.execute(
        '''SELECT le.event_date, u.name emp_name, u.emp_no, d.name dept,
               le.event_type, le.description,
               cb.name created_by_name, le.created_at
        FROM life_events le
        JOIN users u ON u.id = le.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN users cb ON cb.id = le.created_by
        ORDER BY le.event_date DESC'''
    ).fetchall()

    EVENT_KO = {
        'marriage': '결혼', 'birth': '출산', 'join': '입사',
        'bereavement': '경조사', 'illness': '질병', 'other': '기타',
    }
    wb, ws = make_wb("생애사건이력")
    headers = ['사건일','직원명','사번','부서','사건유형','상세내용','등록자','등록일시']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['event_date'] or '', r['emp_name'], r['emp_no'] or '',
            r['dept'] or '',
            EVENT_KO.get(r['event_type'], r['event_type'] or ''),
            r['description'] or '',
            r['created_by_name'] or '', r['created_at'] or '',
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("생애사건이력.xlsx")
    return to_response(wb, fname)


@app.route('/export/succession')
@admin_required
def export_succession():
    db = get_db()
    rows = db.execute(
        '''SELECT sp.position_title,
               inc.name incumbent_name, inc.emp_no incumbent_no, id.name inc_dept,
               cand.name candidate_name, cand.emp_no candidate_no, cd.name cand_dept,
               sp.readiness, sp.note,
               cb.name created_by_name, sp.created_at
        FROM succession_plans sp
        LEFT JOIN users inc  ON inc.id  = sp.incumbent_id
        LEFT JOIN users cand ON cand.id = sp.candidate_id
        LEFT JOIN departments id ON id.id = inc.department_id
        LEFT JOIN departments cd ON cd.id = cand.department_id
        LEFT JOIN users cb ON cb.id = sp.created_by
        ORDER BY sp.position_title, sp.readiness'''
    ).fetchall()

    READINESS_KO = {'ready_now':'즉시 가능','1_2_years':'1~2년 후','3_5_years':'3~5년 후','unknown':'미정'}
    wb, ws = make_wb("승계계획")
    headers = ['포지션','현직자','현직자 사번','현직자 부서',
               '후보자','후보자 사번','후보자 부서',
               '승계 준비도','메모','등록자','등록일']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['position_title'] or '',
            r['incumbent_name'] or '', r['incumbent_no'] or '', r['inc_dept'] or '',
            r['candidate_name'] or '', r['candidate_no'] or '', r['cand_dept'] or '',
            READINESS_KO.get(r['readiness'], r['readiness'] or ''),
            r['note'] or '', r['created_by_name'] or '',
            (r['created_at'] or '')[:10],
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("승계계획.xlsx")
    return to_response(wb, fname)


@app.route('/export/skills')
@admin_required
def export_skills():
    db = get_db()
    skill_rows = db.execute(
        '''SELECT u.name emp_name, u.emp_no, d.name dept, p.name position,
               es.skill_name, es.level, es.created_at
        FROM employee_skills es
        JOIN users u ON u.id = es.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN positions p ON p.id = u.position_id
        ORDER BY u.name, es.skill_name'''
    ).fetchall()
    cert_rows = db.execute(
        '''SELECT u.name emp_name, u.emp_no, d.name dept, p.name position,
               ec.cert_name, ec.issued_by, ec.issued_date, ec.expiry_date
        FROM employee_certs ec
        JOIN users u ON u.id = ec.user_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN positions p ON p.id = u.position_id
        ORDER BY u.name, ec.cert_name'''
    ).fetchall()

    LEVEL_KO = {'beginner':'입문','intermediate':'중급','advanced':'고급','expert':'전문가'}

    wb, ws = make_wb("스킬목록")
    write_header(ws, ['직원명','사번','부서','직위','스킬명','레벨','등록일'])
    for i, r in enumerate(skill_rows, 2):
        write_row(ws, i, [
            r['emp_name'], r['emp_no'] or '', r['dept'] or '', r['position'] or '',
            r['skill_name'], LEVEL_KO.get(r['level'], r['level'] or ''),
            (r['created_at'] or '')[:10],
        ])
    auto_width(ws); freeze_header(ws)

    ws2 = wb.create_sheet("자격증목록")
    write_header(ws2, ['직원명','사번','부서','직위','자격증명','발급기관','취득일','만료일'])
    for i, r in enumerate(cert_rows, 2):
        write_row(ws2, i, [
            r['emp_name'], r['emp_no'] or '', r['dept'] or '', r['position'] or '',
            r['cert_name'], r['issued_by'] or '',
            r['issued_date'] or '', r['expiry_date'] or '',
        ])
    auto_width(ws2)

    fname = urllib.parse.quote("스킬자격증.xlsx")
    return to_response(wb, fname)


@app.route('/export/contracts')
@admin_required
def export_contracts():
    db = get_db()
    rows = db.execute(
        '''SELECT c.id, ct.name template_name, ct.contract_type,
               u.name emp_name, u.emp_no, d.name dept,
               c.status, c.created_at, c.signed_at
        FROM contracts c
        JOIN users u ON u.id = c.employee_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN contract_templates ct ON ct.id = c.template_id
        ORDER BY c.created_at DESC'''
    ).fetchall()

    STATUS_KO = {'draft':'초안','sent':'발송','signed':'서명완료',
                 'rejected':'거절','cancelled':'취소','expired':'만료'}
    TYPE_KO   = {'employment':'근로계약','nda':'NDA','probation':'수습계약',
                 'freelance':'프리랜서계약','other':'기타'}

    wb, ws = make_wb("전자계약")
    headers = ['계약ID','템플릿','계약유형','직원명','사번','부서',
               '상태','발행일','서명일']
    write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        write_row(ws, i, [
            r['id'], r['template_name'] or '',
            TYPE_KO.get(r['contract_type'], r['contract_type'] or ''),
            r['emp_name'], r['emp_no'] or '', r['dept'] or '',
            STATUS_KO.get(r['status'], r['status'] or ''),
            (r['created_at'] or '')[:10],
            (r['signed_at'] or '')[:10],
        ])
    auto_width(ws); freeze_header(ws)
    fname = urllib.parse.quote("전자계약.xlsx")
    return to_response(wb, fname)


# ── Error Handlers ───────────────────────────────────────────
@app.errorhandler(403)
def forbidden(e):
    return render_template('errors/403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('errors/404.html'), 404



# ── 전자계약 ─────────────────────────────────────────────────

CONTRACT_TYPE_LABELS = {
    'employment': '근로계약서', 'nda': '비밀유지서약서',
    'probation': '수습확인서', 'freelance': '프리랜서 계약서'
}

CONTRACT_DEFAULTS = {
    'employment': '''\
<div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:3px double #000;">
  <div style="font-size:22px;font-weight:900;letter-spacing:10px;color:#111;margin-bottom:4px;">근 로 계 약 서</div>
  <div style="font-size:12px;color:#999;">Labor Contract</div>
</div>

<p style="font-size:14px;line-height:2;margin-bottom:24px;">
  <strong>{{company_name}}</strong>(이하 "사용자"라 한다)와 <strong>{{employee_name}}</strong>(이하 "근로자"라 한다)는 다음과 같이 근로계약을 체결한다.
</p>

<table style="width:100%;border-collapse:collapse;margin-bottom:28px;font-size:13.5px;">
  <tr>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;width:22%;">성&nbsp;&nbsp;&nbsp;명</td>
    <td style="padding:9px 14px;border:1px solid #ccc;width:28%;">{{employee_name}}</td>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;width:22%;">소&nbsp;&nbsp;&nbsp;속</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">{{department}}</td>
  </tr>
  <tr>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;">직&nbsp;&nbsp;&nbsp;위</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">{{position}}</td>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;">입 사 일</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">{{hire_date}}</td>
  </tr>
</table>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제1조 (근무 장소 및 업무 내용)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 근무 장소 : {{company_name}} 사무소 및 회사가 지정하는 장소</p>
    <p style="margin:4px 0;">② 업무 내용 : {{position}} 관련 업무 및 회사가 지시하는 제반 업무</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제2조 (근로 기간)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 근로 개시일 : {{hire_date}}</p>
    <p style="margin:4px 0;">② 계약 기간 : 기간의 정함이 없는 근로계약 (정규직)</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제3조 (근무 시간 및 휴게)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 소정 근로 시간 : 1일 8시간, 주 40시간</p>
    <p style="margin:4px 0;">② 근무 시간 : 09:00 ~ 18:00 (월요일 ~ 금요일)</p>
    <p style="margin:4px 0;">③ 휴게 시간 : 12:00 ~ 13:00 (1시간)</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제4조 (임금)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 월 기본급 : <strong>{{salary}}</strong>원 (세전)</p>
    <p style="margin:4px 0;">② 임금 지급일 : 매월 25일 (휴무일인 경우 전 영업일 지급)</p>
    <p style="margin:4px 0;">③ 지급 방법 : 근로자 명의의 금융계좌에 현금으로 지급</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제5조 (휴일)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 주휴일 : 매주 일요일 (근로기준법 제55조)</p>
    <p style="margin:4px 0;">② 법정 공휴일 : 관공서의 공휴일에 관한 규정에 따른 공휴일 및 대체 공휴일</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제6조 (연차 유급 휴가)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">근로기준법 제60조에 따라 연차 유급 휴가를 부여하며, 미사용 연차에 대해서는 관련 법령에 따라 처리한다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 14px;margin-bottom:10px;">제7조 (기타)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 본 계약에 명시되지 않은 사항은 근로기준법 등 관련 법령 및 회사 취업규칙에 따른다.</p>
    <p style="margin:4px 0;">② 본 계약서는 2부 작성하여 사용자와 근로자가 각 1부씩 보관한다.</p>
  </div>
</div>

<div style="margin-top:48px;padding-top:24px;border-top:2px solid #ddd;display:flex;justify-content:space-around;text-align:center;">
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">사 용 자 (갑)</div>
    <div style="font-size:13px;">{{company_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">대표이사</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">근 로 자 (을)</div>
    <div style="font-size:13px;">{{employee_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">{{position}}</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
</div>''',

    'nda': '''\
<div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:3px double #000;">
  <div style="font-size:22px;font-weight:900;letter-spacing:6px;color:#111;margin-bottom:4px;">비 밀 유 지 서 약 서</div>
  <div style="font-size:12px;color:#999;">Non-Disclosure Agreement</div>
</div>

<p style="font-size:14px;line-height:2;margin-bottom:28px;">
  본인 <strong>{{employee_name}}</strong>(이하 "서약자")은(는) <strong>{{company_name}}</strong>(이하 "회사")에 재직하는 동안 및 퇴직 후에도 아래의 사항을 성실히 이행할 것을 서약한다.
</p>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fdf4ff;border-left:4px solid #7c3aed;padding:8px 14px;margin-bottom:10px;">제1조 (비밀정보의 범위)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">본 서약서에서 "비밀정보"란 다음 각 호에 해당하는 정보를 말한다.</p>
    <p style="margin:4px 0;">① 영업비밀 및 기술 정보, 연구개발 데이터, 특허 출원 전 정보</p>
    <p style="margin:4px 0;">② 고객 정보, 거래처 정보, 계약 조건 및 가격 정보</p>
    <p style="margin:4px 0;">③ 내부 경영 자료, 재무 정보, 인사 정보</p>
    <p style="margin:4px 0;">④ 기타 "대외비" 또는 이와 유사한 표시가 된 일체의 정보</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fdf4ff;border-left:4px solid #7c3aed;padding:8px 14px;margin-bottom:10px;">제2조 (비밀유지 의무)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 서약자는 재직 중 및 퇴직 후 3년간 비밀정보를 제3자에게 누설하거나 공개하지 않는다.</p>
    <p style="margin:4px 0;">② 서약자는 비밀정보를 업무 목적 이외의 용도로 사용하지 않는다.</p>
    <p style="margin:4px 0;">③ 서약자는 회사의 서면 동의 없이 비밀정보를 복사·복제·배포하지 않는다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fdf4ff;border-left:4px solid #7c3aed;padding:8px 14px;margin-bottom:10px;">제3조 (위반 시 책임)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">서약자가 본 서약을 위반하여 회사에 손해를 끼친 경우, 관련 법령에 따라 민·형사상 책임을 진다.</p>
  </div>
</div>

<div style="margin-top:48px;padding-top:24px;border-top:2px solid #ddd;text-align:center;">
  <p style="font-size:13.5px;margin-bottom:32px;">본인은 위 사항을 충분히 이해하고 이를 준수할 것을 서약합니다.</p>
  <div style="display:inline-block;min-width:300px;text-align:left;">
    <div style="font-size:13.5px;margin-bottom:6px;">소속 : {{department}}</div>
    <div style="font-size:13.5px;margin-bottom:6px;">직위 : {{position}}</div>
    <div style="font-size:13.5px;margin-bottom:6px;">성명 : {{employee_name}}</div>
    <div style="font-size:13.5px;margin-bottom:32px;">입사일 : {{hire_date}}</div>
    <div style="border-bottom:1px solid #000;margin-bottom:4px;"></div>
    <div style="font-size:11px;color:#999;text-align:center;">(서명 또는 날인)</div>
  </div>
</div>''',

    'probation': '''\
<div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:3px double #000;">
  <div style="font-size:22px;font-weight:900;letter-spacing:6px;color:#111;margin-bottom:4px;">수 습 근 로 계 약 서</div>
  <div style="font-size:12px;color:#999;">Probationary Employment Contract</div>
</div>

<p style="font-size:14px;line-height:2;margin-bottom:24px;">
  <strong>{{company_name}}</strong>(이하 "사용자"라 한다)와 <strong>{{employee_name}}</strong>(이하 "근로자"라 한다)는 다음과 같이 수습 근로계약을 체결한다.
</p>

<table style="width:100%;border-collapse:collapse;margin-bottom:28px;font-size:13.5px;">
  <tr>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;width:22%;">성&nbsp;&nbsp;&nbsp;명</td>
    <td style="padding:9px 14px;border:1px solid #ccc;width:28%;">{{employee_name}}</td>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;width:22%;">소&nbsp;&nbsp;&nbsp;속</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">{{department}}</td>
  </tr>
  <tr>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;">지원 직위</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">{{position}}</td>
    <td style="padding:9px 14px;border:1px solid #ccc;background:#f5f5f5;font-weight:700;">수습 기간</td>
    <td style="padding:9px 14px;border:1px solid #ccc;">입사일로부터 3개월</td>
  </tr>
</table>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fff7ed;border-left:4px solid #f59e0b;padding:8px 14px;margin-bottom:10px;">제1조 (수습 기간)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 수습 개시일 : {{hire_date}}</p>
    <p style="margin:4px 0;">② 수습 기간 : {{hire_date}}로부터 3개월</p>
    <p style="margin:4px 0;">③ 수습 기간 만료 후 회사의 평가에 따라 정규직 전환 여부를 결정한다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fff7ed;border-left:4px solid #f59e0b;padding:8px 14px;margin-bottom:10px;">제2조 (근무 시간)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 소정 근로 시간 : 1일 8시간, 주 40시간</p>
    <p style="margin:4px 0;">② 근무 시간 : 09:00 ~ 18:00 (월~금), 휴게 12:00 ~ 13:00</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fff7ed;border-left:4px solid #f59e0b;padding:8px 14px;margin-bottom:10px;">제3조 (임금)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 수습 기간 중 월 기본급 : <strong>{{salary}}</strong>원 (세전)</p>
    <p style="margin:4px 0;">② 수습 기간 중 임금은 근로기준법이 허용하는 범위 내에서 적용한다.</p>
    <p style="margin:4px 0;">③ 임금 지급일 : 매월 25일</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fff7ed;border-left:4px solid #f59e0b;padding:8px 14px;margin-bottom:10px;">제4조 (휴일 및 연차)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 주휴일 : 매주 일요일, 법정 공휴일 부여 (근로기준법 제55조)</p>
    <p style="margin:4px 0;">② 연차 유급 휴가 : 근로기준법 제60조에 따라 부여한다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#fff7ed;border-left:4px solid #f59e0b;padding:8px 14px;margin-bottom:10px;">제5조 (계약 해지)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 사용자는 수습 기간 중 근로자의 업무 능력·태도·적응력 등을 평가하여 정규직 전환 여부를 결정한다.</p>
    <p style="margin:4px 0;">② 수습 기간 만료 시 별도 통보 없이 정규직으로 전환된다. 단, 평가 결과 부적합 판정 시 계약을 해지할 수 있다.</p>
  </div>
</div>

<div style="margin-top:48px;padding-top:24px;border-top:2px solid #ddd;display:flex;justify-content:space-around;text-align:center;">
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">사 용 자 (갑)</div>
    <div style="font-size:13px;">{{company_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">대표이사</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">근 로 자 (을)</div>
    <div style="font-size:13px;">{{employee_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">{{position}}</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
</div>''',

    'freelance': '''\
<div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:3px double #000;">
  <div style="font-size:22px;font-weight:900;letter-spacing:6px;color:#111;margin-bottom:4px;">프 리 랜 서 계 약 서</div>
  <div style="font-size:12px;color:#999;">Freelance Service Agreement</div>
</div>

<p style="font-size:14px;line-height:2;margin-bottom:24px;">
  <strong>{{company_name}}</strong>(이하 "발주자"라 한다)와 <strong>{{employee_name}}</strong>(이하 "수급자"라 한다)는 다음과 같이 용역 계약을 체결한다.
</p>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#f0fdf4;border-left:4px solid #16a34a;padding:8px 14px;margin-bottom:10px;">제1조 (용역 내용)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 용역 내용 : {{position}} 관련 업무</p>
    <p style="margin:4px 0;">② 납품 방법 : 발주자가 지정하는 방법으로 납품</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#f0fdf4;border-left:4px solid #16a34a;padding:8px 14px;margin-bottom:10px;">제2조 (계약 기간)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 계약 개시일 : {{hire_date}}</p>
    <p style="margin:4px 0;">② 계약 종료일 : 별도 협의에 따름</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#f0fdf4;border-left:4px solid #16a34a;padding:8px 14px;margin-bottom:10px;">제3조 (용역 대가)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">① 월 용역 대가 : <strong>{{salary}}</strong>원 (부가가치세 별도)</p>
    <p style="margin:4px 0;">② 지급 방법 : 세금계산서 발행 후 30일 이내 계좌 이체</p>
    <p style="margin:4px 0;">③ 수급자는 용역 대가에 대한 세금 신고 및 납부 의무를 직접 부담한다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#f0fdf4;border-left:4px solid #16a34a;padding:8px 14px;margin-bottom:10px;">제4조 (지식재산권)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">본 계약에 따라 수급자가 제작·개발한 결과물의 저작권 및 지식재산권은 발주자에게 귀속된다.</p>
  </div>
</div>

<div style="margin-bottom:20px;">
  <div style="font-size:14px;font-weight:700;background:#f0fdf4;border-left:4px solid #16a34a;padding:8px 14px;margin-bottom:10px;">제5조 (비밀유지)</div>
  <div style="font-size:13.5px;line-height:1.9;padding:0 6px;">
    <p style="margin:4px 0;">수급자는 계약 이행 중 취득한 발주자의 영업비밀 및 기밀정보를 제3자에게 누설하지 않으며, 계약 종료 후에도 동일하게 적용된다.</p>
  </div>
</div>

<div style="margin-top:48px;padding-top:24px;border-top:2px solid #ddd;display:flex;justify-content:space-around;text-align:center;">
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">발 주 자 (갑)</div>
    <div style="font-size:13px;">{{company_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">대표이사</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
  <div style="width:42%;">
    <div style="font-size:13px;font-weight:700;margin-bottom:6px;">수 급 자 (을)</div>
    <div style="font-size:13px;">{{employee_name}}</div>
    <div style="font-size:13px;color:#666;margin-bottom:40px;">{{position}}</div>
    <div style="border-bottom:1px solid #000;margin:0 auto 4px;width:90%;"></div>
    <div style="font-size:11px;color:#999;">(서명 또는 날인)</div>
  </div>
</div>''',
}


# ── T5: 입사 확정 뒤 자동 점화 ──────────────────────────────────────────
# 오퍼를 수락한 사람이 직원으로 전환되는 순간, 담당자가 다시 손대지 않아도
# 계약서가 나가고 온보딩이 켜지고 담당자가 그 사실을 안다.
# 이 한 칸이 비어 있으면 "채용요청 결재부터 온보딩까지 사람 손 없이 한 바퀴"라는
# V1 약속이 마지막 걸음에서 끊긴다.

def _auto_contract_enabled(db):
    """자동 발송 스위치. 기본은 켜짐 — 끄려면 설정 → Hire 연동에서 끈다."""
    row = db.execute(
        "SELECT value FROM company_settings WHERE key='auto_contract'").fetchone()
    return (row['value'] if row else '1') != '0'


def _default_employment_template(db, created_by):
    """표준 근로계약서 템플릿을 한 장 보장한다. 없으면 기본 서식으로 만들어 둔다."""
    row = db.execute(
        "SELECT id, content_html FROM contract_templates "
        "WHERE contract_type='employment' ORDER BY id LIMIT 1").fetchone()
    if row:
        return row['id'], row['content_html']
    content = CONTRACT_DEFAULTS.get('employment', '')
    if not content:
        return None, None
    cur = db.execute(
        "INSERT INTO contract_templates (name, contract_type, content_html, created_by) "
        "VALUES (?,?,?,?)", ('표준 근로계약서', 'employment', content, created_by))
    return cur.lastrowid, content


def _fill_contract_vars(db, emp_id, content):
    """{{변수}}를 그 사람의 실제 값으로 바꾼다. /contracts/issue 와 같은 표를 쓴다."""
    emp = db.execute(
        "SELECT u.name, u.hire_date, es.base_salary, d.name AS dept, p.name AS pos "
        "FROM users u "
        "LEFT JOIN departments d ON d.id=u.department_id "
        "LEFT JOIN positions  p ON p.id=u.position_id "
        "LEFT JOIN employee_salary es ON es.user_id=u.id "
        "WHERE u.id=?", (emp_id,)).fetchone()
    if not emp:
        return content, None
    company = get_company_info()
    subst = {
        '{{employee_name}}':   emp['name'] or '',
        '{{department}}':      emp['dept'] or '',
        '{{position}}':        emp['pos'] or '',
        '{{hire_date}}':       emp['hire_date'] or '',
        '{{start_date}}':      emp['hire_date'] or '',
        '{{salary}}':          f"{int(emp['base_salary']):,}" if emp['base_salary'] else '0',
        '{{company_name}}':    company.get('name', ''),
        '{{company_address}}': company.get('address', ''),
    }
    for var, val in subst.items():
        content = content.replace(var, val)
    return content, emp


def ignite_after_hire(db, emp_id, issuer_id, opening_code=None):
    """입사 확정 직후 자동으로 켜지는 것들 — 계약서 · 온보딩 · 담당자 알림.

    무엇이 실제로 켜졌는지 한국어 목록으로 돌려준다(그대로 화면에 보여주려고).
    이미 되어 있는 건 건드리지 않는다 — 두 번 눌러도 계약서가 두 장 나가지 않는다.
    """
    fired = []

    # ① 근로계약서 — 이미 한 장이라도 받은 사람은 건너뛴다
    if _auto_contract_enabled(db):
        already = db.execute(
            'SELECT 1 FROM contracts WHERE employee_id=?', (emp_id,)).fetchone()
        if not already:
            tpl_id, content = _default_employment_template(db, issuer_id)
            if content:
                content, emp = _fill_contract_vars(db, emp_id, content)
                if emp:
                    title = f"{emp['name']} 근로계약서"
                    db.execute(
                        'INSERT INTO contracts (template_id, employee_id, issued_by, '
                        'title, content_html) VALUES (?,?,?,?,?)',
                        (tpl_id, emp_id, issuer_id, title, content))
                    db.execute(
                        'INSERT INTO notifications (user_id, type, category, title, content, link) '
                        'VALUES (?,?,?,?,?,?)',
                        (emp_id, 'action', 'contract', f'서명 요청 — {title}',
                         '입사가 확정되어 근로계약서가 발송되었습니다. 확인 후 서명해 주세요.',
                         url_for('contracts_list')))
                    fired.append('근로계약서 발송')

    # ② 온보딩 체크리스트 — 외부 연동이 꺼져 있거나 실패해도 반드시 깔린다.
    #    보통은 on_employee_created 가 이미 깔아 두었다. 여기는 그물이다.
    have = db.execute(
        'SELECT COUNT(*) c FROM onboarding_progress WHERE user_id=?', (emp_id,)).fetchone()['c']
    db.commit()          # 아래 시딩은 별도 연결로 같은 파일을 연다 — 먼저 잠금을 푼다
    if not have:
        try:
            from integrations.dispatcher import _seed_onboarding_tasks
            _seed_onboarding_tasks(get_tenant_db_path(session.get('tenant_id', 1)), emp_id)
            have = db.execute(
                'SELECT COUNT(*) c FROM onboarding_progress WHERE user_id=?',
                (emp_id,)).fetchone()['c']
        except Exception as e:
            app.logger.warning(f'T5 onboarding seed failed for {emp_id}: {e}')
    if have:
        fired.append('온보딩 체크리스트 시작')

    # ③ 담당자에게 "한 바퀴가 여기서 닫혔다"고 알린다.
    #    자동 발송을 꺼 두었으면 계약서가 아직 안 나갔다는 사실 자체가 알려야 할 일이다.
    emp = db.execute('SELECT name, hire_date FROM users WHERE id=?', (emp_id,)).fetchone()
    if emp:
        seat = f' · 포지션 {opening_code}' if opening_code else ''
        done = ' · '.join(fired) if fired else '포지션 배정'
        body = f"{emp['name']}님 입사가 확정되어 {done}까지 처리했습니다."
        if emp['hire_date']:
            body += f" 입사 예정일 {emp['hire_date']}."
        if '근로계약서 발송' not in fired:
            body += ' 근로계약서는 아직 나가지 않았습니다 — 계약서 화면에서 발행해 주세요.'
        for a in db.execute(
                "SELECT id FROM users WHERE role='admin' AND status='active'").fetchall():
            if a['id'] == issuer_id:
                continue
            db.execute(
                'INSERT INTO notifications (user_id, type, category, title, content, link) '
                'VALUES (?,?,?,?,?,?)',
                (a['id'], 'info', 'action', f"신규 입사자 확정 — {emp['name']}{seat}",
                 body, url_for('contracts_list')))
        db.commit()

    return fired

@app.route('/contracts')
@login_required
def contracts_list():
    db   = get_db()
    uid  = session['user_id']
    role = session['user_role']
    if role in ('admin', 'manager'):
        contracts = db.execute(
            "SELECT c.*, u.name AS emp_name, i.name AS issuer_name "
            "FROM contracts c JOIN users u ON u.id=c.employee_id "
            "JOIN users i ON i.id=c.issued_by ORDER BY c.created_at DESC LIMIT 50"
        ).fetchall()
    else:
        contracts = db.execute(
            "SELECT c.*, u.name AS emp_name, i.name AS issuer_name "
            "FROM contracts c JOIN users u ON u.id=c.employee_id "
            "JOIN users i ON i.id=c.issued_by WHERE c.employee_id=? ORDER BY c.created_at DESC",
            (uid,)
        ).fetchall()
    templates = db.execute("SELECT id, name, contract_type, created_at FROM contract_templates ORDER BY created_at DESC").fetchall()
    # 계약서를 아직 한 장도 못 받은 신규 입사자.
    # 채용(Hire)에서 넘어온 사람은 직원으로 전환되는 순간 계약서가 자동으로 나가므로(T5)
    # 보통 여기 뜨지 않는다. 여기 남는 사람은 셋 중 하나다 —
    # 자동 발송을 꺼 두었거나, 채용을 거치지 않고 직접 등록했거나, 자동 발송이 실패했거나.
    # 어느 쪽이든 본인은 서명할 문서가 없어 아무것도 못 하므로 담당자에게 보이게 한다.
    pending_new = []
    if role in ('admin', 'manager'):
        pending_new = db.execute(
            "SELECT u.id, u.name, u.hire_date, d.name AS dept_name "
            "FROM users u "
            "LEFT JOIN departments d ON d.id = u.department_id "
            "WHERE u.status='active' AND u.role='employee' "
            "  AND u.hire_date >= date('now', '-90 days') "
            "  AND NOT EXISTS (SELECT 1 FROM contracts c WHERE c.employee_id = u.id) "
            "ORDER BY u.hire_date DESC, u.id DESC LIMIT 20"
        ).fetchall()
    return render_template('contracts/list.html',
        contracts=contracts, templates=templates, pending_new=pending_new,
        type_labels=CONTRACT_TYPE_LABELS, active_page='contracts')


@app.route('/contracts/templates/new', methods=['GET', 'POST'])
@admin_required
def contract_template_new():
    db = get_db()
    if request.method == 'POST':
        name    = request.form['name'].strip()
        ctype   = request.form.get('contract_type', 'employment')
        content = request.form.get('content_html', '').strip()
        if not name or not content:
            flash('이름과 내용을 입력해주세요.', 'error')
        else:
            db.execute(
                "INSERT INTO contract_templates (name, contract_type, content_html, created_by) VALUES (?,?,?,?)",
                (name, ctype, content, session['user_id'])
            )
            db.commit()
            flash('템플릿이 저장되었습니다.', 'success')
            return redirect(url_for('contracts_list'))
    default_type = request.args.get('type', 'employment')
    default_content = CONTRACT_DEFAULTS.get(default_type, '')
    return render_template('contracts/template_form.html',
        type_labels=CONTRACT_TYPE_LABELS, default_type=default_type,
        default_content=default_content,
        contract_defaults=CONTRACT_DEFAULTS, active_page='contracts')


@app.route('/contracts/issue', methods=['GET', 'POST'])
@admin_required
def contract_issue():
    db = get_db()
    if request.method == 'POST':
        emp_id      = int(request.form['employee_id'])
        template_id = request.form.get('template_id') or None
        title       = request.form.get('title', '').strip()
        content     = request.form.get('content_html', '').strip()
        if not title or not content:
            flash('제목과 내용을 입력해주세요.', 'error')
        else:
            # Variable substitution: replace {{var}} placeholders with real data
            emp_data = db.execute(
                "SELECT u.name, u.hire_date, es.base_salary, "
                "d.name AS dept, p.name AS pos "
                "FROM users u "
                "LEFT JOIN departments d ON d.id=u.department_id "
                "LEFT JOIN positions p ON p.id=u.position_id "
                "LEFT JOIN employee_salary es ON es.user_id=u.id "
                "WHERE u.id=?", (emp_id,)
            ).fetchone()
            company = get_company_info()
            if emp_data:
                subst = {
                    '{{employee_name}}': emp_data['name'] or '',
                    '{{department}}':    emp_data['dept'] or '',
                    '{{position}}':      emp_data['pos'] or '',
                    '{{hire_date}}':     emp_data['hire_date'] or '',
                    '{{start_date}}':    emp_data['hire_date'] or '',
                    '{{salary}}':        f"{int(emp_data['base_salary']):,}" if emp_data['base_salary'] else '0',
                    '{{company_name}}':  company.get('name', ''),
                    '{{company_address}}': company.get('address', ''),
                }
                for var, val in subst.items():
                    content = content.replace(var, val)
            db.execute(
                "INSERT INTO contracts (template_id, employee_id, issued_by, title, content_html) VALUES (?,?,?,?,?)",
                (template_id, emp_id, session['user_id'], title, content)
            )
            db.commit()
            add_notification(emp_id, 'action', 'contract',
                f"서명 요청 — {title}",
                '계약서 서명을 요청받았습니다. 확인 후 서명해 주세요.',
                url_for('contracts_list'))
            # Slack DM: 직원에게 서명 요청
            _emp_row = db.execute('SELECT email, name FROM users WHERE id=?', (emp_id,)).fetchone()
            if _emp_row and _emp_row['email']:
                from integrations.dispatcher import notify_slack
                notify_slack(
                    _emp_row['email'],
                    f"[TalentCore] 계약서 서명 요청\n"
                    f"'{title}' 계약서 서명 요청이 도착했습니다.\n"
                    f"TalentCore > 계약서에서 확인 후 서명해주세요.",
                    '계약서 서명 요청',
                    name=_emp_row['name']
                )
            flash('계약서가 발송되었습니다.', 'success')
            return redirect(url_for('contracts_list'))
    employees = db.execute(
        "SELECT u.id, u.name, d.name AS dept "
        "FROM users u LEFT JOIN departments d ON d.id=u.department_id "
        "WHERE u.status='active' AND u.role='employee' ORDER BY u.name"
    ).fetchall()
    templates = db.execute("SELECT id, name, contract_type, content_html FROM contract_templates ORDER BY created_at DESC").fetchall()
    company   = get_company_info()
    return render_template('contracts/issue.html',
        employees=employees, templates=templates,
        type_labels=CONTRACT_TYPE_LABELS, company=company, active_page='contracts')


@app.route('/contracts/<int:cid>')
@login_required
def contract_view(cid):
    db  = get_db()
    uid = session['user_id']
    c   = db.execute(
        "SELECT ct.*, u.name AS emp_name, u.hire_date, u.email AS emp_email, "
        "i.name AS issuer_name, p.name AS position "
        "FROM contracts ct JOIN users u ON u.id=ct.employee_id "
        "JOIN users i ON i.id=ct.issued_by "
        "LEFT JOIN positions p ON p.id=u.position_id "
        "WHERE ct.id=?", (cid,)
    ).fetchone()
    if not c:
        abort(404)
    if uid != c['employee_id'] and session['user_role'] not in ('admin', 'manager'):
        abort(403)
    return render_template('contracts/view.html', contract=c,
        is_recipient=(uid == c['employee_id']),
        is_issuer=(uid == c['issued_by']),
        type_labels=CONTRACT_TYPE_LABELS, active_page='contracts')


@app.route('/contracts/<int:cid>/sign', methods=['POST'])
@login_required
def contract_sign(cid):
    db  = get_db()
    uid = session['user_id']
    c   = db.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c or c['employee_id'] != uid:
        abort(403)
    if c['status'] != 'pending':
        flash('이미 처리된 계약서입니다.', 'error')
        return redirect(url_for('contract_view', cid=cid))
    sign_ip = request.remote_addr
    db.execute(
        "UPDATE contracts SET status='signed', signed_at=datetime('now'), sign_ip=? WHERE id=?",
        (sign_ip, cid)
    )
    db.commit()
    if c['comp_review_id']:
        _apply_due_comp_reviews(db)   # 상여 지급 예약 + 적용일 도래 시 연봉 반영
    # 발급자에게 알림
    add_notification(c['issued_by'], 'info', 'contract',
        f"계약서 서명 완료 — {c['title']}",
        f"{session.get('user_name', '직원')}님이 서명했습니다.",
        url_for('contracts_list'))
    # Slack DM: 발급자에게 서명 완료 알림
    _issuer_row = db.execute('SELECT email, name FROM users WHERE id=?', (c['issued_by'],)).fetchone()
    if _issuer_row and _issuer_row['email']:
        from integrations.dispatcher import notify_slack
        notify_slack(
            _issuer_row['email'],
            f"[TalentCore] 계약서 서명 완료\n"
            f"'{c['title']}' 계약서에 {session.get('user_name', '직원')}님이 서명했습니다.\n"
            f"TalentCore > 계약서에서 확인하세요.",
            '계약서 서명 완료',
            name=_issuer_row['name']
        )
    flash('서명이 완료되었습니다.', 'success')
    return redirect(url_for('contract_view', cid=cid))


@app.route('/contracts/<int:cid>/reject', methods=['POST'])
@login_required
def contract_reject(cid):
    db  = get_db()
    uid = session['user_id']
    c   = db.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c or c['employee_id'] != uid:
        abort(403)
    if c['status'] != 'pending':
        flash('이미 처리된 계약서입니다.', 'error')
        return redirect(url_for('contract_view', cid=cid))
    reason = (request.form.get('reject_reason') or request.form.get('reason') or '').strip()
    db.execute(
        "UPDATE contracts SET status='rejected', reject_reason=? WHERE id=?",
        (reason, cid)
    )
    db.commit()
    add_notification(c['issued_by'], 'info', 'contract',
        f"계약서 서명 거절 — {c['title']}",
        f"{session.get('user_name', '직원')}님이 거절했습니다. 사유: {reason or '미기재'}",
        url_for('contracts_list'))
    flash('계약서를 거절했습니다.', 'success')
    return redirect(url_for('contracts_list'))


@app.route('/admin/holidays', methods=['GET', 'POST'])
@admin_required
def admin_holidays():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            hdate = request.form.get('date', '').strip()
            hname = request.form.get('name', '').strip()
            if hdate and hname:
                year = int(hdate[:4])
                try:
                    db.execute('INSERT OR IGNORE INTO public_holidays (date, name, year) VALUES (?,?,?)', (hdate, hname, year))
                    db.commit()
                    flash('공휴일이 추가되었습니다.', 'success')
                except Exception:
                    flash('이미 등록된 날짜입니다.', 'error')
        elif action == 'delete':
            hid = int(request.form.get('id', 0))
            db.execute('DELETE FROM public_holidays WHERE id=?', (hid,))
            db.commit()
            flash('삭제되었습니다.', 'success')
        return redirect(url_for('admin_holidays'))
    year = int(request.args.get('year', 2026))
    holidays = db.execute(
        'SELECT * FROM public_holidays WHERE year=? ORDER BY date', (year,)
    ).fetchall()
    return render_template('admin/holidays.html', holidays=holidays, year=year, active_page='holidays')


# ══════════════════════════════════════════════════════════════
#  연차사용촉진 (Phase B-8, 근로기준법 §61)
# ══════════════════════════════════════════════════════════════

@app.route('/admin/leave-promotion', methods=['GET', 'POST'])
@admin_required
def admin_leave_promotion():
    db   = get_db()
    year = date.today().year

    if request.method == 'POST':
        round_no = int(request.form.get('round_no', 1))
        user_ids = [int(x) for x in request.form.getlist('user_id')]
        if round_no not in (1, 2) or not user_ids:
            flash('발송 대상을 선택해주세요.', 'warning')
            return redirect(url_for('admin_leave_promotion'))

        from integrations.email_sender import send_leave_promotion_email
        sent = 0
        for uid in user_ids:
            u = db.execute("SELECT * FROM users WHERE id=? AND status='active'", (uid,)).fetchone()
            if not u:
                continue
            # 법적 증빙 문서 — 반드시 단일 소스로 계산 (반차·이월·병가 정책 포함)
            remain = get_leave_balance(db, uid, year=year)['remaining']
            if remain <= 0:
                continue
            db.execute(
                'INSERT INTO leave_promotion_logs (user_id, year, round_no, remain_days, sent_by) '
                'VALUES (?,?,?,?,?)', (uid, year, round_no, remain, session['user_id'])
            )
            add_notification(
                uid, 'action', 'leave',
                f'{year}년 연차사용촉진 통보 ({round_no}차)',
                (f'잔여 연차 {remain}일 — 사용 계획을 10일 이내 제출해주세요. 미사용 연차는 소멸될 수 있습니다.'
                 if round_no == 1 else
                 f'잔여 연차 {remain}일의 사용 시기가 회사 지정으로 통보되었습니다. 인사팀 안내를 확인하세요.'),
                url_for('attendance_home', tab='leaves')
            )
            try:
                send_leave_promotion_email(dict(u), remain, round_no, year)
            except Exception as e:
                app.logger.warning(f'leave promotion email failed: {e}')
            sent += 1
        db.commit()
        log_audit('create', 'personal_info', None, f'연차촉진 {round_no}차 통보 발송 — {sent}명 ({year}년)')
        flash(f'{round_no}차 촉진 통보 {sent}건 발송 완료 (인앱 알림 + 이메일).', 'success')
        return redirect(url_for('admin_leave_promotion'))

    # GET — 잔여 연차 현황 + 발송 이력
    rows = []
    for u in db.execute(
        "SELECT id, name, email, hire_date, department_id FROM users "
        "WHERE status='active' AND role != 'guest' ORDER BY name"
    ).fetchall():
        _bal   = get_leave_balance(db, u['id'], year=year)
        total  = _bal['total']
        used   = _bal['used']
        remain = _bal['remaining']
        if remain <= 0:
            continue
        logs = db.execute(
            'SELECT round_no, sent_at FROM leave_promotion_logs WHERE user_id=? AND year=? ORDER BY round_no',
            (u['id'], year)
        ).fetchall()
        rows.append({
            'id': u['id'], 'name': u['name'], 'hire_date': u['hire_date'],
            'total': total, 'used': used, 'remain': remain,
            'r1_sent': next((l['sent_at'] for l in logs if l['round_no'] == 1), None),
            'r2_sent': next((l['sent_at'] for l in logs if l['round_no'] == 2), None),
        })
    rows.sort(key=lambda x: -x['remain'])
    return render_template('admin/leave_promotion.html', rows=rows, year=year,
                           active_page='leave_promotion')


AUDIT_CATEGORY_LABEL = {
    'salary':        '급여',
    'performance':   '성과',
    'personal_info': '개인정보',
    'document':      '문서',
    'export':        '내보내기',
    'auth':          '인증',
}
AUDIT_ACTION_LABEL = {
    'view': '열람', 'create': '생성', 'update': '변경', 'delete': '삭제',
    'download': '다운로드', 'login': '로그인', 'login_failed': '로그인 실패',
}


@app.route('/admin/audit-logs')
@admin_required
def admin_audit_logs():
    """감사 로그 조회 (Phase A-3) — 카테고리/행위/대상/기간 필터."""
    db       = get_db()
    category = request.args.get('category', '')
    action   = request.args.get('action', '')
    q        = request.args.get('q', '').strip()
    days     = int(request.args.get('days', 30))

    sql    = ('SELECT a.*, u.name target_name FROM audit_logs a '
              'LEFT JOIN users u ON a.target_user_id = u.id '
              "WHERE a.created_at >= datetime('now', ?)")
    params = [f'-{days} days']
    if category:
        sql += ' AND a.category=?'
        params.append(category)
    if action:
        sql += ' AND a.action=?'
        params.append(action)
    if q:
        sql += ' AND (a.actor_name LIKE ? OR u.name LIKE ? OR a.detail LIKE ?)'
        params += [f'%{q}%'] * 3
    sql += ' ORDER BY a.created_at DESC LIMIT 500'
    logs = db.execute(sql, params).fetchall()

    stats = db.execute(
        "SELECT category, COUNT(*) cnt FROM audit_logs "
        "WHERE created_at >= datetime('now', ?) GROUP BY category", (f'-{days} days',)
    ).fetchall()

    return render_template('admin/audit_logs.html',
                           logs=logs, stats=stats,
                           category=category, action=action, q=q, days=days,
                           cat_labels=AUDIT_CATEGORY_LABEL,
                           act_labels=AUDIT_ACTION_LABEL,
                           active_page='audit_logs')


# ── 복리후생 설정 ────────────────────────────────────────────────
@app.route('/admin/benefits', methods=['GET', 'POST'])
@admin_required
def admin_benefits():
    """복리후생·비과세 항목 회사 설정 — 4가지 지급 방식으로 분류."""
    db = get_db()

    if request.method == 'POST':
        for key, meta in BENEFIT_CATALOG.items():
            enabled      = 1 if request.form.get(f'enabled_{key}') else 0
            amount       = int(request.form.get(f'amount_{key}', 0) or 0)
            annual_limit = request.form.get(f'annual_limit_{key}')
            annual_limit = int(annual_limit) if annual_limit and annual_limit.strip().isdigit() else None
            pct          = request.form.get(f'pct_{key}')
            pct          = int(pct) if pct and str(pct).strip().isdigit() else None
            platform     = request.form.get(f'platform_{key}', '').strip() or None
            note         = request.form.get(f'note_{key}', '').strip() or None
            payment_type = meta.get('payment_type', 'monthly_fixed')
            # grade_pct 방식 — 등급별 % 저장 (JSON)
            grade_pct_json = None
            if meta.get('calc_type') == 'grade_pct':
                grade_map = {}
                for g in ['S', 'A', 'B', 'C', 'D']:
                    val = request.form.get(f'pct_{key}_{g}', '')
                    try:
                        grade_map[g] = int(val)
                    except (ValueError, TypeError):
                        grade_map[g] = meta.get('grade_pct', {}).get(g, 0)
                grade_pct_json = json.dumps(grade_map, ensure_ascii=False)
            db.execute(
                'INSERT INTO benefit_configs '
                '(key, enabled, payment_type, amount, annual_limit, pct, grade_pct_json, platform, note) '
                'VALUES (?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET '
                'enabled=excluded.enabled, payment_type=excluded.payment_type, '
                'amount=excluded.amount, annual_limit=excluded.annual_limit, '
                'pct=excluded.pct, grade_pct_json=excluded.grade_pct_json, '
                'platform=excluded.platform, '
                'note=excluded.note, updated_at=CURRENT_TIMESTAMP',
                (key, enabled, payment_type, amount, annual_limit, pct, grade_pct_json, platform, note)
            )
        db.commit()
        flash('복리후생 설정이 저장되었습니다.', 'success')
        return redirect(url_for('admin_benefits'))

    # 현재 설정 로드
    configs = {r['key']: dict(r) for r in db.execute('SELECT * FROM benefit_configs').fetchall()}

    # payment_type별로 그룹화
    sections = {pt: [] for pt in PAYMENT_TYPE_LABELS}
    for key, meta in sorted(BENEFIT_CATALOG.items(), key=lambda x: x[1].get('sort', 99)):
        cfg = configs.get(key, {})
        pt  = meta.get('payment_type', 'monthly_fixed')
        sections[pt].append({
            'key':             key,
            'name':            meta['name'],
            'category':        meta['category'],
            'payment_type':    pt,
            'tax_exempt':      meta['tax_exempt'],
            'monthly_limit':   meta.get('monthly_limit'),
            'annual_limit':    cfg.get('annual_limit', meta.get('annual_limit')),
            'legal_basis':     meta['legal_basis'],
            'description':     meta['description'],
            'conditions':      meta.get('conditions'),
            'icon':            meta.get('icon', 'fa-circle'),
            'calc_type':       meta.get('calc_type'),
            'grade_pct':       meta.get('grade_pct'),
            'default_pct':     meta.get('default_pct'),
            'platform_options':meta.get('platform_options', []),
            'enabled':         cfg.get('enabled', 0),
            'amount':          cfg.get('amount', meta.get('default_amount', 0)),
            'pct':             cfg.get('pct', meta.get('default_pct')),
            'platform':        cfg.get('platform', ''),
            'note':            cfg.get('note', ''),
        })

    # ── 분석 데이터 ──────────────────────────────────────────────
    from datetime import date
    this_year = date.today().year

    # 활성 직원 수
    total_emp = db.execute(
        "SELECT COUNT(*) FROM users WHERE role NOT IN ('admin','recruiter','guest') AND (termination_date IS NULL OR termination_date='')"
    ).fetchone()[0] or 1

    # 활성화된 monthly_fixed 항목별 비용 집계
    benefit_cost_items = []
    total_monthly_cost = 0
    total_nontax_cost  = 0
    for key, meta in sorted(BENEFIT_CATALOG.items(), key=lambda x: x[1].get('sort', 99)):
        if meta.get('payment_type') != 'monthly_fixed':
            continue
        cfg = configs.get(key, {})
        if not cfg.get('enabled'):
            continue
        amount     = cfg.get('amount') or meta.get('default_amount', 0)
        monthly    = amount * total_emp
        is_exempt  = meta.get('tax_exempt', False)
        benefit_cost_items.append({
            'name':       meta['name'],
            'icon':       meta.get('icon', 'fa-circle'),
            'tax_exempt': is_exempt,
            'per_person': amount,
            'total':      monthly,
        })
        total_monthly_cost += monthly
        if is_exempt:
            total_nontax_cost += monthly

    # 비과세 절감 효과 (소득세+주민세 약 33% 가정)
    tax_saving = int(total_nontax_cost * 0.33)

    # 부서별 인원 + 1인당 월 복리후생 비용
    dept_rows = db.execute("""
        SELECT d.name AS dept_name, COUNT(u.id) AS cnt
        FROM users u
        JOIN departments d ON u.department_id = d.id
        WHERE u.role NOT IN ('admin','recruiter','guest')
          AND (u.termination_date IS NULL OR u.termination_date='')
        GROUP BY d.id
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    per_person_monthly = total_monthly_cost // total_emp if total_emp else 0
    dept_analysis = [
        {'dept': r['dept_name'], 'cnt': r['cnt'], 'total': r['cnt'] * per_person_monthly}
        for r in dept_rows
    ]

    # 복지포인트 현황
    wp_total_granted = db.execute(
        "SELECT COALESCE(SUM(delta),0) FROM welfare_point_ledger WHERE delta>0 AND strftime('%Y',created_at)=?",
        (str(this_year),)
    ).fetchone()[0]
    wp_total_balance = db.execute(
        "SELECT COALESCE(SUM(delta),0) FROM welfare_point_ledger"
    ).fetchone()[0]
    wp_used = wp_total_granted - wp_total_balance if wp_total_granted > wp_total_balance else 0
    wp_usage_pct = int(wp_used / wp_total_granted * 100) if wp_total_granted > 0 else 0

    return render_template('admin/benefits.html',
                           sections=sections,
                           payment_type_labels=PAYMENT_TYPE_LABELS,
                           total_emp=total_emp,
                           benefit_cost_items=benefit_cost_items,
                           total_monthly_cost=total_monthly_cost,
                           total_nontax_cost=total_nontax_cost,
                           tax_saving=tax_saving,
                           dept_analysis=dept_analysis,
                           per_person_monthly=per_person_monthly,
                           wp_total_granted=wp_total_granted,
                           wp_total_balance=wp_total_balance,
                           wp_used=wp_used,
                           wp_usage_pct=wp_usage_pct,
                           this_year=this_year,
                           active_page='benefits')


# ── 환급 신청 관리 ───────────────────────────────────────────────
@app.route('/admin/benefit-claims')
@admin_required
def admin_benefit_claims():
    """영수증 환급 신청 목록 및 승인/반려 관리."""
    db  = get_db()
    tab = request.args.get('tab', 'pending')

    status_filter = {'pending': 'pending', 'approved': 'approved', 'rejected': 'rejected', 'all': None}
    sf = status_filter.get(tab, 'pending')

    if sf:
        claims = db.execute(
            """SELECT bc.*, u.name AS emp_name, u.emp_no
               FROM benefit_claims bc
               JOIN users u ON bc.user_id = u.id
               WHERE bc.status = ?
               ORDER BY bc.submitted_at DESC""",
            (sf,)
        ).fetchall()
    else:
        claims = db.execute(
            """SELECT bc.*, u.name AS emp_name, u.emp_no
               FROM benefit_claims bc
               JOIN users u ON bc.user_id = u.id
               ORDER BY bc.submitted_at DESC"""
        ).fetchall()

    # 항목명 매핑
    benefit_names = {k: v['name'] for k, v in BENEFIT_CATALOG.items()}
    counts = {
        'pending':  db.execute("SELECT COUNT(*) FROM benefit_claims WHERE status='pending'").fetchone()[0],
        'approved': db.execute("SELECT COUNT(*) FROM benefit_claims WHERE status='approved'").fetchone()[0],
        'rejected': db.execute("SELECT COUNT(*) FROM benefit_claims WHERE status='rejected'").fetchone()[0],
    }

    return render_template('admin/benefit_claims.html',
                           claims=claims,
                           benefit_names=benefit_names,
                           tab=tab, counts=counts,
                           active_page='benefits')


@app.route('/admin/benefit-claims/<int:claim_id>/<action>', methods=['POST'])
@admin_required
def admin_benefit_claim_action(claim_id, action):
    """환급 신청 승인 / 반려."""
    if action not in ('approve', 'reject'):
        abort(400)
    db     = get_db()
    claim  = db.execute('SELECT * FROM benefit_claims WHERE id=?', (claim_id,)).fetchone()
    if not claim:
        abort(404)
    if claim['status'] != 'pending':
        flash('이미 처리된 신청입니다.', 'warning')
        return redirect(url_for('admin_benefit_claims'))

    new_status = 'approved' if action == 'approve' else 'rejected'
    reviewer   = session.get('user_name', '')
    note       = request.form.get('note', '').strip()
    db.execute(
        "UPDATE benefit_claims SET status=?, reviewer_name=?, reviewer_note=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_status, reviewer, note or None, claim_id)
    )
    db.commit()
    label = '승인' if action == 'approve' else '반려'
    flash(f'환급 신청이 {label}되었습니다.', 'success')
    return redirect(url_for('admin_benefit_claims'))


# ── 상여·성과급 지급 관리 ────────────────────────────────────────
@app.route('/admin/bonus-pay', methods=['GET', 'POST'])
@admin_required
def admin_bonus_pay():
    """상여·성과급 별도 지급 관리."""
    db = get_db()

    def _calc_bonus_amount(emp_id, emp_base, calc_type, cfg, meta, achievement_pct=None):
        """직원 1명의 상여 금액 계산. (grade_pct는 DB 저장값 우선)"""
        amount = 0
        grade  = None
        if calc_type == 'pct_of_base':
            pct    = (cfg['pct'] or meta.get('default_pct', 100)) / 100
            amount = int((emp_base or 0) * pct)
        elif calc_type == 'company_pct':
            base_pct = (cfg['pct'] or meta.get('default_pct', 10)) / 100
            ach      = (achievement_pct or 100) / 100
            amount   = int((emp_base or 0) * base_pct * ach)
        elif calc_type == 'grade_pct':
            review = db.execute(
                """SELECT overall_grade FROM performance_reviews
                   WHERE reviewee_id=? ORDER BY submitted_at DESC LIMIT 1""",
                (emp_id,)
            ).fetchone()
            grade = review['overall_grade'] if review and review['overall_grade'] else None
            # DB 저장 grade_pct 우선, 없으면 BENEFIT_CATALOG 기본값
            if cfg['grade_pct_json']:
                try:
                    grade_map = json.loads(cfg['grade_pct_json'])
                except (ValueError, TypeError):
                    grade_map = meta.get('grade_pct', {'S':20,'A':15,'B':10,'C':5,'D':0})
            else:
                grade_map = meta.get('grade_pct', {'S':20,'A':15,'B':10,'C':5,'D':0})
            pct_val = grade_map.get(grade or 'C', 0) / 100
            amount  = int((emp_base or 0) * pct_val / 12)
        return amount, grade

    if request.method == 'POST':
        bonus_type      = request.form.get('bonus_type', '').strip()
        pay_date        = request.form.get('pay_date', '').strip()
        achievement_pct = request.form.get('achievement_pct', '')
        note            = request.form.get('note', '').strip()

        if not bonus_type or not pay_date:
            flash('상여 유형과 지급일을 입력하세요.', 'error')
            return redirect(url_for('admin_bonus_pay'))

        achievement_pct = float(achievement_pct) if achievement_pct else None
        meta            = BENEFIT_CATALOG.get(bonus_type, {})
        calc_type       = meta.get('calc_type', 'pct_of_base')

        cfg = db.execute(
            "SELECT * FROM benefit_configs WHERE key=? AND enabled=1", (bonus_type,)
        ).fetchone()
        if not cfg:
            flash('해당 항목이 비활성화 상태입니다. 복리후생 설정에서 먼저 활성화하세요.', 'error')
            return redirect(url_for('admin_bonus_pay'))

        employees = db.execute(
            "SELECT u.id, u.name, COALESCE(s.base_salary, 0) AS base_salary "
            "FROM users u LEFT JOIN employee_salary s ON u.id=s.user_id "
            "WHERE u.status='active' AND u.role NOT IN ('admin','recruiter')"
        ).fetchall()

        inserted = 0
        for emp in employees:
            amount, _ = _calc_bonus_amount(
                emp['id'], emp['base_salary'], calc_type, cfg, meta, achievement_pct
            )
            if amount > 0:
                db.execute(
                    "INSERT INTO bonus_payments (user_id, bonus_type, amount, pay_date, note) VALUES (?,?,?,?,?)",
                    (emp['id'], bonus_type, amount, pay_date, note or None)
                )
                inserted += 1

        db.commit()
        flash(f'{meta.get("name", bonus_type)} 지급 완료 — {inserted}명, 지급일 {pay_date}', 'success')
        return redirect(url_for('admin_bonus_pay'))

    # 상여 유형 목록 (separate_bonus만)
    bonus_items = [
        {'key': k, **v}
        for k, v in BENEFIT_CATALOG.items()
        if v.get('payment_type') == 'separate_bonus'
    ]
    bonus_items.sort(key=lambda x: x.get('sort', 99))

    # 직원별 성과등급 미리보기 데이터 (grade_pct 타입 전용)
    employees_preview = db.execute(
        "SELECT u.id, u.name, u.emp_no, d.name AS dept_name, COALESCE(s.base_salary, 0) AS base_salary "
        "FROM users u "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN employee_salary s ON u.id=s.user_id "
        "WHERE u.status='active' AND u.role NOT IN ('admin','recruiter') "
        "ORDER BY d.name, u.name"
    ).fetchall()
    employees_preview = [dict(r) for r in employees_preview]

    # 각 직원의 최근 성과등급 매핑 (캘리브레이션 확정 등급 기준)
    grade_map_all = {}
    for row in db.execute(
        """SELECT cr.user_id, cr.final_grade
           FROM calibration_results cr
           WHERE cr.decided_at = (
               SELECT MAX(decided_at) FROM calibration_results
               WHERE user_id = cr.user_id
           )"""
    ).fetchall():
        grade_map_all[row['user_id']] = row['final_grade']

    # 활성화된 benefit_configs 로드
    active_configs = {
        r['key']: dict(r)
        for r in db.execute(
            "SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type='separate_bonus'"
        ).fetchall()
    }

    # 기존 지급 내역
    history = db.execute(
        """SELECT bp.*, u.name AS emp_name
           FROM bonus_payments bp
           JOIN users u ON bp.user_id = u.id
           ORDER BY bp.pay_date DESC, bp.created_at DESC
           LIMIT 200"""
    ).fetchall()

    benefit_names = {k: v['name'] for k, v in BENEFIT_CATALOG.items()}

    return render_template('admin/bonus_pay.html',
                           bonus_items=bonus_items,
                           history=history,
                           benefit_names=benefit_names,
                           employees_preview=employees_preview,
                           grade_map_all=grade_map_all,
                           active_configs=active_configs,
                           active_page='benefits')


# ── 주 52시간 감시 ───────────────────────────────────────────────
@app.route('/admin/overtime-monitor')
@admin_required
def overtime_monitor():
    """주 52시간 위반 모니터링 대시보드."""
    import json as _json
    from datetime import timedelta
    db = get_db()

    # 기준: 최근 8주
    today      = date.today()
    eight_ago  = (today - timedelta(weeks=8)).isoformat()

    # 직원별·주별 근무시간 집계 (checkins 기반)
    # 주 기산: 월요일 (SQLite weekday 계산 — 0=일,1=월,...,6=토 → 월요일 기준 offset)
    rows = db.execute(
        """
        SELECT
            u.id   AS user_id,
            u.name,
            u.emp_no,
            d.name AS dept_name,
            date(c.date, '-' || ((cast(strftime('%w', c.date) AS INTEGER) + 6) % 7) || ' days')
                   AS week_start,
            SUM(c.regular_min + c.overtime_min) AS total_min,
            SUM(c.overtime_min)                 AS ot_min
        FROM checkins c
        JOIN users u ON c.user_id = u.id
        LEFT JOIN departments d ON u.department_id = d.id
        WHERE u.status = 'active'
          AND c.date >= ?
        GROUP BY u.id, week_start
        ORDER BY total_min DESC
        """,
        (eight_ago,)
    ).fetchall()

    # 52시간 = 3120분 기준으로 분류 (payroll_utils 상수 재사용)
    LIMIT_MIN   = WEEKLY_TOTAL_MAX   # 3120분
    WARNING_MIN = WEEKLY_WARNING     # 2880분

    violations = []
    warnings   = []
    safe       = []

    for r in rows:
        entry = dict(r)
        entry['total_h']  = round(entry['total_min'] / 60, 1)
        entry['ot_h']     = round(entry['ot_min'] / 60, 1)
        entry['over_min'] = max(0, entry['total_min'] - LIMIT_MIN)
        entry['over_h']   = round(entry['over_min'] / 60, 1)

        if entry['total_min'] > LIMIT_MIN:
            violations.append(entry)
        elif entry['total_min'] >= WARNING_MIN:
            warnings.append(entry)
        else:
            safe.append(entry)

    # 직원별 최근 4주 추이 (chart용)
    trend_rows = db.execute(
        """
        SELECT
            u.id  AS user_id,
            u.name,
            date(c.date, '-' || ((cast(strftime('%w', c.date) AS INTEGER) + 6) % 7) || ' days')
                  AS week_start,
            SUM(c.regular_min + c.overtime_min) AS total_min
        FROM checkins c
        JOIN users u ON c.user_id = u.id
        WHERE u.status = 'active'
          AND c.date >= ?
        GROUP BY u.id, week_start
        ORDER BY u.id, week_start
        """,
        ((today - timedelta(weeks=4)).isoformat(),)
    ).fetchall()

    # 위반·경고자만 차트 데이터 구성
    flagged_ids = {r['user_id'] for r in violations + warnings}
    chart_data = {}
    for r in trend_rows:
        if r['user_id'] not in flagged_ids:
            continue
        uid  = r['user_id']
        name = r['name']
        if uid not in chart_data:
            chart_data[uid] = {'name': name, 'weeks': [], 'hours': []}
        chart_data[uid]['weeks'].append(r['week_start'])
        chart_data[uid]['hours'].append(round(r['total_min'] / 60, 1))

    return render_template('admin/overtime_monitor.html',
                           violations=violations,
                           warnings=warnings,
                           safe_count=len(safe),
                           chart_data=_json.dumps(list(chart_data.values())),
                           limit_h=52,
                           warning_h=48,
                           active_page='overtime_monitor')


@app.route('/contracts/<int:cid>/cancel', methods=['POST'])
@login_required
def contract_cancel(cid):
    db  = get_db()
    uid = session['user_id']
    c   = db.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c or c['issued_by'] != uid:
        abort(403)
    if c['status'] != 'pending':
        flash('이미 처리된 계약서입니다.', 'error')
        return redirect(url_for('contract_view', cid=cid))
    db.execute("UPDATE contracts SET status='cancelled' WHERE id=?", (cid,))
    db.commit()
    flash('계약서가 취소되었습니다.', 'success')
    return redirect(url_for('contracts_list'))


# ════════════════════════════════════════════════════════════
#  SaaS — 랜딩 / 가입 / 결제
# ════════════════════════════════════════════════════════════

@app.route('/')
def landing():
    """랜딩 페이지 — 로그인 상태면 대시보드로 (단, 체험 모드는 예외)"""
    if 'user_id' in session and not session.get('demo_mode'):
        return redirect(url_for('dashboard'))
    return render_template('landing/index.html', price_per_seat=1000)


@app.route('/robots.txt')
def robots_txt():
    """검색엔진 크롤링 정책 (R5, v1.5.3)"""
    return app.response_class(
        'User-agent: *\n'
        'Disallow: /demo\n'
        'Disallow: /saas\n'
        'Disallow: /api/\n'
        'Disallow: /billing\n',
        mimetype='text/plain')


@app.route('/privacy')
def privacy_policy():
    """개인정보처리방침 (공개 — R5, v1.5.0)"""
    return render_template('legal/privacy.html')


@app.route('/terms')
def terms_of_service():
    """이용약관 (공개 — R5, v1.5.0)"""
    return render_template('legal/terms.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """회사 가입 — 새 테넌트 생성 + 관리자 계정 생성"""
    if 'user_id' in session:
        if session.get('demo_mode'):
            session.clear()
        else:
            return redirect(url_for('dashboard'))

    error = None
    if request.method == 'POST':
        company_name = request.form.get('company_name', '').strip()
        admin_name   = request.form.get('name', '').strip()
        email        = request.form.get('email', '').strip()
        password     = request.form.get('password', '').strip()
        password2    = request.form.get('password2', '').strip()

        # ── 유효성 검사 ──────────────────────────────────────
        if not all([company_name, admin_name, email, password]):
            error = '모든 항목을 입력해주세요.'
        elif not request.form.get('agree_terms') or not request.form.get('agree_privacy'):
            error = '이용약관과 개인정보 수집·이용에 동의해야 가입할 수 있습니다.'
        elif password != password2:
            error = '비밀번호가 일치하지 않습니다.'
        elif validate_password(password):
            error = validate_password(password)
        else:
            # 이미 가입된 이메일인지 확인
            existing = get_tenant_by_email(email)
            if existing:
                error = '이미 가입된 이메일입니다.'
            else:
                # ── 테넌트 생성 ──────────────────────────────
                tenant_id = create_tenant(company_name, email)

                # ── 테넌트 DB 초기화 (스키마만, 시드 없음) ───
                from database import init_db as _init_db
                _init_db(db_path=get_tenant_db_path(tenant_id))

                # ── 관리자 계정 생성 ──────────────────────────
                tdb = sqlite3.connect(get_tenant_db_path(tenant_id))
                tdb.row_factory = sqlite3.Row
                tdb.execute('PRAGMA foreign_keys = ON')
                tdb.execute(
                    '''INSERT INTO users
                       (email, password_hash, name, role, hire_date, onboarded,
                        features_enabled, status)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (email, generate_password_hash(password), admin_name,
                     'admin', date.today().isoformat(), 0,
                     'attendance,payroll,performance,peer_review,calibration,'
                     'recruiting,announcements,org_chart,certificates',
                     'active')
                )
                tdb.execute("UPDATE users SET emp_no='TC-00001' WHERE email=?", (email,))
                tdb.commit()
                tdb.close()

                # ── master.db에 이메일 매핑 ───────────────────
                register_tenant_user(email, tenant_id)

                flash(f'가입 완료! {TRIAL_DAYS}일 무료 체험이 시작됩니다.', 'success')
                return redirect(url_for('login'))

    return render_template('landing/signup.html', error=error, trial_days=TRIAL_DAYS)


# ── 토스페이먼츠 Billing Key 발급 ────────────────────────────

@app.route('/billing/register', methods=['GET'])
@login_required
def billing_register():
    """카드 등록 페이지 — 토스 빌링 위젯 호출"""
    if not BILLING_ENABLED:
        flash('현재 무료 파트너 프로그램 운영 중이라 카드 등록이 필요하지 않습니다.', 'success')
        return redirect(url_for('billing'))
    tenant_id = session.get('tenant_id', 1)
    tenant    = get_tenant(tenant_id)
    customer_key = f'tenant_{tenant_id}'  # 테넌트별 고정 키
    return render_template('billing/register.html',
                           tenant=tenant,
                           customer_key=customer_key,
                           toss_client_key=TOSS_CLIENT_KEY,
                           price_per_seat=PRICE_PER_SEAT)


@app.route('/billing/card-success')
@login_required
def billing_card_success():
    """
    토스 카드 인증 성공 콜백.
    authKey + customerKey를 받아 billing key 발급 후 저장.
    """
    auth_key     = request.args.get('authKey', '')
    customer_key = request.args.get('customerKey', '')
    if not auth_key or not customer_key:
        flash('카드 등록 정보가 올바르지 않습니다.', 'error')
        return redirect(url_for('billing_register'))

    # ── 토스 API: billing key 발급 ───────────────────────────
    try:
        credential = base64.b64encode(f'{TOSS_SECRET_KEY}:'.encode()).decode()
        req_data   = json.dumps({'authKey': auth_key, 'customerKey': customer_key}).encode()
        req = urllib.request.Request(
            'https://api.tosspayments.com/v1/billing/authorizations/issue',
            data=req_data,
            headers={
                'Authorization': f'Basic {credential}',
                'Content-Type': 'application/json',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        billing_key = result.get('billingKey', '')
        if not billing_key:
            raise ValueError('billingKey not found in response')
    except Exception as e:
        app.logger.error(f'Toss billing key issue failed: {e}')
        flash('카드 등록 중 오류가 발생했습니다. 다시 시도해주세요.', 'error')
        return redirect(url_for('billing_register'))

    # ── master.db에 billing key 저장, 구독 active 전환 ───────
    tenant_id = session.get('tenant_id', 1)
    save_billing_key(tenant_id, billing_key)
    session.pop('subscription_expired', None)

    flash('카드가 등록되었습니다. 구독이 시작됩니다.', 'success')
    return redirect(url_for('billing'))


@app.route('/billing/card-fail')
@login_required
def billing_card_fail():
    msg = request.args.get('message', '카드 등록이 취소되었습니다.')
    flash(msg, 'error')
    return redirect(url_for('billing_register'))


@app.route('/billing')
@login_required
def billing():
    """구독 현황 대시보드"""
    tenant_id = session.get('tenant_id', 1)
    tenant    = get_tenant(tenant_id)
    mdb       = get_master_db()
    logs      = mdb.execute(
        '''SELECT * FROM billing_logs WHERE tenant_id=?
           ORDER BY created_at DESC LIMIT 12''',
        (tenant_id,)
    ).fetchall()
    db            = get_db()
    active_count  = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND role!='guest'"
    ).fetchone()[0]
    mdb.close()
    seat_price     = get_plan_price(_current_plan())
    monthly_amount = (tenant['peak_headcount'] or active_count) * seat_price
    return render_template('billing/dashboard.html',
                           billing_enabled=BILLING_ENABLED,
                           tenant=tenant,
                           logs=logs,
                           active_count=active_count,
                           monthly_amount=monthly_amount,
                           price_per_seat=seat_price)


@app.route('/billing/charge', methods=['POST'])
@admin_required
def billing_charge():
    """
    월별 청구 실행 (관리자 수동 트리거 또는 cron 대용).
    Peak headcount × 요금제 단가를 저장된 billing key로 결제.
    """
    if not BILLING_ENABLED:
        flash('현재 무료 파트너 프로그램 운영 중으로 결제가 비활성화되어 있습니다.', 'error')
        return redirect(url_for('billing'))
    tenant_id = session.get('tenant_id', 1)
    tenant    = get_tenant(tenant_id)

    if not tenant or not tenant['toss_billing_key']:
        flash('등록된 결제 수단이 없습니다.', 'error')
        return redirect(url_for('billing'))

    db           = get_db()
    active_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND role!='guest'"
    ).fetchone()[0]
    peak         = max(tenant['peak_headcount'] or 0, active_count)
    amount       = peak * get_plan_price(_current_plan())

    if amount == 0:
        flash('청구 금액이 0원입니다.', 'info')
        return redirect(url_for('billing'))

    order_id     = f'TC-{tenant_id}-{date.today().strftime("%Y%m")}-{uuid.uuid4().hex[:8]}'
    billing_key  = tenant['toss_billing_key']
    customer_key = f'tenant_{tenant_id}'

    # ── 토스 API: 빌링 결제 실행 ────────────────────────────
    try:
        credential = base64.b64encode(f'{TOSS_SECRET_KEY}:'.encode()).decode()
        req_data   = json.dumps({
            'customerKey': customer_key,
            'amount':      amount,
            'orderId':     order_id,
            'orderName':   f'TalentCore {date.today().strftime("%Y년 %m월")} 구독 ({peak}명)',
        }).encode()
        req = urllib.request.Request(
            f'https://api.tosspayments.com/v1/billing/{billing_key}',
            data=req_data,
            headers={
                'Authorization': f'Basic {credential}',
                'Content-Type': 'application/json',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        payment_key = result.get('paymentKey', '')
        status      = result.get('status', '')
        if status == 'DONE':
            log_billing(tenant_id, amount, peak, order_id, payment_key, 'paid')
            reset_peak_headcount(tenant_id, active_count)
            flash(f'{peak}명 기준 {amount:,}원 결제 완료.', 'success')
        else:
            log_billing(tenant_id, amount, peak, order_id, status=f'failed')
            flash(f'결제 실패: {result.get("message", "알 수 없는 오류")}', 'error')
    except Exception as e:
        app.logger.error(f'Toss billing charge failed: {e}')
        log_billing(tenant_id, amount, peak, order_id, status='failed')
        flash('결제 처리 중 오류가 발생했습니다.', 'error')

    return redirect(url_for('billing'))


# ══════════════════════════════════════════════════════════════
#  웹훅 검증 헬퍼 (Phase A-1 보안 기준선)
# ══════════════════════════════════════════════════════════════

def _verify_slack_signature():
    """
    Slack 공식 서명 검증 (v0 방식).
    - SLACK_SIGNING_SECRET 설정 시: X-Slack-Signature = HMAC-SHA256("v0:{ts}:{body}") 비교
      + 타임스탬프 5분 초과(리플레이 공격) 거부
    - 미설정 시: 경고 로그만 남기고 통과 (개발/데모 모드)
    """
    secret = os.environ.get('SLACK_SIGNING_SECRET', '')
    if not secret:
        app.logger.warning('SLACK_SIGNING_SECRET 미설정 — Slack 웹훅 서명 검증 생략 중 (운영 배포 전 필수 설정)')
        return True

    ts  = request.headers.get('X-Slack-Request-Timestamp', '')
    sig = request.headers.get('X-Slack-Signature', '')
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except ValueError:
        return False

    basestring = f'v0:{ts}:'.encode() + request.get_data()
    expected   = 'v0=' + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _verify_toss_payment(payment_key):
    """
    토스 웹훅 검증 — 토스는 서명 헤더를 제공하지 않으므로,
    공식 권장 방식대로 시크릿 키로 결제를 재조회해서 실제 상태를 확인한다.
    - TOSS_SECRET_KEY 설정 시: GET /v1/payments/{paymentKey} 응답의 status 반환 (조회 실패 → None)
    - 미설정 시: 경고 로그 + None 반환 (호출부에서 페이로드 값 사용)
    """
    secret_key = os.environ.get('TOSS_SECRET_KEY', '')
    if not secret_key:
        app.logger.warning('TOSS_SECRET_KEY 미설정 — 토스 웹훅 재조회 검증 생략 중 (운영 배포 전 필수 설정)')
        return None
    try:
        credential = base64.b64encode(f'{secret_key}:'.encode()).decode()
        req = urllib.request.Request(
            f'https://api.tosspayments.com/v1/payments/{payment_key}',
            headers={'Authorization': f'Basic {credential}'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get('status', '')
    except Exception as e:
        app.logger.error(f'Toss payment verify failed: {e}')
        return ''


@app.route('/billing/webhook', methods=['POST'])
def billing_webhook():
    """
    토스 웹훅 수신 — 결제 상태 동기화.
    (토스 대시보드에서 웹훅 URL을 /billing/webhook 으로 설정)
    페이로드는 신뢰하지 않고, 시크릿 키로 결제를 재조회한 상태를 사용한다.
    """
    try:
        payload     = json.loads(request.data)
        event_type  = payload.get('eventType', '')
        data        = payload.get('data', {})
        order_id    = data.get('orderId', '')
        payment_key = data.get('paymentKey', '')
        status      = data.get('status', '')

        if event_type == 'PAYMENT_STATUS_CHANGED':
            verified = _verify_toss_payment(payment_key)
            if verified is not None:
                if verified == '':
                    # 시크릿은 있는데 재조회 실패 → 위조 가능성, 반영하지 않음
                    app.logger.warning(f'Toss webhook 검증 실패 — 무시함 (orderId={order_id})')
                    return '', 400
                status = verified  # 페이로드 대신 재조회된 실제 상태 사용
            if status == 'DONE':
                update_billing_log(order_id, payment_key, 'paid')
            elif status in ('ABORTED', 'EXPIRED'):
                update_billing_log(order_id, payment_key, 'failed',
                                   data.get('failure', {}).get('message', ''))
    except Exception as e:
        app.logger.error(f'Toss webhook error: {e}')
        return '', 400

    return '', 200


# ══════════════════════════════════════════════════════════════
#  Slack 슬래시 커맨드 / 인터랙티브 버튼
# ══════════════════════════════════════════════════════════════

@app.route('/slack/command', methods=['POST'])
def slack_command():
    """
    /talentcore 슬래시 커맨드 핸들러
    Slack이 application/x-www-form-urlencoded 로 POST 함
    """
    if not _verify_slack_signature():
        app.logger.warning('Slack command 서명 검증 실패 — 요청 거부')
        return '', 401
    from integrations.slack import send_dm, IS_DEMO
    text      = request.form.get('text', '').strip()
    slack_uid = request.form.get('user_id', '')
    resp_url  = request.form.get('response_url', '')

    db = get_db()

    # Slack UID → 내부 user 매핑
    # (슬랙 UID를 이메일로 변환하는 방법: users.info API 사용)
    def get_email_from_slack_uid(uid):
        if IS_DEMO:
            return None
        try:
            import urllib.request as _ur, json as _j
            token = os.environ.get('SLACK_BOT_TOKEN', '')
            import urllib.parse as _up
            url = 'https://slack.com/api/users.info?' + _up.urlencode({'user': uid})
            req = _ur.Request(url, headers={'Authorization': f'Bearer {token}'})
            with _ur.urlopen(req, timeout=8) as r:
                data = _j.loads(r.read())
                return data.get('user', {}).get('profile', {}).get('email')
        except Exception:
            return None

    cmd = text.lower().replace(' ', '')

    # ── 내 연차 ──────────────────────────────────────────────
    if cmd in ('내연차', '연차', 'leave', 'myannual'):
        email = get_email_from_slack_uid(slack_uid)
        if not email:
            return jsonify({'response_type': 'ephemeral',
                            'text': '이메일 조회 실패. TalentCore에 이 Slack 계정 이메일이 등록돼 있는지 확인해주세요.'})
        user = db.execute('SELECT * FROM users WHERE email=? AND status="active"', (email,)).fetchone()
        if not user:
            return jsonify({'response_type': 'ephemeral', 'text': 'TalentCore에 등록된 계정을 찾을 수 없습니다.'})

        # 연차 계산
        _bal = get_leave_balance(db, user['id'])
        return jsonify({
            'response_type': 'ephemeral',
            'text': (
                f"*{user['name']}님의 연차 현황*\n"
                f"• 총 부여: {_bal['total']:g}일" + (f" (이월 {_bal['carryover']:g}일 포함)" if _bal['carryover'] else '') + "\n"
                f"• 사용: {_bal['used']:g}일\n"
                f"• 잔여: *{_bal['remaining']:g}일*"
            ),
        })

    # ── 팀 출근 ──────────────────────────────────────────────
    elif cmd in ('팀출근', '팀오늘출근', 'teamcheckin'):
        email = get_email_from_slack_uid(slack_uid)
        if not email:
            return jsonify({'response_type': 'ephemeral', 'text': '이메일 조회 실패.'})
        user = db.execute('SELECT * FROM users WHERE email=? AND status="active"', (email,)).fetchone()
        if not user:
            return jsonify({'response_type': 'ephemeral', 'text': '계정을 찾을 수 없습니다.'})

        today = date.today().isoformat()
        rows  = db.execute(
            """SELECT u.name, c.check_in, c.check_out
               FROM checkins c JOIN users u ON c.user_id = u.id
               WHERE c.date=? AND u.department_id=?
               ORDER BY c.check_in""",
            (today, user['department_id'])
        ).fetchall()

        if not rows:
            return jsonify({'response_type': 'ephemeral', 'text': f'오늘({today}) 팀 출근 기록이 없습니다.'})

        lines = [f"*오늘 팀 출근 현황 ({today})*"]
        for r in rows:
            out = r['check_out'][:5] if r['check_out'] else '근무중'
            lines.append(f"• {r['name']} — {r['check_in'][:5]} 출근 / {out}")
        return jsonify({'response_type': 'ephemeral', 'text': '\n'.join(lines)})

    # ── 도움말 ───────────────────────────────────────────────
    else:
        return jsonify({
            'response_type': 'ephemeral',
            'text': (
                "*TalentCore 슬래시 커맨드 사용법*\n\n"
                "• `/talentcore 내연차` — 나의 연차 잔여일수 조회\n"
                "• `/talentcore 팀출근` — 오늘 우리 팀 출근 현황\n"
            ),
        })


@app.route('/slack/interactive', methods=['POST'])
def slack_interactive():
    """
    Slack 인터랙티브 버튼 핸들러
    payload JSON에 action_id + value 포함
    """
    if not _verify_slack_signature():
        app.logger.warning('Slack interactive 서명 검증 실패 — 요청 거부')
        return '', 401
    from integrations.slack import respond_to_interaction, send_dm
    raw     = request.form.get('payload', '')
    if not raw:
        return '', 400
    payload      = json.loads(raw)
    actions      = payload.get('actions', [])
    response_url = payload.get('response_url', '')
    slack_uid    = payload.get('user', {}).get('id', '')

    if not actions:
        return '', 200

    action    = actions[0]
    action_id = action.get('action_id', '')
    value     = action.get('value', '')

    db = get_db()

    # ── 휴가 승인 버튼 ────────────────────────────────────────
    if action_id == 'leave_approve' and value.isdigit():
        req_id = int(value)
        req    = db.execute('SELECT * FROM leave_requests WHERE id=?', (req_id,)).fetchone()
        if not req or req['status'] != 'pending':
            respond_to_interaction(response_url, '이미 처리된 신청입니다.')
            return '', 200

        # 내부 처리 (manager_only 기준)
        db.execute(
            "UPDATE leave_requests SET status='approved', "
            "manager_approved_at=CURRENT_TIMESTAMP WHERE id=?", (req_id,)
        )
        db.commit()
        add_notification(
            req['user_id'], 'info', 'leave', '휴가 승인 완료',
            '슬랙에서 매니저가 승인했습니다.',
            url_for('attendance_home', tab='leaves')
        )
        # 신청자에게 DM
        emp = db.execute('SELECT email, name FROM users WHERE id=?', (req['user_id'],)).fetchone()
        if emp and emp['email']:
            send_dm(emp['email'],
                    f"[TalentCore] {req['start_date']} ~ {req['end_date']} 휴가가 승인됐습니다.")
        # 버튼 메시지 업데이트
        respond_to_interaction(response_url,
            f"{emp['name'] if emp else ''}님 휴가 승인 완료 ({req['start_date']} ~ {req['end_date']})")
        return '', 200

    # ── 휴가 반려 버튼 ────────────────────────────────────────
    elif action_id == 'leave_reject' and value.isdigit():
        req_id = int(value)
        req    = db.execute('SELECT * FROM leave_requests WHERE id=?', (req_id,)).fetchone()
        if not req or req['status'] != 'pending':
            respond_to_interaction(response_url, '이미 처리된 신청입니다.')
            return '', 200

        db.execute(
            "UPDATE leave_requests SET status='rejected' WHERE id=?", (req_id,)
        )
        db.commit()
        add_notification(
            req['user_id'], 'info', 'leave', '휴가 반려',
            '슬랙에서 매니저가 반려했습니다.',
            url_for('attendance_home', tab='leaves')
        )
        emp = db.execute('SELECT email, name FROM users WHERE id=?', (req['user_id'],)).fetchone()
        if emp and emp['email']:
            send_dm(emp['email'],
                    f"[TalentCore] {req['start_date']} ~ {req['end_date']} 휴가 신청이 반려됐습니다.")
        respond_to_interaction(response_url,
            f"{emp['name'] if emp else ''}님 휴가 반려 ({req['start_date']} ~ {req['end_date']})")
        return '', 200

    return '', 200


# ── Run ─────────────────────────────────────────────────────
if __name__ == '__main__':
    from database import init_db
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true')
