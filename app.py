import os
import sqlite3
from datetime import datetime, date
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from payroll_utils import (calc_payslip, calc_annual_leave, fmt_krw,
                           calc_severance, check_min_wage, MIN_WAGE_MONTHLY,
                           calc_day_hours, calc_extra_pay)

app = Flask(__name__)
app.secret_key = os.environ.get('HR_SECRET_KEY', 'dev-only-change-in-prod')

DATABASE = 'hr_system.db'

# DB 초기화 — gunicorn 포함 모든 실행 방식에서 실행
from database import init_db
init_db()

# 연봉 기준표·부서 확장 시드 (job_families가 비어 있을 때만 자동 실행)
def _ensure_extended_seed():
    _c = sqlite3.connect(DATABASE)
    try:
        empty = _c.execute('SELECT COUNT(*) FROM job_families').fetchone()[0] == 0
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
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


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
        return {'user_features': set(), 'unread_notifications': 0}
    
    db = get_db()
    row = db.execute(
        'SELECT features_enabled FROM users WHERE id=?', (uid,)
    ).fetchone()
    features = set()
    if row and row['features_enabled']:
        features = set(row['features_enabled'].split(','))
    
    # 미읽음 알림 개수
    unread_row = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0', (uid,)
    ).fetchone()
    unread_notifications = unread_row[0] if unread_row else 0
    
    return {
        'user_features': features, 
        'unread_notifications': unread_notifications,
        'FEATURE_DEFS': FEATURE_DEFS,
        'today': date.today().isoformat()
    }


@app.route('/notifications')
@login_required
def notifications():
    db  = get_db()
    uid = session['user_id']
    
    # 읽음 처리 (페이지 접속 시 모든 알림 읽음 처리 혹은 개별 처리 선택 가능)
    # 여기서는 페이지 접속 시 현재 목록을 모두 읽음 처리함
    db.execute('UPDATE notifications SET is_read=1 WHERE user_id=?', (uid,))
    db.commit()
    
    notifs = db.execute(
        'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
        (uid,)
    ).fetchall()
    
    return render_template('notifications.html', notifications=notifs, active_page='notifications')


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
@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

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
            db   = get_db()
            user = db.execute(
                'SELECT u.*, d.name AS dept_name, p.name AS pos_name '
                'FROM users u '
                'LEFT JOIN departments d ON u.department_id = d.id '
                'LEFT JOIN positions   p ON u.position_id   = p.id '
                'WHERE u.email = ? AND u.status = ?',
                (email, 'active')
            ).fetchone()
            if user and check_password_hash(user['password_hash'], password):
                session.clear()
                session['user_id']    = user['id']
                session['user_name']  = user['name']
                session['user_role']  = user['role']
                session['user_email'] = user['email']
                session['dept_name']  = user['dept_name'] or ''
                session['pos_name']   = user['pos_name']  or ''
                session['dept_id']    = user['department_id'] or 0
                session['onboarded']  = 1 if user['onboarded'] else 0
                return redirect(url_for('dashboard'))
            error = '이메일 또는 비밀번호가 올바르지 않습니다.'
    return render_template('login.html', error=error)

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── 기능 목록 (온보딩·사이드바 공용) ─────────────────────────
FEATURE_DEFS = [
    ('attendance',    '근태 관리',    '연차·반차·재택 신청, 매니저 승인, 팀 캘린더, 출퇴근 체크인'),
    ('payroll',       '급여 관리',    '4대보험 자동계산, 월별 급여명세서 발행, 급여 현황 차트'),
    ('performance',   '성과 관리',    'KPI/OKR 목표 설정, 자기평가, 진행률 추적, 주기 관리'),
    ('peer_review',   '다면평가',     '360° 동료평가, 상향평가(Google 방식 5문항), 리뷰어 배정'),
    ('calibration',   '캘리브레이션', '평가 조정 보드, AI 요약, S/A/B/C/D 최종 등급 확정'),
    ('recruiting',    '채용 관리',    '채용공고 관리, 지원자 파이프라인 칸반, 단계별 이력 로그'),
    ('announcements', '공지사항',     '핀 고정 공지, 3일 내 미읽음 배지, 작성·수정·삭제'),
    ('org_chart',     '조직도',       '부문→본부→실→팀 계층 조직도, 인원수 집계, 접기/펼치기'),
    ('certificates',  '증명서 발급',  '재직증명서·경력증명서 법인 양식, 인쇄/PDF 저장'),
]


@app.route('/onboarding', methods=['GET', 'POST'])
@admin_required
def onboarding():
    """기존 호환성 유지 — 새 셋업 마법사로 리다이렉트"""
    return redirect(url_for('admin_setup'))


@app.route('/onboarding/company', methods=['GET', 'POST'])
@admin_required
def onboarding_company():
    """기존 호환성 유지 — 새 셋업 마법사 3단계로 리다이렉트"""
    return redirect(url_for('admin_setup', step=3))


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

        # ── Step 5: 성과관리 ─────────────────────────────────
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
        db.commit()
        session['onboarded'] = 1
        flash('회사 설정이 완료되었습니다! TalentCore에 오신 것을 환영합니다. 🎉', 'success')
        return redirect(url_for('dashboard'))

    config  = get_company_config()
    company = get_company_info()
    return render_template('admin/setup.html', config=config, company=company)


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
        return render_template('dashboard/admin.html',
            greet=greet, today_str=today_str, first_name=first_name,
            total_employees=total_employees, total_departments=total_departments,
            pending_leave=pending_leave, open_postings=open_postings,
            total_applicants=total_applicants, recent_employees=recent_employees,
            recent_posts=recent_posts, who_out=who_out,
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
        pending_reqs = db.execute(
            "SELECT lr.id, lr.type, lr.start_date, lr.end_date, u.name as user_name "
            "FROM leave_requests lr JOIN users u ON lr.user_id=u.id "
            "WHERE u.department_id=? AND lr.status='pending' "
            "ORDER BY lr.created_at DESC LIMIT 5", (dept_id,)
        ).fetchall()
        team_goals = db.execute(
            "SELECT pg.title, pg.self_score, u.name as user_name, AVG(pe.score) as avg_score "
            "FROM performance_goals pg JOIN users u ON pg.user_id=u.id "
            "LEFT JOIN performance_evaluations pe ON pg.id=pe.goal_id "
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
            today_leave=today_leave, pending_reqs=pending_reqs,
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


# ── Org Chart ────────────────────────────────────────────────
@app.route('/org')
@login_required
def org_chart():
    db    = get_db()
    depts = db.execute(
        'SELECT d.*, COUNT(u.id) AS member_count '
        'FROM departments d '
        'LEFT JOIN users u ON u.department_id = d.id AND u.status = "active" '
        'GROUP BY d.id'
    ).fetchall()
    tree = build_dept_tree([dict(d) for d in depts])
    people = [dict(r) for r in db.execute(
        'SELECT u.id, u.name, u.manager_id, d.name AS dept_name, p.name AS pos_name '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions p ON u.position_id = p.id '
        "WHERE u.status='active' ORDER BY u.name"
    ).fetchall()]
    active_ids = {u['id'] for u in people}
    for user in people:
        if user['manager_id'] not in active_ids:
            user['manager_id'] = None
    reporting_tree = build_reporting_tree(people, None)
    total = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    return render_template('org/index.html', tree=tree, reporting_tree=reporting_tree, total=total,
                           active_page='org')


# ── Employees ────────────────────────────────────────────────
@app.route('/employees')
@manager_or_admin
def employees():
    db      = get_db()
    q       = request.args.get('q', '').strip()
    dept_id = request.args.get('dept', '')
    depts   = db.execute('SELECT * FROM departments ORDER BY name').fetchall()

    sql    = ('SELECT u.*, d.name AS dept_name, p.name AS pos_name '
              'FROM users u '
              'LEFT JOIN departments d ON u.department_id = d.id '
              'LEFT JOIN positions   p ON u.position_id   = p.id '
              "WHERE u.status = 'active'")
    params = []
    if q:
        sql   += ' AND (u.name LIKE ? OR u.email LIKE ?)'
        params += [f'%{q}%', f'%{q}%']
    if dept_id:
        sql   += ' AND u.department_id = ?'
        params.append(dept_id)
    sql += ' ORDER BY u.created_at DESC'

    emp_list = db.execute(sql, params).fetchall()
    return render_template('employees/list.html',
                           employees=emp_list, depts=depts, q=q, dept_id=dept_id,
                           active_page='employees')

@app.route('/employees/<int:emp_id>')
@login_required
def employee_detail(emp_id):
    # 관리자/매니저는 전체 조회 가능, 직원은 본인만
    if session['user_role'] not in ('admin', 'manager') and session['user_id'] != emp_id:
        abort(403)
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
                           today=date.today().isoformat(),
                           leave_labels=LEAVE_LABELS,
                           active_page='employees')


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
            db.commit()
            flash(f'직원 {name}(TC-{new_id:05d})이 추가되었습니다.', 'success')
            return redirect(url_for('employees'))

    return render_template('employees/form.html',
                           mode='new', depts=depts, poses=poses, jfs=jfs,
                           managers=managers, error=error, emp=None,
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
    to_val = pa['to_value']

    if a_type == 'dept_change':
        new_id = to_val.split('|')[-1]
        db.execute('UPDATE users SET department_id=? WHERE id=?', (new_id, emp_id))
    elif a_type == 'position_change':
        new_id = to_val.split('|')[-1]
        db.execute('UPDATE users SET position_id=? WHERE id=?', (new_id, emp_id))
    elif a_type == 'role_change':
        db.execute('UPDATE users SET role=? WHERE id=?', (to_val, emp_id))
    elif a_type == 'employment_type_change':
        db.execute('UPDATE users SET employment_type=? WHERE id=?', (to_val, emp_id))
    elif a_type == 'manager_change':
        new_id = to_val.split('|')[-1] or None
        db.execute('UPDATE users SET manager_id=? WHERE id=?', (new_id, emp_id))
    elif a_type == 'salary_change':
        new_salary = int(to_val)
        old_row = db.execute('SELECT 1 FROM employee_salary WHERE user_id=?', (emp_id,)).fetchone()
        if old_row:
            db.execute('UPDATE employee_salary SET base_salary=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?', (new_salary, emp_id))
        else:
            db.execute('INSERT INTO employee_salary (user_id, base_salary) VALUES (?,?)', (emp_id, new_salary))

    db.execute("UPDATE personnel_actions SET status='approved', processed_by=? WHERE id=?", (session['user_id'], action_id))
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

    # 기존 퇴직금 지급 내역
    existing = db.execute(
        'SELECT * FROM severance_payments WHERE user_id=? ORDER BY processed_at DESC LIMIT 1',
        (emp_id,)
    ).fetchone()

    # 최근 3개월 payslip 조회
    recent_payslips = db.execute(
        'SELECT year, month, gross_pay FROM payslips '
        'WHERE user_id=? ORDER BY year DESC, month DESC LIMIT 3',
        (emp_id,)
    ).fetchall()
    payslip_list = [dict(r) for r in recent_payslips]

    term_date = emp['termination_date'] or date.today().isoformat()
    result    = calc_severance(
        emp['hire_date'] or '', term_date, payslip_list
    )

    if request.method == 'POST':
        note = request.form.get('note', '').strip() or None
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
            db.commit()
            flash(f'퇴직금 {fmt_krw(result["severance_amount"])}원 처리가 완료되었습니다.', 'success')
        return redirect(url_for('employees'))

    return render_template('employees/severance.html',
                           emp=emp, result=result, existing=existing,
                           term_date=term_date,
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
            if name:
                db.execute('INSERT INTO departments (name, parent_id) VALUES (?, ?)', (name, parent_id))
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
        'SELECT d.*, p.name AS parent_name, COUNT(u.id) AS member_count '
        'FROM departments d '
        'LEFT JOIN departments p ON d.parent_id = p.id '
        'LEFT JOIN users u ON u.department_id = d.id AND u.status="active" '
        'GROUP BY d.id ORDER BY d.name'
    ).fetchall()
    all_depts = db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    poses     = db.execute('SELECT * FROM positions ORDER BY level').fetchall()
    return render_template('admin/departments.html',
                           depts=depts, all_depts=all_depts, poses=poses,
                           active_page='departments')


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
        'law': '근로기준법 §60',
        'desc': '연간 부여된 유급 연차를 사용합니다. 잔여 연차 내에서 기간을 선택하세요.',
        'icon': 'fa-umbrella-beach', 'color': '#3b82f6',
    },
    'half_am': {
        'label': '오전 반차', 'group': '연차',
        'deduct': 'annual', 'fixed_days': 0.5, 'max_days': 0.5,
        'law': '근로기준법 §60',
        'desc': '오전(~13:00) 반일 유급휴가. 0.5일이 연차에서 차감됩니다.',
        'icon': 'fa-sun', 'color': '#3b82f6',
    },
    'half_pm': {
        'label': '오후 반차', 'group': '연차',
        'deduct': 'annual', 'fixed_days': 0.5, 'max_days': 0.5,
        'law': '근로기준법 §60',
        'desc': '오후(13:00~) 반일 유급휴가. 0.5일이 연차에서 차감됩니다.',
        'icon': 'fa-moon', 'color': '#3b82f6',
    },
    'sick': {
        'label': '병가', 'group': '연차',
        'deduct': 'annual', 'fixed_days': None, 'max_days': None,
        'law': '취업규칙',
        'desc': '질병·부상으로 인한 휴가. 연차에서 차감됩니다. 진단서 제출이 필요할 수 있습니다.',
        'icon': 'fa-kit-medical', 'color': '#ef4444',
    },
    # ── 법정 특별휴가 (연차 비차감) ─────────────────────────
    'maternity': {
        'label': '출산전후휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': 90, 'max_days': 90,
        'law': '근로기준법 §74',
        'desc': '출산 전후 90일 유급 보장 (다태아 120일). 출산 후 최소 45일 이상 포함되어야 합니다. 연차에서 차감되지 않습니다.',
        'icon': 'fa-baby', 'color': '#ec4899',
    },
    'paternity': {
        'label': '배우자출산휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': 10, 'max_days': 10,
        'law': '남녀고용평등법 §18의2',
        'desc': '배우자 출산 시 10일 유급. 출산일로부터 90일 이내 사용. 연차에서 차감되지 않습니다.',
        'icon': 'fa-person', 'color': '#8b5cf6',
    },
    'parental': {
        'label': '육아휴직', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 365,
        'law': '남녀고용평등법 §19',
        'desc': '만 8세 이하 자녀 양육을 위한 최대 1년 휴직. 고용보험에서 육아휴직급여 지급. 신청 30일 전 서면 통보 필요.',
        'icon': 'fa-baby-carriage', 'color': '#10b981',
    },
    'family_care': {
        'label': '가족돌봄휴직', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 90,
        'law': '남녀고용평등법 §22의2',
        'desc': '가족 질병·사고·노령으로 인한 돌봄. 연간 최대 90일 무급. (단기 가족돌봄휴가: 연 10일 별도)',
        'icon': 'fa-heart-pulse', 'color': '#f59e0b',
    },
    'bereavement': {
        'label': '경조사휴가', 'group': '법정 특별휴가',
        'deduct': 'none', 'fixed_days': None, 'max_days': 5,
        'law': '취업규칙 (법정 아님)',
        'desc': '경조사 발생 시 회사 규정에 따라 부여. 부모·배우자 사망 5일, 자녀·형제 3일 등 (회사별 상이). 연차에서 차감되지 않습니다.',
        'icon': 'fa-ribbon', 'color': '#6b7280',
    },
    # ── 기타 (비소진형) ──────────────────────────────────────
    'military': {
        'label': '예비군·민방위', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'law': '병역법 §44',
        'desc': '예비군 훈련 및 민방위 소집 기간. 유급 처리. 소집 통지서 사본 첨부 필요.',
        'icon': 'fa-shield-halved', 'color': '#64748b',
    },
    'compensation': {
        'label': '대체휴무', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'law': '근로기준법 §57',
        'desc': '초과 근무 대신 부여받은 대체 휴무일. 연차에서 차감되지 않습니다.',
        'icon': 'fa-arrows-rotate', 'color': '#64748b',
    },
    'remote': {
        'label': '재택근무', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'law': '취업규칙',
        'desc': '재택근무 신청. 연차에서 차감되지 않습니다.',
        'icon': 'fa-house-laptop', 'color': '#64748b',
    },
    'outing': {
        'label': '외출', 'group': '기타',
        'deduct': 'none', 'fixed_days': None, 'max_days': None,
        'law': '취업규칙',
        'desc': '업무 관련 외출. 연차에서 차감되지 않습니다.',
        'icon': 'fa-person-walking', 'color': '#64748b',
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
        leave_type = request.form.get('type', '')
        start_date = request.form.get('start_date', '')
        end_date   = request.form.get('end_date', '')
        reason     = request.form.get('reason', '').strip()

        if not leave_type or not start_date or not end_date:
            error = '유형, 시작일, 종료일은 필수입니다.'
        elif leave_type not in LEAVE_META:
            error = '올바르지 않은 신청 유형입니다.'
        elif start_date > end_date:
            error = '종료일이 시작일보다 앞설 수 없습니다.'
        else:
            db   = get_db()
            uid  = session['user_id']
            meta = LEAVE_META[leave_type]

            # 일수 계산
            if meta['fixed_days'] is not None and meta['fixed_days'] > 0:
                days = meta['fixed_days']
            elif leave_type in ('half_am', 'half_pm'):
                days = 0.5
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

                # 알림 발송: 매니저에게
                emp = db.execute('SELECT name, manager_id FROM users WHERE id=?', (uid,)).fetchone()
                if emp and emp['manager_id']:
                    add_notification(
                        emp['manager_id'], 'action', 'leave',
                        f"근태 신청: {emp['name']}",
                        f"{emp['name']}님이 {meta['label']}을(를) 신청했습니다. ({start_date} ~ {end_date})",
                        url_for('attendance', status='pending')
                    )

                flash(f'{meta["label"]} 신청이 완료되었습니다.', 'success')
                return redirect(url_for('leave_my'))

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

@app.route('/leave/<int:req_id>/cancel', methods=['POST'])
@login_required
def leave_cancel(req_id):
    db  = get_db()
    req = db.execute('SELECT * FROM leave_requests WHERE id=?', (req_id,)).fetchone()
    if req and req['user_id'] == session['user_id'] and req['status'] == 'pending':
        db.execute("UPDATE leave_requests SET status='cancelled' WHERE id=?", (req_id,))
        db.commit()
    return redirect(url_for('leave_my'))

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
    return render_template('attendance/list.html', reqs=reqs, status=status,
                           depts=depts, dept_id=dept_id,
                           pending_count=pending_count, labels=LEAVE_LABELS,
                           active_page='attendance')

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
            return redirect(url_for('attendance'))
        # 본인의 부서원인지 확인 (또는 직속 부하인지)
        mgr_dept = session.get('dept_id', 0)
        if mgr_dept and req['department_id'] != mgr_dept:
            abort(403)
        
        db.execute(
            "UPDATE leave_requests SET status='reviewed', manager_id=?, manager_approved_at=CURRENT_TIMESTAMP WHERE id=?",
            (uid, req_id)
        )
        db.commit()

        # 알림 발송: HR(Admin)에게 최종 승인 요청
        admins = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
        for admin in admins:
            add_notification(
                admin['id'], 'action', 'leave',
                f"근태 검토 완료: {req['user_id']}",
                f"매니저가 신청 건을 검토했습니다. 최종 승인이 필요합니다.",
                url_for('attendance', status='reviewed')
            )

        flash('매니저 검토 승인이 완료되었습니다. HR 최종 승인을 대기합니다.', 'success')

    # HR(Admin) 최종 승인 단계
    elif role == 'admin':
        if req['status'] not in ['pending', 'reviewed']:
            flash('최종 승인이 불가능한 상태입니다.', 'error')
            return redirect(url_for('attendance'))
        
        db.execute(
            "UPDATE leave_requests SET status='approved', hr_id=?, hr_approved_at=CURRENT_TIMESTAMP, approver_id=? WHERE id=?",
            (uid, uid, req_id)
        )
        db.commit()

        # 알림 발송: 본인에게 승인 알림
        add_notification(
            req['user_id'], 'info', 'leave',
            "근태 승인 완료",
            f"신청하신 근태가 최종 승인되었습니다.",
            url_for('leave_my')
        )

        flash('HR 최종 승인이 완료되었습니다.', 'success')

    return redirect(url_for('attendance'))

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
        return redirect(url_for('attendance'))
    
    mgr_dept = session.get('dept_id', 0)
    if session.get('user_role') == 'manager' and mgr_dept and req['department_id'] != mgr_dept:
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
    return redirect(url_for('attendance'))

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
            uid   = request.form.get('user_id')
            base  = int(request.form.get('base_salary', 0))
            meal  = int(request.form.get('meal_allowance', 0))
            trans = int(request.form.get('transport_allowance', 0))
            mw    = check_min_wage(base)
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

        # 월 급여 일괄 생성 (근태 연동)
        elif action == 'generate':
            year  = int(request.form.get('year', 2026))
            month = int(request.form.get('month', 1))
            if not (1 <= month <= 12):
                error = '올바른 월을 입력해주세요.'
            else:
                import calendar as cal_mod
                first_day = f"{year}-{month:02d}-01"
                last_day  = f"{year}-{month:02d}-{cal_mod.monthrange(year, month)[1]}"
                
                # 해당 월의 공휴일 목록 가져오기
                holiday_rows = db.execute('SELECT date FROM holidays WHERE date BETWEEN ? AND ?', (first_day, last_day)).fetchall()
                month_holidays = {h['date'] for h in holiday_rows}

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
                    
                    # 해당 직원의 월간 근태 기록 기반 수당 합계 계산
                    checkins = db.execute(
                        'SELECT * FROM checkins WHERE user_id=? AND date BETWEEN ? AND ?',
                        (e['id'], first_day, last_day)
                    ).fetchall()
                    
                    total_ot_pay = 0
                    for c in checkins:
                        is_h = c['date'] in month_holidays
                        # calc_extra_pay(overtime_min, night_min, base_salary, is_holiday, holiday_regular_min)
                        res = calc_extra_pay(
                            c['overtime_min'], 
                            c['night_min'], 
                            e['base_salary'],
                            is_holiday=is_h,
                            holiday_regular_min=c['regular_min'] if is_h else 0
                        )
                        total_ot_pay += res['total_extra_pay']

                    result = calc_payslip(
                        e['base_salary'],
                        e['meal_allowance'],
                        e['transport_allowance'],
                        overtime_pay=total_ot_pay
                    )
                    
                    db.execute(
                        'INSERT INTO payslips '
                        '(user_id, year, month, base_salary, meal_allowance, transport_allowance, '
                        'overtime_pay, national_pension, health_insurance, long_term_care, '
                        'employment_insurance, income_tax, local_income_tax, '
                        'gross_pay, total_deduction, net_pay) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (e['id'], year, month,
                         result['base_salary'], result['meal_allowance'],
                         result['transport_allowance'], result['overtime_pay'],
                         result['national_pension'], result['health_insurance'],
                         result['long_term_care'], result['employment_insurance'],
                         result['income_tax'], result['local_income_tax'],
                         result['gross_pay'], result['total_deduction'], result['net_pay'])
                    )
                    count += 1
                db.commit()
                msg = f'{year}년 {month}월 급여명세서 {count}건이 생성되었습니다. (근태 수당 자동 포함)'

    emps = db.execute(
        'SELECT u.id, u.name, d.name AS dept_name, p.name AS pos_name, '
        'COALESCE(s.base_salary, 0) AS base_salary, '
        'COALESCE(s.meal_allowance, 0) AS meal_allowance, '
        'COALESCE(s.transport_allowance, 0) AS transport_allowance '
        'FROM users u '
        'LEFT JOIN departments d ON u.department_id = d.id '
        'LEFT JOIN positions   p ON u.position_id   = p.id '
        'LEFT JOIN employee_salary s ON u.id = s.user_id '
        "WHERE u.status='active' ORDER BY d.name, u.name"
    ).fetchall()
    return render_template('payroll/admin.html', emps=emps,
                           error=error, msg=msg, fmt_krw=fmt_krw,
                           active_page='admin_payroll')


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

    return render_template('performance/index.html',
                           cycles=cycles, active_cycle=active_cycle,
                           selected_cycle=selected_cycle,
                           goals=goals, score_labels=SCORE_LABELS,
                           active_page='performance')

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

    return render_template('performance/goal_form.html',
                           cycles=cycles, error=error,
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
            phone = request.form.get('phone', '').strip() or None
            db.execute('UPDATE users SET phone=? WHERE id=?', (phone, uid))
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
    ('applied',    '지원 접수'),
    ('screening',  '서류 심사'),
    ('interview1', '1차 면접'),
    ('interview2', '2차 면접'),
    ('final',      '최종 면접'),
    ('offered',    '오퍼'),
    ('hired',      '입사 확정'),
    ('rejected',   '불합격'),
]
STAGE_MAP     = dict(STAGES)
ACTIVE_STAGES = [s for s in STAGES if s[0] != 'rejected']

SOURCE_LABELS = {
    'direct':   '직접 지원',
    'referral': '내부 추천',
    'headhunt': '헤드헌팅',
    'platform': '채용 플랫폼',
    'other':    '기타',
}

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
    posting_id = request.args.get('posting', '')
    postings   = db.execute(
        "SELECT * FROM job_postings WHERE status != 'draft' ORDER BY created_at DESC"
    ).fetchall()

    sql    = (
        'SELECT a.*, jp.title AS posting_title '
        'FROM applicants a '
        'JOIN job_postings jp ON a.posting_id = jp.id '
    )
    params = []
    if posting_id:
        sql += 'WHERE a.posting_id = ? '
        params.append(posting_id)
    sql += 'ORDER BY a.created_at DESC'
    applicants = db.execute(sql, params).fetchall()

    pipeline = {stage: [] for stage, _ in STAGES}
    for a in applicants:
        stage = a['stage']
        if stage in pipeline:
            pipeline[stage].append(a)

    return render_template('recruit/pipeline.html',
                           pipeline=pipeline, stages=STAGES,
                           active_stages=ACTIVE_STAGES,
                           postings=postings, posting_id=posting_id,
                           active_page='recruit_pipeline')

@app.route('/recruit/applicants/<int:applicant_id>', methods=['GET', 'POST'])
@recruiter_or_admin
def recruit_applicant_detail(applicant_id):
    db        = get_db()
    applicant = db.execute(
        'SELECT a.*, jp.title AS posting_title, jp.id AS posting_id '
        'FROM applicants a JOIN job_postings jp ON a.posting_id = jp.id '
        'WHERE a.id=?', (applicant_id,)
    ).fetchone()
    if not applicant:
        abort(404)

    if request.method == 'POST':
        new_stage = request.form.get('stage', '')
        note      = request.form.get('note', '').strip() or None
        if new_stage in STAGE_MAP:
            db.execute('UPDATE applicants SET stage=? WHERE id=?', (new_stage, applicant_id))
            db.execute(
                'INSERT INTO applicant_logs (applicant_id, stage, note, changed_by) VALUES (?, ?, ?, ?)',
                (applicant_id, new_stage, note, session['user_id'])
            )
            db.commit()
        return redirect(url_for('recruit_applicant_detail', applicant_id=applicant_id))

    logs = db.execute(
        'SELECT l.*, u.name AS changed_by_name '
        'FROM applicant_logs l JOIN users u ON l.changed_by = u.id '
        'WHERE l.applicant_id=? ORDER BY l.created_at DESC',
        (applicant_id,)
    ).fetchall()
    return render_template('recruit/applicant_detail.html',
                           applicant=applicant, logs=logs,
                           stages=STAGES, stage_map=STAGE_MAP,
                           source_labels=SOURCE_LABELS,
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


# ── Attendance Home ─────────────────────────────────────────
@app.route('/attendance/home')
@login_required
def attendance_home():
    from datetime import date, timedelta
    db    = get_db()
    uid   = session['user_id']
    today = date.today()

    checkin = db.execute(
        'SELECT * FROM checkins WHERE user_id=? AND date=?',
        (uid, today.isoformat())
    ).fetchone()

    hire_row = db.execute('SELECT hire_date FROM users WHERE id=?', (uid,)).fetchone()
    total_leave  = calc_annual_leave(hire_row['hire_date']) if hire_row and hire_row['hire_date'] else 15
    used_leave   = db.execute(
        "SELECT COALESCE(SUM(days),0) FROM leave_requests "
        "WHERE user_id=? AND status='approved' AND type IN ('annual','half_am','half_pm','sick')",
        (uid,)
    ).fetchone()[0]

    recent_requests = db.execute(
        'SELECT * FROM leave_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 5',
        (uid,)
    ).fetchall()

    first_day = today.replace(day=1)
    if today.month == 12:
        last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)

    pending = db.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE user_id=? AND status='pending'",
        (uid,)
    ).fetchone()[0]

    # 해당 월의 근태 기록 기반 수당 합계 계산 (공휴일 반영)
    checkins_month = db.execute(
        'SELECT * FROM checkins WHERE user_id=? AND date>=? AND date<=? ORDER BY date DESC',
        (uid, first_day.isoformat(), last_day.isoformat())
    ).fetchall()

    # 해당 월의 공휴일 목록
    holiday_rows = db.execute('SELECT date FROM holidays WHERE date BETWEEN ? AND ?', (first_day.isoformat(), last_day.isoformat())).fetchall()
    month_holidays = {h['date'] for h in holiday_rows}

    base_row = db.execute(
        'SELECT base_salary FROM employee_salary WHERE user_id=? ORDER BY updated_at DESC LIMIT 1',
        (uid,)
    ).fetchone()
    base_salary = base_row['base_salary'] if base_row else 0

    month_regular_min = 0
    month_overtime_min = 0
    month_night_min = 0
    total_extra_pay_amount = 0

    for c in checkins_month:
        month_regular_min  += c['regular_min']
        month_overtime_min += c['overtime_min']
        month_night_min    += c['night_min']
        
        is_h = c['date'] in month_holidays
        res = calc_extra_pay(
            c['overtime_min'], 
            c['night_min'], 
            base_salary,
            is_holiday=is_h,
            holiday_regular_min=c['regular_min'] if is_h else 0
        )
        total_extra_pay_amount += res['total_extra_pay']

    # 템플릿 호환성을 위해 dict 구조 생성
    extra_pay = {'total_extra_pay': total_extra_pay_amount}

    return render_template('attendance/home.html',
                           today=today,
                           checkin=checkin,
                           total_leave=total_leave,
                           used_leave=float(used_leave),
                           remain_leave=total_leave - float(used_leave),
                           recent_requests=recent_requests,
                           checkins_month=checkins_month,
                           pending=pending,
                           labels=LEAVE_LABELS,
                           month_regular_min=month_regular_min,
                           month_overtime_min=month_overtime_min,
                           month_night_min=month_night_min,
                           extra_pay=extra_pay,
                           active_page='attendance_home')


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


@app.route('/attendance/flex-schedule')
@login_required
def flex_schedule():
    from datetime import date, timedelta
    db  = get_db()
    uid = session['user_id']

    # 주 선택 (기본: 이번 주 월요일)
    week_str = request.args.get('week', '')
    try:
        week_start = date.fromisoformat(week_str)
        week_start = _week_monday(week_start)
    except ValueError:
        week_start = _week_monday(date.today())

    week_end   = week_start + timedelta(days=4)
    prev_week  = (week_start - timedelta(days=7)).isoformat()
    next_week  = (week_start + timedelta(days=7)).isoformat()
    week_days  = [week_start + timedelta(days=i) for i in range(5)]

    # 해당 주 스케줄 조회
    sched = db.execute(
        'SELECT * FROM flex_schedules WHERE user_id=? AND week_start=?',
        (uid, week_start.isoformat())
    ).fetchone()

    blocks = []
    if sched:
        blocks = db.execute(
            'SELECT * FROM flex_blocks WHERE schedule_id=? ORDER BY work_date, start_time',
            (sched['id'],)
        ).fetchall()

    # 사용자 근무제 유형
    emp = db.execute('SELECT work_type FROM users WHERE id=?', (uid,)).fetchone()
    work_type = emp['work_type'] if emp else 'standard'

    return render_template('attendance/flex_schedule.html',
                           week_start=week_start,
                           week_end=week_end,
                           week_days=week_days,
                           prev_week=prev_week,
                           next_week=next_week,
                           sched=sched,
                           blocks=blocks,
                           work_type=work_type,
                           work_type_label=WORK_TYPES.get(work_type, '일반근무'),
                           block_types=BLOCK_TYPES,
                           active_page='flex_schedule')


@app.route('/attendance/flex-schedule/submit', methods=['POST'])
@login_required
def flex_schedule_submit():
    from datetime import date, datetime
    db   = get_db()
    uid  = session['user_id']

    week_start  = request.form.get('week_start', '')
    note        = request.form.get('note', '').strip() or None
    blocks_json = request.form.get('blocks_json', '[]')

    try:
        week_date = date.fromisoformat(week_start)
    except ValueError:
        flash('올바르지 않은 주 정보입니다.', 'error')
        return redirect(url_for('flex_schedule'))

    import json
    try:
        raw_blocks = json.loads(blocks_json)
    except (ValueError, TypeError):
        raw_blocks = []

    # 기존 스케줄 upsert
    existing = db.execute(
        'SELECT id, status FROM flex_schedules WHERE user_id=? AND week_start=?',
        (uid, week_start)
    ).fetchone()

    action = request.form.get('action', 'draft')
    status = 'pending' if action == 'submit' else 'draft'
    now    = datetime.now().isoformat()

    if existing:
        if existing['status'] in ('approved', 'pending') and action == 'submit':
            flash('이미 제출되었거나 승인된 스케줄입니다.', 'error')
            return redirect(url_for('flex_schedule', week=week_start))
        db.execute(
            'UPDATE flex_schedules SET note=?, status=?, submitted_at=? WHERE id=?',
            (note, status, now if action == 'submit' else None, existing['id'])
        )
        sched_id = existing['id']
        db.execute('DELETE FROM flex_blocks WHERE schedule_id=?', (sched_id,))
    else:
        cur = db.execute(
            'INSERT INTO flex_schedules (user_id, week_start, status, note, submitted_at) '
            'VALUES (?,?,?,?,?)',
            (uid, week_start, status, note, now if action == 'submit' else None)
        )
        sched_id = cur.lastrowid

    for blk in raw_blocks:
        work_date  = blk.get('date', '')
        start_time = blk.get('start', '')
        end_time   = blk.get('end', '')
        block_type = blk.get('type', 'office')
        if work_date and start_time and end_time and block_type in BLOCK_TYPES:
            db.execute(
                'INSERT INTO flex_blocks (schedule_id, work_date, start_time, end_time, block_type) '
                'VALUES (?,?,?,?,?)',
                (sched_id, work_date, start_time, end_time, block_type)
            )

    db.commit()

    if action == 'submit':
        flash('근무 계획이 매니저에게 제출되었습니다.', 'success')
    else:
        flash('초안이 저장되었습니다.', 'success')

    return redirect(url_for('flex_schedule', week=week_start))


@app.route('/attendance/flex-approvals')
@manager_or_admin
def flex_approvals():
    db  = get_db()
    uid = session['user_id']

    if session['user_role'] == 'manager':
        rows = db.execute(
            "SELECT fs.*, u.name user_name, u.emp_no, d.name dept_name "
            "FROM flex_schedules fs "
            "JOIN users u ON fs.user_id = u.id "
            "LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE u.department_id = (SELECT department_id FROM users WHERE id=?) "
            "AND fs.status = 'pending' ORDER BY fs.submitted_at DESC",
            (uid,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT fs.*, u.name user_name, u.emp_no, d.name dept_name "
            "FROM flex_schedules fs "
            "JOIN users u ON fs.user_id = u.id "
            "LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE fs.status = 'pending' ORDER BY fs.submitted_at DESC"
        ).fetchall()

    # 각 스케줄의 블록도 함께 조회
    schedules = []
    for row in rows:
        blocks = db.execute(
            'SELECT * FROM flex_blocks WHERE schedule_id=? ORDER BY work_date, start_time',
            (row['id'],)
        ).fetchall()
        schedules.append({'sched': row, 'blocks': blocks})

    return render_template('attendance/flex_approvals.html',
                           schedules=schedules,
                           active_page='flex_approvals')


@app.route('/attendance/flex-approvals/<int:sched_id>/approve', methods=['POST'])
@manager_or_admin
def flex_approve(sched_id):
    from datetime import datetime
    db  = get_db()
    uid = session['user_id']
    db.execute(
        "UPDATE flex_schedules SET status='approved', approved_by=?, approved_at=? WHERE id=?",
        (uid, datetime.now().isoformat(), sched_id)
    )
    db.commit()
    flash('근무 계획을 승인했습니다.', 'success')
    return redirect(url_for('flex_approvals'))


@app.route('/attendance/flex-approvals/<int:sched_id>/reject', methods=['POST'])
@manager_or_admin
def flex_reject(sched_id):
    db     = get_db()
    reason = request.form.get('reason', '').strip() or None
    db.execute(
        "UPDATE flex_schedules SET status='rejected', reject_reason=? WHERE id=?",
        (reason, sched_id)
    )
    db.commit()
    flash('근무 계획을 반려했습니다.', 'success')
    return redirect(url_for('flex_approvals'))


@app.route('/attendance/checkin', methods=['POST'])
@login_required
def do_checkin():
    from datetime import date, datetime
    db    = get_db()
    uid   = session['user_id']
    today = date.today().isoformat()
    now   = datetime.now().strftime('%H:%M')
    db.execute(
        'INSERT INTO checkins (user_id, date, check_in) VALUES (?, ?, ?) '
        'ON CONFLICT(user_id, date) DO UPDATE SET check_in=excluded.check_in',
        (uid, today, now)
    )
    db.commit()
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
        hrs = calc_day_hours(today, row['check_in'] or '09:00', now)
        db.execute(
            'UPDATE checkins SET check_out=?, regular_min=?, overtime_min=?, night_min=? '
            'WHERE user_id=? AND date=?',
            (now, hrs['regular_min'], hrs['overtime_min'], hrs['night_min'], uid, today)
        )
        db.commit()
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

    # 내가 받은 다면평가 결과
    received_peer = []
    if cycle_id:
        rows = db.execute(
            "SELECT pr.*, u.name AS reviewer_name "
            "FROM peer_reviews pr JOIN users u ON pr.reviewer_id = u.id "
            "WHERE pr.cycle_id=? AND pr.reviewee_id=? AND pr.review_type='peer' "
            "ORDER BY pr.created_at DESC",
            (cycle_id, uid)
        ).fetchall()
        received_peer = rows

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

    # 직원 목록 (배정 선택용)
    mgr_dept = int(session.get('dept_id') or 0)
    if role == 'manager' and mgr_dept:
        employees = db.execute(
            "SELECT id, name, role FROM users WHERE department_id=? AND status='active' ORDER BY name",
            (mgr_dept,)
        ).fetchall()
    else:
        employees = db.execute(
            "SELECT id, name, role FROM users WHERE status='active' ORDER BY name"
        ).fetchall()

    return render_template('performance/peer_assignments.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           assignments=assignments, employees=employees,
                           cycle_id=cycle_id, error=error,
                           active_page='peer_assignments')


@app.route('/performance/calibration')
@admin_required
def calibration():
    db = get_db()

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

    employees = []
    grade_dist = {'S': 0, 'A': 0, 'B': 0, 'C': 0, 'D': 0}

    if cycle_id:
        # 해당 주기에 목표가 있는 직원 목록
        rows = db.execute(
            'SELECT u.id, u.name, u.role, d.name AS dept_name, '
            'AVG(g.self_score) AS self_avg, '
            'AVG(r.score) AS mgr_avg '
            'FROM users u '
            'JOIN performance_goals g ON g.user_id = u.id AND g.cycle_id = ? '
            'LEFT JOIN performance_reviews r ON r.goal_id = g.id '
            'LEFT JOIN departments d ON u.department_id = d.id '
            'GROUP BY u.id ORDER BY u.name',
            (cycle_id,)
        ).fetchall()

        for row in rows:
            uid = row['id']

            # 동료평가 평균
            peer_row = db.execute(
                "SELECT AVG(score) AS avg FROM peer_reviews "
                "WHERE cycle_id=? AND reviewee_id=? AND review_type='peer'",
                (cycle_id, uid)
            ).fetchone()
            peer_avg = round(peer_row['avg'], 2) if peer_row['avg'] else None

            # 매니저 upward 평가 평균 (매니저인 경우)
            upward_avg = None
            if row['role'] in ('manager', 'admin'):
                u_rows = db.execute(
                    "SELECT * FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND review_type='upward'",
                    (cycle_id, uid)
                ).fetchall()
                if len(u_rows) >= 3:
                    all_q = []
                    for ur in u_rows:
                        q_vals = [ur[f'q{i}_score'] for i in range(1, 6) if ur[f'q{i}_score'] is not None]
                        if q_vals:
                            all_q.append(sum(q_vals) / len(q_vals))
                    upward_avg = round(sum(all_q) / len(all_q), 2) if all_q else None

            # 캘리브레이션 최종 등급
            cal = db.execute(
                'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=?',
                (cycle_id, uid)
            ).fetchone()

            # 종합 점수 (자기평가, 동료, 매니저 평균)
            scores = [v for v in [row['self_avg'], peer_avg, row['mgr_avg']] if v is not None]
            overall = round(sum(scores) / len(scores), 2) if scores else None

            # 권고 등급
            if overall is None:
                rec_grade = '-'
            elif overall >= 4.5:
                rec_grade = 'S'
            elif overall >= 3.5:
                rec_grade = 'A'
            elif overall >= 2.5:
                rec_grade = 'B'
            elif overall >= 1.5:
                rec_grade = 'C'
            else:
                rec_grade = 'D'

            final_grade = cal['final_grade'] if cal else None
            if final_grade:
                grade_dist[final_grade] = grade_dist.get(final_grade, 0) + 1

            employees.append({
                'id': uid,
                'name': row['name'],
                'role': row['role'],
                'dept_name': row['dept_name'] or '',
                'self_avg': round(row['self_avg'], 2) if row['self_avg'] else None,
                'peer_avg': peer_avg,
                'mgr_avg': round(row['mgr_avg'], 2) if row['mgr_avg'] else None,
                'upward_avg': upward_avg,
                'overall': overall,
                'rec_grade': rec_grade,
                'final_grade': final_grade,
                'cal_note': cal['note'] if cal else None,
            })

    return render_template('performance/calibration.html',
                           cycles=cycles, selected_cycle=selected_cycle,
                           employees=employees, grade_dist=grade_dist,
                           cycle_id=cycle_id,
                           active_page='calibration')


@app.route('/performance/calibration/<int:target_uid>', methods=['GET', 'POST'])
@admin_required
def calibration_detail(target_uid):
    db       = get_db()
    admin_id = session['user_id']

    try:
        cycle_id = int(request.args.get('cycle', 0))
    except (ValueError, TypeError):
        cycle_id = 0
    if not cycle_id:
        abort(400)

    cycle   = db.execute('SELECT * FROM performance_cycles WHERE id=?', (cycle_id,)).fetchone()
    target  = db.execute('SELECT id, name, role FROM users WHERE id=?', (target_uid,)).fetchone()
    if not cycle or not target:
        abort(404)

    if request.method == 'POST':
        final_grade = request.form.get('final_grade', '').strip()
        note        = request.form.get('note', '').strip() or None
        if final_grade in ('S', 'A', 'B', 'C', 'D'):
            db.execute(
                'INSERT INTO calibration_results (cycle_id, user_id, final_grade, note, decided_by) '
                'VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(cycle_id, user_id) DO UPDATE SET '
                'final_grade=excluded.final_grade, note=excluded.note, '
                'decided_by=excluded.decided_by, decided_at=CURRENT_TIMESTAMP',
                (cycle_id, target_uid, final_grade, note, admin_id)
            )
            db.commit()
        return redirect(url_for('calibration', cycle=cycle_id))

    # 목표별 자기평가 + 매니저평가
    goals = db.execute(
        'SELECT g.*, AVG(r.score) AS mgr_score_avg, COUNT(r.id) AS review_count '
        'FROM performance_goals g '
        'LEFT JOIN performance_reviews r ON r.goal_id = g.id '
        'WHERE g.user_id=? AND g.cycle_id=? GROUP BY g.id ORDER BY g.created_at',
        (target_uid, cycle_id)
    ).fetchall()

    # 자기평가 평균
    self_scores = [g['self_score'] for g in goals if g['self_score'] is not None]
    self_avg = round(sum(self_scores) / len(self_scores), 2) if self_scores else None

    # 매니저평가 평균
    mgr_scores = [g['mgr_score_avg'] for g in goals if g['mgr_score_avg'] is not None]
    mgr_avg = round(sum(mgr_scores) / len(mgr_scores), 2) if mgr_scores else None

    # 동료평가
    peer_rows = db.execute(
        "SELECT pr.*, u.name AS reviewer_name FROM peer_reviews pr "
        "JOIN users u ON pr.reviewer_id = u.id "
        "WHERE pr.cycle_id=? AND pr.reviewee_id=? AND pr.review_type='peer' "
        "ORDER BY pr.created_at",
        (cycle_id, target_uid)
    ).fetchall()
    peer_scores = [r['score'] for r in peer_rows if r['score'] is not None]
    peer_avg = round(sum(peer_scores) / len(peer_scores), 2) if peer_scores else None

    # 매니저 upward 평가
    upward_rows = []
    upward_avg = None
    if target['role'] in ('manager', 'admin'):
        upward_rows = db.execute(
            "SELECT * FROM peer_reviews WHERE cycle_id=? AND reviewee_id=? AND review_type='upward'",
            (cycle_id, target_uid)
        ).fetchall()
        if len(upward_rows) >= 3:
            all_q = []
            for ur in upward_rows:
                q_vals = [ur[f'q{i}_score'] for i in range(1, 6) if ur[f'q{i}_score'] is not None]
                if q_vals:
                    all_q.append(sum(q_vals) / len(q_vals))
            upward_avg = round(sum(all_q) / len(all_q), 2) if all_q else None

    # 기존 캘리브레이션 결과
    cal = db.execute(
        'SELECT * FROM calibration_results WHERE cycle_id=? AND user_id=?',
        (cycle_id, target_uid)
    ).fetchone()

    # AI 요약
    summary = generate_calibration_summary(
        target['name'], self_avg, peer_avg, mgr_avg, upward_avg
    )

    return render_template('performance/calibration_detail.html',
                           cycle=cycle, target=target,
                           goals=goals, peer_rows=peer_rows,
                           upward_rows=upward_rows,
                           upward_questions=UPWARD_QUESTIONS,
                           self_avg=self_avg, peer_avg=peer_avg,
                           mgr_avg=mgr_avg, upward_avg=upward_avg,
                           summary=summary, cal=cal,
                           cycle_id=cycle_id,
                           active_page='calibration')


# ── Export (Excel 내보내기) ──────────────────────────────────
from export_utils import (make_wb, write_header, write_row, auto_width,
                           freeze_header, to_response, apply_number_format,
                           KRW_FORMAT, NUM_FORMAT)
import urllib.parse


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


# ── Run ─────────────────────────────────────────────────────
if __name__ == '__main__':
    from database import init_db
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true')
