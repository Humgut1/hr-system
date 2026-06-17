"""
Confluence REST API 래퍼
- 환경변수 JIRA_API_TOKEN 없으면 데모 모드 (Confluence는 Jira와 같은 토큰 사용)
"""
import os
import json
import base64
import urllib.request

BASE_URL    = os.environ.get('CONFLUENCE_BASE_URL', '').rstrip('/')
EMAIL       = os.environ.get('JIRA_EMAIL', '')
API_TOKEN   = os.environ.get('JIRA_API_TOKEN', '')
SPACE_KEY   = os.environ.get('CONFLUENCE_SPACE_KEY', 'TEAM')
IS_DEMO     = not bool(API_TOKEN)


def _auth_header():
    token = base64.b64encode(f'{EMAIL}:{API_TOKEN}'.encode()).decode()
    return {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}


def _post(path, payload):
    url  = f'{BASE_URL}/rest/api/{path}'
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(url, data=data, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get(path):
    url = f'{BASE_URL}/rest/api/{path}'
    req = urllib.request.Request(url, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def add_team_member(employee: dict) -> dict:
    """팀 페이지에 신규 멤버 행 추가"""
    name  = employee.get('name', '')
    dept  = employee.get('dept', '')
    pos   = employee.get('pos', '')
    email = employee.get('email', '')

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'add_team_member',
            'page': f'{dept} 팀 페이지',
            'member': name,
        }
    try:
        # 부서명으로 페이지 검색
        search = _get(f'content?spaceKey={SPACE_KEY}&title={dept}&type=page&expand=body.storage,version')
        results = search.get('results', [])
        if not results:
            return {'ok': False, 'error': f'{dept} 페이지를 찾을 수 없습니다.'}

        page = results[0]
        page_id = page['id']
        version = page['version']['number']
        body    = page['body']['storage']['value']

        new_row = (
            f'<tr><td>{name}</td><td>{pos}</td><td>{email}</td></tr>'
        )
        # 테이블 마지막 행에 추가
        if '</tbody>' in body:
            body = body.replace('</tbody>', new_row + '</tbody>')
        else:
            body += f'<table><tbody>{new_row}</tbody></table>'

        _post(f'content/{page_id}', {
            'version': {'number': version + 1},
            'title':   page['title'],
            'type':    'page',
            'body':    {'storage': {'value': body, 'representation': 'storage'}},
        })
        return {'ok': True, 'page_id': page_id}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def create_member_profile(employee: dict) -> dict:
    """신규 직원 개인 프로필 페이지 생성"""
    name  = employee.get('name', '')
    dept  = employee.get('dept', '')
    pos   = employee.get('pos', '')
    email = employee.get('email', '')

    content = f"""
<h1>{name}</h1>
<table>
  <tbody>
    <tr><th>부서</th><td>{dept}</td></tr>
    <tr><th>직급</th><td>{pos}</td></tr>
    <tr><th>이메일</th><td>{email}</td></tr>
  </tbody>
</table>
<h2>담당 업무</h2>
<p>(입력 예정)</p>
"""

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'create_profile_page',
            'title': f'{name} - 프로필',
        }
    try:
        result = _post('content', {
            'type':  'page',
            'title': f'{name} - 프로필',
            'space': {'key': SPACE_KEY},
            'body':  {'storage': {'value': content, 'representation': 'storage'}},
        })
        return {'ok': True, 'page_id': result.get('id'), 'url': result.get('_links', {}).get('webui')}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
