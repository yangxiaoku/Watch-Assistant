#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RELEASE_ROOT="${1:?usage: deploy_systemd_release.sh RELEASE_ROOT}"
RELEASE_ENV="${WATCH_ASSISTANT_RELEASE_ENV:-/var/lib/watch-assistant/release.env}"
DROP_IN="${WATCH_ASSISTANT_RELEASE_DROP_IN:-/etc/systemd/system/watch-assistant.service.d/release.conf}"
HEALTH_URL="${WATCH_ASSISTANT_HEALTH_URL:-http://127.0.0.1:8115/api/v1/health}"
PYTHON_BIN="${WATCH_ASSISTANT_PYTHON:-/opt/watch-assistant/venv/bin/python}"

"$PYTHON_BIN" "$RELEASE_ROOT/scripts/systemd_release_update.py" \
    --version-file "$RELEASE_ROOT/VERSION" \
    --release-env "$RELEASE_ENV" \
    --stale-drop-in "$DROP_IN"
systemctl daemon-reload
systemctl restart watch-assistant.service
"$PYTHON_BIN" "$RELEASE_ROOT/scripts/postdeploy_release_check.py" \
    --version-file "$RELEASE_ROOT/VERSION" \
    --health-url "$HEALTH_URL" \
    --stale-drop-in "$DROP_IN"
