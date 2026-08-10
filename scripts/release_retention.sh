#!/usr/bin/env bash
# Keep the current systemd release and the N most recent release directories,
# removing older extracted releases and stray tarballs.
#
#   RELEASES_ROOT=/opt/watch-assistant/releases KEEP=4 release_retention.sh
#
# Never touches `current`, `release.env`, data, or state directories. Run as
# root (releases are root-owned). Use --dry-run to preview deletions.

set -euo pipefail

RELEASES_ROOT="${RELEASES_ROOT:-/opt/watch-assistant/releases}"
KEEP="${KEEP:-4}"
DRY_RUN="${1:-}"

# 入参校验:任何非 --dry-run 参数都视为笔误,拒绝执行真实删除;
# KEEP 必须是正整数,避免 KEEP=abc 时算术求值为 0 误删全部。
if [[ -n "$DRY_RUN" && "$DRY_RUN" != "--dry-run" ]]; then
  echo "release retention refused: unknown argument '$DRY_RUN' (use --dry-run for preview)" >&2
  exit 2
fi
if [[ ! "$KEEP" =~ ^[0-9]+$ || "$KEEP" -lt 1 ]]; then
  echo "release retention refused: KEEP must be a positive integer (got '$KEEP')" >&2
  exit 2
fi

if [[ ! -d "$RELEASES_ROOT" ]]; then
  echo "release retention refused: $RELEASES_ROOT does not exist" >&2
  exit 2
fi

cd "$RELEASES_ROOT"

# Directories only (never tarballs).
dirs=$(ls -dt watch-assistant-* 2>/dev/null | grep -v '\.tar\.gz$' || true)
if [[ -z "$dirs" ]]; then
  echo "release retention: no release directories to retain"
  exit 0
fi

current=$(readlink current 2>/dev/null || true)
current_name=$(basename "$current" 2>/dev/null || true)

keep_count=0
for d in $dirs; do
  if [[ "$d" == "$current_name" ]]; then
    echo "keep (current): $d"
    continue
  fi
  if (( keep_count < KEEP )); then
    echo "keep: $d"
    keep_count=$((keep_count + 1))
    continue
  fi
  echo "delete: $d"
  if [[ "$DRY_RUN" != "--dry-run" ]]; then
    rm -rf "$d"
  fi
done

# Stray release tarballs are redundant with CI artifacts.
tarballs=$(find . -maxdepth 1 -name 'watch-assistant-*.tar.gz' -print 2>/dev/null || true)
if [[ -n "$tarballs" ]]; then
  for t in $tarballs; do
    echo "delete: $t"
    if [[ "$DRY_RUN" != "--dry-run" ]]; then
      rm -f "$t"
    fi
  done
fi

echo "release retention: done (KEEP=${KEEP}, current=${current_name:-none})"
