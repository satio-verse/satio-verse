#!/bin/sh
# S.A.T.I.O Verse 社区源 —— 一台空 Ubuntu/Debian 机器的一次性引导。
# 用法（在服务器上，root）：
#   sh deploy/bootstrap.sh satio-verse.gitgraduate.com
# 幂等：重复跑只会补齐缺的东西。
set -eu

DOMAIN=${1:?用法: sh deploy/bootstrap.sh <域名>}
ROOT=${ROOT:-/srv/satio-verse}
DATA=${DATA:-/srv/worldmodel-data}
REPO=${REPO:-https://github.com/satio-verse/satio-verse.git}

echo "== 1/6 系统包 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx curl

echo "== 2/6 代码 =="
mkdir -p "$(dirname "$ROOT")"
if [ -d "$ROOT/.git" ]; then
  git -C "$ROOT" fetch --depth 1 origin main && git -C "$ROOT" reset --hard origin/main
else
  git clone --depth 1 "$REPO" "$ROOT"
fi

echo "== 3/6 数据目录 + 密钥 =="
mkdir -p "$DATA/store" "$DATA/private" "$DATA/releases"
[ -f "$DATA/private/receipt-secret.txt" ] || head -c 32 /dev/urandom | base64 > "$DATA/private/receipt-secret.txt"
[ -f "$DATA/private/authors.json" ] || echo '{}' > "$DATA/private/authors.json"
chmod 700 "$DATA/private"
chmod 600 "$DATA/private/"*

echo "== 4/6 Python 环境 =="
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/deploy/requirements-platform.txt"

echo "== 5/6 systemd =="
cat > /etc/systemd/system/satio-platform.service <<UNIT
[Unit]
Description=S.A.T.I.O Verse community platform
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=WM_RECEIPT_SECRET_FILE=$DATA/private/receipt-secret.txt
Environment=WM_RECEIPT_SECRET=$(cat "$DATA/private/receipt-secret.txt")
ExecStart=$ROOT/.venv/bin/python $ROOT/tools/community_server.py --home $DATA/store --authors $DATA/private/authors.json --public-url https://$DOMAIN --port 8792
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now satio-platform
sleep 2
curl -fsS http://127.0.0.1:8792/index.json >/dev/null && echo "  服务在本机 8792 起来了"

echo "== 6/6 nginx + 证书 =="
cat > /etc/nginx/sites-available/satio-platform <<NGINX
limit_req_zone \$binary_remote_addr zone=worldmodel_platform:10m rate=30r/m;
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 2m;
    location / {
        limit_req zone=worldmodel_platform burst=20 nodelay;
        proxy_pass http://127.0.0.1:8792;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 35s;
    }
    location /releases/ {
        alias $DATA/releases/;
        default_type application/json;
        add_header Cache-Control "no-cache";
    }
}
NGINX
ln -sf /etc/nginx/sites-available/satio-platform /etc/nginx/sites-enabled/satio-platform
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect || \
  echo "  证书没签成（DNS 没指过来？）：先确认 $DOMAIN 的 A 记录指向这台机器，再跑 certbot --nginx -d $DOMAIN"

echo
echo "完成。自检："
echo "  curl -sS https://$DOMAIN/index.json | head -c 300"
echo "  systemctl status satio-platform --no-pager | head -5"
