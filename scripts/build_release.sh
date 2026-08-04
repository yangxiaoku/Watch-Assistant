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

RELEASE_BRANCH="codex/publish-main"
BRANCH_NAME="$(git branch --show-current)"
if [[ "$BRANCH_NAME" != "$RELEASE_BRANCH" ]]; then
    echo "release refused: packages must be built from ${RELEASE_BRANCH}" >&2
    exit 1
fi
REMOTE_REF="refs/heads/${RELEASE_BRANCH}"
if ! REMOTE_PUBLISH_COMMIT="$(
    git ls-remote --exit-code origin "$REMOTE_REF" 2>/dev/null |
        awk -v expected_ref="$REMOTE_REF" 'NF == 2 && $2 == expected_ref { print $1 }'
)"; then
    echo "release refused: remote ${RELEASE_BRANCH} could not be verified" >&2
    exit 1
fi
REMOTE_PUBLISH_COMMIT="$(printf '%s' "$REMOTE_PUBLISH_COMMIT" | tr '[:upper:]' '[:lower:]')"
if [[ ! "$REMOTE_PUBLISH_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "release refused: remote ${RELEASE_BRANCH} returned an invalid commit" >&2
    exit 1
fi
if [[ "$REMOTE_PUBLISH_COMMIT" != "$EXPECTED_COMMIT" ]]; then
    echo "release refused: expected commit is not the current remote ${RELEASE_BRANCH}" >&2
    exit 1
fi
if ! PUBLISH_COMMIT="$(git rev-parse --verify "refs/remotes/origin/${RELEASE_BRANCH}" 2>/dev/null)"; then
    echo "release refused: origin/${RELEASE_BRANCH} is unavailable" >&2
    exit 1
fi
PUBLISH_COMMIT="$(printf '%s' "$PUBLISH_COMMIT" | tr '[:upper:]' '[:lower:]')"
if [[ "$PUBLISH_COMMIT" != "$EXPECTED_COMMIT" ]]; then
    echo "release refused: expected commit is not the latest origin/${RELEASE_BRANCH}" >&2
    exit 1
fi

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

FRONTEND_PM="${WATCH_ASSISTANT_FRONTEND_PM:-}"
if [[ -z "$FRONTEND_PM" ]] && command -v npm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v npm)"
fi
if [[ -z "$FRONTEND_PM" ]] && command -v pnpm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v pnpm)"
fi
if [[ -z "$FRONTEND_PM" ]]; then
    echo "release refused: npm or pnpm is required to build the frontend" >&2
    exit 1
fi

case "$(basename "$FRONTEND_PM")" in
    npm)
        (
            cd "$PACKAGE_ROOT/frontend"
            "$FRONTEND_PM" ci
            "$FRONTEND_PM" run build
            rm -rf node_modules
        )
        ;;
    pnpm)
        (
            cd "$PACKAGE_ROOT"
            # The source tree has an npm lockfile; avoid creating a second pnpm lockfile.
            "$FRONTEND_PM" --dir frontend install --no-lockfile
            "$FRONTEND_PM" --dir frontend run build
            rm -rf frontend/node_modules
        )
        ;;
    *)
        echo "release refused: WATCH_ASSISTANT_FRONTEND_PM must point to npm or pnpm" >&2
        exit 1
        ;;
esac
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
UNIT_SHA256="$(sha256sum "$PACKAGE_ROOT/deploy/watch-assistant.service" | awk '{print $1}')"
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
    --unit-sha256 "$UNIT_SHA256" \
    --build-time "$BUILD_TIME" \
    --branch "$BRANCH_NAME"

PACKAGE_FILE="$TEMP_DIR/$PACKAGE_NAME"
# Keep release ownership stable across macOS BSD tar and Linux GNU tar.
tar --owner=0 --group=0 -czf "$PACKAGE_FILE" -C "$TEMP_DIR" "watch-assistant-${COMMIT_HASH}"

SMOKE_DIR="$TEMP_DIR/release-smoke"
mkdir -p "$SMOKE_DIR"
tar -xzf "$PACKAGE_FILE" -C "$SMOKE_DIR"
"$RELEASE_SMOKE_PYTHON" \
    "$SMOKE_DIR/watch-assistant-${COMMIT_HASH}/scripts/release_startup_smoke.py" \
    --release-root "$SMOKE_DIR/watch-assistant-${COMMIT_HASH}"
cp "$PACKAGE_FILE" "$OUTPUT_DIR/$PACKAGE_NAME"
echo "$OUTPUT_DIR/$PACKAGE_NAME"
