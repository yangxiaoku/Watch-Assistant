#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

# Live verification is intentionally separate from the offline gate. It needs
# the persistent iPad cookie and the LAN host 192.168.6.236, so run it daily.
COOKIE_PATH="${P115_COOKIE_PATH:-/c/Users/98275/.115ts-secrets/.p115-cookie}"
DIRECTORY_ID="${P115_DIRECTORY_ID:-3482085898508567892}"
if [[ ! -f "$COOKIE_PATH" ]]; then
    echo "live verification refused: cookie file is missing" >&2
    exit 1
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
else
    PYTHON_BIN="${PYTHON_BIN:-python}"
fi

exec "$PYTHON_BIN" scripts/p115_c03_live_runner.py \
    --parent-id "$DIRECTORY_ID" \
    --cookie-path "$COOKIE_PATH" \
    --live
