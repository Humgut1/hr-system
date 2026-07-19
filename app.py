import os
import sqlite3
import uuid
import json
import base64
import hmac
import hashlib
import time
import urllib.request
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for, jsonify)
from werkzeug.security import check_password_hash, generate_password_hash
from payroll_utils import (calc_payslip, calc_annual_leave, compute_leave_balance, fmt_krw,
                           calc_severance, check_min_wage, MIN_WAGE_MONTHLY,
                           calc_day_hours, calc_extra_pay,
                           get_week_bounds, calc_weekly_hours,
                           WEEKLY_TOTAL_MAX, WEEKLY_WARNING,
                           BENEFIT_CATALOG, BENEFIT_CATEGORY_LABELS, PAYMENT_TYPE_LABELS,
                           calc_prorated_salary, calc_unused_leave_pay,
                           calc_separation_settlement)
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
from database import init_db
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
        'setup_completed': 0,      'setup_step': 0,
    }


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
CSRF_EXEMPT_ENDPOINTS = {'billing_webhook', 'slack_command', 'slack_interactive', 'hires_webhook'}


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
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,5,?)
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
                setup_completed=1, setup_step=5,
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
        db.commit()
        session['onboarded'] = 1
        flash('회사 설정이 완료되었습니다! TalentCore에 오신 것을 환영합니다. 🎉', 'success')
        return redirect(url_for('dashboard'))

    config  = get_company_config()
    company = get_company_info()
    return render_template('admin/setup.html', config=config, company=company,
                           benefit_catalog=BENEFIT_CATALOG)


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
        db.commit()
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
        open_postings     = db.execute("SELECT COUNT(*) FROM job_postings WHERE status='open'").fetchone()[0]
        total_applicants  = db.execute("SELECT COUNT(*) FROM applicants").fetchone()[0]
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
        # ── Inbox: 처리 대기 항목 집계 ──────────────────
        inbox_items = []
        # 휴가 pending
        leave_pending = db.execute(
            "SELECT lr.id, u.name, lr.type, lr.start_date, lr.end_date "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE lr.status='pending' ORDER BY lr.created_at ASC LIMIT 5"
        ).fetchall()
        for r in leave_pending:
            inbox_items.append({
                'id': r['id'], 'category': 'leave',
                'title': f"{r['name']} — {LEAVE_LABELS.get(r['type'], r['type'])} 신청",
                'sub': f"{r['start_date']} ~ {r['end_date']}",
                'link': url_for('attendance')
            })
        # 증명서 pending
        cert_pending = db.execute(
            "SELECT cr.id, u.name, cr.cert_type, cr.purpose "
            "FROM certificate_requests cr JOIN users u ON cr.user_id=u.id "
            "WHERE cr.status='pending' ORDER BY cr.created_at ASC LIMIT 3"
        ).fetchall()
        CERT_LABELS = {'employment':'재직증명서','career':'경력증명서','income':'소득증명','resignation':'퇴직확인서'}
        for r in cert_pending:
            inbox_items.append({
                'id': r['id'], 'category': 'certificate',
                'title': f"{r['name']} — {CERT_LABELS.get(r['cert_type'], r['cert_type'])} 발급 신청",
                'sub': r['purpose'] or '용도 미기재',
                'link': url_for('certificates_hub')
            })
        # 인사발령 pending
        pa_pending = db.execute(
            "SELECT pa.id, u.name, pa.action_type, pa.from_value, pa.to_value "
            "FROM personnel_actions pa JOIN users u ON pa.user_id=u.id "
            "WHERE pa.status='pending' ORDER BY pa.created_at ASC LIMIT 3"
        ).fetchall()
        ACTION_LABELS2 = {'dept_change':'부서이동','position_change':'직급변경','role_change':'역할변경',
                         'employment_type_change':'고용형태변경','manager_change':'상관변경','salary_change':'급여변경'}
        for r in pa_pending:
            inbox_items.append({
                'id': r['id'], 'category': 'personnel',
                'title': f"{r['name']} — {ACTION_LABELS2.get(r['action_type'], r['action_type'])} 기안",
                'sub': f"{r['from_value'] or '—'} → {r['to_value'] or '—'}",
                'link': url_for('employees')
            })
        # 퇴직 submitted
        term_pending = db.execute(
            "SELECT tr.id, u.name, tr.request_type, tr.requested_last_work_date "
            "FROM termination_requests tr JOIN users u ON tr.user_id=u.id "
            "WHERE tr.status IN ('submitted','under_review') ORDER BY tr.created_at ASC LIMIT 3"
        ).fetchall()
        for r in term_pending:
            inbox_items.append({
                'id': r['id'], 'category': 'termination',
                'title': f"{r['name']} — 퇴직 신청",
                'sub': f"최종 근무일 요청: {r['requested_last_work_date']}",
                'link': url_for('termination_requests')
            })
        # ── 승인 허브 확장 (P1-1) ────────────────────────
        # 목표 승인 대기 (제출된 목표 세트)
        goal_pending = db.execute(
            "SELECT u.id AS uid, u.name, COUNT(*) AS cnt, c.name AS cycle_name, c.id AS cid "
            "FROM performance_goals g JOIN users u ON g.user_id=u.id "
            "JOIN performance_cycles c ON g.cycle_id=c.id "
            "WHERE g.approval_status='submitted' AND c.status='active' "
            "GROUP BY u.id, c.id ORDER BY MIN(g.created_at) ASC LIMIT 3"
        ).fetchall()
        for r in goal_pending:
            inbox_items.append({
                'id': r['uid'], 'category': 'goal',
                'title': f"{r['name']} — 목표 승인 요청 ({r['cnt']}개)",
                'sub': r['cycle_name'],
                'link': url_for('performance', cycle=r['cid'])
            })
        # 등급 이의신청
        appeal_rows = db.execute(
            "SELECT ga.id, u.name, ga.old_grade, ga.cycle_id, c.name AS cycle_name "
            "FROM grade_appeals ga JOIN users u ON ga.user_id=u.id "
            "JOIN performance_cycles c ON ga.cycle_id=c.id "
            "WHERE ga.status='pending' ORDER BY ga.created_at ASC LIMIT 3"
        ).fetchall()
        for r in appeal_rows:
            inbox_items.append({
                'id': r['id'], 'category': 'appeal',
                'title': f"{r['name']} — 등급 이의신청 (현재 {r['old_grade']})",
                'sub': r['cycle_name'],
                'link': url_for('performance_appeals', cycle=r['cycle_id'])
            })
        # OT 승인 대기
        ot_rows = db.execute(
            "SELECT o.id, u.name, o.date, o.ot_minutes FROM overtime_requests o "
            "JOIN users u ON o.user_id=u.id WHERE o.status='pending' "
            "ORDER BY o.created_at ASC LIMIT 3"
        ).fetchall()
        for r in ot_rows:
            inbox_items.append({
                'id': r['id'], 'category': 'overtime',
                'title': f"{r['name']} — 연장근로 승인 요청",
                'sub': f"{r['date']} · {r['ot_minutes'] // 60}시간 {r['ot_minutes'] % 60}분",
                'link': url_for('attendance_home', tab='ot')
            })
        # 입사 예정 D-7
        try:
            hires_soon = db.execute(
                "SELECT id, name, start_date FROM incoming_hires "
                "WHERE status='waiting' AND start_date IS NOT NULL AND start_date <= ? "
                "ORDER BY start_date ASC LIMIT 3",
                ((date.today() + timedelta(days=7)).isoformat(),)
            ).fetchall()
        except sqlite3.OperationalError:
            hires_soon = []
        for r in hires_soon:
            dd = (date.fromisoformat(r['start_date']) - date.today()).days
            inbox_items.append({
                'id': r['id'], 'category': 'hire',
                'title': f"{r['name']} — 입사 {'오늘!' if dd == 0 else ('D-%d' % dd if dd > 0 else 'D+%d 경과' % -dd)}",
                'sub': f"입사 예정일 {r['start_date']} · 직원 전환 필요",
                'link': url_for('hires_list')
            })
        # 급여 초안 (월별 1건)
        try:
            draft_rows = db.execute(
                "SELECT year, month, COUNT(*) AS cnt FROM payslips "
                "WHERE status='draft' GROUP BY year, month ORDER BY year DESC, month DESC LIMIT 2"
            ).fetchall()
        except sqlite3.OperationalError:
            draft_rows = []
        for r in draft_rows:
            inbox_items.append({
                'id': f"{r['year']}{r['month']}", 'category': 'payroll',
                'title': f"{r['year']}년 {r['month']}월 급여 초안 {r['cnt']}건",
                'sub': '검토 후 확정·발송 필요 (직원 비공개 상태)',
                'link': url_for('compensation')
            })
        inbox_count = len(inbox_items)
        # ── 신규 위젯 데이터 ─────────────────────────────────
        this_year   = date.today().year
        this_month_n = date.today().month
        payroll_row = db.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(net_pay),0) as total "
            "FROM payslips WHERE year=? AND month=?", (this_year, this_month_n)
        ).fetchone()
        payroll_summary = {'count': payroll_row['cnt'], 'total': payroll_row['total']}
        open_jobs = db.execute(
            "SELECT jp.title, COUNT(a.id) as applicant_count "
            "FROM job_postings jp LEFT JOIN applicants a ON jp.id=a.posting_id "
            "WHERE jp.status='open' GROUP BY jp.id ORDER BY jp.created_at DESC LIMIT 5"
        ).fetchall()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        ot_violations = db.execute(
            "SELECT u.name, u.id as uid, SUM(c.overtime_min) as ot_min "
            "FROM checkins c JOIN users u ON c.user_id=u.id "
            "WHERE c.check_in >= ? AND u.status='active' "
            "GROUP BY c.user_id HAVING SUM(c.overtime_min) > 720 "
            "ORDER BY ot_min DESC LIMIT 5", (week_ago,)
        ).fetchall()
        enabled_widgets = get_widget_prefs(uid, 'admin')
        widget_catalog  = WIDGET_CATALOG['admin']
        if 'recruiting' not in PLAN_FEATURES.get(_current_plan(), _ENTERPRISE_FEATURES):
            widget_catalog = [w for w in widget_catalog if w['key'] != 'open_positions']
        return render_template('dashboard/admin.html',
            greet=greet, today_str=today_str, first_name=first_name,
            total_employees=total_employees, total_departments=total_departments,
            pending_leave=pending_leave, open_postings=open_postings,
            hires_waiting_count=hires_waiting_count,
            total_applicants=total_applicants, recent_employees=recent_employees,
            recent_posts=recent_posts, who_out=who_out,
            inbox_items=inbox_items, inbox_count=inbox_count,
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
        open_postings     = db.execute("SELECT COUNT(*) FROM job_postings WHERE status='open'").fetchone()[0]
        total_applicants  = db.execute('SELECT COUNT(*) FROM applicants').fetchone()[0]
        interview_count   = db.execute("SELECT COUNT(*) FROM applicants WHERE stage='interview'").fetchone()[0]
        month_start       = date.today().replace(day=1).isoformat()
        hired_month       = db.execute(
            "SELECT COUNT(*) FROM applicants WHERE stage='hired' AND created_at>=?", (month_start,)
        ).fetchone()[0]
        recent_applicants = db.execute(
            'SELECT a.name, a.stage, a.created_at, jp.title AS posting_title '
            'FROM applicants a LEFT JOIN job_postings jp ON a.posting_id=jp.id '
            'ORDER BY a.created_at DESC LIMIT 6'
        ).fetchall()
        recent_posts      = db.execute(
            'SELECT id, title, pinned, created_at FROM announcements '
            'ORDER BY pinned DESC, created_at DESC LIMIT 5'
        ).fetchall()
        STAGE_MAP = {
            'applied':'지원접수','screening':'서류심사','interview':'면접',
            'offered':'오퍼발송','hired':'입사확정','rejected':'불합격'
        }
        return render_template('dashboard/recruiter.html',
            greet=greet, today_str=today_str, first_name=first_name,
            open_postings=open_postings, total_applicants=total_applicants,
            interview_count=interview_count, hired_month=hired_month,
            recent_applicants=recent_applicants, recent_posts=recent_posts,
            stage_map=STAGE_MAP, active_page='home')

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
                hire_row = db.execute('SELECT salary FROM incoming_hires WHERE id=?', (from_hire_id,)).fetchone()
                if hire_row and hire_row['salary']:
                    db.execute(
                        'INSERT OR REPLACE INTO employee_salary (user_id, base_salary) VALUES (?, ?)',
                        (new_id, int(hire_row['salary'] / 12))
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


@app.route('/hires')
@recruiter_or_admin
def hires_list():
    db = get_db()
    status_filter = request.args.get('status', 'waiting')
    if status_filter not in ('waiting', 'converted', 'cancelled', 'all'):
        status_filter = 'waiting'

    q = ('SELECT h.*, u.name AS converted_name, u.emp_no AS converted_emp_no '
         'FROM incoming_hires h LEFT JOIN users u ON h.converted_user_id=u.id ')
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

    return render_template('hires/list.html',
                           hires=hires, counts=counts, status_filter=status_filter,
                           source_label=HIRE_SOURCE_LABEL,
                           api_token=api_token,
                           active_page='employees')


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

    if not name:
        flash('이름은 필수입니다.', 'error')
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
                "position", "job_title", "salary", "memo"}
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
        email = str(payload.get('email') or '').strip() or None
        if email:
            if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
                return {'ok': False, 'error': 'email already exists as employee'}, 409
            if conn.execute("SELECT id FROM incoming_hires WHERE email=? AND status='waiting'",
                            (email,)).fetchone():
                return {'ok': False, 'error': 'duplicate waiting hire'}, 409
        cur = conn.execute(
            'INSERT INTO incoming_hires (name, email, phone, start_date, department_name, '
            "position_name, job_title, salary, memo, source) VALUES (?,?,?,?,?,?,?,?,?,'webhook')",
            (name, email, str(payload.get('phone') or '').strip() or None, start,
             str(payload.get('department') or '').strip() or None,
             str(payload.get('position') or '').strip() or None,
             str(payload.get('job_title') or '').strip() or None,
             salary, str(payload.get('memo') or '').strip() or None)
        )
        # 관리자에게 인앱 알림
        admins = conn.execute("SELECT id FROM users WHERE role='admin' AND status='active'").fetchall()
        for a in admins:
            conn.execute(
                'INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                (a['id'], 'action', 'action', '입사 예정자 수신',
                 f'외부 ATS에서 합격자 {name}님이 등록되었습니다.' + (f' 입사 예정일: {start}' if start else ''),
                 '/hires')
            )
        conn.commit()
        return {'ok': True, 'id': cur.lastrowid}, 201
    finally:
        conn.close()


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
        'SELECT d.*, p.name AS parent_name, p.dept_type AS parent_type, COUNT(u.id) AS member_count '
        'FROM departments d '
        'LEFT JOIN departments p ON d.parent_id = p.id '
        'LEFT JOIN users u ON u.department_id = d.id AND u.status="active" '
        'GROUP BY d.id ORDER BY d.dept_type, d.parent_id NULLS FIRST, d.name'
    ).fetchall()
    all_depts = db.execute(
        'SELECT * FROM departments ORDER BY dept_type, parent_id NULLS FIRST, name'
    ).fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    return render_template('admin/departments.html',
                           depts=depts, all_depts=all_depts, poses=poses,
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
        {'key': 'kpi_cards',            'label': '핵심 지표',           'icon': 'fa-chart-bar'},
        {'key': 'inbox',                'label': '인박스',              'icon': 'fa-inbox'},
        {'key': 'quick_actions',        'label': '빠른 실행',           'icon': 'fa-bolt'},
        {'key': 'payroll_summary',      'label': '급여 현황',           'icon': 'fa-won-sign'},
        {'key': 'open_positions',       'label': '채용 중인 포지션',     'icon': 'fa-briefcase'},
        {'key': 'overtime_violations',  'label': '52h 위반 현황',        'icon': 'fa-clock'},
        {'key': 'recent_employees',     'label': '최근 입사자',          'icon': 'fa-user-plus'},
        {'key': 'whos_out',             'label': '오늘 부재중',          'icon': 'fa-door-open'},
        {'key': 'announcements',        'label': '공지사항',            'icon': 'fa-bullhorn'},
    ],
    'manager': [
        {'key': 'kpi_cards',        'label': '핵심 지표',           'icon': 'fa-chart-bar'},
        {'key': 'inbox',            'label': '인박스',              'icon': 'fa-inbox'},
        {'key': 'quick_actions',    'label': '빠른 실행',           'icon': 'fa-bolt'},
        {'key': 'team_performance', 'label': '팀 성과',             'icon': 'fa-chart-line'},
        {'key': 'upcoming_reviews', 'label': '예정된 평가',          'icon': 'fa-calendar-check'},
        {'key': 'whos_out',         'label': '오늘 부재중',          'icon': 'fa-door-open'},
        {'key': 'announcements',    'label': '공지사항',            'icon': 'fa-bullhorn'},
    ],
    'employee': [
        {'key': 'kpi_cards',        'label': '핵심 지표',           'icon': 'fa-chart-bar'},
        {'key': 'quick_actions',    'label': '빠른 실행',           'icon': 'fa-bolt'},
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
@app.route('/approvals')
@manager_or_admin
def approvals_hub():
    db      = get_db()
    uid     = session['user_id']
    role    = session['user_role']
    dept_id = session.get('dept_id') or 0
    is_admin = (role == 'admin')
    today   = date.today()

    groups = []   # [{key, label, icon, items:[{title, sub, requested_at, link}]}]

    # ── 휴가·근태 ──
    if is_admin:
        rows = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, lr.days, lr.status, lr.created_at, u.name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE lr.status IN ('pending','reviewed') ORDER BY lr.created_at ASC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, lr.days, lr.status, lr.created_at, u.name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE lr.status='pending' AND (u.department_id=? OR u.manager_id=?) "
            "ORDER BY lr.created_at ASC", (dept_id, uid)
        ).fetchall()
    groups.append({'key': 'leave', 'label': '휴가·근태', 'icon': 'fa-calendar-times', 'items': [{
        'title': f"{r['name']} — {LEAVE_LABELS.get(r['type'], r['type'])} {r['days']}일",
        'sub': f"{r['start_date']} ~ {r['end_date']}" + (' · 매니저 검토 완료 — HR 최종 승인 대기' if r['status'] == 'reviewed' else ''),
        'requested_at': r['created_at'],
        'link': url_for('attendance_home', tab='approvals'),
    } for r in rows]})

    # ── 연장근로(OT) ──
    if is_admin:
        rows = db.execute(
            "SELECT o.id, o.date, o.ot_minutes, o.reason, o.status, o.created_at, u.name "
            "FROM overtime_requests o JOIN users u ON o.user_id=u.id "
            "WHERE o.status IN ('pending','reviewed') ORDER BY o.created_at ASC").fetchall()
    else:
        rows = db.execute(
            "SELECT o.id, o.date, o.ot_minutes, o.reason, o.status, o.created_at, u.name "
            "FROM overtime_requests o JOIN users u ON o.user_id=u.id "
            "WHERE o.status='pending' AND (u.department_id=? OR u.manager_id=?) "
            "ORDER BY o.created_at ASC", (dept_id, uid)).fetchall()
    groups.append({'key': 'overtime', 'label': '연장근로', 'icon': 'fa-clock', 'items': [{
        'title': f"{r['name']} — 연장근로 {r['ot_minutes'] // 60}시간 {r['ot_minutes'] % 60}분",
        'sub': f"{r['date']}" + (f" · {r['reason']}" if r['reason'] else '')
               + (' · 매니저 검토 완료 — HR 최종 승인 대기' if r['status'] == 'reviewed' else ''),
        'requested_at': r['created_at'],
        'link': url_for('attendance_home', tab='ot'),
    } for r in rows]})

    # ── 목표 승인 ──
    if is_admin:
        rows = db.execute(
            "SELECT u.name, COUNT(*) AS cnt, c.id AS cid, c.name AS cycle_name, MIN(g.created_at) AS created_at "
            "FROM performance_goals g JOIN users u ON g.user_id=u.id "
            "JOIN performance_cycles c ON g.cycle_id=c.id "
            "WHERE g.approval_status='submitted' AND c.status='active' "
            "GROUP BY u.id, c.id ORDER BY created_at ASC").fetchall()
    else:
        rows = db.execute(
            "SELECT u.name, COUNT(*) AS cnt, c.id AS cid, c.name AS cycle_name, MIN(g.created_at) AS created_at "
            "FROM performance_goals g JOIN users u ON g.user_id=u.id "
            "JOIN performance_cycles c ON g.cycle_id=c.id "
            "WHERE g.approval_status='submitted' AND c.status='active' "
            "AND (u.department_id=? OR u.manager_id=?) "
            "GROUP BY u.id, c.id ORDER BY created_at ASC", (dept_id, uid)).fetchall()
    groups.append({'key': 'goal', 'label': '목표 승인', 'icon': 'fa-bullseye', 'items': [{
        'title': f"{r['name']} — 목표 {r['cnt']}개 승인 요청",
        'sub': r['cycle_name'],
        'requested_at': r['created_at'],
        'link': url_for('performance', cycle=r['cid']),
    } for r in rows]})

    # ── 등급 이의신청 ──
    if is_admin:
        rows = db.execute(
            "SELECT ga.id, ga.old_grade, ga.created_at, ga.cycle_id, u.name, c.name AS cycle_name "
            "FROM grade_appeals ga JOIN users u ON ga.user_id=u.id "
            "JOIN performance_cycles c ON ga.cycle_id=c.id "
            "WHERE ga.status='pending' ORDER BY ga.created_at ASC").fetchall()
    else:
        rows = db.execute(
            "SELECT ga.id, ga.old_grade, ga.created_at, ga.cycle_id, u.name, c.name AS cycle_name "
            "FROM grade_appeals ga JOIN users u ON ga.user_id=u.id "
            "JOIN performance_cycles c ON ga.cycle_id=c.id "
            "WHERE ga.status='pending' AND u.manager_id=? ORDER BY ga.created_at ASC", (uid,)).fetchall()
    groups.append({'key': 'appeal', 'label': '등급 이의신청', 'icon': 'fa-gavel', 'items': [{
        'title': f"{r['name']} — 이의신청 (현재 {r['old_grade']}등급)",
        'sub': r['cycle_name'],
        'requested_at': r['created_at'],
        'link': url_for('performance_appeals', cycle=r['cycle_id']),
    } for r in rows]})

    if is_admin:
        # ── 증명서 ──
        CERT_LABELS = {'employment': '재직증명서', 'career': '경력증명서', 'income': '소득증명', 'resignation': '퇴직확인서'}
        rows = db.execute(
            "SELECT cr.id, cr.cert_type, cr.purpose, cr.created_at, u.name "
            "FROM certificate_requests cr JOIN users u ON cr.user_id=u.id "
            "WHERE cr.status='pending' ORDER BY cr.created_at ASC").fetchall()
        groups.append({'key': 'certificate', 'label': '증명서 발급', 'icon': 'fa-file-alt', 'items': [{
            'title': f"{r['name']} — {CERT_LABELS.get(r['cert_type'], r['cert_type'])}",
            'sub': r['purpose'] or '용도 미기재',
            'requested_at': r['created_at'],
            'link': url_for('certificates_hub'),
        } for r in rows]})

        # ── 인사발령 ──
        rows = db.execute(
            "SELECT pa.id, pa.action_type, pa.from_value, pa.to_value, pa.created_at, pa.user_id, u.name "
            "FROM personnel_actions pa JOIN users u ON pa.user_id=u.id "
            "WHERE pa.status='pending' ORDER BY pa.created_at ASC").fetchall()
        groups.append({'key': 'personnel', 'label': '인사발령', 'icon': 'fa-user-edit', 'items': [{
            'title': f"{r['name']} — {ACTION_LABELS.get(r['action_type'], r['action_type'])} 기안",
            'sub': f"{r['from_value'] or '—'} → {(r['to_value'] or '—').split('|')[0]}",
            'requested_at': r['created_at'],
            'link': url_for('employee_detail', emp_id=r['user_id']) + '#hr',
        } for r in rows]})

        # ── 퇴직 ──
        rows = db.execute(
            "SELECT tr.id, tr.requested_last_work_date, tr.created_at, u.name "
            "FROM termination_requests tr JOIN users u ON tr.user_id=u.id "
            "WHERE tr.status IN ('submitted','under_review') ORDER BY tr.created_at ASC").fetchall()
        groups.append({'key': 'termination', 'label': '퇴직', 'icon': 'fa-user-clock', 'items': [{
            'title': f"{r['name']} — 퇴직 신청",
            'sub': f"최종 근무일 요청: {r['requested_last_work_date']}",
            'requested_at': r['created_at'],
            'link': url_for('termination_requests'),
        } for r in rows]})

        # ── 급여 초안 ──
        try:
            rows = db.execute(
                "SELECT year, month, COUNT(*) AS cnt, MIN(created_at) AS created_at "
                "FROM payslips WHERE status='draft' GROUP BY year, month "
                "ORDER BY year DESC, month DESC").fetchall()
        except sqlite3.OperationalError:
            rows = []
        groups.append({'key': 'payroll', 'label': '급여 확정', 'icon': 'fa-file-invoice-dollar', 'items': [{
            'title': f"{r['year']}년 {r['month']}월 급여 초안 {r['cnt']}건",
            'sub': '검토 후 확정·발송 필요 (직원 비공개 상태)',
            'requested_at': r['created_at'],
            'link': url_for('compensation'),
        } for r in rows]})

        # ── 입사 예정 (D-7 이내) ──
        try:
            rows = db.execute(
                "SELECT id, name, start_date, created_at FROM incoming_hires "
                "WHERE status='waiting' AND start_date IS NOT NULL AND start_date <= ? "
                "ORDER BY start_date ASC",
                ((today + timedelta(days=7)).isoformat(),)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        def _dday_label(sd):
            dd = (date.fromisoformat(sd) - today).days
            return '오늘 입사!' if dd == 0 else (f'D-{dd}' if dd > 0 else f'D+{-dd} 경과')
        groups.append({'key': 'hire', 'label': '입사 예정', 'icon': 'fa-door-open', 'items': [{
            'title': f"{r['name']} — 입사 {_dday_label(r['start_date'])}",
            'sub': f"입사 예정일 {r['start_date']} · 직원 전환 필요",
            'requested_at': r['created_at'],
            'link': url_for('hires_list'),
        } for r in rows]})

    groups = [g for g in groups]   # 빈 그룹도 유지 (0건 표시)
    total = sum(len(g['items']) for g in groups)
    return render_template('approvals/hub.html',
                           groups=groups, total=total,
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

    first_day = f"{year}-{month:02d}-01"
    last_day  = f"{year}-{month:02d}-{cal_mod.monthrange(year, month)[1]}"

    holiday_rows   = db.execute('SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?', (first_day, last_day)).fetchall()
    month_holidays = {h['date'] for h in holiday_rows}

    benefit_cfg_rows = db.execute(
        "SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type='monthly_fixed'"
    ).fetchall()
    company_benefits = {r['key']: dict(r) for r in benefit_cfg_rows}

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

        checkins = db.execute(
            'SELECT * FROM checkins WHERE user_id=? AND date BETWEEN ? AND ?',
            (e['id'], first_day, last_day)
        ).fetchall()
        total_ot_pay = 0
        for c in checkins:
            is_h = c['date'] in month_holidays
            res  = calc_extra_pay(
                c['overtime_min'], c['night_min'], e['base_salary'],
                is_holiday=is_h,
                holiday_regular_min=c['regular_min'] if is_h else 0
            )
            total_ot_pay += res['total_extra_pay']

        emp_overrides = {
            r['benefit_key']: dict(r)
            for r in db.execute('SELECT * FROM employee_benefit_overrides WHERE user_id=?', (e['id'],)).fetchall()
        }
        extra_benefits = []
        for key, cfg in company_benefits.items():
            meta     = BENEFIT_CATALOG.get(key, {})
            override = emp_overrides.get(key)
            if override and not override['enabled']:
                continue
            amount = override['amount'] if override else cfg['amount']
            if not amount and cfg.get('pct'):
                amount = int(e['base_salary'] * cfg['pct'] / 100)
            if amount > 0:
                extra_benefits.append({
                    'key': key, 'name': meta.get('name', key),
                    'amount': amount, 'tax_exempt': meta.get('tax_exempt', False),
                    'monthly_limit': meta.get('monthly_limit'),
                })

        result = calc_payslip(
            e['base_salary'], e['meal_allowance'], e['transport_allowance'],
            overtime_pay=total_ot_pay, extra_benefits=extra_benefits,
        )
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
    db = get_db()
    departments = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall()
    cfg = get_company_config()

    # 가장 최근 확정된 캘리브레이션 등급 (직원별)
    latest_grades = {}
    rows = db.execute(
        '''SELECT cr.user_id, cr.final_grade
           FROM calibration_results cr
           JOIN performance_cycles pc ON cr.cycle_id = pc.id
           WHERE cr.final_grade IS NOT NULL
           ORDER BY pc.start_date DESC'''
    ).fetchall()
    for r in rows:
        if r['user_id'] not in latest_grades:
            latest_grades[r['user_id']] = r['final_grade']

    # Merit 기본 인상률 매핑 (company_config 기반)
    MERIT_PCT = {
        'S': float(cfg.get('merit_s', 0.08)) * 100,
        'A': float(cfg.get('merit_a', 0.05)) * 100,
        'B': float(cfg.get('merit_b', 0.03)) * 100,
        'C': float(cfg.get('merit_c', 0.00)) * 100,
        'D': float(cfg.get('merit_d', -0.01)) * 100,
    }

    if request.method == 'POST':
        mode    = request.form.get('mode', 'flat')   # flat | merit
        pct     = float(request.form.get('pct', 0))
        dept_id = request.form.get('dept_id') or None
        reason  = request.form.get('reason', '').strip()
        changer = session['user_id']

        if mode == 'flat' and (pct <= 0 or pct > 100):
            flash('인상률은 0~100% 사이로 입력해주세요.', 'error')
            return redirect(url_for('payroll_bulk_raise'))

        query = (
            "SELECT u.id, s.base_salary, s.meal_allowance, s.transport_allowance "
            "FROM users u JOIN employee_salary s ON u.id=s.user_id "
            "WHERE u.status='active'"
        )
        params = []
        if dept_id:
            query += " AND u.department_id=?"
            params.append(int(dept_id))
        emps = db.execute(query, params).fetchall()

        count = 0
        for e in emps:
            if mode == 'merit':
                grade    = latest_grades.get(e['id'])
                emp_pct  = MERIT_PCT.get(grade, 0) if grade else 0
                r_reason = reason or f'Merit 인상 ({grade or "미평가"} → {emp_pct:+.1f}%)'
            else:
                emp_pct  = pct
                r_reason = reason or f'{pct}% 일괄 인상'

            if emp_pct == 0:
                continue

            new_base = int(e['base_salary'] * (1 + emp_pct / 100))
            db.execute(
                'INSERT INTO salary_history '
                '(user_id, changed_by, old_base_salary, new_base_salary, '
                'old_meal, new_meal, old_transport, new_transport, reason) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (e['id'], changer,
                 e['base_salary'], new_base,
                 e['meal_allowance'], e['meal_allowance'],
                 e['transport_allowance'], e['transport_allowance'],
                 r_reason)
            )
            db.execute(
                'UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
                (new_base, e['id'])
            )
            count += 1
        db.commit()
        label = 'Merit 등급별 인상' if mode == 'merit' else f'{pct}% 일괄 인상'
        log_audit('update', 'salary', None, f'{label} 적용 — {count}명')
        flash(f'{count}명 기본급 {label} 완료했습니다.', 'success')
        return redirect(url_for('payroll_bulk_raise'))

    # GET — 현재 직원 급여 목록 + 최근 성과 등급
    dept_id = request.args.get('dept_id') or None
    query = (
        "SELECT u.id, u.name, d.name dept_name, s.base_salary "
        "FROM users u "
        "JOIN employee_salary s ON u.id=s.user_id "
        "LEFT JOIN departments d ON u.department_id=d.id "
        "WHERE u.status='active'"
    )
    params = []
    if dept_id:
        query += " AND u.department_id=?"
        params.append(int(dept_id))
    query += " ORDER BY d.name, u.name"
    emps = db.execute(query, params).fetchall()

    # 직원별 등급 + Merit 제안 병합
    emp_rows = []
    for e in emps:
        grade       = latest_grades.get(e['id'])
        merit_pct   = MERIT_PCT.get(grade, 0) if grade else None
        new_base    = int(e['base_salary'] * (1 + merit_pct / 100)) if merit_pct else None
        emp_rows.append({
            'id':         e['id'],
            'name':       e['name'],
            'dept_name':  e['dept_name'],
            'base_salary': e['base_salary'],
            'grade':      grade,
            'merit_pct':  merit_pct,
            'new_base':   new_base,
        })

    return render_template('payroll/bulk_raise.html',
                           emps=emp_rows,
                           departments=departments,
                           selected_dept=dept_id,
                           merit_pct=MERIT_PCT,
                           cfg=cfg,
                           active_page='admin_payroll')


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
                  f'급여 저장 완료 — ⚠️ 최저임금 미달 (부족 {fmt_krw(mw["shortage"])}원)',
                  'success' if mw['ok'] else 'warning')

        elif action == 'generate':
            year  = int(request.form.get('year', datetime.date.today().year))
            month = int(request.form.get('month', datetime.date.today().month))
            if not (1 <= month <= 12):
                flash('올바른 월을 입력해주세요.', 'danger')
                return redirect(url_for('compensation', tab='ops'))
            else:
                first_day = f"{year}-{month:02d}-01"
                last_day  = f"{year}-{month:02d}-{cal_mod.monthrange(year, month)[1]}"
                holiday_dates = {h['date'] for h in db.execute(
                    'SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?', (first_day, last_day)
                ).fetchall()}
                benefit_cfgs = {r['key']: dict(r) for r in db.execute(
                    "SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type='monthly_fixed'"
                ).fetchall()}
                emps_sal = db.execute(
                    "SELECT u.id, s.base_salary, s.meal_allowance, s.transport_allowance "
                    "FROM users u JOIN employee_salary s ON u.id=s.user_id WHERE u.status='active'"
                ).fetchall()
                count = 0
                for e in emps_sal:
                    if db.execute('SELECT 1 FROM payslips WHERE user_id=? AND year=? AND month=?',
                                  (e['id'], year, month)).fetchone():
                        continue
                    checkins = db.execute(
                        'SELECT * FROM checkins WHERE user_id=? AND date BETWEEN ? AND ?',
                        (e['id'], first_day, last_day)
                    ).fetchall()
                    # 기존 버그 수정: calc_extra_pay 인자 순서 오류 + dict 합산 크래시
                    # (근태 기록이 있는 달에 급여 생성 시 500 — admin_payroll 경로와 동일 패턴으로 통일)
                    total_ot_pay = 0
                    for c in checkins:
                        is_h = c['date'] in holiday_dates
                        res  = calc_extra_pay(
                            c['overtime_min'] or 0, c['night_min'] or 0, e['base_salary'],
                            is_holiday=is_h,
                            holiday_regular_min=(c['regular_min'] or 0) if is_h else 0
                        )
                        total_ot_pay += res['total_extra_pay']
                    extra_benefits = {}
                    for key, bcfg in benefit_cfgs.items():
                        extra_benefits[key] = (bcfg['amount'] if bcfg['amount_type'] == 'fixed'
                                               else int(e['base_salary'] * bcfg['amount'] / 100))
                    emp_d     = db.execute('SELECT birth_date, gender FROM users WHERE id=?', (e['id'],)).fetchone()
                    is_female = (emp_d['gender'] == 'F') if emp_d and emp_d['gender'] else False
                    dependents = db.execute(
                        'SELECT * FROM employee_dependents WHERE user_id=?', (e['id'],)
                    ).fetchall()
                    result = calc_payslip(
                        base_salary=e['base_salary'], meal_allowance=e['meal_allowance'],
                        transport_allowance=e['transport_allowance'],
                        overtime_pay=total_ot_pay,
                        extra_benefits=extra_benefits, dependents=dependents, is_female=is_female
                    )
                    bonus_pay = result.get('benefits_gross', 0)
                    db.execute(
                        'INSERT INTO payslips '
                        '(user_id, year, month, base_salary, meal_allowance, transport_allowance, '
                        'overtime_pay, bonus_pay, national_pension, health_insurance, long_term_care, '
                        'employment_insurance, income_tax, local_income_tax, '
                        'gross_pay, total_deduction, net_pay, benefits_json, '
                        'income_deduction, earned_income, total_personal_deduction, '
                        'num_dependents, child_tax_credit_amount, status) '
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft')",
                        (e['id'], year, month,
                         result['base_salary'], result['meal_allowance'], result['transport_allowance'],
                         result['overtime_pay'], bonus_pay,
                         result['national_pension'], result['health_insurance'],
                         result['long_term_care'], result['employment_insurance'],
                         result['income_tax'], result['local_income_tax'],
                         result['gross_pay'], result['total_deduction'], result['net_pay'],
                         _json.dumps(result.get('benefits_breakdown', []), ensure_ascii=False),
                         result['income_deduction'], result['earned_income'],
                         result['total_personal_deduction'], result['num_dependents'],
                         result['child_tax_credit_amount'])
                    )
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
            flash('Merit Matrix가 저장되었습니다.', 'success')
            _tab = 'acr'

        elif action == 'bulk_raise':
            mode    = request.form.get('mode', 'flat')
            pct     = float(request.form.get('pct', 0))
            dept_id = request.form.get('dept_id') or None
            reason  = request.form.get('reason', '').strip()
            changer = session['user_id']
            MERIT_PCT = {g: float(cfg.get(f'merit_{g.lower()}', 0)) * 100
                         for g in ['S', 'A', 'B', 'C', 'D']}
            latest_grades = {}
            for r in db.execute(
                "SELECT cr.user_id, cr.final_grade FROM calibration_results cr "
                "JOIN performance_cycles pc ON cr.cycle_id=pc.id "
                "WHERE cr.final_grade IS NOT NULL ORDER BY pc.start_date DESC"
            ).fetchall():
                if r['user_id'] not in latest_grades:
                    latest_grades[r['user_id']] = r['final_grade']
            query  = ("SELECT u.id, s.base_salary FROM users u "
                      "JOIN employee_salary s ON u.id=s.user_id WHERE u.status='active'")
            params = []
            if dept_id:
                query += " AND u.department_id=?"
                params.append(int(dept_id))
            count = 0
            for e in db.execute(query, params).fetchall():
                emp_pct = (MERIT_PCT.get(latest_grades.get(e['id']), 0)
                           if mode == 'merit' else pct)
                if emp_pct == 0:
                    continue
                new_base = int(e['base_salary'] * (1 + emp_pct / 100))
                db.execute(
                    'INSERT INTO salary_history (user_id, changed_by, old_base_salary, new_base_salary, reason) '
                    'VALUES (?,?,?,?,?)',
                    (e['id'], changer, e['base_salary'], new_base,
                     reason or f'{"Merit" if mode == "merit" else "일괄"} 인상 {emp_pct:+.1f}%')
                )
                db.execute('UPDATE employee_salary SET base_salary=? WHERE user_id=?', (new_base, e['id']))
                count += 1
            db.commit()
            log_audit('update', 'salary', None, f'{"Merit" if mode == "merit" else "일괄"} 인상 적용 — {count}명')
            flash(f'인상 완료 — {count}명 적용', 'success')
            _tab = 'analysis'

        elif action == 'merit_apply':
            emp_ids = request.form.getlist('emp_id')
            changer = session['user_id']
            count = 0
            for eid in emp_ids:
                eid = int(eid)
                pct = float(request.form.get(f'pct_{eid}', 0) or 0)
                if pct == 0:
                    continue
                old = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (eid,)).fetchone()
                if not old:
                    continue
                new_base = int(old['base_salary'] * (1 + pct / 100))
                perf_grade = request.form.get(f'grade_{eid}', '')
                db.execute(
                    'INSERT INTO salary_history (user_id, changed_by, old_base_salary, new_base_salary, reason) '
                    'VALUES (?,?,?,?,?)',
                    (eid, changer, old['base_salary'], new_base,
                     f'성과 연동 인상 {pct:+.1f}% (등급: {perf_grade})')
                )
                db.execute(
                    'UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
                    (new_base, eid)
                )
                count += 1
            db.commit()
            log_audit('update', 'salary', None, f'성과 연동 급여 반영 — {count}명')
            flash(f'급여 반영 완료 — {count}명', 'success')
            _tab = 'acr'

        return redirect(url_for('compensation', tab=_tab))

    # ── GET ──────────────────────────────────────────────────────────────────
    today       = datetime.date.today()
    today_year  = today.year
    today_month = today.month
    _tab        = request.args.get('tab', 'ops')

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
        'SELECT c.*, u.name creator_name FROM compensation_review_cycles c '
        'LEFT JOIN users u ON c.created_by=u.id ORDER BY c.id DESC'
    ).fetchall()

    departments = db.execute('SELECT id, name FROM departments ORDER BY name').fetchall()

    # ── 성과 연동 급여 검토 데이터 (ACR 탭) ──────────────────────────────────
    raw_merit = db.execute(
        '''SELECT u.id, u.name, d.name dept_name, p.name pos_name,
                  COALESCE(s.base_salary, 0) base_salary,
                  sg.mid_salary,
                  (SELECT cr.final_grade
                   FROM calibration_results cr
                   JOIN performance_cycles pc ON cr.cycle_id = pc.id
                   WHERE cr.user_id = u.id AND cr.final_grade IS NOT NULL
                   ORDER BY pc.start_date DESC LIMIT 1) perf_grade,
                  (SELECT cr.downgrade_reason
                   FROM calibration_results cr
                   JOIN performance_cycles pc ON cr.cycle_id = pc.id
                   WHERE cr.user_id = u.id AND cr.final_grade IS NOT NULL
                   ORDER BY pc.start_date DESC LIMIT 1) downgrade_reason,
                  (SELECT COUNT(*) FROM succession_plans sp
                   WHERE sp.candidate_id = u.id) is_key_talent
           FROM users u
           LEFT JOIN departments d ON u.department_id = d.id
           LEFT JOIN positions   p ON u.position_id   = p.id
           LEFT JOIN employee_salary s ON u.id = s.user_id
           LEFT JOIN salary_grades sg ON sg.position_id   = u.position_id
                                     AND sg.job_family_id = u.job_family_id
           WHERE u.status = 'active' AND u.role NOT IN ('admin','guest')
           ORDER BY d.name, u.name'''
    ).fetchall()
    from datetime import date as _date, datetime as _datetime
    merit_review_rows = []
    for r in raw_merit:
        ratio   = calc_compa_ratio(r['base_salary'], r['mid_salary'])
        band    = _compa_band(ratio)
        sug_pct = merit_from_matrix(db, r['perf_grade'] or 'B', ratio)
        # Flight Risk 자동 감지
        fr_reasons = []
        if ratio is not None and ratio < 0.85:
            fr_reasons.append(f'Compa {ratio:.2f}')
        if r['perf_grade'] in ('C', 'D'):
            fr_reasons.append(f'성과 {r["perf_grade"]}')
        recent_promo = db.execute(
            "SELECT id FROM personnel_actions WHERE user_id=? AND action_type='promotion' AND applied_at >= date('now','-2 years') LIMIT 1",
            (r['id'],)
        ).fetchone()
        hire_date = db.execute("SELECT hire_date FROM users WHERE id=?", (r['id'],)).fetchone()
        if not recent_promo and hire_date and hire_date['hire_date']:
            try:
                hd = _datetime.strptime(hire_date['hire_date'][:10], '%Y-%m-%d').date()
                if (_date.today() - hd).days > 730:
                    fr_reasons.append('미승진 2년+')
            except Exception:
                pass
        flight_risk = len(fr_reasons) >= 2
        merit_review_rows.append({**dict(r), 'compa_ratio': ratio,
                                  'compa_band': band, 'suggested_pct': sug_pct,
                                  'flight_risk': flight_risk, 'flight_risk_reasons': fr_reasons})
    merit_target_count = sum(1 for r in merit_review_rows if r['perf_grade'])
    merit_avg_pct = (
        round(sum(r['suggested_pct'] for r in merit_review_rows if r['perf_grade']) / merit_target_count, 1)
        if merit_target_count else 0
    )
    merit_total_increase = sum(
        int(r['base_salary'] * (r['suggested_pct'] / 100))
        for r in merit_review_rows if r['perf_grade']
    )

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
        merit_review_rows=merit_review_rows,
        merit_target_count=merit_target_count,
        merit_avg_pct=merit_avg_pct,
        merit_total_increase=merit_total_increase,
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
    cfg  = get_company_config()
    name = request.form.get('name', '').strip()
    effective_date = request.form.get('effective_date', '').strip()
    mode = request.form.get('mode', 'zero')          # zero | flat | merit
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

    merit_rates = {'S': float(cfg.get('merit_s', 0.08) or 0), 'A': float(cfg.get('merit_a', 0.05) or 0),
                   'B': float(cfg.get('merit_b', 0.03) or 0), 'C': float(cfg.get('merit_c', 0.0) or 0),
                   'D': float(cfg.get('merit_d', -0.01) or 0)}

    cur = db.execute(
        'INSERT INTO salary_adjustments (name, effective_date, created_by) VALUES (?,?,?)',
        (name, effective_date, session['user_id'])
    )
    adj_id = cur.lastrowid
    for e in emps:
        if mode == 'flat':
            pct = flat_pct
        elif mode == 'merit':
            pct = round(merit_rates.get(e['perf_grade'] or '', 0) * 100, 1)
        else:
            pct = 0
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
            flash('Merit Matrix가 저장되었습니다.', 'success')
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
                msg = (f'급여가 저장되었으나 ⚠️ 최저임금 미달입니다. '
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
                first_day = f"{year}-{month:02d}-01"
                last_day  = f"{year}-{month:02d}-{cal_mod.monthrange(year, month)[1]}"

                # 해당 월의 공휴일 목록
                holiday_rows   = db.execute('SELECT date FROM public_holidays WHERE date BETWEEN ? AND ?', (first_day, last_day)).fetchall()
                month_holidays = {h['date'] for h in holiday_rows}

                # 월 급여 반영 항목만 로드 (payment_type='monthly_fixed')
                benefit_cfg_rows = db.execute(
                    "SELECT * FROM benefit_configs WHERE enabled=1 AND payment_type='monthly_fixed'"
                ).fetchall()
                company_benefits = {r['key']: dict(r) for r in benefit_cfg_rows}

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

                    # 근태 수당 계산 (체크인 기반)
                    checkins = db.execute(
                        'SELECT * FROM checkins WHERE user_id=? AND date BETWEEN ? AND ?',
                        (e['id'], first_day, last_day)
                    ).fetchall()
                    total_ot_pay = 0
                    for c in checkins:
                        is_h = c['date'] in month_holidays
                        res = calc_extra_pay(
                            c['overtime_min'], c['night_min'], e['base_salary'],
                            is_holiday=is_h,
                            holiday_regular_min=c['regular_min'] if is_h else 0
                        )
                        total_ot_pay += res['total_extra_pay']

                    # 승인된 OT 신청 추가 반영 (체크인이 없는 날의 OT 포함)
                    ot_rows = db.execute(
                        "SELECT ot_minutes, date FROM overtime_requests "
                        "WHERE user_id=? AND status='approved' AND date BETWEEN ? AND ?",
                        (e['id'], first_day, last_day)
                    ).fetchall()
                    # 이미 checkin에 반영된 날짜 제외 (중복 방지)
                    checkin_dates = {c['date'] for c in checkins}
                    for ot in ot_rows:
                        if ot['date'] not in checkin_dates:
                            ot_res = calc_extra_pay(
                                ot['ot_minutes'], 0, e['base_salary'],
                                is_holiday=(ot['date'] in month_holidays)
                            )
                            total_ot_pay += ot_res['total_extra_pay']

                    # 복리후생 항목 구성 (직원별 오버라이드 우선)
                    emp_overrides = {
                        r['benefit_key']: dict(r)
                        for r in db.execute(
                            'SELECT * FROM employee_benefit_overrides WHERE user_id=?',
                            (e['id'],)
                        ).fetchall()
                    }
                    extra_benefits = []
                    for key, cfg in company_benefits.items():
                        meta = BENEFIT_CATALOG.get(key, {})
                        # 직원 오버라이드 확인
                        override = emp_overrides.get(key)
                        if override and not override['enabled']:
                            continue   # 이 직원은 해당 항목 제외
                        amount = override['amount'] if override else cfg['amount']
                        # pct 기반 계산 (명절상여, 성과급)
                        if not amount and cfg.get('pct'):
                            amount = int(e['base_salary'] * cfg['pct'] / 100)
                        if amount > 0:
                            extra_benefits.append({
                                'key':           key,
                                'name':          meta.get('name', key),
                                'amount':        amount,
                                'tax_exempt':    meta.get('tax_exempt', False),
                                'monthly_limit': meta.get('monthly_limit'),
                            })

                    # 부양가족 조회 (소득세 정확 계산용)
                    dependents = db.execute(
                        'SELECT * FROM employee_dependents WHERE user_id=?', (e['id'],)
                    ).fetchall()
                    emp_info = db.execute(
                        'SELECT gender, marital_status FROM users WHERE id=?', (e['id'],)
                    ).fetchone()
                    is_female = emp_info and emp_info['gender'] == 'F'

                    result = calc_payslip(
                        e['base_salary'],
                        e['meal_allowance'],
                        e['transport_allowance'],
                        overtime_pay=total_ot_pay,
                        extra_benefits=extra_benefits,
                        dependents=dependents,
                        is_female=is_female,
                    )
                    bonus_pay = result.get('benefits_gross', 0)

                    db.execute(
                        'INSERT INTO payslips '
                        '(user_id, year, month, base_salary, meal_allowance, transport_allowance, '
                        'overtime_pay, bonus_pay, national_pension, health_insurance, long_term_care, '
                        'employment_insurance, income_tax, local_income_tax, '
                        'gross_pay, total_deduction, net_pay, benefits_json, '
                        'income_deduction, earned_income, total_personal_deduction, '
                        'num_dependents, child_tax_credit_amount, status) '
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft')",
                        (e['id'], year, month,
                         result['base_salary'], result['meal_allowance'],
                         result['transport_allowance'], result['overtime_pay'], bonus_pay,
                         result['national_pension'], result['health_insurance'],
                         result['long_term_care'], result['employment_insurance'],
                         result['income_tax'], result['local_income_tax'],
                         result['gross_pay'], result['total_deduction'], result['net_pay'],
                         _json.dumps(result.get('benefits_breakdown', []), ensure_ascii=False),
                         result['income_deduction'], result['earned_income'],
                         result['total_personal_deduction'], result['num_dependents'],
                         result['child_tax_credit_amount'])
                    )
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


# ── v0.52: ACR 워크플로우 ────────────────────────────────────────────────────
@app.route('/payroll/acr')
@admin_required
def acr_list():
    """ACR 주기 목록 + 생성"""
    db = get_db()
    cycles = db.execute(
        'SELECT c.*, u.name creator_name FROM compensation_review_cycles c '
        'LEFT JOIN users u ON c.created_by = u.id '
        'ORDER BY c.id DESC'
    ).fetchall()
    return render_template('payroll/acr_list.html', cycles=cycles,
                           active_page='acr')


@app.route('/payroll/acr/new', methods=['POST'])
@admin_required
def acr_new():
    db = get_db()
    name   = request.form.get('name', '').strip()
    year   = int(request.form.get('review_year', 2026))
    eff    = request.form.get('effective_date', '').strip() or None
    if not name:
        flash('주기 이름을 입력해주세요.', 'danger')
        return redirect(url_for('acr_list'))
    db.execute(
        'INSERT INTO compensation_review_cycles (name, review_year, effective_date, created_by) '
        'VALUES (?,?,?,?)',
        (name, year, eff, session['user_id'])
    )
    db.commit()
    flash(f'"{name}" ACR 주기가 생성되었습니다.', 'success')
    return redirect(url_for('acr_list'))


@app.route('/payroll/acr/<int:cycle_id>/open', methods=['POST'])
@admin_required
def acr_open(cycle_id):
    db = get_db()
    db.execute("UPDATE compensation_review_cycles SET status='open' WHERE id=?", (cycle_id,))
    # 활성 직원 전원 draft 레코드 생성
    emps = db.execute(
        "SELECT u.id, COALESCE(s.base_salary,0) base_salary "
        "FROM users u LEFT JOIN employee_salary s ON u.id=s.user_id "
        "WHERE u.status='active' AND u.role NOT IN ('admin','guest')"
    ).fetchall()
    for e in emps:
        mgr = db.execute('SELECT manager_id FROM users WHERE id=?', (e['id'],)).fetchone()
        db.execute(
            'INSERT OR IGNORE INTO compensation_reviews '
            '(cycle_id, employee_id, manager_id, current_salary) VALUES (?,?,?,?)',
            (cycle_id, e['id'], mgr['manager_id'] if mgr else None, e['base_salary'])
        )
    db.commit()
    flash('ACR이 오픈되었습니다. 매니저가 인상안을 입력할 수 있습니다.', 'success')
    return redirect(url_for('acr_list'))


@app.route('/payroll/acr/<int:cycle_id>/close', methods=['POST'])
@admin_required
def acr_close(cycle_id):
    db = get_db()
    db.execute("UPDATE compensation_review_cycles SET status='closed' WHERE id=?", (cycle_id,))
    db.commit()
    flash('ACR 주기가 마감되었습니다.', 'success')
    return redirect(url_for('acr_list'))


@app.route('/payroll/acr/<int:cycle_id>')
@login_required
def acr_detail(cycle_id):
    """매니저: 내 팀 인상안 입력 / HR Admin: 전체 검토"""
    from payroll_utils import calc_compa_ratio, compa_band, merit_from_matrix
    db     = get_db()
    cycle  = db.execute('SELECT * FROM compensation_review_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)

    role = session['user_role']

    if role == 'admin':
        # HR: 전체 조회
        reviews = db.execute(
            '''SELECT cr.*, u.name emp_name, d.name dept_name, p.name pos_name,
                      m.name mgr_name,
                      sg.mid_salary,
                      (SELECT cr2.final_grade FROM calibration_results cr2
                       WHERE cr2.user_id = cr.employee_id
                       ORDER BY cr2.id DESC LIMIT 1) perf_grade
               FROM compensation_reviews cr
               JOIN users u ON cr.employee_id = u.id
               LEFT JOIN departments d ON u.department_id = d.id
               LEFT JOIN positions   p ON u.position_id   = p.id
               LEFT JOIN users       m ON cr.manager_id   = m.id
               LEFT JOIN salary_grades sg ON sg.position_id   = u.position_id
                                         AND sg.job_family_id = u.job_family_id
               WHERE cr.cycle_id = ?
               ORDER BY d.name, u.name''',
            (cycle_id,)
        ).fetchall()
    else:
        # 매니저: 자기 팀만
        reviews = db.execute(
            '''SELECT cr.*, u.name emp_name, d.name dept_name, p.name pos_name,
                      sg.mid_salary,
                      (SELECT cr2.final_grade FROM calibration_results cr2
                       WHERE cr2.user_id = cr.employee_id
                       ORDER BY cr2.id DESC LIMIT 1) perf_grade
               FROM compensation_reviews cr
               JOIN users u ON cr.employee_id = u.id
               LEFT JOIN departments d ON u.department_id = d.id
               LEFT JOIN positions   p ON u.position_id   = p.id
               LEFT JOIN salary_grades sg ON sg.position_id   = u.position_id
                                         AND sg.job_family_id = u.job_family_id
               WHERE cr.cycle_id = ? AND cr.manager_id = ?
               ORDER BY u.name''',
            (cycle_id, session['user_id'])
        ).fetchall()

    # Merit Matrix 가이드 + Compa-Ratio 계산
    review_rows = []
    for r in reviews:
        ratio     = calc_compa_ratio(r['current_salary'], r['mid_salary'])
        band      = compa_band(ratio)
        suggested = merit_from_matrix(db, r['perf_grade'] or 'B', ratio)
        review_rows.append({**dict(r), 'compa_ratio': ratio,
                             'compa_band': band, 'suggested_pct': suggested})

    matrix_rows = db.execute('SELECT * FROM merit_matrix').fetchall()
    matrix = {(m['performance_grade'], m['compa_band']): m['increase_pct'] for m in matrix_rows}

    return render_template('payroll/acr.html',
                           cycle=cycle, reviews=review_rows,
                           matrix=matrix, role=role,
                           active_page='acr')


@app.route('/payroll/acr/<int:cycle_id>/submit', methods=['POST'])
@login_required
def acr_submit(cycle_id):
    """매니저: 인상안 저장 + 제출"""
    db    = get_db()
    cycle = db.execute('SELECT * FROM compensation_review_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle or cycle['status'] != 'open':
        flash('열린 ACR 주기가 아닙니다.', 'danger')
        return redirect(url_for('acr_list'))

    action = request.form.get('action', 'save')   # save | submit
    emp_ids = request.form.getlist('emp_id')

    for eid in emp_ids:
        eid  = int(eid)
        pct  = float(request.form.get(f'pct_{eid}', 0) or 0)
        note = request.form.get(f'note_{eid}', '').strip()
        cur  = db.execute(
            'SELECT current_salary FROM compensation_reviews WHERE cycle_id=? AND employee_id=?',
            (cycle_id, eid)
        ).fetchone()
        if not cur:
            continue
        proposed = int(cur['current_salary'] * (1 + pct / 100))
        new_status = 'submitted' if action == 'submit' else 'pending'
        db.execute(
            '''UPDATE compensation_reviews
               SET proposed_increase_pct=?, proposed_salary=?,
                   manager_note=?, status=?
               WHERE cycle_id=? AND employee_id=?''',
            (pct, proposed, note, new_status, cycle_id, eid)
        )
    db.commit()
    if action == 'submit':
        flash('인상안이 HR에 제출되었습니다.', 'success')
    else:
        flash('임시저장 완료.', 'success')
    return redirect(url_for('acr_detail', cycle_id=cycle_id))


@app.route('/payroll/acr/<int:cycle_id>/approve', methods=['POST'])
@admin_required
def acr_approve(cycle_id):
    """HR: 개별 또는 일괄 승인 + 급여 반영"""
    db     = get_db()
    cycle  = db.execute('SELECT * FROM compensation_review_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)

    action  = request.form.get('action', 'approve_all')
    emp_ids = request.form.getlist('emp_id')
    if action == 'approve_all':
        # 제출된 것 전부
        rows = db.execute(
            "SELECT * FROM compensation_reviews WHERE cycle_id=? AND status='submitted'",
            (cycle_id,)
        ).fetchall()
        emp_ids = [str(r['employee_id']) for r in rows]

    approved = 0
    for eid in emp_ids:
        eid  = int(eid)
        rev  = db.execute(
            'SELECT * FROM compensation_reviews WHERE cycle_id=? AND employee_id=?',
            (cycle_id, eid)
        ).fetchone()
        if not rev:
            continue

        # HR 오버라이드 있으면 적용
        override_pct = request.form.get(f'hr_pct_{eid}')
        hr_note      = request.form.get(f'hr_note_{eid}', '').strip()
        if override_pct:
            override_pct   = float(override_pct)
            override_salary = int(rev['current_salary'] * (1 + override_pct / 100))
            db.execute(
                'UPDATE compensation_reviews SET hr_override_pct=?, hr_override_salary=?, hr_note=? '
                'WHERE cycle_id=? AND employee_id=?',
                (override_pct, override_salary, hr_note, cycle_id, eid)
            )
            final_salary = override_salary
            final_pct    = override_pct
        else:
            final_salary = rev['proposed_salary'] or rev['current_salary']
            final_pct    = rev['proposed_increase_pct'] or 0

        # salary_history 기록
        old = db.execute('SELECT * FROM employee_salary WHERE user_id=?', (eid,)).fetchone()
        if old:
            db.execute(
                'INSERT INTO salary_history '
                '(user_id, changed_by, old_base_salary, new_base_salary, '
                'old_meal, new_meal, old_transport, new_transport, reason) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (eid, session['user_id'],
                 old['base_salary'], final_salary,
                 old['meal_allowance'], old['meal_allowance'],
                 old['transport_allowance'], old['transport_allowance'],
                 f'ACR {cycle["name"]} 승인 (인상률 {final_pct:.1f}%)')
            )
        # 급여 업데이트
        db.execute(
            'INSERT INTO employee_salary (user_id, base_salary, meal_allowance, transport_allowance) '
            'VALUES (?,?,COALESCE((SELECT meal_allowance FROM employee_salary WHERE user_id=?),0),'
            'COALESCE((SELECT transport_allowance FROM employee_salary WHERE user_id=?),0)) '
            'ON CONFLICT(user_id) DO UPDATE SET base_salary=excluded.base_salary, '
            'updated_at=CURRENT_TIMESTAMP',
            (eid, final_salary, eid, eid)
        )
        # review 상태 업데이트
        db.execute(
            "UPDATE compensation_reviews SET status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP "
            'WHERE cycle_id=? AND employee_id=?',
            (session['user_id'], cycle_id, eid)
        )
        # 인앱 알림
        add_notification(eid, 'info', 'payroll',
                         'ACR 급여 인상 확정',
                         f'급여 인상이 확정되었습니다. ({final_pct:+.1f}% → {final_salary:,}원)',
                         link=f'/payroll/{cycle["review_year"]}/1')
        approved += 1

    db.commit()
    log_audit('update', 'salary', None, f'ACR 급여 인상 승인·반영 — {approved}명')
    flash(f'{approved}명 급여 인상이 승인되고 반영되었습니다.', 'success')
    return redirect(url_for('acr_detail', cycle_id=cycle_id))


# ── v0.53: Total Compensation Statement ─────────────────────────────────────
@app.route('/payroll/total-compensation/<int:uid>')
@login_required
def total_compensation(uid):
    from payroll_utils import calc_compa_ratio, calc_severance, BENEFIT_CATALOG
    role = session['user_role']
    if role not in ('admin',) and session['user_id'] != uid:
        abort(403)
    db  = get_db()
    emp = db.execute(
        'SELECT u.*, d.name dept_name, p.name pos_name, jf.name jf_name '
        'FROM users u '
        'LEFT JOIN departments d  ON u.department_id = d.id '
        'LEFT JOIN positions   p  ON u.position_id   = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id = jf.id '
        'WHERE u.id=?', (uid,)
    ).fetchone()
    if not emp:
        abort(404)

    year = int(request.args.get('year', 2026))

    # 연간 급여 합계
    payslips = db.execute(
        "SELECT * FROM payslips WHERE user_id=? AND year=? AND status='confirmed' ORDER BY month",
        (uid, year)
    ).fetchall()
    total_gross   = sum(p['gross_pay']  for p in payslips)
    total_net     = sum(p['net_pay']    for p in payslips)
    total_base    = sum(p['base_salary'] for p in payslips)
    total_bonus   = sum((p['bonus_pay'] if p['bonus_pay'] else 0) for p in payslips)
    months_paid   = len(payslips)

    # 현재 기본급
    salary_row = db.execute('SELECT base_salary FROM employee_salary WHERE user_id=?', (uid,)).fetchone()
    base_salary = salary_row['base_salary'] if salary_row else 0

    # 복리후생 연간 추정액
    benefit_cfgs = db.execute("SELECT * FROM benefit_configs WHERE enabled=1").fetchall()
    benefit_total = 0
    benefit_items = []
    for cfg in benefit_cfgs:
        key  = cfg['key']
        meta = BENEFIT_CATALOG.get(key, {})
        monthly = cfg['amount'] or 0
        annual  = monthly * 12
        if annual > 0:
            benefit_items.append({'name': meta.get('name', key), 'annual': annual,
                                   'tax_exempt': meta.get('tax_exempt', False)})
            benefit_total += annual

    # 퇴직금 적립 추정 (연간 기본급 / 12)
    severance_accrual = base_salary  # 1년치 기본급 = 퇴직금 적립액

    # 성과등급 + 상여 배수
    cal = db.execute(
        'SELECT final_grade FROM calibration_results WHERE user_id=? ORDER BY id DESC LIMIT 1',
        (uid,)
    ).fetchone()
    perf_grade = cal['final_grade'] if cal else None
    bonus_cfg = db.execute(
        'SELECT bonus_months FROM grade_bonus_config WHERE grade=?',
        (perf_grade or 'B',)
    ).fetchone()
    bonus_months = bonus_cfg['bonus_months'] if bonus_cfg else 0
    estimated_bonus = int(base_salary / 12 * bonus_months)

    # Compa-Ratio
    band = db.execute(
        'SELECT min_salary, mid_salary, max_salary FROM salary_grades '
        'WHERE position_id=? AND job_family_id=?',
        (emp['position_id'], emp['job_family_id'])
    ).fetchone() if emp['position_id'] and emp['job_family_id'] else None
    compa = calc_compa_ratio(base_salary, band['mid_salary'] if band else None)

    total_comp = total_gross + benefit_total + severance_accrual

    return render_template('payroll/total_comp.html',
                           emp=emp, year=year,
                           payslips=payslips, months_paid=months_paid,
                           total_gross=total_gross, total_net=total_net,
                           total_base=total_base, total_bonus=total_bonus,
                           base_salary=base_salary,
                           benefit_items=benefit_items, benefit_total=benefit_total,
                           severance_accrual=severance_accrual,
                           perf_grade=perf_grade, bonus_months=bonus_months,
                           estimated_bonus=estimated_bonus,
                           band=band, compa_ratio=compa,
                           total_comp=total_comp,
                           fmt_krw=fmt_krw,
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
                           active_page='performance')

@app.route('/performance/goals/ai-assist', methods=['POST'])
@login_required
def performance_goal_ai_assist():
    """Grok AI로 OKR/KPI 작성 도움 — 키 없으면 rule-based fallback"""
    import urllib.request, urllib.error, json as _json

    title    = request.json.get('title', '').strip()
    category = request.json.get('category', 'KPI')
    job      = request.json.get('job', '')

    if not title:
        return {'error': '목표 제목을 먼저 입력해주세요.'}, 400

    grok_key = os.environ.get('GROK_API_KEY', '')

    # ── Grok API 호출 ─────────────────────────────────────
    if grok_key:
        system_prompt = (
            "당신은 HR 성과관리 전문가입니다. "
            "직원이 작성한 목표를 SMART 기준(Specific·Measurable·Achievable·Relevant·Time-bound)에 맞게 "
            "개선하고, 측정 기준과 목표치가 명확하도록 도와주세요. "
            "한국어로 답변하고, JSON 형식으로 반환하세요."
        )
        user_prompt = (
            f"직원 직무: {job or '미입력'}\n"
            f"목표 유형: {category}\n"
            f"현재 작성한 목표: {title}\n\n"
            "다음 JSON 형식으로 개선안을 제시해주세요:\n"
            '{"improved_title": "개선된 목표 제목", '
            '"reason": "개선 이유 (1~2문장)", '
            '"smart_check": {"S": true/false, "M": true/false, "A": true/false, "R": true/false, "T": true/false}, '
            '"tips": ["팁1", "팁2", "팁3"]}'
        )
        try:
            payload = _json.dumps({
                "model": "grok-2-latest",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 600,
                "response_format": {"type": "json_object"}
            }).encode()
            req = urllib.request.Request(
                'https://api.x.ai/v1/chat/completions',
                data=payload,
                headers={
                    'Authorization': f'Bearer {grok_key}',
                    'Content-Type': 'application/json'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                resp    = _json.loads(r.read())
                content = resp['choices'][0]['message']['content']
                result  = _json.loads(content)
                result['source'] = 'grok'
                return result
        except Exception as e:
            # API 실패 시 rule-based로 fallback
            pass

    # ── Rule-based fallback (키 없거나 API 실패 시) ────────
    smart = {
        'S': any(w in title for w in ['달성','개선','완료','구축','구현','감소','증가','확보','작성','수립']),
        'M': any(c.isdigit() for c in title) or any(w in title for w in ['%','건','명','개','회','점','배','원']),
        'A': len(title) > 5,
        'R': True,
        'T': any(w in title for w in ['분기','반기','월','주','연간','Q1','Q2','Q3','Q4','상반기','하반기','까지','이내']),
    }
    missing = [k for k, v in smart.items() if not v]
    tips = []
    if not smart['M']:
        tips.append('측정 가능한 수치를 추가하세요. 예: "20% 향상", "3건 완료", "90점 이상"')
    if not smart['T']:
        tips.append('기간을 명시하세요. 예: "Q2 말까지", "6월 30일까지", "상반기 내"')
    if not smart['S']:
        tips.append('구체적인 행동 동사를 사용하세요. 예: "달성", "구축", "개선", "완료"')
    if not tips:
        tips.append('목표가 비교적 잘 작성되었습니다. 측정 기준을 설명란에 구체적으로 적어보세요.')

    improved = title
    if not smart['M']:
        improved += ' (수치 목표 추가 필요)'
    if not smart['T']:
        improved += ' — Q2 말까지' if category == 'KPI' else ' — 상반기 내'

    return {
        'improved_title': improved,
        'reason': f"{'·'.join(missing) + ' 기준이 부족합니다.' if missing else 'SMART 기준을 대체로 충족합니다.'}",
        'smart_check': smart,
        'tips': tips,
        'source': 'rule'
    }


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
                db.execute(
                    'INSERT INTO performance_goals (cycle_id, user_id, category, title, description, weight, approval_status) '
                    "VALUES (?, ?, ?, ?, ?, ?, 'draft')",
                    (cycle_id, uid, category, title, desc, weight)
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
                           active_page='performance')


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
                           active_page='performance')


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
        flash('이번 단계에서 처리할 팀원을 모두 완료했습니다. 🎉', 'success')
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
            n = db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
                "WHERE cycle_id=? AND approval_status != 'confirmed'", (cyc['id'],)
            ).fetchone()[0]
            if n:
                pending_info[cyc['id']] = f'목표 미확정 {n}명'
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
    db.execute("UPDATE performance_cycles SET status='closed', stage='closed' WHERE status='active'")
    db.execute(
        "INSERT INTO performance_cycles (name, start_date, end_date, status, stage, include_peer, "
        "goal_deadline, review_deadline) "
        "VALUES (?, ?, ?, 'active', 'goal', ?, ?, ?)",
        (name, start_date, end_date, include_peer, goal_deadline, review_deadline)
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
        flash(f'"{cycle["name"]}" 주기가 종료되었습니다. 보상 관리에서 등급 연동 인상을 진행할 수 있습니다.', 'success')
        return redirect(url_for('performance_cycles'))

    # 전환 체크리스트 경고 (R1-D — 진행은 허용하되 미완료 현황을 알림)
    if direction == 'next' and new_stage == 'progress':
        unconfirmed = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM performance_goals "
            "WHERE cycle_id=? AND approval_status != 'confirmed'", (cycle_id,)
        ).fetchone()[0]
        if unconfirmed:
            flash(f'⚠ 목표가 아직 확정되지 않은 직원이 {unconfirmed}명 있습니다. 목표 수립 단계가 지나면 신규 등록·제출이 제한됩니다.', 'error')
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
            flash(f'⚠ 자기평가 미제출 {no_self}명 · 매니저 미평가 목표 {no_mgr}개 상태로 조정 단계에 진입했습니다. 점수 없는 항목은 집계에서 빠집니다.', 'error')

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


# ── Onboarding Dashboard ────────────────────────────────────
@app.route('/me/onboarding')
@login_required
def me_onboarding():
    db  = get_db()
    uid = session['user_id']

    # 행이 없으면 기본 체크리스트 생성 (기존 직원도 볼 수 있도록)
    count = db.execute("SELECT COUNT(*) FROM onboarding_progress WHERE user_id=?", (uid,)).fetchone()[0]
    if count == 0:
        from integrations.dispatcher import ONBOARDING_TASKS, _seed_onboarding_tasks
        _seed_onboarding_tasks(get_tenant_db_path(session.get('tenant_id', 1)), uid)

    tasks = db.execute(
        "SELECT * FROM onboarding_progress WHERE user_id=? ORDER BY sort_order",
        (uid,)
    ).fetchall()
    tasks = [dict(t) for t in tasks]

    # 카테고리별 그룹핑
    from collections import OrderedDict
    CAT_LABEL = {
        'setup':    '시스템 설정',
        'learning': '학습 & 이해',
        'admin':    '행정 처리',
        'social':   '팀 문화',
        'team':     '팀 온보딩',
    }
    grouped = OrderedDict()
    for t in tasks:
        cat = t['category']
        grouped.setdefault(cat, {'label': CAT_LABEL.get(cat, cat), 'tasks': []})
        grouped[cat]['tasks'].append(t)

    total = len(tasks)
    done  = sum(1 for t in tasks if t['done'])
    pct   = int(done / total * 100) if total else 0

    # 버디 정보
    buddy = db.execute(
        "SELECT u.name, u.email, d.name AS dept, p.name AS pos "
        "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
        "LEFT JOIN positions p ON u.position_id=p.id "
        "WHERE u.id=(SELECT buddy_id FROM users WHERE id=?)", (uid,)
    ).fetchone()

    # Jira 에픽 키
    me = db.execute("SELECT jira_epic_key, hire_date FROM users WHERE id=?", (uid,)).fetchone()

    return render_template('me/onboarding.html',
        grouped=grouped, total=total, done=done, pct=pct,
        buddy=buddy, jira_epic_key=me['jira_epic_key'] if me else None,
        hire_date=me['hire_date'] if me else None)


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
            '합격하셨음을 알려드립니다. 축하드립니다! 🎉\n\n'
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
    'pending_dept': '부서장 승인 대기',
    'pending_hr':   'HR 승인 대기',
    'approved':     '승인 완료',
    'rejected':     '반려',
    'posted':       '공고 전환 완료',
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

@app.route('/recruit/requisitions')
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
    if role == 'manager':
        mgr_dept = session.get('dept_id') or 0
        # 본인 신청 + 같은 부서 신청 (부서장으로서 승인할 것들)
        sql += ' AND (r.requester_id=? OR r.department_id=?)'
        params += [uid, mgr_dept]
    elif role not in ('admin', 'recruiter'):
        sql += ' AND r.requester_id=?'
        params.append(uid)

    if status_f:
        sql += ' AND r.status=?'
        params.append(status_f)

    sql += ' ORDER BY r.created_at DESC'
    reqs = db.execute(sql, params).fetchall()

    return render_template('recruit/requisition_list.html',
        reqs=reqs,
        status_filter=status_f,
        status_labels=REQUISITION_STATUS_LABEL,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        active_page='recruit'
    )


def _requisition_submit_for_approval(req_id, uid):
    """작성 완료 → 부서장 승인 요청 처리 (draft 상태 요청만). 성공 시 True."""
    db  = get_db()
    req = db.execute('SELECT * FROM job_requisitions WHERE id=? AND requester_id=?', (req_id, uid)).fetchone()
    if not req or req['status'] != 'draft':
        flash('처리할 수 없는 요청입니다.', 'error')
        return False

    db.execute(
        "UPDATE job_requisitions SET status='pending_dept', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req_id,)
    )
    db.commit()

    approvers = db.execute(
        "SELECT id FROM users WHERE role IN ('manager','admin') AND department_id=? AND id!=?",
        (req['department_id'], uid)
    ).fetchall()
    for a in approvers:
        add_notification(a['id'], 'info', 'action', '채용 요청서 승인 요청',
                         f'"{req["title"]}" 채용 요청서 부서장 승인이 필요합니다.',
                         link=url_for('requisition_detail', req_id=req_id))
    db.commit()
    flash('부서장 승인 요청이 전송되었습니다.', 'success')
    return True


@app.route('/recruit/requisitions/new', methods=['GET', 'POST'])
@login_required
def requisition_new():
    db    = get_db()
    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs   = db.execute('SELECT jf.*, jfg.name AS group_name, jfg.sort_order AS group_sort FROM job_families jf LEFT JOIN job_family_groups jfg ON jf.group_id=jfg.id ORDER BY jfg.sort_order, jf.sort_order').fetchall()

    if request.method == 'POST':
        f = request.form
        rid = db.execute(
            'INSERT INTO job_requisitions '
            '(title, department_id, position_id, job_family_id, track, '
            ' headcount, employment_type, reason, '
            ' required_skills, salary_min, salary_mid, salary_max, target_start_date, '
            ' status, requester_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
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
                'draft',
                session['user_id'],
            )
        ).lastrowid
        db.commit()

        action = f.get('action', 'save')
        if action == 'submit':
            _requisition_submit_for_approval(rid, session['user_id'])
        return redirect(url_for('requisition_detail', req_id=rid))

    return render_template('recruit/requisition_form.html',
        req=None, depts=depts, poses=poses, jfs=jfs,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        track_labels=REQUISITION_TRACK_LABEL,
        manager_track_min_level=MANAGER_TRACK_MIN_LEVEL,
        ic_track_title=IC_TRACK_TITLE,
        m_track_title=M_TRACK_TITLE,
        active_page='recruit'
    )


@app.route('/recruit/requisitions/<int:req_id>')
@login_required
def requisition_detail(req_id):
    db  = get_db()
    uid = session['user_id']
    role = session.get('user_role')

    req = db.execute(
        'SELECT r.*, d.name AS dept_name, p.name AS pos_name, p.level AS pos_level, '
        'jf.name AS jf_name, jf.code AS jf_code, '
        'u.name AS requester_name, da.name AS dept_approver_name, ha.name AS hr_approver_name '
        'FROM job_requisitions r '
        'LEFT JOIN departments d  ON r.department_id    = d.id '
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
    if role not in ('admin', 'recruiter') and req['requester_id'] != uid:
        if role != 'manager' or req['department_id'] != mgr_dept:
            flash('접근 권한이 없습니다.', 'error')
            return redirect(url_for('requisition_list'))

    posting = None
    if req['posting_id']:
        posting = db.execute('SELECT * FROM job_postings WHERE id=?', (req['posting_id'],)).fetchone()

    return render_template('recruit/requisition_detail.html',
        req=req, posting=posting,
        status_labels=REQUISITION_STATUS_LABEL,
        emp_type_labels=REQUISITION_EMP_TYPE_LABEL,
        ic_track_title=IC_TRACK_TITLE,
        m_track_title=M_TRACK_TITLE,
        active_page='recruit'
    )


@app.route('/recruit/requisitions/<int:req_id>/submit', methods=['POST'])
@login_required
def requisition_submit(req_id):
    """작성 완료 → 부서장 승인 요청."""
    _requisition_submit_for_approval(req_id, session['user_id'])
    return redirect(url_for('requisition_detail', req_id=req_id))


@app.route('/recruit/requisitions/<int:req_id>/dept-approve', methods=['POST'])
@login_required
def requisition_dept_approve(req_id):
    """부서장 승인."""
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    if role not in ('admin', 'manager'):
        flash('권한이 없습니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    req = db.execute('SELECT * FROM job_requisitions WHERE id=?', (req_id,)).fetchone()
    if not req or req['status'] != 'pending_dept':
        flash('처리할 수 없는 요청입니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    action = request.form.get('action', 'approve')
    if action == 'approve':
        db.execute(
            "UPDATE job_requisitions SET status='pending_hr', "
            "dept_approver_id=?, dept_approved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (uid, req_id)
        )
        # HR Admin에게 알림
        hr_admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
        for h in hr_admins:
            add_notification(h['id'], 'info', 'action', 'HR 채용 요청서 승인 요청',
                             f'"{req["title"]}" 요청서가 부서장 승인을 완료하고 HR 최종 승인을 기다립니다.',
                             link=url_for('requisition_detail', req_id=req_id))
        flash('부서장 승인 완료. HR 검토 단계로 이동했습니다.', 'success')
    else:
        reason = request.form.get('reject_reason', '')
        db.execute(
            "UPDATE job_requisitions SET status='rejected', "
            "dept_approver_id=?, dept_approved_at=CURRENT_TIMESTAMP, "
            "dept_reject_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, reason, req_id)
        )
        add_notification(req['requester_id'], 'info', 'action', '채용 요청서 반려',
                         f'"{req["title"]}" 요청서가 부서장 검토에서 반려되었습니다. 사유: {reason}',
                         link=url_for('requisition_detail', req_id=req_id))
        flash('요청서를 반려했습니다.', 'success')

    db.commit()
    return redirect(url_for('requisition_detail', req_id=req_id))


@app.route('/recruit/requisitions/<int:req_id>/hr-approve', methods=['POST'])
@login_required
def requisition_hr_approve(req_id):
    """HR 최종 승인 → 공고 자동 생성."""
    db   = get_db()
    uid  = session['user_id']
    role = session.get('user_role')
    if role != 'admin':
        flash('HR Admin만 최종 승인할 수 있습니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    req = db.execute('SELECT * FROM job_requisitions WHERE id=?', (req_id,)).fetchone()
    if not req or req['status'] != 'pending_hr':
        flash('처리할 수 없는 요청입니다.', 'error')
        return redirect(url_for('requisition_detail', req_id=req_id))

    action = request.form.get('action', 'approve')
    if action == 'approve':
        # 채용 공고 자동 생성
        posting_id = db.execute(
            'INSERT INTO job_postings (title, department_id, position_id, description, '
            'employment_type, salary_min, salary_max, status, created_by) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                req['title'],
                req['department_id'],
                req['position_id'],
                req['reason'] or '',
                req['employment_type'],
                req['salary_min'] or 0,
                req['salary_max'] or 0,
                'draft',
                uid,
            )
        ).lastrowid

        db.execute(
            "UPDATE job_requisitions SET status='posted', "
            "hr_approver_id=?, hr_approved_at=CURRENT_TIMESTAMP, "
            "posting_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, posting_id, req_id)
        )
        add_notification(req['requester_id'], 'info', 'action', '채용 요청서 최종 승인',
                         f'"{req["title"]}" 요청서가 승인되어 채용 공고가 생성되었습니다.',
                         link=url_for('requisition_detail', req_id=req_id))
        flash('HR 승인 완료. 채용 공고(draft)가 자동 생성되었습니다.', 'success')
    else:
        reason = request.form.get('reject_reason', '')
        db.execute(
            "UPDATE job_requisitions SET status='rejected', "
            "hr_approver_id=?, hr_approved_at=CURRENT_TIMESTAMP, "
            "hr_reject_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, reason, req_id)
        )
        add_notification(req['requester_id'], 'info', 'action', '채용 요청서 HR 반려',
                         f'"{req["title"]}" 요청서가 HR 검토에서 반려되었습니다. 사유: {reason}',
                         link=url_for('requisition_detail', req_id=req_id))
        flash('요청서를 반려했습니다.', 'success')

    db.commit()
    return redirect(url_for('requisition_detail', req_id=req_id))


@app.route('/recruit/dashboard')
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

    flash(f'🎉 {name}({emp_no}) 입사 확정 완료! Jira·Slack·온보딩 체크리스트가 자동으로 준비됩니다.', 'success')
    return redirect(url_for('employee_detail', emp_id=new_user_id))


# ── 오퍼 관리 ─────────────────────────────────────────────────────────────────

@app.route('/recruit/applicants/<int:applicant_id>/offers', methods=['GET', 'POST'])
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
@recruiter_or_admin
def recruit_email_preview():
    """이메일 템플릿 미리보기 JSON"""
    tpl_key = request.json.get('type', 'fail')
    context = request.json.get('context', {})
    subject, body = _render_email_template(tpl_key, context)
    return jsonify({'subject': subject, 'body': body})


@app.route('/recruit/rounds/<int:round_id>/notes/add', methods=['POST'])
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


# ── Performance: Progress & Self-Review ─────────────────────
@app.route('/performance/goals/<int:goal_id>/progress', methods=['POST'])
@login_required
def performance_goal_progress(goal_id):
    db   = get_db()
    uid  = session['user_id']
    goal = db.execute(
        'SELECT g.user_id, g.cycle_id, c.stage FROM performance_goals g '
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
    db.execute('UPDATE performance_goals SET progress=? WHERE id=?', (progress, goal_id))
    db.commit()
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
    flash('연장근무 신청이 접수되었습니다.', 'success')
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
                f'⚠️ 주 52시간 초과! 이번 주 총 {weekly["total_h"]}시간 근무 '
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
            cid = int(request.form.get('cycle_id'))
            appeal_until = (date.today() + timedelta(days=7)).isoformat()
            db.execute('UPDATE calibration_results SET is_shared=1 WHERE cycle_id=?', (cid,))
            db.execute("UPDATE performance_cycles SET stage='appeal', appeal_until=? WHERE id=?",
                       (appeal_until, cid))
            count = db.execute(
                'SELECT COUNT(*) FROM calibration_results WHERE cycle_id=? AND is_shared=1', (cid,)
            ).fetchone()[0]
            db.commit()
            # 인앱 알림 발송
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
            log_audit('update', 'performance', None,
                      f'평가 결과 공개 + 이의신청 기간 시작 (~{appeal_until}, {count}명)')
            flash(f'{count}명의 평가 결과가 공개되었습니다. 이의신청 기간: {appeal_until}까지.', 'success')

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

    # 권장 배분 가이드 (%) — 분포 바 점선 기준
    target_dist = {'S': 10, 'A': 20, 'B': 40, 'C': 20, 'D': 10}
    anomaly_count = sum(1 for r in all_rows if r['anomaly'])

    return render_template('performance/calibration.html',
                           anomaly_count=anomaly_count,
                           cycles=cycles, selected_cycle=selected_cycle,
                           cycle_id=cycle_id, rows=rows,
                           dept_list=dept_list, selected_dept=selected_dept,
                           dept_info=dept_info, target_dist=target_dist,
                           grade_dist=grade_dist, confirmed_count=confirmed_count,
                           total_count=total_count, publish_ready=publish_ready,
                           active_acr=active_acr,
                           appeal_pending=appeal_pending,
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

    # 상향 평가 평균
    upward_rows = db.execute(
        'SELECT score FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND review_type=\'upward\'',
        (cycle_id, user_id)
    ).fetchall()
    upward_avg = round(sum(r['score'] for r in upward_rows) / len(upward_rows), 2) if upward_rows else None

    # 종합 점수 (있는 것만 평균)
    scores = [s for s in [self_avg, peer_avg, mgr_avg] if s is not None]
    overall = round(sum(scores) / len(scores), 2) if scores else None

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
                flash('후계자 계획이 추가되었습니다.', 'success')

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
                           upward_questions=UPWARD_QUESTIONS,
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

    if request.method == 'POST':
        if review_type == 'peer':
            try:
                score = int(request.form.get('score', 0))
            except (ValueError, TypeError):
                score = 0
            strength    = request.form.get('strength', '').strip() or None
            improvement = request.form.get('improvement', '').strip() or None
            comment     = request.form.get('comment', '').strip() or None
            if not (1 <= score <= 5):
                error = '점수를 선택해주세요.'
            elif not strength:
                error = 'Continue 항목을 입력해주세요.'
            elif not comment:
                error = 'Stop 항목을 입력해주세요.'
            elif not improvement:
                error = 'Start 항목을 입력해주세요.'
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
            for i in range(1, 6):
                try:
                    v = int(request.form.get(f'q{i}', 0))
                except (ValueError, TypeError):
                    v = 0
                q_scores.append(v)
            comment = request.form.get('comment', '').strip() or None
            if any(not (1 <= s <= 5) for s in q_scores):
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
                           upward_questions=UPWARD_QUESTIONS, error=error,
                           reviewee_goals=reviewee_goals,
                           active_page='peer')


@app.route('/performance/peer/assignments', methods=['GET', 'POST'])
@manager_or_admin
def peer_assignments():
    db   = get_db()
    role = session['user_role']

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

    error = None
    if selected_cycle and not selected_cycle['include_peer']:
        error = '이 주기는 다면평가가 포함되지 않은 주기입니다. 배정할 수 없습니다.'
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            try:
                reviewee_id = int(request.form.get('reviewee_id', 0))
                reviewer_id = int(request.form.get('reviewer_id', 0))
                cid         = int(request.form.get('cycle_id', 0))
            except (ValueError, TypeError):
                error = '입력값이 올바르지 않습니다.'
                reviewee_id = reviewer_id = cid = 0
            if not error and cid:
                cyc = db.execute('SELECT include_peer FROM performance_cycles WHERE id=?', (cid,)).fetchone()
                if not cyc or not cyc['include_peer']:
                    flash('다면평가가 포함되지 않은 주기에는 배정할 수 없습니다.', 'error')
                    return redirect(url_for('peer_assignments', cycle=cycle_id))
            if not error and reviewee_id == reviewer_id:
                error = '평가자와 피평가자가 동일할 수 없습니다.'
            if not error and cid and reviewee_id and reviewer_id:
                existing = db.execute(
                    'SELECT id FROM peer_assignments WHERE cycle_id=? AND reviewee_id=? AND reviewer_id=?',
                    (cid, reviewee_id, reviewer_id)
                ).fetchone()
                if existing:
                    error = '이미 등록된 배정입니다.'
                else:
                    db.execute(
                        'INSERT INTO peer_assignments (cycle_id, reviewee_id, reviewer_id) VALUES (?, ?, ?)',
                        (cid, reviewee_id, reviewer_id)
                    )
                    db.commit()
        elif action == 'delete':
            try:
                assign_id = int(request.form.get('assign_id', 0))
            except (ValueError, TypeError):
                assign_id = 0
            if assign_id:
                db.execute('DELETE FROM peer_assignments WHERE id=?', (assign_id,))
                db.commit()
        return redirect(url_for('peer_assignments', cycle=cycle_id))

    # 배정 목록
    assignments = []
    if cycle_id:
        assignments = db.execute(
            'SELECT pa.*, '
            'rv.name AS reviewee_name, rr.name AS reviewer_name '
            'FROM peer_assignments pa '
            'JOIN users rv ON pa.reviewee_id = rv.id '
            'JOIN users rr ON pa.reviewer_id = rr.id '
            'WHERE pa.cycle_id=? ORDER BY rv.name, rr.name',
            (cycle_id,)
        ).fetchall()

    # 직원 목록 — 부서별 그룹으로 제공
    mgr_dept = int(session.get('dept_id') or 0)
    if role == 'manager' and mgr_dept:
        employees = db.execute(
            "SELECT u.id, u.name, u.role, d.name dept_name "
            "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
            "WHERE u.department_id=? AND u.status='active' ORDER BY u.name",
            (mgr_dept,)
        ).fetchall()
    else:
        employees = db.execute(
            "SELECT u.id, u.name, u.role, d.name dept_name "
            "FROM users u LEFT JOIN departments d ON u.department_id=d.id "
            "WHERE u.status='active' ORDER BY d.name, u.name"
        ).fetchall()

    # 부서 목록 (조직도 기반 선택용)
    departments = db.execute(
        "SELECT DISTINCT d.id, d.name "
        "FROM departments d JOIN users u ON u.department_id=d.id "
        "WHERE u.status='active' ORDER BY d.name"
    ).fetchall()

    return render_template('performance/peer_assignments.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           assignments=assignments, employees=employees,
                           departments=departments,
                           cycle_id=cycle_id, error=error,
                           active_page='peer_assignments')


# ── Export (Excel 내보내기) ──────────────────────────────────
from export_utils import (make_wb, write_header, write_row, auto_width,
                           freeze_header, to_response, apply_number_format,
                           KRW_FORMAT, NUM_FORMAT)
import urllib.parse


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
    )[:8]

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

    # ── 6. Attrition Risk (Deloitte 모델 간소화) ────
    # 팩터: 재직기간, Compa-ratio, 성과등급, 최근 휴가 사용 패턴
    risk_rows = db.execute(
        "SELECT u.id, u.name, u.hire_date, "
        "  d.name AS dept, p.name AS grade, "
        "  COALESCE(es.base_salary, 0) AS salary, "
        "  COALESCE(sg.annual_salary, 0) AS grade_salary, "
        "  COALESCE(cr.final_grade, 'B') AS perf_grade, "
        "  COALESCE(lv.leave_days, 0) AS leave_days_used "
        "FROM users u "
        "LEFT JOIN departments d ON d.id=u.department_id "
        "LEFT JOIN positions p ON p.id=u.position_id "
        "LEFT JOIN employee_salary es ON es.user_id=u.id "
        "LEFT JOIN salary_grades sg ON sg.position_id=u.position_id AND sg.job_family_id=u.job_family_id "
        "LEFT JOIN ("
        "  SELECT user_id, final_grade FROM calibration_results "
        "  WHERE id IN (SELECT MAX(id) FROM calibration_results GROUP BY user_id)"
        ") cr ON cr.user_id=u.id "
        "LEFT JOIN ("
        "  SELECT user_id, COALESCE(SUM(days),0) AS leave_days "
        "  FROM leave_requests WHERE status='approved' "
        "  AND start_date >= date('now','-6 months') GROUP BY user_id"
        ") lv ON lv.user_id=u.id "
        "WHERE u.status='active' AND u.role='employee' AND u.hire_date IS NOT NULL "
        "ORDER BY u.hire_date ASC LIMIT 30"
    ).fetchall()

    def calc_risk_score(row):
        score = 0
        # 재직기간 < 1년: +25, 1-2년: +15
        if row['hire_date']:
            from datetime import datetime as dt
            hd = dt.strptime(row['hire_date'], '%Y-%m-%d').date()
            months = (today.year - hd.year) * 12 + (today.month - hd.month)
            if months < 12: score += 25
            elif months < 24: score += 15
        # Compa-ratio < 80: +30, 80-95: +15
        if row['grade_salary'] and row['grade_salary'] > 0:
            compa = row['salary'] * 12 / row['grade_salary'] * 100
            if compa < 80: score += 30
            elif compa < 95: score += 15
        # 성과 등급 C/D: +20
        if row['perf_grade'] in ('C', 'D'): score += 20
        # 최근 6개월 휴가 0일: +10 (번아웃 징후)
        if row['leave_days_used'] == 0: score += 10
        return min(score, 100)

    risk_employees = []
    for r in risk_rows:
        risk = calc_risk_score(r)
        if risk >= 20:
            risk_employees.append({
                'name': r['name'], 'dept': r['dept'] or '—',
                'grade': r['grade'] or '—', 'risk': risk,
                'level': 'high' if risk >= 60 else ('medium' if risk >= 35 else 'low')
            })
    risk_employees.sort(key=lambda x: x['risk'], reverse=True)

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
    high_risk_count = sum(1 for e in risk_employees if e['level'] == 'high')

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

    return render_template('analytics/index.html',
        active_page='analytics',
        total_active=total_active, turnover_rate=turnover_rate,
        avg_tenure=avg_tenure, open_reqs=open_reqs, high_risk_count=high_risk_count,
        dept_headcount=dept_headcount, grade_headcount=grade_headcount,
        monthly_turnover=monthly_turnover, leave_util=leave_util,
        compa_rows=compa_rows, risk_employees=risk_employees,
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
    return render_template('export/hub.html', active_page='export',
                           cycles=cycles,
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
    headers = ['이름','부서','직위','기본급','식대','교통비','초과근무수당',
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
    return redirect(url_for('people_analytics', tab='wizard'))


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
    wb, ws = make_wb("후계자계획")
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
    fname = urllib.parse.quote("후계자계획.xlsx")
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
    return render_template('contracts/list.html',
        contracts=contracts, templates=templates,
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
    reason = request.form.get('reason', '').strip()
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
            f"✅ {emp['name'] if emp else ''}님 휴가 승인 완료 ({req['start_date']} ~ {req['end_date']})")
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
            f"❌ {emp['name'] if emp else ''}님 휴가 반려 ({req['start_date']} ~ {req['end_date']})")
        return '', 200

    return '', 200


# ── Run ─────────────────────────────────────────────────────
if __name__ == '__main__':
    from database import init_db
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true')
