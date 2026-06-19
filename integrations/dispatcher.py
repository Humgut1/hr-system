"""
HR 이벤트 → 외부 서비스 디스패처
모든 연동 호출은 여기서 시작됩니다.
"""
import sqlite3
import os
import json
from datetime import datetime

from . import slack, jira, confluence
from .email_sender import send_welcome_email

# 온보딩 체크리스트 기본 항목 (신입이 Day 1에 완료할 항목)
ONBOARDING_TASKS = [
    ('slack_profile',    'Slack 프로필 완성 (사진·직함·상태 설정)',     'setup',   1),
    ('email_signature',  '이메일 서명 설정',                             'setup',   2),
    ('talentcore_login', 'TalentCore 로그인 & 프로필 완성',              'setup',   3),
    ('apps_install',     '필수 앱 설치 (Slack·Zoom·1Password·Notion)', 'setup',   4),
    ('security_2fa',     '보안 설정 완료 (이메일 2FA·화면잠금·VPN)',    'setup',   5),
    ('confluence_read',  'Confluence 온보딩 가이드 필독',                'learning', 6),
    ('contract_sign',    '근로계약서 전자서명 완료',                      'admin',   7),
    ('slack_intro',      'Slack #general에 자기소개 올리기 🎉',          'social',  8),
    ('1on1_schedule',    '매니저와 첫 1:1 미팅 일정 잡기',              'team',    9),
    ('buddy_meet',       '버디(Buddy)와 점심 약속 잡기',                 'social',  10),
    ('jira_check',       'Jira 온보딩 태스크 확인',                      'admin',   11),
    ('profile_photo',    '사원증 사진 촬영 (GA 안내)',                   'admin',   12),
]


def _log(db_path: str, event: str, service: str, result: dict, employee_name: str = ''):
    is_demo = result.get('demo', False)
    is_ok   = result.get('ok', False)
    status  = 'demo' if is_demo else ('success' if is_ok else 'error')
    detail  = result.get('error') or result.get('action') or ''

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO integration_logs (event, service, status, detail, employee_name, raw_response, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event, service, status, detail, employee_name, json.dumps(result, ensure_ascii=False),
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def _db_path() -> str:
    from master_db import get_tenant_db_path
    try:
        from flask import session
        tid = session.get('tenant_id', 1)
    except RuntimeError:
        tid = 1
    return get_tenant_db_path(tid)


def _enabled(service: str) -> bool:
    try:
        db = sqlite3.connect(_db_path())
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT enabled FROM integration_configs WHERE service=?", (service,)
        ).fetchone()
        db.close()
        return bool(row and row['enabled'])
    except Exception:
        return False


def _seed_onboarding_tasks(db_path: str, user_id: int):
    """신규 직원의 온보딩 체크리스트 항목 생성"""
    conn = sqlite3.connect(db_path)
    for key, label, category, order in ONBOARDING_TASKS:
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_progress "
            "(user_id, task_key, task_label, category, sort_order) VALUES (?,?,?,?,?)",
            (user_id, key, label, category, order)
        )
    conn.commit()
    conn.close()


def _get_buddy(db_path: str, user_id: int) -> dict | None:
    """버디 정보 조회"""
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT u.id, u.name, u.email, p.name as pos,
                      d.name as dept
               FROM users u
               LEFT JOIN positions p ON u.position_id = p.id
               LEFT JOIN departments d ON u.department_id = d.id
               WHERE u.id = (SELECT buddy_id FROM users WHERE id=?)""",
            (user_id,)
        ).fetchone()
        db.close()
        return dict(row) if row else None
    except Exception:
        return None


def notify_slack(email: str, text: str, event: str, db_path: str = None, name: str = '') -> dict:
    """Slack DM 발송 — enabled 체크 포함. app.py에서 직접 호출용."""
    path = db_path or _db_path()
    if not _enabled('slack'):
        return {'ok': False, 'demo': True, 'reason': 'slack_disabled'}
    r = slack.send_dm(email, text)
    _log(path, event, 'slack', r, name)
    return r


def notify_slack_multi(emails_names: list, text: str, event: str, db_path: str = None):
    """여러 명에게 동일 메시지 DM — [(email, name), ...]"""
    path = db_path or _db_path()
    if not _enabled('slack'):
        return
    for email, name in emails_names:
        if not email:
            continue
        r = slack.send_dm(email, text)
        _log(path, event, 'slack', r, name)


def on_employee_created(employee: dict, db_path: str = None):
    """신규 직원 등록 시 트리거"""
    path    = db_path or _db_path()
    name    = employee.get('name', '')
    user_id = employee.get('id')

    # 온보딩 체크리스트 생성
    if user_id:
        _seed_onboarding_tasks(path, user_id)

    # 버디 정보 조회
    buddy = _get_buddy(path, user_id) if user_id else None

    # 웰컴 이메일
    r_email = send_welcome_email(employee, buddy)
    _log(path, '웰컴 이메일 발송', 'email', r_email, name)

    # Slack
    if _enabled('slack'):
        r = slack.invite_user(employee.get('email', ''), name)
        _log(path, '직원 신규 등록', 'slack', r, name)

        r2 = slack.add_to_channels(employee.get('email', ''), employee.get('dept', ''))
        _log(path, '채널 자동 추가', 'slack', r2, name)

        r3 = slack.post_welcome(employee)
        _log(path, '입사 소개 메시지', 'slack', r3, name)

        # 버디에게 Slack DM
        if buddy:
            buddy_msg = (
                f"안녕하세요 {buddy['name']}님! 🙌\n"
                f"*{name}*님({employee.get('dept','')} / {employee.get('pos','')})이 "
                f"{employee.get('hire_date','')}에 입사합니다.\n"
                f"{buddy['name']}님이 온보딩 버디로 배정되었습니다.\n\n"
                f"• 입사 당일 오전 9시 로비에서 맞아주세요 🎉\n"
                f"• 첫 3일은 점심을 함께 해주세요\n"
                f"• 궁금한 점은 TalentCore 온보딩 가이드를 참고하세요!"
            )
            r4 = slack.send_dm(buddy['email'], buddy_msg)
            _log(path, '버디 알림 DM', 'slack', r4, name)

    # Jira
    if _enabled('jira'):
        r = jira.create_onboarding_epic(employee)
        _log(path, '직원 신규 등록', 'jira', r, name)
        # epic key를 DB에 저장
        if r.get('ok') and r.get('epic_key') and user_id:
            try:
                conn = sqlite3.connect(path)
                conn.execute("UPDATE users SET jira_epic_key=? WHERE id=?",
                             (r['epic_key'], user_id))
                conn.commit()
                conn.close()
            except Exception:
                pass

    # Confluence
    if _enabled('confluence'):
        r = confluence.add_team_member(employee)
        _log(path, '팀 페이지 멤버 추가', 'confluence', r, name)

        r2 = confluence.create_member_profile(employee)
        _log(path, '프로필 페이지 생성', 'confluence', r2, name)


def on_employee_terminated(employee: dict, db_path: str = None):
    """퇴직 처리 시 트리거"""
    path = db_path or _db_path()
    name = employee.get('name', '')

    if _enabled('slack'):
        r = slack.deactivate_user(employee.get('email', ''))
        _log(path, '퇴직 처리', 'slack', r, name)

    if _enabled('jira'):
        r = jira.create_offboarding_epic(employee)
        _log(path, '퇴직 처리', 'jira', r, name)


def on_employee_transferred(employee: dict, new_dept: str, old_dept: str, db_path: str = None):
    """부서 이동 시 트리거"""
    path = db_path or _db_path()
    name = employee.get('name', '')

    if _enabled('slack'):
        r = slack.add_to_channels(employee.get('email', ''), new_dept)
        _log(path, f'부서이동 ({old_dept}→{new_dept})', 'slack', r, name)


def on_buddy_assigned(employee: dict, buddy: dict, db_path: str = None):
    """버디 배정 시 트리거 — 버디에게 Slack DM"""
    path = db_path or _db_path()
    name = employee.get('name', '')

    if _enabled('slack') and buddy:
        msg = (
            f"안녕하세요 {buddy.get('name','')}님! 🙌\n"
            f"*{name}*님({employee.get('dept','')} / {employee.get('pos','')})의 온보딩 버디로 배정되었습니다.\n\n"
            f"입사일: *{employee.get('hire_date','')}*\n"
            f"• 입사 당일 오전 9시 로비에서 맞아주세요\n"
            f"• 첫 3일 점심 동행 부탁드려요\n"
            f"• 질문은 언제든지 열린 마음으로 🙂"
        )
        r = slack.send_dm(buddy.get('email', ''), msg)
        _log(path, '버디 배정 알림', 'slack', r, buddy.get('name', ''))

    # 신입에게도 버디 소개 DM
    if _enabled('slack') and buddy:
        new_msg = (
            f"안녕하세요 {name}님! 🎉\n"
            f"온보딩 버디로 *{buddy.get('name','')}*님({buddy.get('dept','')} / {buddy.get('pos','')})이 배정되었습니다.\n"
            f"궁금한 것은 뭐든지 Slack DM으로 물어보세요!"
        )
        r2 = slack.send_dm(employee.get('email', ''), new_msg)
        _log(path, '버디 소개 DM (신입)', 'slack', r2, name)
