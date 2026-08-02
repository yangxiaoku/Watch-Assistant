#!/usr/bin/env bash
set -euo pipefail

PACKAGE_FILE="${1:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"
EXPECTED_COMMIT="${2:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"
OUTPUT_DIR="${3:?usage: verify_release_artifact.sh PACKAGE_FILE EXPECTED_COMMIT OUTPUT_DIR}"

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "release artifact refused: expected commit is not a full SHA" >&2
    exit 1
fi
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

tar -xzf "$PACKAGE_FILE" -C "$TEMP_DIR"
VERSION_FILE="$TEMP_DIR/$PACKAGE_ROOT/VERSION"
if [[ ! -f "$VERSION_FILE" ]]; then
    echo "release artifact refused: VERSION is missing" >&2
    exit 1
fi
VERSION_COMMIT="$(awk -F= '$1 == "commit" { print $2; exit }' "$VERSION_FILE")"
if [[ "$VERSION_COMMIT" != "$SHORT_COMMIT" && "$VERSION_COMMIT" != "$EXPECTED_COMMIT" ]]; then
    echo "release artifact refused: VERSION does not identify the expected commit" >&2
    exit 1
fi

PACKAGE_SHA256="$(sha256sum "$PACKAGE_FILE" | awk '{print $1}')"
PACKAGE_SIZE="$(stat -c '%s' "$PACKAGE_FILE")"
mkdir -p "$OUTPUT_DIR"
cp "$PACKAGE_FILE" "$OUTPUT_DIR/$PACKAGE_NAME"
cp "$VERSION_FILE" "$OUTPUT_DIR/VERSION"
printf '%s  %s\n' "$PACKAGE_SHA256" "$PACKAGE_NAME" > "$OUTPUT_DIR/SHA256SUMS"

jq -n \
    --arg artifact "$PACKAGE_NAME" \
    --arg commit "$EXPECTED_COMMIT" \
    --arg short_commit "$SHORT_COMMIT" \
    --arg package_sha256 "$PACKAGE_SHA256" \
    --arg package_size "$PACKAGE_SIZE" \
    --arg version "$(<"$VERSION_FILE")" \
    '{
      schema_version: 1,
      artifact: $artifact,
      commit: $commit,
      short_commit: $short_commit,
      package_sha256: $package_sha256,
      package_size_bytes: ($package_size | tonumber),
      version: $version
    }' > "$OUTPUT_DIR/release-manifest.json"

(
    cd "$OUTPUT_DIR"
    sha256sum --check SHA256SUMS
)

echo "release artifact verified: $PACKAGE_NAME"
echo "release artifact SHA256: $PACKAGE_SHA256"
