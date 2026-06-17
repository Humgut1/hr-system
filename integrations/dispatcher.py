"""
HR 이벤트 → 외부 서비스 디스패처
모든 연동 호출은 여기서 시작됩니다.
"""
import sqlite3
import os
import json
from datetime import datetime

from . import slack, jira, confluence


def _log(db_path: str, event: str, service: str, result: dict, employee_name: str = ''):
    """연동 실행 결과를 integration_logs 테이블에 기록"""
    is_demo   = result.get('demo', False)
    is_ok     = result.get('ok', False)
    status    = 'demo' if is_demo else ('success' if is_ok else 'error')
    detail    = result.get('error') or result.get('action') or ''

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO integration_logs (event, service, status, detail, employee_name, raw_response, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event, service, status, detail, employee_name, json.dumps(result), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def _db_path() -> str:
    from master_db import get_tenant_db_path
    # 현재 요청 컨텍스트에서 tenant_id 가져오기
    try:
        from flask import session
        tid = session.get('tenant_id', 1)
    except RuntimeError:
        tid = 1
    return get_tenant_db_path(tid)


def _enabled(service: str) -> bool:
    """해당 서비스 연동이 활성화 상태인지 확인"""
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


def on_employee_created(employee: dict, db_path: str = None):
    """신규 직원 등록 시 트리거"""
    path = db_path or _db_path()
    name = employee.get('name', '')

    if _enabled('slack'):
        r = slack.invite_user(employee.get('email', ''), name)
        _log(path, '직원 신규 등록', 'slack', r, name)

        r2 = slack.add_to_channels(employee.get('email', ''), employee.get('dept', ''))
        _log(path, '채널 자동 추가', 'slack', r2, name)

        r3 = slack.post_welcome(employee)
        _log(path, '입사 소개 메시지', 'slack', r3, name)

    if _enabled('jira'):
        r = jira.create_onboarding_epic(employee)
        _log(path, '직원 신규 등록', 'jira', r, name)

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
        # 기존 채널 제거 후 새 채널 추가 (간소화: 새 채널만 추가)
        r = slack.add_to_channels(employee.get('email', ''), new_dept)
        _log(path, f'부서이동 ({old_dept}→{new_dept})', 'slack', r, name)
