#!/bin/bash
# DB 자동 백업 cron 등록 (Oracle Cloud VM에서 1회 실행)
# 매일 03:00 KST에 backup_db.py 실행 → backups/ 폴더에 최근 14일치 보관

set -e
APP_DIR="/home/ubuntu/hr-system"
CRON_LINE="0 3 * * * cd $APP_DIR && venv/bin/python backup_db.py >> /var/log/talentcore/backup.log 2>&1"

# 로그 폴더 확인
sudo mkdir -p /var/log/talentcore
sudo chown ubuntu:ubuntu /var/log/talentcore

# 중복 등록 방지 후 crontab에 추가
( crontab -l 2>/dev/null | grep -v 'backup_db.py' ; echo "$CRON_LINE" ) | crontab -

echo "완료! 등록된 크론탭:"
crontab -l | grep backup_db.py

echo ""
echo "즉시 1회 실행 테스트:"
cd $APP_DIR && venv/bin/python backup_db.py
