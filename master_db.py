"""
master_db.py — SaaS 멀티테넌시 마스터 DB 관리

master.db 역할:
  - tenants: 가입 회사 목록, 상태(active/suspended)
  - tenant_users: 이메일 → tenant_id 매핑 (로그인 라우팅용)

결제·요금제(subscriptions / billing_logs / 요금제 3계층)는 2026-10 무료·오픈소스
전환으로 삭제했다. tenants.trial_ends_at·plan 칼럼은 옛 DB 호환용으로만 남는다.

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

# ── 경로 헬퍼 ────────────────────────────────────────────────
def get_tenant_db_path(tenant_id: int) -> str:
    """테넌트 DB 파일 경로 반환. 테넌트 1은 기존 hr_system.db 사용.

    tenant_id가 None/비정상이면 데모 테넌트(1)로 폴백 — 과거 tenant_None.db가
    생성된 버그의 재발 방지 가드 (P0-3).
    """
    if not tenant_id or not str(tenant_id).isdigit():
        import logging
        logging.getLogger(__name__).warning(f'get_tenant_db_path: 잘못된 tenant_id={tenant_id!r} → 1로 폴백')
        tenant_id = 1
    tenant_id = int(tenant_id)
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
    """superadmins 테이블에 기본 운영자 계정이 없으면 생성 (멱등, 워커 동시 기동에도 안전)."""
    from werkzeug.security import generate_password_hash
    conn = get_master_db()
    conn.execute(
        'INSERT OR IGNORE INTO superadmins (username, password_hash) VALUES (?,?)',
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
    """전체 테넌트 목록 (SaaS 관리 페이지용)"""
    conn = get_master_db()
    rows = conn.execute(
        '''SELECT t.id, t.slug, t.company_name, t.admin_email, t.status AS tenant_status,
                  t.created_at
           FROM tenants t
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
    새 테넌트 생성. slug 충돌 시 숫자 접미사 추가. tenant_id 반환.
    """
    conn = get_master_db()
    base_slug = slugify(company_name)

    # slug 충돌 처리
    slug = base_slug
    suffix = 1
    while conn.execute('SELECT id FROM tenants WHERE slug=?', (slug,)).fetchone():
        slug = f'{base_slug}-{suffix}'
        suffix += 1

    c = conn.cursor()
    # trial_ends_at 은 옛 스키마의 NOT NULL 칼럼이라 가입일을 채워 둔다 (의미 없음)
    c.execute(
        "INSERT INTO tenants (slug, company_name, admin_email, status, trial_ends_at) "
        "VALUES (?,?,?,'active',?)",
        (slug, company_name, admin_email, date.today().isoformat())
    )
    tenant_id = c.lastrowid
    conn.commit()
    conn.close()
    return tenant_id


# ── 테넌트 조회 ──────────────────────────────────────────────
def get_tenant_by_email(email: str):
    """이메일로 테넌트 조회 (로그인 라우팅용)"""
    conn = get_master_db()
    row = conn.execute(
        '''SELECT t.id, t.slug, t.company_name, t.status
           FROM tenant_users tu
           JOIN tenants t ON t.id = tu.tenant_id
           WHERE tu.email = ?''',
        (email,)
    ).fetchone()
    conn.close()
    return row


def get_tenant(tenant_id: int):
    """tenant_id로 테넌트 조회"""
    conn = get_master_db()
    row = conn.execute(
        'SELECT * FROM tenants WHERE id = ?',
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


# ── master.db 칼럼 마이그레이션 ─────────────────────────────────
def _add_column(conn, table, col, definition):
    """칼럼 추가 (멱등 + 동시 실행 안전).

    앱이 뜰 때 워커 여러 개가 동시에 이 함수를 지난다. 'PRAGMA 로 확인 →
    ALTER' 사이에 다른 워커가 먼저 넣으면 duplicate column 으로 죽는다
    (실제로 운영에서 워커 하나가 부팅에 실패했다). 이미 있으면 그냥 넘어간다.
    """
    cols = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
    if col in cols:
        return
    try:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
    except sqlite3.OperationalError as exc:
        if 'duplicate column' not in str(exc).lower():
            raise


def migrate_master_db():
    """master.db 칼럼·표 보장 (멱등)"""
    conn = get_master_db()
    # 결제·요금제 삭제 (2026-10 무료·오픈소스 전환) — 옛 표를 치우고 체험 상태를 정상으로
    conn.execute('DROP TABLE IF EXISTS subscriptions')
    conn.execute('DROP TABLE IF EXISTS billing_logs')
    conn.execute("UPDATE tenants SET status='active' WHERE status='trial'")
    # tenants.api_token (Phase C-11 — 입사 예정자 웹훅 수신 인증)
    _add_column(conn, 'tenants', 'api_token', 'TEXT')
    # tenants.is_training (Grow 교육 모드 — 연습 회사 표시)
    # 연습 전용 기능(비밀번호 없는 입장·처음으로 되돌리기)은 이 값이 1인 테넌트에만 열린다.
    _add_column(conn, 'tenants', 'is_training', 'INTEGER NOT NULL DEFAULT 0')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS training_tickets (
            token      TEXT PRIMARY KEY,
            tenant_id  INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at    TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    # 개인 연습 자리 — 표를 받은 사람의 연습 계정(없으면 예전처럼 연습 관리자)
    _add_column(conn, 'training_tickets', 'user_id', 'INTEGER')
    # 로그인 실패 기록 (M2 — 무차별 대입 방지). 테넌트를 가리지 않으므로 master.db 에 둔다.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS login_failures (
            scope TEXT NOT NULL,     -- 'account' | 'ip'
            key   TEXT NOT NULL,     -- 'user:<email>' / 'sa:<username>' / IP
            ts    REAL NOT NULL
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_login_failures ON login_failures(scope, key, ts)')
    conn.commit()
    conn.close()


# ── 로그인 시도 제한 (M2) ─────────────────────────────────────
# 같은 계정 5회 또는 같은 IP 20회를 15분 안에 틀리면, 가장 오래된 실패가 15분을 넘길 때까지 막는다.
# 막힌 동안은 비밀번호를 확인하지도 않는다(그동안의 시도는 맞혀도 소용없다).
LOGIN_WINDOW_SEC      = 15 * 60
LOGIN_MAX_PER_ACCOUNT = 5
LOGIN_MAX_PER_IP      = 20


def login_retry_after(account_key: str, ip: str, now: float = None) -> int:
    """막혀 있으면 다시 시도할 수 있을 때까지 남은 초, 아니면 0."""
    import time
    now = now or time.time()
    since = now - LOGIN_WINDOW_SEC
    conn = get_master_db()
    wait = 0
    for scope, key, limit in (('account', account_key, LOGIN_MAX_PER_ACCOUNT),
                              ('ip', ip, LOGIN_MAX_PER_IP)):
        if not key:
            continue
        rows = conn.execute(
            'SELECT ts FROM login_failures WHERE scope=? AND key=? AND ts>? ORDER BY ts DESC LIMIT ?',
            (scope, key, since, limit)).fetchall()
        if len(rows) >= limit:
            # 최근 limit 번째 실패가 창 밖으로 나가면 다시 열린다
            wait = max(wait, int(rows[-1]['ts'] + LOGIN_WINDOW_SEC - now) + 1)
    conn.close()
    return wait


def record_login_failure(account_key: str, ip: str, now: float = None):
    import time
    now = now or time.time()
    conn = get_master_db()
    conn.execute('DELETE FROM login_failures WHERE ts<?', (now - LOGIN_WINDOW_SEC,))
    if account_key:
        conn.execute('INSERT INTO login_failures (scope, key, ts) VALUES (?,?,?)', ('account', account_key, now))
    if ip:
        conn.execute('INSERT INTO login_failures (scope, key, ts) VALUES (?,?,?)', ('ip', ip, now))
    conn.commit()
    conn.close()


def clear_login_failures(account_key: str):
    """로그인 성공 시 그 계정의 실패 기록만 지운다 (IP 기록은 남긴다)."""
    conn = get_master_db()
    conn.execute("DELETE FROM login_failures WHERE scope='account' AND key=?", (account_key,))
    conn.commit()
    conn.close()


# ── 연습(교육) 회사 ──────────────────────────────────────────
TRAINING_TICKET_SECONDS = 120   # 입장표 유효 시간


def get_training_tenant():
    """연습 테넌트 1건 (없으면 None)."""
    conn = get_master_db()
    row = conn.execute(
        'SELECT * FROM tenants WHERE is_training=1 ORDER BY id LIMIT 1').fetchone()
    conn.close()
    return row


def mark_training_tenant(tenant_id: int):
    """이 테넌트를 연습 회사로 표시."""
    conn = get_master_db()
    conn.execute("UPDATE tenants SET is_training=1, status='active' WHERE id=?", (tenant_id,))
    taken = conn.execute("SELECT id FROM tenants WHERE slug='training' AND id<>?",
                         (tenant_id,)).fetchone()
    if not taken:
        conn.execute("UPDATE tenants SET slug='training' WHERE id=?", (tenant_id,))
    conn.commit()
    conn.close()


def issue_training_ticket(tenant_id: int, user_id: int = None) -> str:
    """연습 회사 입장표 발급 — 한 번만, 2분만 쓸 수 있다. user_id = 들어갈 연습 계정."""
    import secrets
    from datetime import datetime
    token = secrets.token_urlsafe(24)
    exp = (datetime.utcnow() + timedelta(seconds=TRAINING_TICKET_SECONDS)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_master_db()
    conn.execute("DELETE FROM training_tickets WHERE expires_at < datetime('now','-1 day')")
    conn.execute('INSERT INTO training_tickets (token, tenant_id, expires_at, user_id) VALUES (?,?,?,?)',
                 (token, tenant_id, exp, user_id))
    conn.commit()
    conn.close()
    return token


def use_training_ticket(token: str):
    """입장표를 쓴다 — 한 번 쓰면 다시 못 쓴다. 성공하면 tenant row(+ticket_user_id) 반환."""
    if not token:
        return None
    conn = get_master_db()
    row = conn.execute(
        "SELECT t.*, k.user_id AS ticket_user_id FROM training_tickets k JOIN tenants t ON t.id=k.tenant_id "
        "WHERE k.token=? AND k.used_at IS NULL AND k.expires_at >= datetime('now') "
        'AND t.is_training=1', (token,)).fetchone()
    if row:
        conn.execute("UPDATE training_tickets SET used_at=datetime('now') WHERE token=?", (token,))
        conn.commit()
    conn.close()
    return row


def get_or_create_api_token(tenant_id):
    """테넌트 웹훅 API 토큰 조회 — 없으면 생성 (Phase C-11)."""
    import secrets
    conn = get_master_db()
    row = conn.execute('SELECT api_token FROM tenants WHERE id=?', (tenant_id,)).fetchone()
    if not row:
        conn.close()
        return None
    token = row['api_token']
    if not token:
        token = 'tc_' + secrets.token_hex(24)
        conn.execute('UPDATE tenants SET api_token=? WHERE id=?', (token, tenant_id))
        conn.commit()
    conn.close()
    return token


def regenerate_api_token(tenant_id):
    """웹훅 API 토큰 재발급 (기존 토큰 즉시 무효화)."""
    import secrets
    token = 'tc_' + secrets.token_hex(24)
    conn = get_master_db()
    conn.execute('UPDATE tenants SET api_token=? WHERE id=?', (token, tenant_id))
    conn.commit()
    conn.close()
    return token


def get_tenant_by_api_token(token):
    """API 토큰으로 테넌트 조회 (웹훅 인증용)."""
    if not token:
        return None
    conn = get_master_db()
    row = conn.execute("SELECT * FROM tenants WHERE api_token=? AND status != 'suspended'",
                       (token,)).fetchone()
    conn.close()
    return row
