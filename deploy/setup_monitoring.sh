#!/bin/bash
# 일일 에러 요약 cron 등록 (launch_plan P1-7, v1.5.2) — VM에서 1회 실행
# 사용법: bash ~/hr-system/deploy/setup_monitoring.sh
set -e

APP_DIR="/home/ubuntu/hr-system"
PYTHON="$APP_DIR/venv/bin/python3"
CRON_LINE="0 9 * * * cd $APP_DIR && $PYTHON deploy/error_digest.py >> /var/log/talentcore/digest_cron.log 2>&1"

# 중복 등록 방지
( crontab -l 2>/dev/null | grep -v 'error_digest.py' ; echo "$CRON_LINE" ) | crontab -

echo "========================================"
echo " 에러 요약 cron 등록 완료 (매일 09:00 UTC = 한국 18:00)"
echo " 요약 파일: /var/log/talentcore/digest-YYYYMMDD.txt"
echo " SMTP 설정(.env: SMTP_HOST/USER/PASSWORD, DIGEST_EMAIL) 시 에러가 있으면 메일 발송"
echo "========================================"

# 즉시 1회 실행해 동작 확인
cd "$APP_DIR" && sudo -n true 2>/dev/null || true
$PYTHON deploy/error_digest.py || true
