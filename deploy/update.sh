#!/bin/bash
# 코드 업데이트 배포 스크립트
# git push 후 서버에서 실행: bash ~/hr-system/deploy/update.sh

set -e
APP_DIR="/home/ubuntu/hr-system"

echo "TalentCore 업데이트 중..."
cd $APP_DIR

git pull
source venv/bin/activate
pip install -r requirements.txt -q
python migrate_db.py

sudo systemctl restart talentcore
echo "완료! 서비스 재시작됨."
sudo systemctl status talentcore --no-pager
