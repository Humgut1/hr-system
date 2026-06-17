"""
Jira REST API 래퍼
- 환경변수 JIRA_API_TOKEN 없으면 데모 모드
"""
import os
import json
import base64
import urllib.request
from datetime import date, timedelta

BASE_URL    = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
EMAIL       = os.environ.get('JIRA_EMAIL', '')
API_TOKEN   = os.environ.get('JIRA_API_TOKEN', '')
PROJECT_KEY = os.environ.get('JIRA_PROJECT_KEY', 'HR')
IS_DEMO     = not bool(API_TOKEN)

ONBOARDING_TASKS = [
    {'summary': '노트북/장비 준비',           'assignee_role': 'IT',  'days_before': -3, 'priority': 'High'},
    {'summary': '회사 이메일 계정 생성',       'assignee_role': 'IT',  'days_before': -1, 'priority': 'High'},
    {'summary': '사내 시스템 접근 권한 부여',  'assignee_role': 'IT',  'days_before':  0, 'priority': 'High'},
    {'summary': '사무 용품 준비',              'assignee_role': 'GA',  'days_before': -1, 'priority': 'Medium'},
    {'summary': '사수/버디 배정 확인',         'assignee_role': 'MGR', 'days_before':  0, 'priority': 'Medium'},
    {'summary': '보안 서약서 서명',            'assignee_role': 'HR',  'days_before':  1, 'priority': 'Medium'},
    {'summary': '온보딩 교육 일정 등록',       'assignee_role': 'HR',  'days_before':  3, 'priority': 'Low'},
    {'summary': '3개월 수습 목표 설정',        'assignee_role': 'MGR', 'days_before':  7, 'priority': 'Low'},
]

OFFBOARDING_TASKS = [
    {'summary': '노트북/장비 반납 확인',              'days_from': 0,   'priority': 'High'},
    {'summary': '사내 시스템 접근 권한 회수',         'days_from': 0,   'priority': 'High'},
    {'summary': '이메일 계정 비활성화',               'days_from': 1,   'priority': 'High'},
    {'summary': '사원증 반납',                        'days_from': 0,   'priority': 'Medium'},
    {'summary': '퇴직 면담 (Exit Interview) 진행',    'days_from': -3,  'priority': 'Medium'},
    {'summary': '퇴직금 정산 완료',                   'days_from': 14,  'priority': 'Medium'},
]


def _auth_header():
    token = base64.b64encode(f'{EMAIL}:{API_TOKEN}'.encode()).decode()
    return {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}


def _post(path, payload):
    url  = f'{BASE_URL}/rest/api/3/{path}'
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(url, data=data, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _fmt_date(d: date) -> str:
    return d.isoformat()


def create_onboarding_epic(employee: dict) -> dict:
    name      = employee.get('name', '')
    hire_date = employee.get('hire_date') or date.today().isoformat()
    hd        = date.fromisoformat(hire_date)

    if IS_DEMO:
        task_count = len(ONBOARDING_TASKS)
        return {
            'ok': True, 'demo': True,
            'action': 'create_onboarding_epic',
            'epic_key': f'{PROJECT_KEY}-DEMO',
            'summary': f'[온보딩] {name} ({hire_date})',
            'task_count': task_count,
        }
    try:
        epic = _post('issue', {
            'fields': {
                'project':     {'key': PROJECT_KEY},
                'summary':     f'[온보딩] {name} ({hire_date})',
                'issuetype':   {'name': 'Epic'},
                'description': {
                    'type': 'doc', 'version': 1,
                    'content': [{'type': 'paragraph', 'content': [
                        {'type': 'text', 'text': f'{name}님 온보딩 체크리스트 (입사일: {hire_date})'}
                    ]}]
                },
            }
        })
        epic_key = epic.get('key', '')
        subtasks = []
        for task in ONBOARDING_TASKS:
            due = _fmt_date(hd + timedelta(days=task['days_before']))
            r = _post('issue', {
                'fields': {
                    'project':   {'key': PROJECT_KEY},
                    'summary':   task['summary'],
                    'issuetype': {'name': 'Task'},
                    'priority':  {'name': task['priority']},
                    'duedate':   due,
                    'parent':    {'key': epic_key},
                }
            })
            subtasks.append(r.get('key'))
        return {'ok': True, 'epic_key': epic_key, 'subtasks': subtasks}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def create_offboarding_epic(employee: dict) -> dict:
    name     = employee.get('name', '')
    last_day = employee.get('last_work_date') or date.today().isoformat()
    ld       = date.fromisoformat(last_day)

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'create_offboarding_epic',
            'epic_key': f'{PROJECT_KEY}-DEMO',
            'summary': f'[오프보딩] {name} (최종 {last_day})',
            'task_count': len(OFFBOARDING_TASKS),
        }
    try:
        epic = _post('issue', {
            'fields': {
                'project':   {'key': PROJECT_KEY},
                'summary':   f'[오프보딩] {name} (최종 근무일: {last_day})',
                'issuetype': {'name': 'Epic'},
            }
        })
        epic_key = epic.get('key', '')
        for task in OFFBOARDING_TASKS:
            due = _fmt_date(ld + timedelta(days=task['days_from']))
            _post('issue', {
                'fields': {
                    'project':   {'key': PROJECT_KEY},
                    'summary':   task['summary'],
                    'issuetype': {'name': 'Task'},
                    'priority':  {'name': task['priority']},
                    'duedate':   due,
                    'parent':    {'key': epic_key},
                }
            })
        return {'ok': True, 'epic_key': epic_key}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
