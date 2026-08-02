#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

EXPECTED_COMMIT="${1:-${WATCH_ASSISTANT_EXPECTED_COMMIT:-}}"
if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "release refused: expected commit must be a full git SHA" >&2
    exit 2
fi
EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "release refused: working tree is not clean" >&2
    exit 1
fi

if [[ "$(git rev-parse HEAD)" != "$EXPECTED_COMMIT" ]]; then
    echo "release refused: checked-out commit does not match expected SHA" >&2
    exit 1
fi

if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    RELEASE_SMOKE_PYTHON="$ROOT_DIR/.venv/Scripts/python.exe"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    RELEASE_SMOKE_PYTHON="$ROOT_DIR/.venv/bin/python"
else
    echo "release refused: worktree .venv Python is required for release manifest and startup smoke" >&2
    exit 1
fi

COMMIT_HASH="${EXPECTED_COMMIT:0:7}"
BRANCH_NAME="$(git branch --show-current)"
if [[ -z "$BRANCH_NAME" ]]; then
    BRANCH_NAME="detached"
fi
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FILE_STAMP="$(date -u +%Y%m%d-%H%M)"
PACKAGE_NAME="watch-assistant-${COMMIT_HASH}-${FILE_STAMP}.tar.gz"
OUTPUT_DIR="${RELEASE_OUTPUT_DIR:-$ROOT_DIR/release-archive/$(date -u +%Y%m%d)}"
TEMP_DIR="$(mktemp -d)"
PACKAGE_ROOT="$TEMP_DIR/watch-assistant-${COMMIT_HASH}"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

mkdir -p "$PACKAGE_ROOT" "$OUTPUT_DIR"
SOURCE_ARCHIVE="$TEMP_DIR/source.tar"
git archive --format=tar "$EXPECTED_COMMIT" -o "$SOURCE_ARCHIVE"
SOURCE_SHA256="$(sha256sum "$SOURCE_ARCHIVE" | awk '{print $1}')"
tar -xf "$SOURCE_ARCHIVE" -C "$PACKAGE_ROOT"

if ! command -v npm >/dev/null 2>&1; then
    echo "release refused: npm is required to build the frontend" >&2
    exit 1
fi
(
    cd "$PACKAGE_ROOT/frontend"
    npm ci
    npm run build
    rm -rf node_modules
)
if [[ ! -f "$PACKAGE_ROOT/frontend/dist/index.html" ]]; then
    echo "release refused: frontend production build did not produce index.html" >&2
    exit 1
fi

frontend_hash() {
    local directory="$1"
    (
        cd "$directory"
        find . -type f -print0 \
            | LC_ALL=C sort -z \
            | while IFS= read -r -d '' file; do
                sha256sum "$file"
            done
    ) | sha256sum | awk '{print $1}'
}

FRONTEND_SHA256="$(frontend_hash "$PACKAGE_ROOT/frontend/dist")"
cat > "$PACKAGE_ROOT/VERSION" <<EOF
commit=${EXPECTED_COMMIT}
build_time=${BUILD_TIME}
branch=${BRANCH_NAME}
EOF

"$RELEASE_SMOKE_PYTHON" "$ROOT_DIR/scripts/release_manifest.py" write-build \
    --output "$PACKAGE_ROOT/release-manifest.json" \
    --commit "$EXPECTED_COMMIT" \
    --short-commit "$COMMIT_HASH" \
    --source-sha256 "$SOURCE_SHA256" \
    --frontend-sha256 "$FRONTEND_SHA256" \
    --build-time "$BUILD_TIME" \
    --branch "$BRANCH_NAME"

PACKAGE_FILE="$TEMP_DIR/$PACKAGE_NAME"
tar -czf "$PACKAGE_FILE" -C "$TEMP_DIR" "watch-assistant-${COMMIT_HASH}"

SMOKE_DIR="$TEMP_DIR/release-smoke"
mkdir -p "$SMOKE_DIR"
tar -xzf "$PACKAGE_FILE" -C "$SMOKE_DIR"
"$RELEASE_SMOKE_PYTHON" \
    "$SMOKE_DIR/watch-assistant-${COMMIT_HASH}/scripts/release_startup_smoke.py" \
    --release-root "$SMOKE_DIR/watch-assistant-${COMMIT_HASH}"
cp "$PACKAGE_FILE" "$OUTPUT_DIR/$PACKAGE_NAME"
echo "$OUTPUT_DIR/$PACKAGE_NAME"
