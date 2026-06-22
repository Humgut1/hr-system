"""
Slack API 래퍼
- 환경변수 SLACK_BOT_TOKEN 없으면 데모 모드로 동작 (실제 호출 없음)
"""
import os
import json
import urllib.request
import urllib.parse

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
        'Content-Type':  'application/json; charset=utf-8',
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _lookup_user_by_email(email: str) -> dict:
    """이메일로 Slack 유저 조회 (GET + query param)"""
    url = 'https://slack.com/api/users.lookupByEmail?' + urllib.parse.urlencode({'email': email})
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {BOT_TOKEN}'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get_uid(email: str) -> str | None:
    """이메일 → Slack UID 변환. 실패 시 None."""
    try:
        r = _lookup_user_by_email(email)
        return r['user']['id'] if r.get('ok') else None
    except Exception:
        return None


def _open_dm(uid: str) -> str | None:
    """UID → DM 채널 ID"""
    try:
        r = _api(BOT_TOKEN, 'conversations.open', {'users': uid})
        return r['channel']['id']
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  기본 메시지 발송
# ─────────────────────────────────────────────────────────────

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

    uid = _get_uid(email)
    if not uid:
        return {'ok': False, 'error': 'user not found'}

    results = []
    # 채널 목록 한 번만 조회
    try:
        ch_list = _api(BOT_TOKEN, 'conversations.list', {'types': 'public_channel', 'limit': 1000})
        ch_map  = {c['name']: c['id'] for c in ch_list.get('channels', [])}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    for ch in channels:
        ch_id = ch_map.get(ch)
        if not ch_id:
            results.append({'channel': ch, 'ok': False, 'error': 'not found'})
            continue
        try:
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
        f"안녕하세요! 오늘부터 함께하게 된 *{name}*님을 소개합니다.\n\n"
        f"• 부서: {dept or '—'}\n"
        f"• 직급: {pos or '—'}\n"
        f"• 이메일: {email}\n\n"
        f"함께 잘 부탁드립니다!"
    )

    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'post_message', 'text': text}

    try:
        return _api(BOT_TOKEN, 'chat.postMessage', {'channel': '#general', 'text': text})
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def send_dm(email: str, text: str) -> dict:
    """특정 사용자에게 단순 텍스트 DM 발송"""
    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'send_dm', 'email': email, 'text': text[:80]}
    try:
        uid   = _get_uid(email)
        if not uid:
            return {'ok': False, 'error': f'user not found: {email}'}
        ch_id = _open_dm(uid)
        if not ch_id:
            return {'ok': False, 'error': 'cannot open DM'}
        return _api(BOT_TOKEN, 'chat.postMessage', {'channel': ch_id, 'text': text})
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def send_dm_blocks(email: str, text: str, blocks: list) -> dict:
    """Block Kit 버튼이 포함된 DM 발송"""
    if IS_DEMO:
        return {'ok': True, 'demo': True, 'action': 'send_dm_blocks', 'email': email}
    try:
        uid   = _get_uid(email)
        if not uid:
            return {'ok': False, 'error': f'user not found: {email}'}
        ch_id = _open_dm(uid)
        if not ch_id:
            return {'ok': False, 'error': 'cannot open DM'}
        return _api(BOT_TOKEN, 'chat.postMessage', {
            'channel': ch_id,
            'text':    text,
            'blocks':  blocks,
        })
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def respond_to_interaction(response_url: str, text: str) -> dict:
    """버튼 클릭 후 원본 메시지 업데이트 (response_url 사용)"""
    try:
        data = json.dumps({
            'replace_original': True,
            'text': text,
        }).encode('utf-8')
        req = urllib.request.Request(response_url, data=data, headers={
            'Content-Type': 'application/json; charset=utf-8',
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def deactivate_user(email: str) -> dict:
    """퇴직 시 Slack 계정 비활성화 (Admin Token + Pro 플랜 필요)"""
    if IS_DEMO or not ADMIN_TOKEN:
        return {'ok': True, 'demo': True, 'action': 'deactivate', 'email': email}
    try:
        uid = _get_uid(email)
        if not uid:
            return {'ok': False, 'error': 'user not found'}
        return _api(ADMIN_TOKEN, 'admin.users.remove', {
            'team_id': os.environ.get('SLACK_WORKSPACE_ID', ''),
            'user_id': uid,
        })
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ─────────────────────────────────────────────────────────────
#  Block Kit 헬퍼
# ─────────────────────────────────────────────────────────────

def leave_approval_blocks(req_id: int, requester: str, leave_type: str,
                           start: str, end: str, days: int) -> list:
    """휴가 승인 요청 Block Kit (승인/반려 버튼 포함)"""
    return [
        {
            'type': 'section',
            'text': {
                'type': 'mrkdwn',
                'text': (
                    f"*[TalentCore] 휴가 승인 요청*\n\n"
                    f"• 신청자: *{requester}*\n"
                    f"• 종류: {leave_type}\n"
                    f"• 기간: {start} ~ {end} ({days}일)"
                ),
            },
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type':      'button',
                    'text':      {'type': 'plain_text', 'text': '승인'},
                    'style':     'primary',
                    'action_id': 'leave_approve',
                    'value':     str(req_id),
                },
                {
                    'type':      'button',
                    'text':      {'type': 'plain_text', 'text': '반려'},
                    'style':     'danger',
                    'action_id': 'leave_reject',
                    'value':     str(req_id),
                },
            ],
        },
    ]
