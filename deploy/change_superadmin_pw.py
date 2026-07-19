#!/usr/bin/env python3
"""
SaaS 슈퍼어드민 비밀번호 변경 스크립트 (launch_plan P0-5, v1.5.2)

사용법 (프로젝트 루트에서):
  python deploy/change_superadmin_pw.py hunie0709                # 무작위 비밀번호 생성·출력
  python deploy/change_superadmin_pw.py hunie0709 "새비밀번호"    # 직접 지정 (10자 이상)

- 새 비밀번호는 한 번만 출력됩니다. 반드시 안전한 곳에 보관하세요.
- 운영 VM에서는: cd ~/hr-system && venv/bin/python3 deploy/change_superadmin_pw.py hunie0709
"""
import os
import sys
import secrets
import string
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_DB = os.path.join(BASE, 'master.db')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    username = sys.argv[1]

    if len(sys.argv) >= 3:
        new_pw = sys.argv[2]
        if len(new_pw) < 10:
            print('오류: 비밀번호는 10자 이상이어야 합니다.')
            sys.exit(1)
    else:
        alphabet = string.ascii_letters + string.digits + '!@#$%^&*'
        new_pw = ''.join(secrets.choice(alphabet) for _ in range(16))

    sys.path.insert(0, BASE)
    from werkzeug.security import generate_password_hash

    conn = sqlite3.connect(MASTER_DB)
    row = conn.execute('SELECT id FROM superadmins WHERE username=?', (username,)).fetchone()
    if not row:
        print(f'오류: 슈퍼어드민 계정 "{username}"을 찾을 수 없습니다.')
        conn.close()
        sys.exit(1)
    conn.execute('UPDATE superadmins SET password_hash=? WHERE username=?',
                 (generate_password_hash(new_pw), username))
    conn.commit()
    conn.close()

    print('=' * 52)
    print(f' 슈퍼어드민 "{username}" 비밀번호가 변경되었습니다.')
    print(f' 새 비밀번호: {new_pw}')
    print(' * 이 비밀번호는 다시 표시되지 않습니다. 지금 보관하세요.')
    print('=' * 52)


if __name__ == '__main__':
    main()
