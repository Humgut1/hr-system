#!/bin/bash
# 도메인 + HTTPS(SSL) 적용 스크립트 (launch_plan P0-3, v1.5.2)
#
# 선행 조건 (승헌씨):
#   1. 도메인 구매 완료
#   2. DNS A 레코드: @ (및 www) → 161.33.39.127, 전파 확인 (nslookup <도메인>)
#
# 사용법 (VM에서):
#   bash ~/hr-system/deploy/setup_ssl.sh talentcore.co.kr
#   bash ~/hr-system/deploy/setup_ssl.sh talentcore.co.kr admin@example.com   # 인증서 만료 알림 이메일
set -e

DOMAIN="$1"
EMAIL="${2:-}"

if [ -z "$DOMAIN" ]; then
  echo "사용법: bash deploy/setup_ssl.sh <도메인> [이메일]"
  echo "예:     bash deploy/setup_ssl.sh talentcore.co.kr hunie0649@gmail.com"
  exit 1
fi

echo "[1/4] DNS 확인..."
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
echo "  $DOMAIN → ${RESOLVED:-해석 실패}"
if [ -z "$RESOLVED" ]; then
  echo "  ⚠ DNS가 아직 전파되지 않았습니다. A 레코드 설정 후 다시 시도하세요."
  exit 1
fi

echo "[2/4] nginx server_name 갱신..."
NGINX_CONF="/etc/nginx/sites-available/talentcore"
if [ -f "$NGINX_CONF" ]; then
  sudo sed -i "s/server_name .*/server_name $DOMAIN www.$DOMAIN;/" "$NGINX_CONF"
  sudo nginx -t && sudo systemctl reload nginx
else
  echo "  ⚠ $NGINX_CONF 를 찾을 수 없습니다. deploy/setup_nginx.sh를 먼저 실행하세요."
  exit 1
fi

echo "[3/4] certbot 설치..."
sudo apt-get update -qq
sudo apt-get install -y -qq certbot python3-certbot-nginx

echo "[4/4] SSL 인증서 발급 + https 리다이렉트..."
if [ -n "$EMAIL" ]; then
  sudo certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --redirect --non-interactive --agree-tos -m "$EMAIL"
else
  sudo certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --redirect --non-interactive --agree-tos --register-unsafely-without-email
fi

echo ""
echo "========================================"
echo " HTTPS 적용 완료!"
echo "  https://$DOMAIN"
echo "  - http 접속은 https로 자동 리다이렉트"
echo "  - 인증서 자동 갱신: certbot.timer (systemctl list-timers 로 확인)"
echo "========================================"
