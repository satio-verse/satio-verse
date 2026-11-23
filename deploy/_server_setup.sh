#!/bin/bash
set -euo pipefail
DOMAIN="satio-verse.gitgratitude.com"
ROOT=/srv/satio-verse
DATA=/srv/worldmodel-data
TARBALL=/home/ubuntu/satio-verse.tar.gz

echo "== 1/6 apt 装包 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl tar
nginx -v 2>&1 || true

echo "== 2/6 解包代码到 $ROOT（服务器连不上 GitHub，用本机打包上传）=="
mkdir -p "$ROOT"
rm -rf "$ROOT"/*
tar -xzf "$TARBALL" -C "$ROOT"
ls "$ROOT" | head -20
echo "files: $(find "$ROOT" -type f | wc -l)"

echo "== 3/6 数据目录 + 密钥 =="
mkdir -p "$DATA/store" "$DATA/private" "$DATA/releases" /srv/worldmodel-releases
[ -f "$DATA/private/receipt-secret.txt" ] || head -c 32 /dev/urandom | base64 > "$DATA/private/receipt-secret.txt"
[ -f "$DATA/private/authors.json" ] || echo '{}' > "$DATA/private/authors.json"
chmod 700 "$DATA/private"
chmod 600 "$DATA/private/"*

echo "== 4/6 venv + aiohttp（走清华源）=="
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/deploy/requirements-platform.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
"$ROOT/.venv/bin/python" -c "import aiohttp; print('aiohttp', aiohttp.__version__)"

echo "== 5/6 systemd =="
cat > /etc/systemd/system/satio-platform.service <<UNIT
[Unit]
Description=S.A.T.I.O Verse community platform
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=WM_RECEIPT_SECRET_FILE=$DATA/private/receipt-secret.txt
ExecStart=$ROOT/.venv/bin/python $ROOT/tools/community_server.py --home $DATA/store --authors $DATA/private/authors.json --public-url https://$DOMAIN --port 8792
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now satio-platform
sleep 2
systemctl --no-pager -l status satio-platform | head -12 || true
echo "-- 本机自检 --"
curl -fsS -m 5 http://127.0.0.1:8792/index.json && echo "  <= 8792 OK" || echo "  <= 8792 失败"

echo "== 6/6 nginx（先 HTTP；证书等防火墙放通后再签）=="
cat > /etc/nginx/sites-available/satio-platform <<NGX
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
        alias /srv/worldmodel-releases/;
        default_type application/json;
        add_header Cache-Control "no-cache";
    }
}
NGX
ln -sf /etc/nginx/sites-available/satio-platform /etc/nginx/sites-enabled/satio-platform
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "-- 本机 nginx 自检（带 Host）--"
curl -fsS -m 5 -H "Host: $DOMAIN" http://127.0.0.1/index.json && echo "  <= nginx->8792 OK" || echo "  <= nginx 失败"
echo "== done =="
