#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
    BOOTSTRAP_PYTHON="${WATCH_ASSISTANT_BOOTSTRAP_PYTHON:-}"
    if [[ -z "$BOOTSTRAP_PYTHON" ]] && command -v py >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON="$(command -v py)"
    fi
    if [[ -z "$BOOTSTRAP_PYTHON" ]] && command -v python3.12 >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON="$(command -v python3.12)"
    fi
    if [[ -z "$BOOTSTRAP_PYTHON" ]] && command -v python3 >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON="$(command -v python3)"
    fi
    if [[ -z "$BOOTSTRAP_PYTHON" ]] && command -v python >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON="$(command -v python)"
    fi
    if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
        echo "bootstrap refused: Python 3.12+ is required to create .venv" >&2
        exit 2
    fi

    case "$(basename "$BOOTSTRAP_PYTHON")" in
        py|py.exe)
        "$BOOTSTRAP_PYTHON" -3.12 -m venv "$ROOT_DIR/.venv"
            ;;
        *)
            "$BOOTSTRAP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else "Python 3.12+ is required")'
            "$BOOTSTRAP_PYTHON" -m venv "$ROOT_DIR/.venv"
            ;;
    esac
    if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
        PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
    elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
        PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
    else
        echo "bootstrap refused: .venv Python was not created" >&2
        exit 2
    fi
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else "Python 3.12+ is required")'

TASK_TMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TASK_TMP_DIR"
}
trap cleanup EXIT

LOCK_CONSTRAINTS="$TASK_TMP_DIR/pip-constraints.txt"
"$PYTHON_BIN" scripts/generate_pip_constraints.py \
    --lock-file uv.lock \
    --output "$LOCK_CONSTRAINTS"
"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --constraint "$LOCK_CONSTRAINTS" \
    -e ".[dev]"
"$PYTHON_BIN" -m pip check

if [[ -n "${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH:-}" ]]; then
    export DYLD_LIBRARY_PATH="${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi
if ! "$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; Fernet.generate_key()' >/dev/null 2>&1; then
    echo "bootstrap refused: cryptography cannot load OpenSSL 3" >&2
    echo "install an OpenSSL 3 runtime or set WATCH_ASSISTANT_NATIVE_LIBRARY_PATH to its lib directory" >&2
    exit 2
fi

FRONTEND_NODE="${WATCH_ASSISTANT_FRONTEND_NODE:-}"
if [[ -n "$FRONTEND_NODE" ]]; then
    if [[ ! -x "$FRONTEND_NODE" ]]; then
        echo "bootstrap refused: WATCH_ASSISTANT_FRONTEND_NODE must point to an executable Node binary" >&2
        exit 2
    fi
    FRONTEND_NODE_DIR="$(cd "$(dirname "$FRONTEND_NODE")" && pwd)"
    export PATH="$FRONTEND_NODE_DIR${PATH:+:$PATH}"
fi

FRONTEND_PM="${WATCH_ASSISTANT_FRONTEND_PM:-}"
if [[ -z "$FRONTEND_PM" ]] && command -v npm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v npm)"
fi
if [[ -z "$FRONTEND_PM" ]] && command -v pnpm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v pnpm)"
fi
if [[ -z "$FRONTEND_PM" ]]; then
    echo "bootstrap refused: npm or pnpm is required for frontend dependencies" >&2
    exit 2
fi

case "$(basename "$FRONTEND_PM")" in
    npm)
        "$FRONTEND_PM" ci --prefix frontend
        ;;
    pnpm)
        # The repository locks exact package versions in package.json.  The
        # fallback avoids creating a second lockfile when only bundled pnpm is available.
        "$FRONTEND_PM" --dir frontend install --no-lockfile
        ;;
    *)
        echo "bootstrap refused: WATCH_ASSISTANT_FRONTEND_PM must point to npm or pnpm" >&2
        exit 2
        ;;
esac

echo "Development environment ready: $PYTHON_BIN and $FRONTEND_PM"

if [[ "${1:-}" == "--verify" ]]; then
    exec bash scripts/verify.sh
fi
