#!/bin/sh
# Usage: sh deploy/start-platform.sh /srv/worldmodel-data https://community.example.com /path/to/venv/bin/python
set -eu
platform_data_root=${1:?data root required}
platform_public_url=${2:?public HTTPS URL required}
platform_python=${3:-python3}
platform_package_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WM_RECEIPT_SECRET=$(cat "$platform_data_root/private/receipt-secret.txt")
export WM_RECEIPT_SECRET
exec "$platform_python" "$platform_package_root/tools/community_server.py" --home "$platform_data_root/store" --authors "$platform_data_root/private/authors.json" --public-url "$platform_public_url" --port 8792
