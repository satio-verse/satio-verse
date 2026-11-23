#!/bin/bash
set -euo pipefail
DATA=/srv/worldmodel-data
ROOT=/srv/satio-verse
SECRET=$(cat "$DATA/private/receipt-secret.txt" | tr -d '\n')
printf 'WM_RECEIPT_SECRET=%s\n' "$SECRET" > "$DATA/private/platform.env"
chmod 600 "$DATA/private/platform.env"

cat > /etc/systemd/system/satio-platform.service <<UNIT
[Unit]
Description=S.A.T.I.O Verse community platform
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$DATA/private/platform.env
ExecStart=$ROOT/.venv/bin/python $ROOT/tools/community_server.py --home $DATA/store --authors $DATA/private/authors.json --public-url https://satio-verse.gitgratitude.com --port 8792
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl restart satio-platform
sleep 2
systemctl is-active satio-platform || true
echo "-- 8792 --"
curl -fsS -m 5 http://127.0.0.1:8792/index.json; echo
echo "-- nginx (Host 头) --"
curl -s -m 5 -o /dev/null -w 'nginx code=%{http_code}\n' -H "Host: satio-verse.gitgratitude.com" http://127.0.0.1/index.json
curl -fsS -m 5 -H "Host: satio-verse.gitgratitude.com" http://127.0.0.1/index.json; echo
echo "-- nginx 配置清单 --"
ls -l /etc/nginx/sites-enabled/
