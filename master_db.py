"""
master_db.py — SaaS 멀티테넌시 마스터 DB 관리

master.db 역할:
  - tenants: 가입 회사 목록, 상태(trial/active/suspended)
  - subscriptions: 플랜, peak headcount, 토스 billing key
  - billing_logs: 결제 기록
  - tenant_users: 이메일 → tenant_id 매핑 (로그인 라우팅용)

테넌트 DB 규칙:
  - 테넌트 1 (데모): hr_system.db (기존 파일 유지)
  - 테넌트 2+: tenant_2.db, tenant_3.db ...
"""

import os
import sqlite3
import re
from datetime import date, timedelta

_db_dir = os.environ.get('DB_DIR', '')
MASTER_DB = os.path.join(_db_dir, 'master.db') if _db_dir else 'master.db'

PRICE_PER_SEAT = 1000   # 원/인/월
TRIAL_DAYS     = 14


# ── 경로 헬퍼 ────────────────────────────────────────────────
def get_tenant_db_path(tenant_id: int) -> str:
    """테넌트 DB 파일 경로 반환. 테넌트 1은 기존 hr_system.db 사용."""
    name = 'hr_system.db' if tenant_id == 1 else f'tenant_{tenant_id}.db'
    return os.path.join(_db_dir, name) if _db_dir else name


# ── 마스터 DB 연결 ────────────────────────────────────────────
def get_master_db():
    conn = sqlite3.connect(MASTER_DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_master_db():
    """마스터 DB 테이블 초기화 (멱등)"""
    conn = get_master_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS tenants (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slug            TEXT UNIQUE NOT NULL,
            company_name    TEXT NOT NULL,
            admin_email     TEXT UNIQUE NOT NULL,
            status          TEXT NOT NULL DEFAULT 'trial'
                                CHECK(status IN ('trial','active','suspended','cancelled')),
            trial_ends_at   DATE NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id             INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
            status                TEXT NOT NULL DEFAULT 'trialing'
                                      CHECK(status IN ('trialing','active','past_due','cancelled')),
            peak_headcount        INTEGER NOT NULL DEFAULT 0,
            current_period_start  DATE,
            current_period_end    DATE,
            toss_billing_key      TEXT,
            grace_until           DATE,
            payment_retry_count   INTEGER NOT NULL DEFAULT 0,
            last_payment_attempt  TIMESTAMP,
            updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS billing_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
            amount          INTEGER NOT NULL,
            headcount       INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','paid','failed')),
            payment_key     TEXT,
            toss_order_id   TEXT UNIQUE,
            fail_reason     TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tenant_users (
            email       TEXT NOT NULL,
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
            PRIMARY KEY (email, tenant_id)
        );

        CREATE TABLE IF NOT EXISTS superadmins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()


# ── SaaS 관리자(운영자) ─────────────────────────────────────────
def seed_default_superadmin():
    """superadmins 테이블이 비어 있으면 기본 운영자 계정 생성 (멱등)."""
    from werkzeug.security import generate_password_hash
    conn = get_master_db()
    exists = conn.execute('SELECT 1 FROM superadmins LIMIT 1').fetchone()
    if not exists:
        conn.execute(
            'INSERT INTO superadmins (username, password_hash) VALUES (?,?)',
            ('hunie0709', generate_password_hash('1234'))
        )
        conn.commit()
    conn.close()


def get_superadmin_by_username(username: str):
    conn = get_master_db()
    row = conn.execute(
        'SELECT * FROM superadmins WHERE username = ?', (username,)
    ).fetchone()
    conn.close()
    return row


def list_tenants_with_state():
    """전체 테넌트 목록 + 구독/헤드카운트 요약 (SaaS 관리 페이지용)"""
    conn = get_master_db()
    rows = conn.execute(
        '''SELECT t.id, t.slug, t.company_name, t.admin_email, t.status AS tenant_status,
                  t.trial_ends_at, t.created_at,
                  s.status AS sub_status, s.peak_headcount,
                  s.current_period_start, s.current_period_end, s.toss_billing_key
           FROM tenants t
           LEFT JOIN subscriptions s ON s.tenant_id = t.id
           ORDER BY t.created_at DESC'''
    ).fetchall()
    conn.close()
    return rows


def set_tenant_status(tenant_id: int, status: str):
    """SaaS 운영자가 테넌트 상태를 수동으로 변경 (active/suspended/cancelled 등)"""
    conn = get_master_db()
    conn.execute('UPDATE tenants SET status=? WHERE id=?', (status, tenant_id))
    conn.commit()
    conn.close()


# ── 테넌트 생성 ──────────────────────────────────────────────
def slugify(text: str) -> str:
    """회사명을 URL-safe slug로 변환. 한글은 ASCII 음역 없이 랜덤 ID 대체."""
    import uuid
    # ASCII 문자만 남기기
    ascii_text = text.encode('ascii', errors='ignore').decode().lower().strip()
    ascii_text = re.sub(r'[^\w\s-]', '', ascii_text)
    ascii_text = re.sub(r'[\s_-]+', '-', ascii_text)
    ascii_text = re.sub(r'^-+|-+$', '', ascii_text)
    # ASCII 결과가 없으면 (순수 한글 사명 등) 랜덤 8자리 ID 사용
    return ascii_text if ascii_text else uuid.uuid4().hex[:8]


def create_tenant(company_name: str, admin_email: str) -> int:
    """
    새 테넌트 생성 (14일 트라이얼).
    slug 충돌 시 숫자 접미사 추가.
    tenant_id 반환.
    """
    conn = get_master_db()
    base_slug = slugify(company_name)

    # slug 충돌 처리
    slug = base_slug
    suffix = 1
    while conn.execute('SELECT id FROM tenants WHERE slug=?', (slug,)).fetchone():
        slug = f'{base_slug}-{suffix}'
        suffix += 1

    trial_ends = (date.today() + timedelta(days=TRIAL_DAYS)).isoformat()
    c = conn.cursor()
    c.execute(
        'INSERT INTO tenants (slug, company_name, admin_email, trial_ends_at) VALUES (?,?,?,?)',
        (slug, company_name, admin_email, trial_ends)
    )
    tenant_id = c.lastrowid
    c.execute(
        '''INSERT INTO subscriptions
           (tenant_id, status, current_period_start, current_period_end)
           VALUES (?,?,?,?)''',
        (tenant_id, 'trialing',
         date.today().isoformat(),
         (date.today() + timedelta(days=TRIAL_DAYS)).isoformat())
    )
    conn.commit()
    conn.close()
    return tenant_id


# ── 테넌트 조회 ──────────────────────────────────────────────
def get_tenant_by_email(email: str):
    """이메일로 테넌트 조회 (로그인 라우팅용)"""
    conn = get_master_db()
    row = conn.execute(
        '''SELECT t.id, t.slug, t.company_name, t.status, t.trial_ends_at,
                  s.status AS sub_status, s.peak_headcount,
                  s.toss_billing_key, s.current_period_end
           FROM tenant_users tu
           JOIN tenants t ON t.id = tu.tenant_id
           LEFT JOIN subscriptions s ON s.tenant_id = t.id
           WHERE tu.email = ?''',
        (email,)
    ).fetchone()
    conn.close()
    return row


def get_tenant(tenant_id: int):
    """tenant_id로 테넌트 + 구독 정보 조회"""
    conn = get_master_db()
    row = conn.execute(
        '''SELECT t.*, s.status AS sub_status, s.peak_headcount,
                  s.toss_billing_key, s.current_period_start, s.current_period_end
           FROM tenants t
           LEFT JOIN subscriptions s ON s.tenant_id = t.id
           WHERE t.id = ?''',
        (tenant_id,)
    ).fetchone()
    conn.close()
    return row


# ── tenant_users 동기화 ──────────────────────────────────────
def register_tenant_user(email: str, tenant_id: int):
    """직원 생성 시 master.db에 이메일-테넌트 매핑 등록"""
    conn = get_master_db()
    conn.execute(
        'INSERT OR IGNORE INTO tenant_users (email, tenant_id) VALUES (?,?)',
        (email, tenant_id)
    )
    conn.commit()
    conn.close()


def update_tenant_user_email(old_email: str, new_email: str, tenant_id: int):
    """직원 이메일 변경 시 master.db 갱신"""
    conn = get_master_db()
    conn.execute(
        'UPDATE tenant_users SET email=? WHERE email=? AND tenant_id=?',
        (new_email, old_email, tenant_id)
    )
    conn.commit()
    conn.close()


def remove_tenant_user(email: str, tenant_id: int):
    """직원 퇴직/삭제 시 master.db에서 제거"""
    conn = get_master_db()
    conn.execute(
        'DELETE FROM tenant_users WHERE email=? AND tenant_id=?',
        (email, tenant_id)
    )
    conn.commit()
    conn.close()


# ── Peak Headcount ───────────────────────────────────────────
def update_peak_headcount(tenant_id: int, current_count: int):
    """
    현재 활성 직원 수가 이번 달 최고 기록보다 많으면 갱신.
    직원 추가 후 호출.
    """
    conn = get_master_db()
    sub = conn.execute(
        'SELECT peak_headcount FROM subscriptions WHERE tenant_id=?', (tenant_id,)
    ).fetchone()
    if sub and current_count > (sub['peak_headcount'] or 0):
        conn.execute(
            '''UPDATE subscriptions
               SET peak_headcount=?, updated_at=CURRENT_TIMESTAMP
               WHERE tenant_id=?''',
            (current_count, tenant_id)
        )
        conn.commit()
    conn.close()


def reset_peak_headcount(tenant_id: int, current_count: int):
    """
    월말 결제 완료 후 peak_headcount를 현재 직원 수로 리셋.
    새 달의 시작점 설정.
    """
    conn = get_master_db()
    today = date.today()
    next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    conn.execute(
        '''UPDATE subscriptions
           SET peak_headcount=?,
               current_period_start=?,
               current_period_end=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE tenant_id=?''',
        (current_count,
         today.isoformat(),
         next_month_start.isoformat(),
         tenant_id)
    )
    conn.commit()
    conn.close()


# ── Toss Billing Key ─────────────────────────────────────────
def save_billing_key(tenant_id: int, billing_key: str):
    """토스 billing key 저장 + 구독 상태 active로 변경"""
    conn = get_master_db()
    today = date.today()
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    conn.execute(
        '''UPDATE subscriptions
           SET toss_billing_key=?, status='active',
               current_period_start=?, current_period_end=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE tenant_id=?''',
        (billing_key, today.isoformat(), next_month.isoformat(), tenant_id)
    )
    conn.execute(
        "UPDATE tenants SET status='active' WHERE id=?",
        (tenant_id,)
    )
    conn.commit()
    conn.close()


# ── 결제 로그 ────────────────────────────────────────────────
def log_billing(tenant_id: int, amount: int, headcount: int,
                toss_order_id: str, payment_key: str = None,
                status: str = 'pending') -> int:
    """결제 로그 생성, log_id 반환"""
    conn = get_master_db()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO billing_logs
           (tenant_id, amount, headcount, toss_order_id, payment_key, status)
           VALUES (?,?,?,?,?,?)''',
        (tenant_id, amount, headcount, toss_order_id, payment_key, status)
    )
    log_id = c.lastrowid
    conn.commit()
    conn.close()
    return log_id


def update_billing_log(toss_order_id: str, payment_key: str, status: str,
                       fail_reason: str = None):
    """토스 webhook 수신 후 결제 로그 상태 갱신"""
    conn = get_master_db()
    conn.execute(
        '''UPDATE billing_logs
           SET payment_key=?, status=?, fail_reason=?
           WHERE toss_order_id=?''',
        (payment_key, status, fail_reason, toss_order_id)
    )
    conn.commit()
    conn.close()


# ── 구독 상태 컬럼 마이그레이션 ─────────────────────────────────
def migrate_subscriptions():
    """grace_until 등 신규 컬럼 추가 (멱등)"""
    conn = get_master_db()
    existing = [row[1] for row in conn.execute('PRAGMA table_info(subscriptions)')]
    for col, definition in [
        ('grace_until',          'DATE'),
        ('payment_retry_count',  'INTEGER NOT NULL DEFAULT 0'),
        ('last_payment_attempt', 'TIMESTAMP'),
    ]:
        if col not in existing:
            conn.execute(f'ALTER TABLE subscriptions ADD COLUMN {col} {definition}')
    conn.commit()
    conn.close()


# ── 구독 상태 계산 ────────────────────────────────────────────
GRACE_DAYS = 7
WARNING_DAYS = 7   # trial 종료 N일 전부터 배너 표시


def compute_sub_state(tenant_id: int) -> dict:
    """
    테넌트 구독 상태를 계산해서 반환.

    state 값:
      trial_active    — 체험 중 (경고 없음)
      trial_warning   — 체험 D-7 이하 (주황 배너)
      grace           — 체험 만료 or 결제 실패, 유예 기간 중 (빨간 배너, 조회 전용)
      locked          — 유예 만료 (완전 차단)
      active          — 정상 구독
      payment_failed  — 결제 실패, 유예 기간 중

    can_write: POST/PUT/DELETE 허용 여부
    locked:    앱 접근 완전 차단 여부
    days_left: 체험 또는 유예 잔여일 (배너용)
    """
    today = date.today()
    conn = get_master_db()
    row = conn.execute(
        '''SELECT t.status AS tenant_status, t.trial_ends_at,
                  s.status AS sub_status, s.toss_billing_key,
                  s.grace_until, s.payment_retry_count
           FROM tenants t
           LEFT JOIN subscriptions s ON s.tenant_id = t.id
           WHERE t.id = ?''',
        (tenant_id,)
    ).fetchone()
    conn.close()

    if not row:
        return {'state': 'active', 'can_write': True, 'locked': False, 'days_left': 0}

    sub_status  = row['sub_status'] or 'trialing'
    trial_ends  = row['trial_ends_at']
    grace_until = row['grace_until']
    has_card    = bool(row['toss_billing_key'])

    # ── 1. 정상 유료 구독 ─────────────────────────────────────
    if sub_status == 'active' and has_card:
        return {'state': 'active', 'can_write': True, 'locked': False, 'days_left': 0}

    # ── 2. 트라이얼 중 ────────────────────────────────────────
    if sub_status == 'trialing':
        if not trial_ends:
            return {'state': 'trial_active', 'can_write': True, 'locked': False, 'days_left': 14}
        delta = (date.fromisoformat(trial_ends) - today).days
        if delta > WARNING_DAYS:
            return {'state': 'trial_active', 'can_write': True, 'locked': False, 'days_left': delta}
        if delta >= 0:
            return {'state': 'trial_warning', 'can_write': True, 'locked': False, 'days_left': delta}
        # 트라이얼 만료 → 유예 시작
        if not grace_until:
            _start_grace(tenant_id, today)
            grace_until = (today + timedelta(days=GRACE_DAYS)).isoformat()
        grace_delta = (date.fromisoformat(grace_until) - today).days
        if grace_delta >= 0:
            return {'state': 'grace', 'can_write': False, 'locked': False, 'days_left': grace_delta}
        return {'state': 'locked', 'can_write': False, 'locked': True, 'days_left': 0}

    # ── 3. 결제 실패 ──────────────────────────────────────────
    if sub_status == 'past_due':
        if not grace_until:
            _start_grace(tenant_id, today)
            grace_until = (today + timedelta(days=GRACE_DAYS)).isoformat()
        grace_delta = (date.fromisoformat(grace_until) - today).days
        if grace_delta >= 0:
            return {'state': 'payment_failed', 'can_write': False, 'locked': False, 'days_left': grace_delta}
        return {'state': 'locked', 'can_write': False, 'locked': True, 'days_left': 0}

    # ── 4. 취소/기타 ─────────────────────────────────────────
    if sub_status == 'cancelled':
        return {'state': 'locked', 'can_write': False, 'locked': True, 'days_left': 0}

    return {'state': 'active', 'can_write': True, 'locked': False, 'days_left': 0}


def _start_grace(tenant_id: int, today: date):
    """유예 기간 시작 — grace_until 설정 + subscriptions 상태 갱신"""
    conn = get_master_db()
    grace_until = (today + timedelta(days=GRACE_DAYS)).isoformat()
    conn.execute(
        '''UPDATE subscriptions
           SET grace_until=?, updated_at=CURRENT_TIMESTAMP
           WHERE tenant_id=?''',
        (grace_until, tenant_id)
    )
    conn.commit()
    conn.close()


def start_grace_period(tenant_id: int):
    """외부에서 유예 기간 강제 시작 (결제 실패 webhook 수신 시 호출)"""
    conn = get_master_db()
    today = date.today()
    grace_until = (today + timedelta(days=GRACE_DAYS)).isoformat()
    conn.execute(
        '''UPDATE subscriptions
           SET status='past_due', grace_until=?, updated_at=CURRENT_TIMESTAMP
           WHERE tenant_id=?''',
        (grace_until, tenant_id)
    )
    conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant_id,))
    conn.commit()
    conn.close()


def lock_tenant(tenant_id: int):
    """유예 만료 후 테넌트 완전 잠금"""
    conn = get_master_db()
    conn.execute(
        '''UPDATE subscriptions SET status='cancelled', updated_at=CURRENT_TIMESTAMP
           WHERE tenant_id=?''',
        (tenant_id,)
    )
    conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant_id,))
    conn.commit()
    conn.close()
