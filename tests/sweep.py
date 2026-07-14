# -*- coding: utf-8 -*-
"""전 GET 라우트 × 3역할(admin/manager/employee) 에러 스윕.

릴리즈 전 필수 실행 (CLAUDE.md 릴리즈 워크플로우 참조):
    python tests/sweep.py        # 로컬 DB(hr_system.db) 기준
500 에러 또는 처리되지 않은 예외가 1건이라도 있으면 exit 1.

잡아내는 유형 (실제로 v1.2.2~v1.2.5에서 발견된 버그들):
  - 존재하지 않는 컬럼/테이블 참조 (sqlite3.OperationalError)
  - 존재하지 않는 엔드포인트 url_for (BuildError)
  - sqlite3.Row에 없는 메서드 호출 (.get 등 AttributeError)
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from app import app          # noqa: E402
import sqlite3               # noqa: E402

app.config['TESTING'] = True

ADMIN_EMAIL    = os.environ.get('SWEEP_ADMIN_EMAIL', 'admin@company.com')
ADMIN_PW       = os.environ.get('SWEEP_ADMIN_PW', 'admin1234!')
MANAGER_EMAIL  = os.environ.get('SWEEP_MANAGER_EMAIL', 'manager@company.com')
EMPLOYEE_EMAIL = os.environ.get('SWEEP_EMPLOYEE_EMAIL', 'emp002@company.com')  # employee@company.com은 v1.2.6에서 manager로 승격됨
DEFAULT_PW     = os.environ.get('SWEEP_DEFAULT_PW', 'changeme!')

SKIP_PREFIX = ('/static', '/logout', '/demo', '/tour', '/slack',
               '/billing/webhook', '/api/', '/export')


def _sample_ids():
    db = sqlite3.connect(os.path.join(BASE, 'hr_system.db'))
    db.row_factory = sqlite3.Row

    def one(sql, default=1):
        row = db.execute(sql).fetchone()
        return row[0] if row else default

    ids = {
        'emp_id':         one("SELECT id FROM users WHERE role='employee' AND status='active' LIMIT 1"),
        'applicant_id':   one('SELECT id FROM applicants LIMIT 1'),
        'posting_id':     one('SELECT id FROM job_postings LIMIT 1'),
        'cycle_id':       one('SELECT id FROM performance_cycles ORDER BY id DESC LIMIT 1'),
        'goal_id':        one('SELECT id FROM performance_goals LIMIT 1'),
        'contract_id':    one('SELECT id FROM contracts LIMIT 1'),
        'req_id':         one('SELECT id FROM job_requisitions LIMIT 1'),
        'offer_id':       one('SELECT id FROM offers LIMIT 1'),
        'hire_id':        one('SELECT id FROM incoming_hires LIMIT 1'),
    }
    db.close()
    # url 파라미터 이름 → 샘플 값
    return {
        'emp_id': ids['emp_id'], 'user_id': ids['emp_id'], 'uid': ids['emp_id'],
        'applicant_id': ids['applicant_id'], 'posting_id': ids['posting_id'],
        'cycle_id': ids['cycle_id'], 'goal_id': ids['goal_id'],
        'contract_id': ids['contract_id'],
        'requisition_id': ids['req_id'], 'req_id': ids['req_id'],
        'offer_id': ids['offer_id'], 'hire_id': ids['hire_id'],
    }


def _login(email, pw):
    c = app.test_client()
    c.get('/login')
    with c.session_transaction() as s:
        token = s.get('csrf_token')
    c.post('/login', data={'email': email, 'password': pw, 'csrf_token': token},
           follow_redirects=True)
    return c


def run_sweep():
    sub = _sample_ids()
    clients = {
        'admin':    _login(ADMIN_EMAIL, ADMIN_PW),
        'manager':  _login(MANAGER_EMAIL, DEFAULT_PW),
        'employee': _login(EMPLOYEE_EMAIL, DEFAULT_PW),
    }
    for role, c in clients.items():
        with c.session_transaction() as s:
            if not s.get('user_id'):
                print(f'[경고] {role} 로그인 실패 — 해당 역할 스윕이 무의미해집니다. 계정/비밀번호 확인.')

    errors, count = [], 0
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods:
            continue
        url = str(rule)
        if url.startswith(SKIP_PREFIX) or url == '/':
            continue
        skip = False
        for arg in rule.arguments:
            if arg in sub:
                url = url.replace(f'<int:{arg}>', str(sub[arg])).replace(f'<{arg}>', str(sub[arg]))
            elif f'<int:{arg}>' in url:
                url = url.replace(f'<int:{arg}>', '1')
            else:
                skip = True
        if skip or '<' in url:
            continue
        count += 1
        for role, c in clients.items():
            try:
                r = c.get(url, follow_redirects=False)
                if r.status_code >= 500:
                    errors.append((role, url, r.status_code))
            except Exception as e:
                errors.append((role, url, f'EXC {type(e).__name__}: {e}'))

    print(f'스윕 완료: GET 라우트 {count}개 × 3역할')
    if errors:
        print(f'오류 {len(errors)}건:')
        for role, url, code in errors:
            print(f'  [{role}] {url} → {code}')
        return 1
    print('500 에러 0건 — 전 라우트 정상')
    return 0


if __name__ == '__main__':
    sys.exit(run_sweep())
