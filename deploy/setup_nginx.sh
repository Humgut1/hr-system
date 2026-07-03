#!/bin/bash
# nginx + gunicorn systemd 서비스 세팅
# setup_server.sh 실행 후 실행할 것

set -e
APP_DIR="/home/ubuntu/hr-system"
DOMAIN=${1:-"_"}   # 도메인 없으면 IP로 접속

echo "[1/3] systemd 서비스 등록..."
sudo tee /etc/systemd/system/talentcore.service > /dev/null << EOF
[Unit]
Description=TalentCore HR System
After=network.target

[Service]
User=ubuntu
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn \\
    --workers 2 \\
    --bind 127.0.0.1:5000 \\
    --timeout 120 \\
    --access-logfile /var/log/talentcore/access.log \\
    --error-logfile /var/log/talentcore/error.log \\
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /var/log/talentcore
sudo chown ubuntu:ubuntu /var/log/talentcore
sudo systemctl daemon-reload
sudo systemctl enable talentcore
sudo systemctl start talentcore

echo "[2/3] nginx 설정..."
sudo tee /etc/nginx/sites-available/talentcore > /dev/null << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120;
    }

    location /static/ {
        alias ${APP_DIR}/static/;
        expires 7d;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/talentcore /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo "[3/3] Oracle Cloud 방화벽 포트 개방..."
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

echo ""
echo "========================================"
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "IP 조회 실패")
echo " 배포 완료!"
echo " 접속 주소: http://${PUBLIC_IP}"
echo " 로그 확인: sudo journalctl -u talentcore -f"
echo "========================================"
