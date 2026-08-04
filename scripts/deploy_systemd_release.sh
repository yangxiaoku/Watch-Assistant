#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMMAND="${1:?usage: deploy_systemd_release.sh RELEASE_ROOT EXPECTED_RELEASE [--install-unit] | rollback --confirm}"

RELEASE_ENV="${WATCH_ASSISTANT_RELEASE_ENV:-/var/lib/watch-assistant/release.env}"
UNIT_PATH="/etc/systemd/system/watch-assistant.service"
DROP_IN_DIR="/etc/systemd/system/watch-assistant.service.d"
CURRENT_ROOT="${WATCH_ASSISTANT_CURRENT_ROOT:-/opt/watch-assistant/current}"
RELEASES_ROOT="${WATCH_ASSISTANT_RELEASES_ROOT:-/opt/watch-assistant/releases}"
STATE_FILE="${WATCH_ASSISTANT_RELEASE_STATE:-/var/lib/watch-assistant/release-rollback.json}"
SERVICE_USER="${WATCH_ASSISTANT_SERVICE_USER:-watch-assistant}"
HEALTH_URL="${WATCH_ASSISTANT_HEALTH_URL:-http://127.0.0.1:8115/api/v1/health}"
DIAGNOSTICS_URL="${WATCH_ASSISTANT_DIAGNOSTICS_URL:-http://127.0.0.1:8115/api/v1/deployment/diagnostics}"
HEALTH_TIMEOUT="${WATCH_ASSISTANT_HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_POLL_INTERVAL="${WATCH_ASSISTANT_HEALTH_POLL_INTERVAL_SECONDS:-1}"
PYTHON_BIN="${WATCH_ASSISTANT_PYTHON:-/opt/watch-assistant/venv/bin/python}"
SYSTEMCTL_BIN="${WATCH_ASSISTANT_SYSTEMCTL:-systemctl}"
UPDATE_SCRIPT_SUFFIX="update"
INSTALL_UNIT=false
INSTALL_ARGS=()
case "${WATCH_ASSISTANT_INSTALL_UNIT:-0}" in
    1|true|TRUE|yes|YES) INSTALL_UNIT=true ;;
    0|false|FALSE|no|NO|"") ;;
    *)
        echo "deployment refused: WATCH_ASSISTANT_INSTALL_UNIT must be 1 or 0" >&2
        exit 2
        ;;
esac

postdeploy_check() {
    local check_script="$1"
    local version_file="$2"
    local expected_release="${3:-}"
    local unit_sha256="${4:-}"
    local expected_args=()
    local unit_sha256_args=()
    if [[ -n "$expected_release" ]]; then
        expected_args=(--expected-release "$expected_release")
    fi
    if [[ -n "$unit_sha256" ]]; then
        unit_sha256_args=(--unit-sha256 "$unit_sha256")
    fi
    "$PYTHON_BIN" "$check_script" \
        --version-file "$version_file" \
        --unit watch-assistant.service \
        "${expected_args[@]}" \
        --health-url "$HEALTH_URL" \
        --health-timeout "$HEALTH_TIMEOUT" \
        --health-poll-interval "$HEALTH_POLL_INTERVAL" \
        --release-env "$RELEASE_ENV" \
        --current-root "$CURRENT_ROOT" \
        --unit-path "$UNIT_PATH" \
        "${unit_sha256_args[@]}" \
        --drop-in-dir "$DROP_IN_DIR" \
        --diagnostics-url "$DIAGNOSTICS_URL"
}

ROLLBACK_UNIT_SHA256=""
rollback_release_update() {
    local update_script="$1"
    local rollback_output=""
    local rollback_code
    if rollback_output="$("$PYTHON_BIN" "$update_script" \
        --rollback \
        --release-env "$RELEASE_ENV" \
        --current-root "$CURRENT_ROOT" \
        --state-file "$STATE_FILE" \
        --allowed-releases-root "$RELEASES_ROOT" \
        --unit-path "$UNIT_PATH" \
        --drop-in-dir "$DROP_IN_DIR")"; then
        rollback_code=0
    else
        rollback_code=$?
    fi
    printf '%s\n' "$rollback_output"
    ROLLBACK_UNIT_SHA256="$(printf '%s\n' "$rollback_output" | sed -n \
        's/^SYSTEMD_RELEASE_ROLLBACK_UNIT_SHA256=\([0-9a-f]\{64\}\)$/\1/p' | tail -n 1)"
    return "$rollback_code"
}

if [[ "$COMMAND" == "rollback" ]]; then
    if [[ "${2:-}" != "--confirm" || "$#" -ne 2 ]]; then
        echo "rollback refused: explicit --confirm is required" >&2
        exit 2
    fi
    if [[ -z "${WATCH_ASSISTANT_DIAGNOSTICS_TOKEN:-}" ]]; then
        echo "rollback refused: WATCH_ASSISTANT_DIAGNOSTICS_TOKEN is required" >&2
        exit 2
    fi
    # Resolve the release containing this script before current is switched.
    TOOL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
    if ! rollback_release_update \
        "$TOOL_ROOT/scripts/systemd_release_${UPDATE_SCRIPT_SUFFIX}.py"; then
        echo "rollback refused: release state restoration failed" >&2
        exit 1
    fi
    "$SYSTEMCTL_BIN" daemon-reload
    "$SYSTEMCTL_BIN" restart watch-assistant.service
    postdeploy_check "$TOOL_ROOT/scripts/postdeploy_release_check.py" \
        "$CURRENT_ROOT/VERSION" \
        "" \
        "$ROLLBACK_UNIT_SHA256"
    exit 0
fi

RELEASE_ROOT="$COMMAND"
EXPECTED_RELEASE="${2:-${WATCH_ASSISTANT_EXPECTED_RELEASE:-}}"
shift 2 || true
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--install-unit" ]]; then
        INSTALL_UNIT=true
    else
        echo "deployment refused: unknown option" >&2
        exit 2
    fi
    shift
done
if [[ "$INSTALL_UNIT" == true ]]; then
    INSTALL_ARGS+=(--install-unit)
fi
if [[ ! "$EXPECTED_RELEASE" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "deployment refused: EXPECTED_RELEASE must be a full git SHA" >&2
    exit 2
fi
EXPECTED_RELEASE="$(printf '%s' "$EXPECTED_RELEASE" | tr '[:upper:]' '[:lower:]')"
if [[ -z "${WATCH_ASSISTANT_DIAGNOSTICS_TOKEN:-}" ]]; then
    echo "deployment refused: WATCH_ASSISTANT_DIAGNOSTICS_TOKEN is required" >&2
    exit 2
fi

"$PYTHON_BIN" "$RELEASE_ROOT/scripts/systemd_release_prepare.py" \
    --release-root "$RELEASE_ROOT" \
    --expected-release "$EXPECTED_RELEASE" \
    --allowed-releases-root "$RELEASES_ROOT" \
    --service-user "$SERVICE_USER"

# systemd_release_update.py runs only after the prepared release passes its gate.
"$PYTHON_BIN" "$RELEASE_ROOT/scripts/systemd_release_${UPDATE_SCRIPT_SUFFIX}.py" \
    --switch \
    --version-file "$RELEASE_ROOT/VERSION" \
    --release-root "$RELEASE_ROOT" \
    --expected-release "$EXPECTED_RELEASE" \
    --release-env "$RELEASE_ENV" \
    --current-root "$CURRENT_ROOT" \
    --state-file "$STATE_FILE" \
    --allowed-releases-root "$RELEASES_ROOT" \
    --unit-path "$UNIT_PATH" \
    --drop-in-dir "$DROP_IN_DIR" \
    "${INSTALL_ARGS[@]}"

rollback_after_failure() {
    local reason="$1"
    set +e
    echo "deployment failed: ${reason}; attempting automatic rollback" >&2
    rollback_release_update \
        "$RELEASE_ROOT/scripts/systemd_release_${UPDATE_SCRIPT_SUFFIX}.py"
    local rollback_code=$?
    "$SYSTEMCTL_BIN" daemon-reload
    local reload_code=$?
    "$SYSTEMCTL_BIN" restart watch-assistant.service
    local restart_code=$?
    if (( rollback_code != 0 || reload_code != 0 || restart_code != 0 )); then
        echo "automatic rollback failed" >&2
        return 1
    fi

    if [[ ! -f "$CURRENT_ROOT/VERSION" ]]; then
        echo "automatic rollback health verification failed: previous VERSION is missing" >&2
        return 1
    fi
    if ! postdeploy_check \
        "$RELEASE_ROOT/scripts/postdeploy_release_check.py" \
        "$CURRENT_ROOT/VERSION" \
        "" \
        "$ROLLBACK_UNIT_SHA256"; then
        echo "automatic rollback health verification failed" >&2
        return 1
    fi
    return 0
}

if ! "$SYSTEMCTL_BIN" daemon-reload; then
    rollback_after_failure "daemon_reload_failed" || true
    exit 1
fi
if ! "$SYSTEMCTL_BIN" restart watch-assistant.service; then
    rollback_after_failure "service_restart_failed" || true
    exit 1
fi
if ! postdeploy_check \
    "$RELEASE_ROOT/scripts/postdeploy_release_check.py" \
    "$RELEASE_ROOT/VERSION" \
    "$EXPECTED_RELEASE"; then
    rollback_after_failure "postdeploy_check_failed" || true
    exit 1
fi

echo "systemd release deployed: ${EXPECTED_RELEASE}"
