#!/usr/bin/env python3
"""
일일 에러 로그 요약 (launch_plan P1-7, v1.5.2)

- gunicorn 에러 로그에서 최근 24시간의 ERROR/Traceback 라인을 추출해
  /var/log/talentcore/digest-YYYYMMDD.txt 로 저장한다.
- .env에 SMTP_HOST/SMTP_USER/SMTP_PASSWORD(+ DIGEST_EMAIL)가 설정돼 있으면
  에러가 있을 때만 요약 메일을 발송한다 (미설정 시 파일 저장만).
- cron 등록은 deploy/setup_monitoring.sh 사용.
"""
import os
import re
import sys
from datetime import datetime, timedelta

LOG_PATH   = os.environ.get('TALENTCORE_ERROR_LOG', '/var/log/talentcore/error.log')
DIGEST_DIR = os.environ.get('TALENTCORE_DIGEST_DIR', '/var/log/talentcore')

# .env 로드 (프로젝트 루트)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, '.env'))
except Exception:
    pass

# gunicorn 로그 타임스탬프: [2026-07-16 20:59:03 +0000] / Flask: [2026-07-16 20:59:03,123]
TS_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})')
SKIP_PATTERNS = ('[INFO]', 'CSRF 검증 실패')   # 봇 스캔 CSRF 경고는 소음이라 제외


def main():
    since = datetime.utcnow() - timedelta(hours=24)
    lines = []
    try:
        with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
            in_traceback = False
            for line in f:
                m = TS_RE.search(line)
                if m:
                    try:
                        ts = datetime.strptime(f'{m.group(1)} {m.group(2)}', '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        ts = None
                    if ts and ts < since:
                        in_traceback = False
                        continue
                    if any(p in line for p in SKIP_PATTERNS):
                        in_traceback = False
                        continue
                    if '[ERROR]' in line or 'ERROR in' in line or 'WARNING in' in line:
                        lines.append(line.rstrip())
                        in_traceback = True
                    else:
                        in_traceback = False
                elif in_traceback or line.startswith('Traceback') or line.startswith('  File '):
                    lines.append(line.rstrip())
                    in_traceback = True
    except FileNotFoundError:
        print(f'로그 파일 없음: {LOG_PATH}')
        sys.exit(0)

    today = datetime.utcnow().strftime('%Y%m%d')
    digest_path = os.path.join(DIGEST_DIR, f'digest-{today}.txt')
    header = (f'TalentCore 일일 에러 요약 — {datetime.utcnow():%Y-%m-%d %H:%M} UTC\n'
              f'최근 24시간 에러/경고: {len(lines)}줄\n' + '=' * 50 + '\n')
    body = header + ('\n'.join(lines) if lines else '에러 없음 ✅')
    try:
        with open(digest_path, 'w', encoding='utf-8') as f:
            f.write(body)
        print(f'요약 저장: {digest_path} ({len(lines)}줄)')
    except OSError as e:
        print(f'요약 저장 실패: {e}')

    # SMTP 설정 시 + 에러가 있을 때만 메일 발송
    smtp_host = os.environ.get('SMTP_HOST')
    to_addr   = os.environ.get('DIGEST_EMAIL') or os.environ.get('SMTP_USER')
    if lines and smtp_host and to_addr:
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(body, _charset='utf-8')
            msg['Subject'] = f'[TalentCore] 에러 요약 {len(lines)}줄 — {datetime.utcnow():%m/%d}'
            msg['From'] = os.environ.get('SMTP_USER', 'noreply@talentcore')
            msg['To'] = to_addr
            port = int(os.environ.get('SMTP_PORT', 587))
            with smtplib.SMTP(smtp_host, port, timeout=15) as s:
                s.starttls()
                s.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
                s.send_message(msg)
            print(f'요약 메일 발송: {to_addr}')
        except Exception as e:
            print(f'메일 발송 실패 (파일 저장은 완료): {e}')


if __name__ == '__main__':
    main()
