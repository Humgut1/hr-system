"""
Slack API 래퍼
- 환경변수 SLACK_BOT_TOKEN 없으면 데모 모드로 동작 (실제 호출 없음)
"""
import os
import json
import urllib.request
import urllib.error

BOT_TOKEN   = os.environ.get('SLACK_BOT_TOKEN', '')
ADMIN_TOKEN = os.environ.get('SLACK_ADMIN_TOKEN', '')
IS_DEMO     = not bool(BOT_TOKEN)

DEPT_CHANNEL_MAP = {
    '엔지니어링': 'team-engineering',
    '개발':       'team-engineering',
    '디자인':     'team-design',
    'HR':         'team-hr',
    '인사':       'team-hr',
    '마케팅':     'team-marketing',
    '영업':       'team-sales',
    '재무':       'team-finance',
    '운영':       'team-ops',
}

def _api(token, method, payload):
    url  = f'https://slack.com/api/{method}'
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(url, data=data, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def invite_user(email: str, name: str) -> dict:
    """워크스페이스 초대 (Admin Token 필요)"""
    if IS_DEMO or not ADMIN_TOKEN:
        return {'ok': True, 'demo': True, 'action': 'invite', 'email': email}
    try:
        result = _api(ADMIN_TOKEN, 'admin.users.invite', {
            'email': email,
            'team_id': os.environ.get('SLACK_WORKSPACE_ID', ''),
            'resend': True,
        })
        return result
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def add_to_channels(email: str, dept: str) -> dict:
    """부서 채널 + 공통 채널에 추가"""
    channels = ['general', 'announcements']
    dept_ch = DEPT_CHANNEL_MAP.get(dept or '', '')
    if dept_ch:
        channels.append(dept_ch)

    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'add_channels', 'channels': channels}

    results = []
    for ch in channels:
        try:
            # 채널 ID 조회
            ch_list = _api(BOT_TOKEN, 'conversations.list', {'types': 'public_channel', 'limit': 200})
            ch_id = next((c['id'] for c in ch_list.get('channels', []) if c['name'] == ch), None)
            if not ch_id:
                results.append({'channel': ch, 'ok': False, 'error': 'not found'})
                continue
            # 사용자 ID 조회
            users = _api(BOT_TOKEN, 'users.lookupByEmail', {'email': email})
            if not users.get('ok'):
                results.append({'channel': ch, 'ok': False, 'error': 'user not found'})
                continue
            uid = users['user']['id']
            r = _api(BOT_TOKEN, 'conversations.invite', {'channel': ch_id, 'users': uid})
            results.append({'channel': ch, 'ok': r.get('ok'), 'error': r.get('error')})
        except Exception as e:
            results.append({'channel': ch, 'ok': False, 'error': str(e)})
    return {'ok': True, 'results': results}


def post_welcome(employee: dict) -> dict:
    """#general 채널에 입사 소개 메시지"""
    name  = employee.get('name', '')
    dept  = employee.get('dept', '')
    pos   = employee.get('pos', '')
    email = employee.get('email', '')

    text = (
        f"안녕하세요! 👋 오늘부터 함께하게 된 *{name}*님을 소개합니다.\n\n"
        f"• 부서: {dept or '—'}\n"
        f"• 직급: {pos or '—'}\n"
        f"• 이메일: {email}\n\n"
        f"함께 잘 부탁드립니다! 🎉"
    )

    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'post_message', 'text': text}

    try:
        return _api(BOT_TOKEN, 'chat.postMessage', {'channel': '#general', 'text': text})
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def send_dm(email: str, text: str) -> dict:
    """특정 사용자에게 DM 발송"""
    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'send_dm', 'email': email, 'text': text[:80]}
    try:
        users = _api(BOT_TOKEN, 'users.lookupByEmail', {'email': email})
        if not users.get('ok'):
            return {'ok': False, 'error': 'user not found'}
        uid = users['user']['id']
        open_dm = _api(BOT_TOKEN, 'conversations.open', {'users': uid})
        ch_id = open_dm['channel']['id']
        return _api(BOT_TOKEN, 'chat.postMessage', {'channel': ch_id, 'text': text})
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def deactivate_user(email: str) -> dict:
    """퇴직 시 Slack 계정 비활성화 (Admin Token + Pro 플랜 필요)"""
    if IS_DEMO or not ADMIN_TOKEN:
        return {'ok': True, 'demo': True, 'action': 'deactivate', 'email': email}
    try:
        users = _api(ADMIN_TOKEN, 'users.lookupByEmail', {'email': email})
        if not users.get('ok'):
            return {'ok': False, 'error': 'user not found'}
        uid = users['user']['id']
        return _api(ADMIN_TOKEN, 'admin.users.remove', {
            'team_id': os.environ.get('SLACK_WORKSPACE_ID', ''),
            'user_id': uid,
        })
    except Exception as e:
        return {'ok': False, 'error': str(e)}
