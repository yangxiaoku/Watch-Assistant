#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "release refused: working tree is not clean" >&2
    exit 1
fi

COMMIT_HASH="$(git rev-parse --short=7 HEAD)"
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
git archive HEAD | tar -x -C "$PACKAGE_ROOT"
cat > "$PACKAGE_ROOT/VERSION" <<EOF
commit=${COMMIT_HASH}
build_time=${BUILD_TIME}
branch=${BRANCH_NAME}
EOF

tar -czf "$OUTPUT_DIR/$PACKAGE_NAME" -C "$TEMP_DIR" "watch-assistant-${COMMIT_HASH}"
echo "$OUTPUT_DIR/$PACKAGE_NAME"
