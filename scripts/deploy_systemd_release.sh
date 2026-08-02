#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RELEASE_ROOT="${1:?usage: deploy_systemd_release.sh RELEASE_ROOT EXPECTED_RELEASE}"
EXPECTED_RELEASE="${2:-${WATCH_ASSISTANT_EXPECTED_RELEASE:-}}"
if [[ -z "$EXPECTED_RELEASE" ]]; then
    echo "deployment refused: EXPECTED_RELEASE is required" >&2
    exit 2
fi
RELEASE_ENV="${WATCH_ASSISTANT_RELEASE_ENV:-/var/lib/watch-assistant/release.env}"
DROP_IN="${WATCH_ASSISTANT_RELEASE_DROP_IN:-/etc/systemd/system/watch-assistant.service.d/release.conf}"
CURRENT_ROOT="${WATCH_ASSISTANT_CURRENT_ROOT:-/opt/watch-assistant/current}"
RELEASES_ROOT="${WATCH_ASSISTANT_RELEASES_ROOT:-/opt/watch-assistant/releases}"
SERVICE_USER="${WATCH_ASSISTANT_SERVICE_USER:-watch-assistant}"
HEALTH_URL="${WATCH_ASSISTANT_HEALTH_URL:-http://127.0.0.1:8115/api/v1/health}"
PYTHON_BIN="${WATCH_ASSISTANT_PYTHON:-/opt/watch-assistant/venv/bin/python}"

"$PYTHON_BIN" "$RELEASE_ROOT/scripts/systemd_release_prepare.py" \
    --release-root "$RELEASE_ROOT" \
    --expected-release "$EXPECTED_RELEASE" \
    --allowed-releases-root "$RELEASES_ROOT" \
    --service-user "$SERVICE_USER"
"$PYTHON_BIN" "$RELEASE_ROOT/scripts/systemd_release_update.py" \
    --version-file "$RELEASE_ROOT/VERSION" \
    --release-env "$RELEASE_ENV" \
    --stale-drop-in "$DROP_IN"
systemctl daemon-reload
systemctl restart watch-assistant.service
"$PYTHON_BIN" "$RELEASE_ROOT/scripts/postdeploy_release_check.py" \
    --version-file "$RELEASE_ROOT/VERSION" \
    --health-url "$HEALTH_URL" \
    --release-env "$RELEASE_ENV" \
    --current-root "$CURRENT_ROOT" \
    --stale-drop-in "$DROP_IN"
