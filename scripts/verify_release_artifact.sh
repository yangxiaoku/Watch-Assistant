#!/usr/bin/env bash
set -euo pipefail

PACKAGE_FILE="${1:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"
EXPECTED_COMMIT="${2:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"
OUTPUT_DIR="${3:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "release artifact refused: expected commit is not a full SHA" >&2
    exit 1
fi
EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"
if [[ ! -f "$PACKAGE_FILE" ]]; then
    echo "release artifact refused: package is missing" >&2
    exit 1
fi

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"
if [[ "$(git rev-parse HEAD)" != "$EXPECTED_COMMIT" ]]; then
    echo "release artifact refused: checked-out commit does not match expected SHA" >&2
    exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "release artifact refused: working tree is not clean" >&2
    exit 1
fi

if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    RELEASE_SMOKE_PYTHON="$ROOT_DIR/.venv/Scripts/python.exe"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    RELEASE_SMOKE_PYTHON="$ROOT_DIR/.venv/bin/python"
else
    echo "release artifact refused: worktree .venv Python is required for manifest validation and startup smoke" >&2
    exit 1
fi

PACKAGE_NAME="$(basename "$PACKAGE_FILE")"
SHORT_COMMIT="${EXPECTED_COMMIT:0:7}"
PACKAGE_PATTERN="^watch-assistant-${SHORT_COMMIT}-[0-9]{8}-[0-9]{4}\.tar\.gz$"
if [[ ! "$PACKAGE_NAME" =~ $PACKAGE_PATTERN ]]; then
    echo "release artifact refused: package name does not identify the expected commit" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

PACKAGE_ROOT="watch-assistant-${SHORT_COMMIT}"
TAR_LIST="$TEMP_DIR/tar.list"
tar -tzf "$PACKAGE_FILE" > "$TAR_LIST"
while IFS= read -r entry; do
    case "$entry" in
        "$PACKAGE_ROOT/"*) ;;
        *)
            echo "release artifact refused: archive entry is outside the release root" >&2
            exit 1
            ;;
    esac
    case "$entry" in
        /*|*"../"*|".."*)
            echo "release artifact refused: archive contains unsafe path" >&2
            exit 1
            ;;
    esac
done < "$TAR_LIST"

REQUIRED_ENTRIES=(
    "$PACKAGE_ROOT/VERSION"
    "$PACKAGE_ROOT/release-manifest.json"
    "$PACKAGE_ROOT/frontend/dist/index.html"
    "$PACKAGE_ROOT/src/watch_assistant/app.py"
    "$PACKAGE_ROOT/src/watch_assistant/release_metadata.py"
    "$PACKAGE_ROOT/scripts/release_manifest.py"
    "$PACKAGE_ROOT/scripts/release_startup_smoke.py"
)
for required in "${REQUIRED_ENTRIES[@]}"; do
    if ! grep -Fxq "$required" "$TAR_LIST"; then
        echo "release artifact refused: required archive entry is missing" >&2
        exit 1
    fi
done
if grep -Eq '(^|/)node_modules(/|$)|(^|/)\.git(/|$)' "$TAR_LIST"; then
    echo "release artifact refused: archive contains build or git metadata" >&2
    exit 1
fi

tar -xzf "$PACKAGE_FILE" -C "$TEMP_DIR"
if find "$TEMP_DIR/$PACKAGE_ROOT" -type l -print -quit | grep -q .; then
    echo "release artifact refused: archive contains symlinks" >&2
    exit 1
fi
VERSION_FILE="$TEMP_DIR/$PACKAGE_ROOT/VERSION"
if [[ ! -f "$VERSION_FILE" ]]; then
    echo "release artifact refused: VERSION is missing" >&2
    exit 1
fi
VERSION_COMMIT="$(awk -F= '$1 == "commit" { print $2; exit }' "$VERSION_FILE")"
if [[ "$VERSION_COMMIT" != "$EXPECTED_COMMIT" ]]; then
    echo "release artifact refused: VERSION does not identify the expected commit" >&2
    exit 1
fi

MANIFEST_FILE="$TEMP_DIR/$PACKAGE_ROOT/release-manifest.json"
SOURCE_SHA256="$(git archive --format=tar "$EXPECTED_COMMIT" | sha256sum | awk '{print $1}')"

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

FRONTEND_SHA256="$(frontend_hash "$TEMP_DIR/$PACKAGE_ROOT/frontend/dist")"
"$RELEASE_SMOKE_PYTHON" "$TEMP_DIR/$PACKAGE_ROOT/scripts/release_manifest.py" verify-build \
    --manifest "$MANIFEST_FILE" \
    --expected-commit "$EXPECTED_COMMIT" \
    --expected-short-commit "$SHORT_COMMIT" \
    --expected-source-sha256 "$SOURCE_SHA256" \
    --expected-frontend-sha256 "$FRONTEND_SHA256"

PACKAGE_SHA256="$(sha256sum "$PACKAGE_FILE" | awk '{print $1}')"
PACKAGE_SIZE="$(stat -c '%s' "$PACKAGE_FILE")"
mkdir -p "$OUTPUT_DIR"
cp "$PACKAGE_FILE" "$OUTPUT_DIR/$PACKAGE_NAME"
cp "$VERSION_FILE" "$OUTPUT_DIR/VERSION"
printf '%s  %s\n' "$PACKAGE_SHA256" "$PACKAGE_NAME" > "$OUTPUT_DIR/SHA256SUMS"

"$RELEASE_SMOKE_PYTHON" "$TEMP_DIR/$PACKAGE_ROOT/scripts/release_manifest.py" write-artifact \
    --output "$OUTPUT_DIR/release-manifest.json" \
    --artifact "$PACKAGE_NAME" \
    --commit "$EXPECTED_COMMIT" \
    --short-commit "$SHORT_COMMIT" \
    --package-sha256 "$PACKAGE_SHA256" \
    --package-size-bytes "$PACKAGE_SIZE" \
    --source-sha256 "$SOURCE_SHA256" \
    --frontend-sha256 "$FRONTEND_SHA256" \
    --version-file "$VERSION_FILE"

(
    cd "$OUTPUT_DIR"
    sha256sum --check SHA256SUMS
)

"$RELEASE_SMOKE_PYTHON" \
    "$TEMP_DIR/$PACKAGE_ROOT/scripts/release_startup_smoke.py" \
    --release-root "$TEMP_DIR/$PACKAGE_ROOT"

echo "release artifact verified: $PACKAGE_NAME"
echo "release artifact SHA256: $PACKAGE_SHA256"
