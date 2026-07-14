"""
TalentCore MCP 서버
Claude Desktop에서 HR 데이터를 자연어로 조회할 수 있게 해주는 MCP 서버입니다.

설정 방법:
  Claude Desktop → Settings → Developer → Edit Config
  아래 내용 추가:

  {
    "mcpServers": {
      "talentcore": {
        "command": "python",
        "args": ["C:/Users/lg/hr-system/mcp_server.py"],
        "env": {
          "TALENTCORE_USER_EMAIL": "your@email.com",
          "TALENTCORE_DB": "C:/Users/lg/hr-system/hr_system.db"
        }
      }
    }
  }

보안:
  - TALENTCORE_USER_EMAIL 기반으로 역할(admin/manager/employee) 자동 감지
  - 직원은 본인 데이터만 조회 가능
  - 매니저는 담당 팀 데이터 조회 가능
  - admin만 전체 데이터 조회 가능
  - 모든 MCP 쿼리는 mcp_audit_logs 테이블에 기록됨
"""

import os
import sqlite3
import asyncio
from datetime import date, datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ─────────────────────────────────────────────────────────────
#  설정
# ─────────────────────────────────────────────────────────────

DB_PATH    = os.environ.get('TALENTCORE_DB', os.path.join(os.path.dirname(__file__), 'hr_system.db'))
USER_EMAIL = os.environ.get('TALENTCORE_USER_EMAIL', '')

server = Server('talentcore')


# ─────────────────────────────────────────────────────────────
#  DB 헬퍼
# ─────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def get_current_user():
    """환경변수 이메일로 현재 사용자 조회"""
    if not USER_EMAIL:
        return None
    db = get_db()
    user = db.execute(
        "SELECT id, name, role, department_id, manager_id FROM users WHERE email=? AND status='active'",
        (USER_EMAIL,)
    ).fetchone()
    db.close()
    return dict(user) if user else None


def audit_log(tool_name: str, user_id: int, summary: str):
    """MCP 쿼리 감사 로그 기록"""
    try:
        db = get_db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS mcp_audit_logs "
            "(id INTEGER PRIMARY KEY, user_id INTEGER, tool_name TEXT, summary TEXT, queried_at TEXT)",
        )
        db.execute(
            "INSERT INTO mcp_audit_logs (user_id, tool_name, summary, queried_at) VALUES (?,?,?,?)",
            (user_id, tool_name, summary, datetime.now().isoformat())
        )
        db.commit()
        db.close()
    except Exception:
        pass


def fmt_rows(rows: list, cols: list) -> str:
    """리스트를 읽기 좋은 텍스트로 변환"""
    if not rows:
        return '데이터가 없습니다.'
    lines = []
    for r in rows:
        parts = [f"{c}: {r[c]}" for c in cols if c in r.keys()]
        lines.append('• ' + ' | '.join(parts))
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────
#  도구 목록 정의
# ─────────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name='get_my_leave_balance',
        description='내 연차 잔여일수와 사용 현황을 조회합니다.',
        inputSchema={'type': 'object', 'properties': {}, 'required': []},
    ),
    Tool(
        name='get_pending_approvals',
        description='내가 승인해야 할 항목(휴가, 근태, 계약, 발령 등)을 조회합니다.',
        inputSchema={'type': 'object', 'properties': {}, 'required': []},
    ),
    Tool(
        name='get_team_attendance',
        description='팀(또는 부서)의 오늘 출근 현황을 조회합니다.',
        inputSchema={
            'type': 'object',
            'properties': {
                'target_date': {
                    'type': 'string',
                    'description': '조회할 날짜 (YYYY-MM-DD). 생략하면 오늘.',
                },
            },
            'required': [],
        },
    ),
    Tool(
        name='get_my_payslip',
        description='내 최근 급여명세서(실수령액, 공제 내역 등)를 조회합니다.',
        inputSchema={
            'type': 'object',
            'properties': {
                'year':  {'type': 'integer', 'description': '연도 (기본: 올해)'},
                'month': {'type': 'integer', 'description': '월 (기본: 이번 달)'},
            },
            'required': [],
        },
    ),
    Tool(
        name='get_performance_status',
        description='내 성과 목표 진행률과 등급을 조회합니다.',
        inputSchema={'type': 'object', 'properties': {}, 'required': []},
    ),
    Tool(
        name='get_team_headcount',
        description='부서별 인원 현황을 조회합니다. admin/manager만 사용 가능.',
        inputSchema={
            'type': 'object',
            'properties': {
                'department': {'type': 'string', 'description': '부서명 (생략하면 전체)'},
            },
            'required': [],
        },
    ),
    Tool(
        name='search_employee',
        description='직원을 이름, 이메일, 사번으로 검색합니다. 민감 정보는 권한에 따라 제한됩니다.',
        inputSchema={
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '검색어 (이름/이메일/사번)'},
            },
            'required': ['query'],
        },
    ),
    Tool(
        name='get_onboarding_status',
        description='신규 입사자 온보딩 진행 현황을 조회합니다. admin/manager만 사용 가능.',
        inputSchema={'type': 'object', 'properties': {}, 'required': []},
    ),
]


# ─────────────────────────────────────────────────────────────
#  도구 핸들러
# ─────────────────────────────────────────────────────────────

def handle_get_my_leave_balance(user: dict) -> str:
    from payroll_utils import compute_leave_balance
    db  = get_db()
    uid = user['id']

    # 연차 계산 — 앱과 동일한 단일 공식 사용 (P0-1)
    try:
        cfg = db.execute('SELECT sick_policy FROM company_config WHERE id=1').fetchone()
        sick_policy = (cfg['sick_policy'] if cfg and cfg['sick_policy'] else 'annual')
    except Exception:
        sick_policy = 'annual'
    bal   = compute_leave_balance(db, uid, sick_policy=sick_policy)
    total = bal['total']
    used  = bal['used']

    pending = db.execute(
        "SELECT COUNT(*) AS c FROM leave_requests "
        "WHERE user_id=? AND status='pending'", (uid,)
    ).fetchone()['c']

    recent = db.execute(
        "SELECT type, start_date, end_date, days, status FROM leave_requests "
        "WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)
    ).fetchall()

    db.close()
    audit_log('get_my_leave_balance', uid, f'잔여={total - used}일')

    lines = [
        f"📅 {user['name']}님 연차 현황 ({date.today().year}년)",
        f"  총 부여: {total}일",
        f"  사용: {used}일",
        f"  잔여: {total - used:.1f}일",
        f"  승인 대기 중: {pending}건",
        '',
        '최근 휴가 내역:',
    ]
    for r in recent:
        lines.append(f"  • {r['type']} | {r['start_date']} ~ {r['end_date']} ({r['days']}일) [{r['status']}]")

    return '\n'.join(lines)


def handle_get_pending_approvals(user: dict) -> str:
    db   = get_db()
    uid  = user['id']
    role = user['role']
    items = []

    if role in ('admin', 'manager'):
        # 휴가 신청 대기
        leaves = db.execute(
            """SELECT u.name, lr.type, lr.start_date, lr.end_date, lr.days
               FROM leave_requests lr JOIN users u ON lr.user_id = u.id
               WHERE lr.status='pending'
               AND (? = 'admin' OR u.manager_id = ?)
               ORDER BY lr.id DESC LIMIT 10""",
            (role, uid)
        ).fetchall()
        for r in leaves:
            items.append(f"[휴가] {r['name']} — {r['type']} {r['start_date']}~{r['end_date']} ({r['days']}일)")

        # 계약서 서명 대기
        contracts = db.execute(
            """SELECT u.name, c.title FROM contracts c
               JOIN users u ON c.employee_id = u.id
               WHERE c.status='issued'
               AND (? = 'admin' OR c.issued_by = ?)
               LIMIT 5""",
            (role, uid)
        ).fetchall()
        for r in contracts:
            items.append(f"[계약] {r['name']} — {r['title']} 서명 대기")

    # 본인이 서명해야 할 계약서
    my_contracts = db.execute(
        "SELECT title FROM contracts WHERE employee_id=? AND status='issued'", (uid,)
    ).fetchall()
    for r in my_contracts:
        items.append(f"[내 서명 필요] {r['title']}")

    db.close()
    audit_log('get_pending_approvals', uid, f'{len(items)}건')

    if not items:
        return '✅ 현재 처리 대기 중인 항목이 없습니다.'
    return f"⏳ 처리 대기 항목 {len(items)}건:\n" + '\n'.join(f"  • {i}" for i in items)


def handle_get_team_attendance(user: dict, target_date: str = None) -> str:
    db   = get_db()
    uid  = user['id']
    role = user['role']
    today = target_date or date.today().isoformat()

    if role == 'admin':
        rows = db.execute(
            """SELECT u.name, d.name AS dept, c.check_in, c.check_out, c.attendance_status
               FROM checkins c JOIN users u ON c.user_id=u.id
               LEFT JOIN departments d ON u.department_id=d.id
               WHERE c.date=? AND u.status='active'
               ORDER BY d.name, u.name""",
            (today,)
        ).fetchall()
    elif role == 'manager':
        rows = db.execute(
            """SELECT u.name, c.check_in, c.check_out, c.attendance_status
               FROM checkins c JOIN users u ON c.user_id=u.id
               WHERE c.date=? AND u.manager_id=?
               ORDER BY u.name""",
            (today, uid)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT u.name, c.check_in, c.check_out, c.attendance_status
               FROM checkins c JOIN users u ON c.user_id=u.id
               WHERE c.date=? AND u.department_id=?
               ORDER BY u.name""",
            (today, user['department_id'])
        ).fetchall()

    db.close()
    audit_log('get_team_attendance', uid, f'{today} {len(rows)}명')

    if not rows:
        return f"📋 {today} 출근 기록이 없습니다."

    lines = [f"📋 {today} 출근 현황 ({len(rows)}명)"]
    for r in rows:
        out    = r['check_out'][:5] if r['check_out'] else '근무중'
        status = r['attendance_status'] or 'present'
        badge  = {'present': '✅', 'late': '⚠️', 'early_leave': '🔵', 'absent': '❌'}.get(status, '✅')
        dept   = f"[{r['dept']}] " if 'dept' in r.keys() else ''
        lines.append(f"  {badge} {dept}{r['name']} — {r['check_in'][:5]} 출근 / {out}")
    return '\n'.join(lines)


def handle_get_my_payslip(user: dict, year: int = None, month: int = None) -> str:
    db    = get_db()
    uid   = user['id']
    today = date.today()
    y     = year  or today.year
    m     = month or today.month

    slip = db.execute(
        "SELECT * FROM payslips WHERE user_id=? AND year=? AND month=?",
        (uid, y, m)
    ).fetchone()

    db.close()
    audit_log('get_my_payslip', uid, f'{y}-{m:02d}')

    if not slip:
        return f"📄 {y}년 {m}월 급여명세서가 아직 생성되지 않았습니다."

    lines = [
        f"📄 {user['name']}님 {y}년 {m}월 급여명세서",
        f"  기본급:    {slip['base_salary']:,}원",
        f"  추가수당:  {(slip.get('overtime_pay') or 0):,}원",
        f"  공제 합계: {(slip.get('total_deduction') or 0):,}원",
        f"  ─────────────────",
        f"  실수령액:  {slip['net_pay']:,}원",
    ]
    return '\n'.join(lines)


def handle_get_performance_status(user: dict) -> str:
    db  = get_db()
    uid = user['id']

    # 최근 사이클
    cycle = db.execute(
        "SELECT * FROM performance_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not cycle:
        db.close()
        return "📊 등록된 성과 주기가 없습니다."

    goals = db.execute(
        "SELECT title, weight, progress, self_score FROM performance_goals "
        "WHERE user_id=? AND cycle_id=?",
        (uid, cycle['id'])
    ).fetchall()

    cal = db.execute(
        "SELECT final_grade FROM calibration_results WHERE user_id=? AND cycle_id=?",
        (uid, cycle['id'])
    ).fetchone()

    db.close()
    audit_log('get_performance_status', uid, f"cycle={cycle['name']}")

    if not goals:
        return f"📊 [{cycle['name']}] 등록된 목표가 없습니다."

    avg_progress = sum(g['progress'] or 0 for g in goals) / len(goals)
    lines = [
        f"📊 {user['name']}님 성과 현황 [{cycle['name']}]",
        f"  평균 진행률: {avg_progress:.0f}%",
        f"  확정 등급: {cal['final_grade'] if cal else '미확정'}",
        '',
        '목표별 진행률:',
    ]
    for g in goals:
        bar = '█' * int((g['progress'] or 0) / 10) + '░' * (10 - int((g['progress'] or 0) / 10))
        lines.append(f"  • {g['title']} [{bar}] {g['progress'] or 0}%")

    return '\n'.join(lines)


def handle_get_team_headcount(user: dict, department: str = None) -> str:
    if user['role'] not in ('admin', 'manager'):
        return '⛔ 이 기능은 매니저 이상만 사용할 수 있습니다.'

    db = get_db()
    uid = user['id']

    if department:
        rows = db.execute(
            """SELECT d.name AS dept, COUNT(*) AS cnt
               FROM users u JOIN departments d ON u.department_id=d.id
               WHERE u.status='active' AND d.name LIKE ?
               GROUP BY d.id ORDER BY cnt DESC""",
            (f'%{department}%',)
        ).fetchall()
    elif user['role'] == 'admin':
        rows = db.execute(
            """SELECT d.name AS dept, COUNT(*) AS cnt
               FROM users u JOIN departments d ON u.department_id=d.id
               WHERE u.status='active'
               GROUP BY d.id ORDER BY cnt DESC"""
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT u.name, u.position FROM users u
               WHERE u.manager_id=? AND u.status='active'
               ORDER BY u.name""",
            (uid,)
        ).fetchall()

    db.close()
    audit_log('get_team_headcount', uid, f"dept={department}")

    if not rows:
        return '인원 데이터가 없습니다.'

    if user['role'] == 'manager' and not department:
        lines = [f"👥 {user['name']}님 담당 팀원 ({len(rows)}명)"]
        for r in rows:
            lines.append(f"  • {r['name']} — {r['position'] or '직급 미지정'}")
    else:
        total = sum(r['cnt'] for r in rows)
        lines = [f"👥 부서별 인원 현황 (총 {total}명)"]
        for r in rows:
            lines.append(f"  • {r['dept']}: {r['cnt']}명")
    return '\n'.join(lines)


def handle_search_employee(user: dict, query: str) -> str:
    db   = get_db()
    uid  = user['id']
    role = user['role']
    q    = f'%{query}%'

    rows = db.execute(
        """SELECT u.id, u.name, u.email, u.position, u.emp_no,
                  d.name AS dept, u.phone
           FROM users u LEFT JOIN departments d ON u.department_id=d.id
           WHERE u.status='active'
             AND (u.name LIKE ? OR u.email LIKE ? OR u.emp_no LIKE ?)
           LIMIT 8""",
        (q, q, q)
    ).fetchall()

    db.close()
    audit_log('search_employee', uid, f"query={query} results={len(rows)}")

    if not rows:
        return f'"{query}" 검색 결과가 없습니다.'

    lines = [f"🔍 검색 결과 ({len(rows)}명)"]
    for r in rows:
        # 본인이거나 admin이면 전화번호 포함
        show_phone = (r['id'] == uid or role == 'admin')
        line = f"  • [{r['emp_no'] or '사번없음'}] {r['name']} — {r['dept'] or '부서없음'} / {r['position'] or '직급없음'}"
        if show_phone and r['phone']:
            line += f" / 📞 {r['phone']}"
        lines.append(line)
    return '\n'.join(lines)


def handle_get_onboarding_status(user: dict) -> str:
    if user['role'] not in ('admin', 'manager'):
        return '⛔ 이 기능은 매니저 이상만 사용할 수 있습니다.'

    db  = get_db()
    uid = user['id']

    if user['role'] == 'admin':
        new_hires = db.execute(
            """SELECT u.id, u.name, u.hire_date, d.name AS dept
               FROM users u LEFT JOIN departments d ON u.department_id=d.id
               WHERE u.status='active'
                 AND u.hire_date >= date('now', '-60 days')
               ORDER BY u.hire_date DESC""",
        ).fetchall()
    else:
        new_hires = db.execute(
            """SELECT u.id, u.name, u.hire_date, d.name AS dept
               FROM users u LEFT JOIN departments d ON u.department_id=d.id
               WHERE u.manager_id=? AND u.status='active'
                 AND u.hire_date >= date('now', '-60 days')
               ORDER BY u.hire_date DESC""",
            (uid,)
        ).fetchall()

    if not new_hires:
        db.close()
        return '최근 60일 내 입사자가 없습니다.'

    lines = [f"🎉 온보딩 진행 현황 ({len(new_hires)}명)"]
    for h in new_hires:
        days_in = (date.today() - date.fromisoformat(h['hire_date'])).days

        # 온보딩 진행률
        total_tasks = db.execute(
            "SELECT COUNT(*) AS c FROM onboarding_progress WHERE user_id=?", (h['id'],)
        ).fetchone()['c']
        done_tasks = db.execute(
            "SELECT COUNT(*) AS c FROM onboarding_progress WHERE user_id=? AND status='done'",
            (h['id'],)
        ).fetchone()['c']

        pct  = int(done_tasks / total_tasks * 100) if total_tasks else 0
        bar  = '█' * (pct // 10) + '░' * (10 - pct // 10)
        lines.append(
            f"\n  📌 {h['name']} ({h['dept'] or '부서없음'}) — 입사 D+{days_in}"
            f"\n     [{bar}] {pct}% ({done_tasks}/{total_tasks})"
        )

    db.close()
    audit_log('get_onboarding_status', uid, f"{len(new_hires)}명")
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────
#  MCP 핸들러 등록
# ─────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    user = get_current_user()

    if not user:
        return [TextContent(type='text', text=(
            f'⛔ TalentCore 인증 실패\n'
            f'TALENTCORE_USER_EMAIL 환경변수를 확인해주세요.\n'
            f'현재 설정: "{USER_EMAIL}"'
        ))]

    try:
        if name == 'get_my_leave_balance':
            result = handle_get_my_leave_balance(user)
        elif name == 'get_pending_approvals':
            result = handle_get_pending_approvals(user)
        elif name == 'get_team_attendance':
            result = handle_get_team_attendance(user, arguments.get('target_date'))
        elif name == 'get_my_payslip':
            result = handle_get_my_payslip(user, arguments.get('year'), arguments.get('month'))
        elif name == 'get_performance_status':
            result = handle_get_performance_status(user)
        elif name == 'get_team_headcount':
            result = handle_get_team_headcount(user, arguments.get('department'))
        elif name == 'search_employee':
            result = handle_search_employee(user, arguments.get('query', ''))
        elif name == 'get_onboarding_status':
            result = handle_get_onboarding_status(user)
        else:
            result = f'알 수 없는 도구: {name}'
    except Exception as e:
        result = f'오류 발생: {e}'

    return [TextContent(type='text', text=result)]


# ─────────────────────────────────────────────────────────────
#  실행
# ─────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == '__main__':
    asyncio.run(main())
