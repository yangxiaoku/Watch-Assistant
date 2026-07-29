#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
else
    PYTHON_BIN="${PYTHON_BIN:-python}"
fi

COMMIT_HASH="$(git rev-parse --short=7 HEAD)"
RUN_STAMP="$(date -u +%Y%m%d-%H%M%S)"
TEMP_DIR="$(mktemp -d)"
EVIDENCE_DIR="$ROOT_DIR/evidence/${COMMIT_HASH}-${RUN_STAMP}"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

run_stage() {
    local name="$1"
    shift
    echo "==> ${name}"
    if ! "$@" 2>&1 | tee "$TEMP_DIR/${name}.log"; then
        echo "FAILED: ${name}" >&2
        exit 1
    fi
}

# Keep test roots separate.  The repository contains archived release trees and
# a few legacy unit helpers imported as top-level modules; a single repository
# wide collection makes both cases ambiguous.
run_stage unit-tests "$PYTHON_BIN" -m pytest -q tests/unit
for test_file in "$ROOT_DIR"/tests/integration/test_*.py; do
    test_name="${test_file##*/}"
    test_name="${test_name%.py}"
    run_stage "integration-${test_name}" "$PYTHON_BIN" -m pytest -q "$test_file"
done
run_stage contract-tests "$PYTHON_BIN" -m pytest -q tests/contracts
run_stage ruff "$PYTHON_BIN" -m ruff check src tests scripts
run_stage frontend-tests npm --prefix frontend test -- --run
run_stage frontend-build npm --prefix frontend run build

mkdir -p "$EVIDENCE_DIR"
cp "$TEMP_DIR"/*.log "$EVIDENCE_DIR/"
cat > "$EVIDENCE_DIR/SUMMARY.txt" <<EOF
commit=${COMMIT_HASH}
timestamp=${RUN_STAMP}
status=passed
EOF
echo "Verification passed; evidence: $EVIDENCE_DIR"
