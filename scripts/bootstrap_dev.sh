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

PLATFORM="$(uname -s 2>/dev/null || printf 'unknown')"

runtime_dependencies_dir() {
    local base_python
    local python_bin_dir

    base_python="$($PYTHON_BIN -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
    if [[ -z "$base_python" || ! -x "$base_python" ]]; then
        return 0
    fi
    python_bin_dir="$(dirname "$base_python")"
    if [[ ! -d "$python_bin_dir/../.." ]]; then
        return 0
    fi
    (cd "$python_bin_dir/../.." && pwd -P)
}

native_library_path_ready() {
    [[ -f "$1/libssl.3.dylib" && -f "$1/libcrypto.3.dylib" ]]
}

if [[ -z "${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH:-}" && "$PLATFORM" == "Darwin" ]]; then
    RUNTIME_DEPENDENCIES_DIR="$(runtime_dependencies_dir || true)"
    for CANDIDATE_NATIVE_LIBRARY_PATH in \
        "/opt/homebrew/opt/openssl@3/lib" \
        "/usr/local/opt/openssl@3/lib" \
        "$RUNTIME_DEPENDENCIES_DIR/native/poppler/poppler/lib"; do
        if native_library_path_ready "$CANDIDATE_NATIVE_LIBRARY_PATH"; then
            WATCH_ASSISTANT_NATIVE_LIBRARY_PATH="$CANDIDATE_NATIVE_LIBRARY_PATH"
            echo "Detected macOS OpenSSL 3 runtime: $WATCH_ASSISTANT_NATIVE_LIBRARY_PATH"
            break
        fi
    done
fi

if [[ "$PLATFORM" == "Darwin" && -n "${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH:-}" ]] && \
    ! native_library_path_ready "$WATCH_ASSISTANT_NATIVE_LIBRARY_PATH"; then
    echo "bootstrap refused: OpenSSL 3 libraries were not found in WATCH_ASSISTANT_NATIVE_LIBRARY_PATH" >&2
    echo "set WATCH_ASSISTANT_NATIVE_LIBRARY_PATH to a directory containing libssl.3.dylib and libcrypto.3.dylib" >&2
    exit 2
fi

if [[ -n "${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH:-}" ]]; then
    # Use a fallback search path so bundled OpenSSL does not override macOS
    # system libraries used by unrelated tools.
    export WATCH_ASSISTANT_NATIVE_LIBRARY_PATH
    export DYLD_FALLBACK_LIBRARY_PATH="${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH}${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi

TASK_TMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TASK_TMP_DIR"
}
trap cleanup EXIT

dependency_fingerprint() {
    printf 'python=%s\n' "$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
    git hash-object \
        pyproject.toml \
        uv.lock \
        frontend/package.json \
        frontend/package-lock.json
}

python_dependencies_ready() {
    "$PYTHON_BIN" -c '
import aiosqlite
import cryptography
import fastapi
import httpx
import p115client
import pydantic_settings
import pytest
import respx
import ruff
import sqlalchemy
from cryptography.fernet import Fernet
Fernet.generate_key()
' >/dev/null 2>&1 && "$PYTHON_BIN" -m pip check >/dev/null 2>&1
}

LOCK_CONSTRAINTS="$TASK_TMP_DIR/pip-constraints.txt"
PYTHON_MARKER="$ROOT_DIR/.venv/.watch-assistant-python-ready"
dependency_fingerprint > "$TASK_TMP_DIR/python-fingerprint"
if [[ -f "$PYTHON_MARKER" ]] && cmp -s "$TASK_TMP_DIR/python-fingerprint" "$PYTHON_MARKER" && python_dependencies_ready; then
    echo "Reusing worktree Python dependencies"
else
    "$PYTHON_BIN" scripts/generate_pip_constraints.py \
        --lock-file uv.lock \
        --output "$LOCK_CONSTRAINTS"
    "$PYTHON_BIN" -m pip install \
        --disable-pip-version-check \
        --constraint "$LOCK_CONSTRAINTS" \
        -e ".[dev]"
    "$PYTHON_BIN" -m pip check
    if ! "$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; Fernet.generate_key()' >/dev/null 2>&1; then
        echo "bootstrap refused: cryptography cannot load OpenSSL 3" >&2
        echo "install an OpenSSL 3 runtime or set WATCH_ASSISTANT_NATIVE_LIBRARY_PATH to its lib directory" >&2
        exit 2
    fi
    cp "$TASK_TMP_DIR/python-fingerprint" "$PYTHON_MARKER"
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
if [[ -z "$FRONTEND_NODE" && "$PLATFORM" == "Darwin" ]]; then
    RUNTIME_DEPENDENCIES_DIR="${RUNTIME_DEPENDENCIES_DIR:-$(runtime_dependencies_dir || true)}"
    CANDIDATE_FRONTEND_NODE="$RUNTIME_DEPENDENCIES_DIR/node/bin/node"
    if [[ -x "$CANDIDATE_FRONTEND_NODE" ]]; then
        FRONTEND_NODE="$CANDIDATE_FRONTEND_NODE"
        FRONTEND_NODE_DIR="$(cd "$(dirname "$FRONTEND_NODE")" && pwd)"
        export PATH="$FRONTEND_NODE_DIR${PATH:+:$PATH}"
        echo "Detected bundled Node.js runtime: $FRONTEND_NODE"
    fi
fi
if [[ -z "$FRONTEND_NODE" ]] && command -v node >/dev/null 2>&1; then
    FRONTEND_NODE="$(command -v node)"
fi
if [[ -z "$FRONTEND_NODE" ]]; then
    echo "bootstrap refused: Node.js is required for frontend dependencies" >&2
    exit 2
fi

FRONTEND_PM="${WATCH_ASSISTANT_FRONTEND_PM:-}"
if [[ -z "$FRONTEND_PM" ]] && command -v npm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v npm)"
fi
if [[ -z "$FRONTEND_PM" ]] && command -v pnpm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v pnpm)"
fi
FRONTEND_MARKER="$ROOT_DIR/frontend/node_modules/.watch-assistant-bootstrap"
FRONTEND_FINGERPRINT="$TASK_TMP_DIR/frontend-fingerprint"
{
    printf 'node=%s\n' "$("$FRONTEND_NODE" --version)"
    git hash-object frontend/package.json frontend/package-lock.json
} > "$FRONTEND_FINGERPRINT"
if [[ -f "$FRONTEND_MARKER" && \
    -f "$ROOT_DIR/frontend/node_modules/vitest/vitest.mjs" && \
    -f "$ROOT_DIR/frontend/node_modules/vite/bin/vite.js" ]] && cmp -s "$FRONTEND_FINGERPRINT" "$FRONTEND_MARKER"; then
    echo "Reusing frontend dependencies"
else
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
    cp "$FRONTEND_FINGERPRINT" "$FRONTEND_MARKER"
fi

FRONTEND_TOOL="${FRONTEND_PM:-$FRONTEND_NODE}"
echo "Development environment ready: $PYTHON_BIN and $FRONTEND_TOOL"

if [[ "${1:-}" == "--verify" ]]; then
    exec bash scripts/verify.sh
fi
