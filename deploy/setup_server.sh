#!/bin/bash
# TalentCore 서버 초기 세팅 스크립트
# Oracle Cloud Ubuntu 22.04 ARM 기준
# 사용법: bash setup_server.sh

set -e
echo "========================================"
echo " TalentCore 서버 세팅 시작"
echo "========================================"

# ── 1. 시스템 업데이트 ───────────────────────
echo "[1/7] 시스템 업데이트..."
sudo apt-get update -y && sudo apt-get upgrade -y

# ── 2. Python + 필수 패키지 ──────────────────
echo "[2/7] Python 설치..."
sudo apt-get install -y python3 python3-pip python3-venv git nginx ufw

# ── 3. 방화벽 설정 ───────────────────────────
echo "[3/7] 방화벽 설정..."
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable

# ── 4. 앱 클론 ───────────────────────────────
echo "[4/7] TalentCore 클론..."
cd /home/ubuntu
if [ -d "hr-system" ]; then
    cd hr-system && git pull
else
    git clone https://github.com/Humgut1/hr-system.git hr-system
    cd hr-system
fi

# ── 5. Python 가상환경 + 패키지 설치 ─────────
echo "[5/7] Python 패키지 설치..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn

# ── 6. DB 초기화 ─────────────────────────────
echo "[6/7] DB 초기화 (시드 데이터 포함)..."
python migrate_db.py

# ── 7. .env 파일 생성 (기본값) ───────────────
echo "[7/7] 환경변수 파일 생성..."
if [ ! -f ".env" ]; then
GENERATED_SECRET=$(openssl rand -hex 32)
cat > .env << ENVEOF
FLASK_DEBUG=false
HR_SECRET_KEY=${GENERATED_SECRET}
COMPANY_NAME=TalentCore Demo
# Slack (선택)
# SLACK_BOT_TOKEN=xoxb-...
# SLACK_WORKSPACE_ID=T...
# Jira (선택)
# JIRA_EMAIL=
# JIRA_API_TOKEN=
# JIRA_BASE_URL=
# JIRA_PROJECT_KEY=KAN
# Confluence (선택)
# CONFLUENCE_BASE_URL=
# CONFLUENCE_SPACE_KEY=HR
ENVEOF
fi

echo ""
echo "========================================"
echo " 앱 세팅 완료!"
echo " 다음 명령어 실행:"
echo "   bash /home/ubuntu/hr-system/deploy/setup_nginx.sh"
echo "========================================"
