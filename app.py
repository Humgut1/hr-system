import os
import sqlite3
import uuid
import json
import base64
import urllib.request
from datetime import datetime, date
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for, jsonify)
from werkzeug.security import check_password_hash, generate_password_hash
from payroll_utils import (calc_payslip, calc_annual_leave, fmt_krw,
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
)

app = Flask(__name__)
app.secret_key = os.environ.get('HR_SECRET_KEY', 'dev-only-change-in-prod')
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

# ── 토스페이먼츠 키 ─────────────────────────────────────────
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


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') == 'guest' and request.method == 'POST':
            flash('게스트 계정은 조회만 가능합니다.', 'error')
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
        return f(*args, **kwargs)
    return decorated

def manager_or_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') not in ('admin', 'manager'):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def recruiter_or_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') not in ('admin', 'recruiter'):
            abort(403)
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

                return redirect(url_for('dashboard'))
            error = '이메일 또는 비밀번호가 올바르지 않습니다.'
    return render_template('login.html', error=error)

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


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
                    (benefit_key, enabled, amount, payment_type, annual_limit, platform)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(benefit_key) DO UPDATE SET
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
    return render_template('admin/settings.html', config=config, company=company,
                           active_page='settings')


# ── Dashboard helpers ─────────────────────────────────────────
def _greeting():
    h = datetime.now().hour
    if h < 12:  return 'Good morning'
    if h < 17:  return 'Good afternoon'
    return 'Good evening'

def _today_label():
    d = date.today()
    return d.strftime('%A, %B ') + str(d.day)

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
    LEAVE_LABELS = {'annual':'Annual','half_am':'Half AM','half_pm':'Half PM','sick':'Sick','etc':'Other'}

    cfg = get_company_config()
    if not cfg.get('setup_completed'):
        return redirect(url_for('admin_setup'))

    if role == 'admin':
        total_employees   = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        total_departments = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
        pending_leave     = db.execute("SELECT COUNT(*) FROM leave_requests WHERE status='pending'").fetchone()[0]
        open_postings     = db.execute("SELECT COUNT(*) FROM job_postings WHERE status='open'").fetchone()[0]
        total_applicants  = db.execute("SELECT COUNT(*) FROM applicants").fetchone()[0]
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
                'title': f"{r['name']} — {LEAVE_LABELS.get(r['type'], r['type'])} 휴가 신청",
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
        inbox_count = len(inbox_items)
        return render_template('dashboard/admin.html',
            greet=greet, today_str=today_str, first_name=first_name,
            total_employees=total_employees, total_departments=total_departments,
            pending_leave=pending_leave, open_postings=open_postings,
            total_applicants=total_applicants, recent_employees=recent_employees,
            recent_posts=recent_posts, who_out=who_out,
            inbox_items=inbox_items, inbox_count=inbox_count,
            labels=LEAVE_LABELS, active_page='home')

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
            {'id': r['id'], 'title': r['user_name'] + ' — ' + LEAVE_LABELS.get(r['type'], r['type']),
             'sub': r['start_date'] + (' ~ ' + r['end_date'] if r['start_date'] != r['end_date'] else '')}
            for r in inbox_rows
        ]
        inbox_count = pending_count
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
        return render_template('dashboard/manager.html',
            greet=greet, today_str=today_str, first_name=first_name,
            team_count=team_count, pending_count=pending_count,
            today_leave=today_leave, inbox_items=inbox_items, inbox_count=inbox_count,
            team_goals=team_goals, recent_posts=recent_posts,
            who_out=who_out, labels=LEAVE_LABELS, active_page='home')

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
            'applied':'Applied','screening':'Screening','interview':'Interview',
            'offered':'Offered','hired':'Hired','rejected':'Rejected'
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
    total_leave   = calc_annual_leave(hire_date_str) if hire_date_str else 15
    used_leave    = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]
    remain_leave  = total_leave - float(used_leave)
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
    return render_template('dashboard/employee.html',
        greet=greet, today_str=today_str, first_name=first_name,
        total_leave=total_leave, used_leave=used_leave,
        remain_leave=remain_leave, pct_used=pct_used,
        recent_reqs=recent_reqs, upcoming_leave=upcoming_leave,
        recent_posts=recent_posts, tenure_str=tenure_str,
        labels=LEAVE_LABELS, active_page='home')


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

    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    jfs   = db.execute('SELECT * FROM job_families ORDER BY name').fetchall()
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
        "WHERE u.status = 'active'"
    )
    params = []
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
    return render_template('employees/list.html',
                           employees=emp_list, depts=depts, jfs=jfs, poses=poses,
                           q=q, dept_id=dept_id, jf_id=jf_id, pos_id=pos_id,
                           emp_type=emp_type, perf_grade=perf_grade,
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
        '       jf.name jf_name, m.name manager_name '
        'FROM users u '
        'LEFT JOIN departments d  ON u.department_id = d.id '
        'LEFT JOIN positions   p  ON u.position_id   = p.id '
        'LEFT JOIN job_families jf ON u.job_family_id = jf.id '
        'LEFT JOIN users       m  ON u.manager_id    = m.id '
        'WHERE u.id=?', (emp_id,)
    ).fetchone()
    if not emp:
        abort(404)

    # 매니저: 직속 팀원(manager_id == 본인)이면 민감 정보 허용
    if role == 'manager' and emp['manager_id'] == uid:
        can_see_sensitive = True

    payslips = db.execute(
        'SELECT year, month, gross_pay, net_pay, base_salary '
        'FROM payslips WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 6',
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

    annual_leave = calc_annual_leave(emp['hire_date']) if emp['hire_date'] else 15.0
    used_leave   = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND type='annual' AND status='approved' "
        "AND strftime('%Y',start_date)=strftime('%Y','now')",
        (emp_id,)
    ).fetchone()[0]

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
                           skill_levels=SKILL_LEVELS,
                           today=date.today().isoformat(),
                           leave_labels=LEAVE_LABELS,
                           can_see_sensitive=can_see_sensitive,
                           active_page='employees')


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
            {'key': 'goal_progress', 'label': '목표진행률',   'sql': 'ROUND(AVG(pg.progress),1)', 'agg': True,  'needs': ['goal_join']},
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
            'LEFT JOIN (SELECT user_id, final_grade, self_avg, mgr_avg '
            'FROM calibration_results WHERE id IN '
            '(SELECT MAX(id) FROM calibration_results GROUP BY user_id)) cr ON cr.user_id = u.id'
        )
    if 'goal_join' in needs:
        joins.append('LEFT JOIN performance_goals pg ON pg.user_id = u.id')

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


@app.route('/employees/new', methods=['GET', 'POST'])
@admin_required
def employee_new():
    db      = get_db()
    depts   = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses   = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs     = db.execute('SELECT * FROM job_families ORDER BY name').fetchall()
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
            db.commit()
            # ── master.db 동기화: 이메일 매핑 + peak headcount ──
            tid = session.get('tenant_id', 1)
            register_tenant_user(email, tid)
            active_count = db.execute(
                "SELECT COUNT(*) FROM users WHERE status='active'"
            ).fetchone()[0]
            update_peak_headcount(tid, active_count)
            flash(f'직원 {name}(TC-{new_id:05d})이 추가되었습니다.', 'success')
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

    return render_template('employees/form.html',
                           mode='new', depts=depts, poses=poses, jfs=jfs,
                           managers=managers, error=error, emp=None,
                           prefill=prefill,
                           active_page='employees')

@app.route('/employees/<int:emp_id>/edit', methods=['GET', 'POST'])
@admin_required
def employee_edit(emp_id):
    db       = get_db()
    emp      = db.execute('SELECT * FROM users WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        abort(404)
    depts    = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses    = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs      = db.execute('SELECT * FROM job_families ORDER BY name').fetchall()
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

    db.execute(
        'INSERT INTO personnel_actions '
        '(user_id, action_type, from_value, to_value, effective_date, reason, status, processed_by) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (emp_id, action_type, from_value, to_value, effective_date, reason, 'pending', session['user_id'])
    )
    db.commit()

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
            flash('An open termination request already exists.', 'error')
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
            flash('Termination date must be on or after the last work date.', 'error')
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
        flash('Termination request submitted.', 'success')
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
            flash('Termination date must be on or after the last work date.', 'error')
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
        flash('Termination process started.', 'success')
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
                flash('This request can no longer be cancelled.', 'error')
            else:
                db.execute(
                    "UPDATE termination_requests SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (req_id,)
                )
                db.commit()
                flash('Termination request cancelled.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'manager_approve':
            if not can_manage:
                abort(403)
            if termination['manager_approved_by']:
                flash('Manager approval is already recorded.', 'error')
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
                flash('Manager review completed.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'reject_request':
            if not can_manage:
                abort(403)
            rejection_reason = request.form.get('rejection_reason', '').strip() or 'Rejected during review.'
            db.execute(
                "UPDATE termination_requests "
                "SET status='rejected', rejection_reason=?, updated_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (rejection_reason, req_id)
            )
            db.commit()
            flash('Termination request rejected.', 'success')
            return redirect(url_for('termination_request_detail', req_id=req_id))

        if action == 'hr_approve':
            if session.get('user_role') != 'admin':
                abort(403)
            if not termination['manager_approved_by']:
                flash('Manager review is required before HR approval.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            final_last_work_date = request.form.get('final_last_work_date') or termination['requested_last_work_date']
            final_termination_date = request.form.get('final_termination_date') or termination['requested_termination_date']
            if final_termination_date < final_last_work_date:
                flash('Final termination date must be on or after the last work date.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            db.execute(
                "UPDATE termination_requests "
                "SET hr_approved_by=?, hr_approved_at=CURRENT_TIMESTAMP, "
                "final_last_work_date=?, final_termination_date=?, "
                "status='in_progress', updated_at=CURRENT_TIMESTAMP "
                'WHERE id=?',
                (session['user_id'], final_last_work_date, final_termination_date, req_id)
            )
            create_offboarding_tasks(db, req_id, final_last_work_date)
            db.commit()
            flash('HR approval completed. Offboarding tasks created.', 'success')
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
                flash('Complete all offboarding tasks before finalization.', 'error')
                return redirect(url_for('termination_request_detail', req_id=req_id))

            term_date = request.form.get('final_termination_date') or termination['final_termination_date'] or termination['requested_termination_date']
            last_work_date = request.form.get('final_last_work_date') or termination['final_last_work_date'] or termination['requested_last_work_date']
            payslips = db.execute(
                'SELECT year, month, gross_pay FROM payslips '
                'WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 3',
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
            flash('Termination completed.', 'success')
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
        'WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 3',
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
        'WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 3',
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
        'WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 3',
        (emp_id,)
    ).fetchall()
    payslip_list = [dict(r) for r in recent_payslips]

    term_date = emp['termination_date'] or date.today().isoformat()

    # 사용 연차일수 조회 (해당 연도 approved 건)
    term_year  = int(term_date[:4])
    used_rows  = db.execute(
        "SELECT COALESCE(SUM(days), 0) AS used "
        "FROM leave_requests "
        "WHERE user_id=? AND type='annual' AND status='approved' "
        "AND strftime('%Y', start_date)=?",
        (emp_id, str(term_year))
    ).fetchone()
    used_days = float(used_rows['used'] if used_rows else 0)

    # 마지막 월 일할계산 파라미터
    term_dt         = date.fromisoformat(term_date)
    days_in_month   = _cal.monthrange(term_dt.year, term_dt.month)[1]
    month_start     = date(term_dt.year, term_dt.month, 1)
    days_worked_last = (term_dt - month_start).days + 1

    # 종합 정산 계산
    settlement = calc_separation_settlement(
        hire_date_str        = emp['hire_date'] or '',
        termination_date_str = term_date,
        recent_payslips      = payslip_list,
        used_leave_days      = used_days,
        final_month_base_salary = emp.get('base_salary') or 0,
        final_month_days_worked = days_worked_last,
        final_month_days_total  = days_in_month,
    )
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
    'career': 'Career Move',
    'compensation': 'Compensation',
    'culture': 'Culture / Team Fit',
    'performance': 'Performance',
    'contract_end': 'Contract End',
    'retirement': 'Retirement',
    'personal': 'Personal',
    'other': 'Other',
}

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
    # 연차 사용일 합산 (승인된 것만)
    used = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]
    hire_row = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    total = calc_annual_leave(hire_row['hire_date']) if hire_row and hire_row['hire_date'] else 15
    return render_template('leave/my.html', requests=requests,
                           used=used, total=total, labels=LEAVE_LABELS,
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

            # 연차 소진 유형: 잔여 연차 검사
            if not error and meta['deduct'] == 'annual':
                hire_row = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
                total = calc_annual_leave(hire_row['hire_date']) if hire_row and hire_row['hire_date'] else 15
                used  = db.execute(
                    "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                    "WHERE user_id=? AND status='approved' "
                    "AND type IN ('annual','half_am','half_pm','sick')",
                    (uid,)
                ).fetchone()[0]
                if used + days > total:
                    error = f'잔여 연차가 부족합니다. (잔여: {total - used:.1f}일, 신청: {days:.1f}일)'

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

                flash(f'{meta["label"]} 신청이 완료되었습니다.', 'success')
                return redirect(url_for('attendance_home', tab='leaves'))

    # 연차 잔여일 계산 (폼에 표시용)
    db  = get_db()
    uid = session['user_id']
    hire_row   = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    annual_total = calc_annual_leave(hire_row['hire_date']) if hire_row and hire_row['hire_date'] else 15
    annual_used  = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]
    annual_remain = round(annual_total - annual_used, 1)

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


# ── Payroll ─────────────────────────────────────────────────
@app.route('/payroll')
@login_required
def payroll_list():
    db  = get_db()
    uid = session['user_id']
    slips = db.execute(
        'SELECT year, month, gross_pay, total_deduction, net_pay '
        'FROM payslips WHERE user_id=? ORDER BY year DESC, month DESC',
        (uid,)
    ).fetchall()
    # 연차 잔여일 계산
    user = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    total_leave = calc_annual_leave(user['hire_date']) if user and user['hire_date'] else 15
    used_leave  = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]
    return render_template('payroll/list.html', slips=slips,
                           total_leave=total_leave, used_leave=used_leave,
                           fmt_krw=fmt_krw,
                           active_page='payroll')

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
        'WHERE p.user_id=? AND p.year=? AND p.month=?',
        (uid, year, month)
    ).fetchone()
    if not slip:
        abort(404)
    return render_template('payroll/detail.html', slip=slip,
                           year=year, month=month, fmt_krw=fmt_krw,
                           active_page='payroll')

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
    job_families = db.execute('SELECT id, name FROM job_families ORDER BY id').fetchall()

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

                    # 근태 수당 계산
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

                    result = calc_payslip(
                        e['base_salary'],
                        e['meal_allowance'],
                        e['transport_allowance'],
                        overtime_pay=total_ot_pay,
                        extra_benefits=extra_benefits,
                    )
                    bonus_pay = result.get('benefits_gross', 0)

                    db.execute(
                        'INSERT INTO payslips '
                        '(user_id, year, month, base_salary, meal_allowance, transport_allowance, '
                        'overtime_pay, bonus_pay, national_pension, health_insurance, long_term_care, '
                        'employment_insurance, income_tax, local_income_tax, '
                        'gross_pay, total_deduction, net_pay, benefits_json) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (e['id'], year, month,
                         result['base_salary'], result['meal_allowance'],
                         result['transport_allowance'], result['overtime_pay'], bonus_pay,
                         result['national_pension'], result['health_insurance'],
                         result['long_term_care'], result['employment_insurance'],
                         result['income_tax'], result['local_income_tax'],
                         result['gross_pay'], result['total_deduction'], result['net_pay'],
                         _json.dumps(result.get('benefits_breakdown', []), ensure_ascii=False))
                    )
                    # 급여 확정 인앱 알림 발송
                    add_notification(
                        e['id'], 'info', 'payroll',
                        f'{year}년 {month}월 급여명세서가 확정되었습니다',
                        f'실수령액 {fmt_krw(result["net_pay"])}원 · 명세서를 확인해보세요.',
                        link=f'/payroll/{year}/{month}'
                    )
                    count += 1
                db.commit()
                msg = f'{year}년 {month}월 급여명세서 {count}건이 생성되었습니다. (근태 수당·복리후생 자동 포함)'

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
        'SELECT * FROM payslips WHERE user_id=? AND year=? ORDER BY month',
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
    job_families = db.execute('SELECT * FROM job_families ORDER BY id').fetchall()
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
        slips = db.execute('SELECT * FROM payslips WHERE user_id=? AND year=? ORDER BY month', (uid, year)).fetchall()
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
        # manager는 자기 부서 팀원만 조회
        mgr_dept = int(session.get('dept_id') or 0)
        if role == 'manager' and mgr_dept:
            goals = db.execute(
                'SELECT g.*, u.id AS user_id, u.name AS user_name, d.name AS dept_name, '
                'AVG(r.score) AS avg_score, COUNT(r.id) AS review_count '
                'FROM performance_goals g '
                'JOIN users u ON g.user_id = u.id '
                'LEFT JOIN departments d ON u.department_id = d.id '
                'LEFT JOIN performance_reviews r ON g.id = r.goal_id '
                'WHERE g.cycle_id = ? AND u.department_id = ? '
                'GROUP BY g.id ORDER BY u.name, g.created_at',
                (cycle_id, mgr_dept)
            ).fetchall()
        else:
            goals = db.execute(
                'SELECT g.*, u.id AS user_id, u.name AS user_name, d.name AS dept_name, '
                'AVG(r.score) AS avg_score, COUNT(r.id) AS review_count '
                'FROM performance_goals g '
                'JOIN users u ON g.user_id = u.id '
                'LEFT JOIN departments d ON u.department_id = d.id '
                'LEFT JOIN performance_reviews r ON g.id = r.goal_id '
                'WHERE g.cycle_id = ? '
                'GROUP BY g.id ORDER BY u.name, g.created_at',
                (cycle_id,)
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

    # ── 직원 전용 추가 데이터 ─────────────────────────────
    peer_assignments_mine = []   # 내가 써야 할 피어리뷰
    calibration_result    = None # 내 캘리브레이션 결과
    todo_items            = []   # 지금 해야 할 일

    if role == 'employee' and cycle_id:
        # 피어리뷰 배정 + 완료 여부
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

        # 캘리브레이션 결과 (is_shared 컬럼 없으면 항상 표시)
        calibration_result = db.execute(
            'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=?',
            (cycle_id, uid)
        ).fetchone()

        # To-Do 계산
        goals_no_self = [g for g in goals if not g['self_score']]
        if goals_no_self:
            todo_items.append({
                'icon': 'fa-pen',
                'color': '#dc2626',
                'text': f'자기평가 미완료 목표 {len(goals_no_self)}개',
                'url': url_for('performance_self_review', goal_id=goals_no_self[0]['id'])
            })
        peer_undone = [p for p in peer_assignments_mine if not p['done_id']]
        if peer_undone:
            todo_items.append({
                'icon': 'fa-star',
                'color': '#d97706',
                'text': f'작성 대기 중인 동료 평가 {len(peer_undone)}명',
                'url': url_for('peer_reviews_page')
            })
        if not goals:
            todo_items.append({
                'icon': 'fa-plus',
                'color': '#1d4ed8',
                'text': '이번 주기 목표를 등록하세요',
                'url': url_for('performance_goal_new')
            })

    return render_template('performance/index.html',
                           cycles=cycles, active_cycle=active_cycle,
                           selected_cycle=selected_cycle,
                           goals=goals, score_labels=SCORE_LABELS,
                           peer_assignments_mine=peer_assignments_mine,
                           calibration_result=calibration_result,
                           todo_items=todo_items,
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
    cycles = db.execute(
        "SELECT * FROM performance_cycles WHERE status='active' ORDER BY start_date DESC"
    ).fetchall()
    error = None

    if request.method == 'POST':
        cycle_id = request.form.get('cycle_id')
        category = request.form.get('category', 'KPI')
        title    = request.form.get('title', '').strip()
        desc     = request.form.get('description', '').strip() or None
        weight   = int(request.form.get('weight', 100))

        if not cycle_id or not title:
            error = '평가 주기와 목표명은 필수입니다.'
        elif not (1 <= weight <= 100):
            error = '가중치는 1~100 사이여야 합니다.'
        else:
            db.execute(
                'INSERT INTO performance_goals (cycle_id, user_id, category, title, description, weight) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (cycle_id, uid, category, title, desc, weight)
            )
            db.commit()
            return redirect(url_for('performance'))

    goal_templates_list = db.execute(
        "SELECT id, title, description, category, weight FROM goal_templates WHERE is_active=1 ORDER BY category, title"
    ).fetchall()
    return render_template('performance/goal_form.html',
                           cycles=cycles, error=error,
                           goal_templates=goal_templates_list,
                           active_page='performance')

@app.route('/performance/goals/<int:goal_id>/review', methods=['GET', 'POST'])
@manager_or_admin
def performance_review(goal_id):
    db   = get_db()
    goal = db.execute(
        'SELECT g.*, u.name AS user_name '
        'FROM performance_goals g JOIN users u ON g.user_id = u.id '
        'WHERE g.id=?', (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)

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
    return render_template('performance/cycles.html', cycles=cycles,
                           active_page='performance_cycles')


@app.route('/performance/cycles/new', methods=['POST'])
@admin_required
def performance_cycle_new():
    db         = get_db()
    name       = request.form.get('name', '').strip()
    start_date = request.form.get('start_date', '').strip()
    end_date   = request.form.get('end_date', '').strip()

    if not name or not start_date or not end_date:
        flash('모든 항목을 입력해 주세요.', 'error')
        return redirect(url_for('performance_cycles'))
    if start_date >= end_date:
        flash('종료일은 시작일보다 이후여야 합니다.', 'error')
        return redirect(url_for('performance_cycles'))

    # 기존 active 사이클이 있으면 자동 closed 처리
    db.execute("UPDATE performance_cycles SET status='closed' WHERE status='active'")
    db.execute(
        'INSERT INTO performance_cycles (name, start_date, end_date, status) VALUES (?, ?, ?, ?)',
        (name, start_date, end_date, 'active')
    )
    db.commit()
    flash(f'평가 주기 "{name}"이 생성되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


@app.route('/performance/cycles/<int:cycle_id>/close', methods=['POST'])
@admin_required
def performance_cycle_close(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    db.execute("UPDATE performance_cycles SET status='closed' WHERE id=?", (cycle_id,))
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
    # 기존 active 사이클 종료 후 대상 활성화
    db.execute("UPDATE performance_cycles SET status='closed' WHERE status='active'")
    db.execute("UPDATE performance_cycles SET status='active' WHERE id=?", (cycle_id,))
    db.commit()
    flash(f'"{cycle["name"]}" 평가 주기가 활성화되었습니다.', 'success')
    return redirect(url_for('performance_cycles'))


# ── Profile ─────────────────────────────────────────────────
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
            elif len(new_pw) < 8:
                error = '새 비밀번호는 8자 이상이어야 합니다.'
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


@app.route('/recruit/requisitions/new', methods=['GET', 'POST'])
@login_required
def requisition_new():
    db    = get_db()
    depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    jfs   = db.execute('SELECT * FROM job_families ORDER BY id').fetchall()

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
            return redirect(url_for('requisition_submit', req_id=rid))
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
    db  = get_db()
    uid = session['user_id']
    req = db.execute('SELECT * FROM job_requisitions WHERE id=? AND requester_id=?', (req_id, uid)).fetchone()
    if not req or req['status'] != 'draft':
        flash('처리할 수 없는 요청입니다.', 'error')
        return redirect(url_for('requisition_list'))

    db.execute(
        "UPDATE job_requisitions SET status='pending_dept', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req_id,)
    )
    db.commit()

    # 같은 부서 매니저/어드민에게 알림
    approvers = db.execute(
        "SELECT id FROM users WHERE role IN ('manager','admin') AND department_id=? AND id!=?",
        (req['department_id'], uid)
    ).fetchall()
    for a in approvers:
        add_notification(db, a['id'], '채용 요청서 승인 요청',
                         f'"{req["title"]}" 채용 요청서 부서장 승인이 필요합니다.')
    db.commit()
    flash('부서장 승인 요청이 전송되었습니다.', 'success')
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
            add_notification(db, h['id'], 'HR 채용 요청서 승인 요청',
                             f'"{req["title"]}" 요청서가 부서장 승인을 완료하고 HR 최종 승인을 기다립니다.')
        flash('부서장 승인 완료. HR 검토 단계로 이동했습니다.', 'success')
    else:
        reason = request.form.get('reject_reason', '')
        db.execute(
            "UPDATE job_requisitions SET status='rejected', "
            "dept_approver_id=?, dept_approved_at=CURRENT_TIMESTAMP, "
            "dept_reject_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, reason, req_id)
        )
        add_notification(db, req['requester_id'], '채용 요청서 반려',
                         f'"{req["title"]}" 요청서가 부서장 검토에서 반려되었습니다. 사유: {reason}')
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
        add_notification(db, req['requester_id'], '채용 요청서 최종 승인',
                         f'"{req["title"]}" 요청서가 승인되어 채용 공고가 생성되었습니다.')
        flash('HR 승인 완료. 채용 공고(draft)가 자동 생성되었습니다.', 'success')
    else:
        reason = request.form.get('reject_reason', '')
        db.execute(
            "UPDATE job_requisitions SET status='rejected', "
            "hr_approver_id=?, hr_approved_at=CURRENT_TIMESTAMP, "
            "hr_reject_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, reason, req_id)
        )
        add_notification(db, req['requester_id'], '채용 요청서 HR 반려',
                         f'"{req["title"]}" 요청서가 HR 검토에서 반려되었습니다. 사유: {reason}')
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
    flash(f'{applicant["name"]} 님이 오퍼를 거절했습니다.', 'info')
    return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))


@app.route('/recruit/applicants/<int:applicant_id>/hire', methods=['POST'])
@recruiter_or_admin
def recruit_hire(applicant_id):
    """최종 합격 처리 — offer 단계 후보자에 한해 + 직원 전환 프리필 리다이렉트"""
    db        = get_db()
    applicant = db.execute(
        'SELECT a.*, jp.title AS posting_title, jp.department_id '
        'FROM applicants a JOIN job_postings jp ON a.posting_id = jp.id WHERE a.id=?',
        (applicant_id,)
    ).fetchone()
    if not applicant:
        abort(404)
    if applicant['stage'] != 'offer':
        flash('오퍼 단계의 후보자에게만 최종 합격 처리가 가능합니다.', 'warning')
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    db.execute('UPDATE applicants SET stage=? WHERE id=?', ('accepted', applicant_id))
    db.execute(
        'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?,?,?,?)',
        (applicant_id, 'accepted', '최종 합격 처리', session['user_id'])
    )
    # 오퍼 상태 동기화
    db.execute(
        "UPDATE offers SET status='accepted', responded_at=CURRENT_TIMESTAMP "
        "WHERE applicant_id=? AND status IN ('sent','negotiating')", (applicant_id,)
    )
    log_recruit(applicant_id, 'hired', {})
    db.commit()
    flash(f'🎉 {applicant["name"]} 님 최종 합격! 직원 등록을 완료해주세요.', 'success')
    # 지원자 데이터 프리필해서 직원 신규 등록 폼으로 리다이렉트 (기획서 P0: 지원자→직원 전환)
    from urllib.parse import urlencode
    params = urlencode({
        'from_applicant': applicant_id,
        'name':  applicant['name'],
        'email': applicant['email'] or '',
        'phone': applicant['phone'] or '',
        'dept':  applicant['department_id'] or '',
    })
    return redirect(url_for('employee_new') + '?' + params)


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
        rsu_total    = _int('rsu_total') or 0
        rsu_vest_yrs = _int('rsu_vest_years') or 4
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
            'rsu_total, rsu_vest_years, signing_bonus, start_date, expiry_date, '
            'location, wfh_days, job_level, track, company_signer, company_signer_title, '
            'sent_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (applicant_id, applicant['posting_id'], status, salary, bonus_pct,
             rsu_total, rsu_vest_yrs, signing, start_date, expiry_date,
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

    # 요청서 기반 연봉 밴드 조회 (프리필용)
    band = None
    if applicant['job_family_id'] and applicant['job_level']:
        level_num = int(applicant['job_level'][1:]) if applicant['job_level'] and applicant['job_level'][1:].isdigit() else None
        if level_num:
            band = db.execute(
                'SELECT sg.min_salary, sg.mid_salary, sg.max_salary, jf.name AS family_name '
                'FROM salary_grades sg JOIN job_families jf ON sg.job_family_id = jf.id '
                'WHERE sg.job_family_id=? AND sg.level=?', (applicant['job_family_id'], level_num)
            ).fetchone()

    return render_template('recruit/offers.html',
                           applicant=applicant, band=band,
                           offer_status_label=OFFER_STATUS_LABEL)


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
    for key in ('salary', 'bonus_pct', 'rsu_total', 'rsu_vest_years', 'signing_bonus', 'wfh_days'):
        if key in data:
            fields[key] = _safe_int(data[key])
    for key in ('start_date', 'expiry_date', 'location', 'job_level', 'track',
                'company_signer', 'company_signer_title', 'body'):
        if key in data:
            fields[key] = str(data[key]).strip() or None

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
        iv = db.execute('SELECT name FROM users WHERE id=?', (interviewer_id,)).fetchone()
        log_recruit(r['applicant_id'], 'interviewer_assigned',
                    {'interviewer_id': interviewer_id,
                     'interviewer_name': iv['name'] if iv else ''},
                    round_id=round_id)
        add_notification(
            interviewer_id,
            f'{r["round_no"]}차 면접 인터뷰어로 배정되었습니다.',
            url_for('recruit_applicant_detail', applicant_id=r['applicant_id'])
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
    goal = db.execute('SELECT user_id FROM performance_goals WHERE id=?', (goal_id,)).fetchone()
    if not goal:
        abort(404)
    if goal['user_id'] != uid:
        abort(403)
    try:
        progress = max(0, min(100, int(request.form.get('progress', 0))))
    except (ValueError, TypeError):
        progress = 0
    db.execute('UPDATE performance_goals SET progress=? WHERE id=?', (progress, goal_id))
    db.commit()
    return redirect(url_for('performance'))


@app.route('/performance/goals/<int:goal_id>/self-review', methods=['GET', 'POST'])
@login_required
def performance_self_review(goal_id):
    db   = get_db()
    uid  = session['user_id']
    goal = db.execute(
        'SELECT g.*, c.name AS cycle_name FROM performance_goals g '
        'JOIN performance_cycles c ON g.cycle_id = c.id WHERE g.id=?',
        (goal_id,)
    ).fetchone()
    if not goal:
        abort(404)
    if goal['user_id'] != uid:
        abort(403)
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

    # ── 공통: 연차 계산 ──────────────────────────────────────────
    hire_row    = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    total_leave = calc_annual_leave(hire_row['hire_date']) if hire_row and hire_row['hire_date'] else 15
    used_leave  = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]

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
            "WHERE o.status='pending'"
        )
        ot_params = []
        if role == 'manager':
            mgr_dept = session.get('dept_id') or 0
            cur_uid  = session.get('user_id')
            ot_sql  += ' AND (u.department_id=? OR u.manager_id=?)'
            ot_params += [mgr_dept, cur_uid]
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
@login_required
def overtime_approve(ot_id):
    db  = get_db()
    uid = session['user_id']
    if session.get('user_role') not in ('admin', 'manager'):
        flash('권한이 없습니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))
    row = db.execute('SELECT * FROM overtime_requests WHERE id=?', (ot_id,)).fetchone()
    if not row or row['status'] != 'pending':
        flash('처리할 수 없는 신청입니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))
    db.execute(
        "UPDATE overtime_requests SET status='approved', approver_id=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
        (uid, ot_id)
    )
    db.commit()
    add_notification(db, row['user_id'], 'OT 신청 승인',
                     f'{row["date"]} OT 신청({row["ot_minutes"]}분)이 승인되었습니다.')
    flash('OT 신청을 승인했습니다.', 'success')
    return redirect(url_for('attendance_home', tab='ot'))


@app.route('/attendance/overtime/<int:ot_id>/reject', methods=['POST'])
@login_required
def overtime_reject(ot_id):
    db  = get_db()
    uid = session['user_id']
    if session.get('user_role') not in ('admin', 'manager'):
        flash('권한이 없습니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))
    row = db.execute('SELECT * FROM overtime_requests WHERE id=?', (ot_id,)).fetchone()
    if not row or row['status'] != 'pending':
        flash('처리할 수 없는 신청입니다.', 'error')
        return redirect(url_for('attendance_home', tab='ot'))
    reason = request.form.get('reject_reason', '')
    db.execute(
        "UPDATE overtime_requests SET status='rejected', approver_id=?, approved_at=CURRENT_TIMESTAMP, "
        "reject_reason=? WHERE id=?",
        (uid, reason, ot_id)
    )
    db.commit()
    add_notification(db, row['user_id'], 'OT 신청 반려',
                     f'{row["date"]} OT 신청이 반려되었습니다. 사유: {reason}')
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
        total = calc_annual_leave(emp['hire_date']) if emp['hire_date'] else 15
        used  = db.execute(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm') "
            "AND strftime('%Y',start_date)=?",
            (emp['id'], str(year - 1))
        ).fetchone()[0]
        remain    = max(0, total - float(used))
        carry_amt = min(remain, carry_max)

        db.execute(
            'INSERT INTO leave_balances (user_id, year, total_days, used_days, carry_over_days, carry_over_max) '
            'VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(user_id, year) DO UPDATE SET '
            '  total_days=excluded.total_days, used_days=excluded.used_days, '
            '  carry_over_days=excluded.carry_over_days, updated_at=CURRENT_TIMESTAMP',
            (emp['id'], year, total + carry_amt, float(used), carry_amt, carry_max)
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
                      potential_score=excluded.potential_score,
                      decided_by=excluded.decided_by,
                      decided_at=CURRENT_TIMESTAMP
                ''', (cid, uid,
                      row['self_avg'], row['peer_avg'], row['mgr_avg'], row['upward_avg'],
                      suggested, final_grade, summary, note, downgrade_reason,
                      potential_score, session['user_id']))
                db.commit()
                flash('등급이 확정되었습니다.', 'success')

        # 직원에게 공개
        elif action == 'publish':
            cid = int(request.form.get('cycle_id'))
            db.execute('UPDATE calibration_results SET is_shared=1 WHERE cycle_id=?', (cid,))
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
                    r['user_id'], 'info', 'performance',
                    '성과 평가 결과가 공개되었습니다',
                    f'이번 주기 최종 등급: {r["final_grade"]}등급 · 성과 페이지에서 확인하세요.',
                    link='/performance'
                )
            flash(f'{count}명의 평가 결과가 직원에게 공개되었습니다.', 'success')

        return redirect(url_for('calibration', cycle=cycle_id))

    # GET — 전 직원 집계
    rows = []
    if cycle_id:
        emps = db.execute(
            "SELECT u.id, u.name, d.name dept_name FROM users u "
            "LEFT JOIN departments d ON u.department_id=d.id "
            "WHERE u.status='active' AND u.role NOT IN ('admin','guest') "
            "ORDER BY d.name, u.name"
        ).fetchall()

        for emp in emps:
            row = _calc_calibration_row(db, emp['id'], cycle_id)
            # 기존 확정 결과
            saved = db.execute(
                'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=?',
                (cycle_id, emp['id'])
            ).fetchone()
            row['confirmed']   = saved is not None
            row['final_grade'] = saved['final_grade'] if saved else row['suggested_grade']
            row['is_shared']   = saved['is_shared'] if saved else 0
            row['note']        = saved['note'] if saved else ''
            rows.append(row)

    # 분포 집계
    grade_dist = {'S':0,'A':0,'B':0,'C':0,'D':0}
    confirmed_count = 0
    for r in rows:
        if r['confirmed']:
            confirmed_count += 1
            grade_dist[r['final_grade']] = grade_dist.get(r['final_grade'], 0) + 1

    return render_template('performance/calibration.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           cycle_id=cycle_id, rows=rows,
                           grade_dist=grade_dist, confirmed_count=confirmed_count,
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
                  cr.self_avg, cr.peer_avg, cr.mgr_avg, cr.is_shared
           FROM calibration_results cr
           JOIN performance_cycles  pc ON cr.cycle_id = pc.id
           WHERE cr.user_id = ?
           ORDER BY pc.start_date DESC''', (user_id,)
    ).fetchall()

    # 가장 최근 캘리브레이션 결과
    latest = grade_history[0] if grade_history else None

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
        active_page='performance',
    )


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

    return render_template('performance/peer_write.html',
                           cycle=cycle, reviewee=reviewee,
                           review_type=review_type, existing=existing,
                           upward_questions=UPWARD_QUESTIONS, error=error,
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

    # ── 4. Leave utilization by dept ───────────────
    leave_util = db.execute(
        "SELECT d.name AS dept, "
        "  ROUND(AVG(u_leave.used * 100.0 / COALESCE(u_leave.total, 15)), 1) AS pct "
        "FROM departments d "
        "JOIN users u ON u.department_id=d.id AND u.status='active' "
        "JOIN ("
        "  SELECT user_id, "
        "    COALESCE(SUM(days),0) AS used, 15 AS total "
        "  FROM leave_requests WHERE status='approved' "
        "  AND type IN ('annual','half_am','half_pm') "
        "  GROUP BY user_id"
        ") u_leave ON u_leave.user_id=u.id "
        "GROUP BY d.id, d.name ORDER BY pct DESC LIMIT 8"
    ).fetchall()

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
        "SELECT u.emp_no, u.name, u.email, "
        "       d.name dept, p.name pos, jf.name jf, "
        "       u.employment_type, u.role, u.status, "
        "       u.hire_date, u.birth_date, u.phone, "
        "       u.termination_date, u.termination_reason, "
        "       mgr.name manager_name, "
        "       es.base_salary, "
        "       ROUND((JULIANDAY('now') - JULIANDAY(u.hire_date)) / 365.25, 1) years_of_service, "
        "       cr.final_grade last_grade, "
        "       es.updated_at last_salary_change "
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

    wb, ws = make_wb("직원 명단")
    headers = [
        '사번', '이름', '이메일', '부서', '직위', '직군',
        '고용형태', '역할', '재직상태',
        '입사일', '생년월일', '연락처',
        '퇴사일', '퇴사사유',
        '직속상관', '기본급(월)', '근속연수(년)',
        '최근성과등급', '최근급여변경일'
    ]
    write_header(ws, headers)

    EMP_TYPE_KO = {'full_time':'정규직','part_time':'시간제','contract':'계약직','intern':'인턴'}
    STATUS_KO   = {'active':'재직','inactive':'휴직','resigned':'퇴직'}
    ROLE_KO     = {'admin':'관리자','manager':'매니저','employee':'직원','recruiter':'채용담당'}
    am = {i: 'center' for i in range(1, 20)}
    am.update({2:'left', 3:'left', 4:'left', 5:'left', 6:'left', 13:'left', 15:'left'})

    for i, r in enumerate(rows, 2):
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

    return render_template('admin/benefits.html',
                           sections=sections,
                           payment_type_labels=PAYMENT_TYPE_LABELS,
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

    # 각 직원의 최근 성과등급 매핑
    grade_map_all = {}
    for row in db.execute(
        """SELECT reviewee_id, overall_grade
           FROM performance_reviews pr
           WHERE submitted_at = (
               SELECT MAX(submitted_at) FROM performance_reviews
               WHERE reviewee_id = pr.reviewee_id
           )"""
    ).fetchall():
        grade_map_all[row['reviewee_id']] = row['overall_grade']

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
    """랜딩 페이지 — 로그인 상태면 대시보드로"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """회사 가입 — 새 테넌트 생성 + 관리자 계정 생성"""
    if 'user_id' in session:
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
        elif password != password2:
            error = '비밀번호가 일치하지 않습니다.'
        elif len(password) < 8:
            error = '비밀번호는 8자 이상이어야 합니다.'
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
    return redirect(url_for('billing_dashboard'))


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
    monthly_amount = (tenant['peak_headcount'] or active_count) * PRICE_PER_SEAT
    return render_template('billing/dashboard.html',
                           tenant=tenant,
                           logs=logs,
                           active_count=active_count,
                           monthly_amount=monthly_amount,
                           price_per_seat=PRICE_PER_SEAT)


@app.route('/billing/charge', methods=['POST'])
@admin_required
def billing_charge():
    """
    월별 청구 실행 (관리자 수동 트리거 또는 cron 대용).
    Peak headcount × 1,000원을 저장된 billing key로 결제.
    """
    tenant_id = session.get('tenant_id', 1)
    tenant    = get_tenant(tenant_id)

    if not tenant or not tenant['toss_billing_key']:
        flash('등록된 결제 수단이 없습니다.', 'error')
        return redirect(url_for('billing_dashboard'))

    db           = get_db()
    active_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE status='active' AND role!='guest'"
    ).fetchone()[0]
    peak         = max(tenant['peak_headcount'] or 0, active_count)
    amount       = peak * PRICE_PER_SEAT

    if amount == 0:
        flash('청구 금액이 0원입니다.', 'info')
        return redirect(url_for('billing_dashboard'))

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

    return redirect(url_for('billing_dashboard'))


@app.route('/billing/webhook', methods=['POST'])
def billing_webhook():
    """
    토스 웹훅 수신 — 결제 상태 동기화.
    (토스 대시보드에서 웹훅 URL을 /billing/webhook 으로 설정)
    """
    try:
        payload     = json.loads(request.data)
        event_type  = payload.get('eventType', '')
        data        = payload.get('data', {})
        order_id    = data.get('orderId', '')
        payment_key = data.get('paymentKey', '')
        status      = data.get('status', '')

        if event_type == 'PAYMENT_STATUS_CHANGED':
            if status == 'DONE':
                update_billing_log(order_id, payment_key, 'paid')
            elif status in ('ABORTED', 'EXPIRED'):
                update_billing_log(order_id, payment_key, 'failed',
                                   data.get('failure', {}).get('message', ''))
    except Exception as e:
        app.logger.error(f'Toss webhook error: {e}')
        return '', 400

    return '', 200


# ── Run ─────────────────────────────────────────────────────
if __name__ == '__main__':
    from database import init_db
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true')
