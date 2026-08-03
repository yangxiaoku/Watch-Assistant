#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

if [[ -n "${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH:-}" ]]; then
    export DYLD_LIBRARY_PATH="${WATCH_ASSISTANT_NATIVE_LIBRARY_PATH}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi

# Prefer the checkout under test when the shared worktree virtualenv has an
# editable install from another checkout.
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
    echo "verification refused: worktree .venv Python is required" >&2
    exit 2
fi

COMMIT_HASH="$(git rev-parse --short=7 HEAD)"
RUN_STAMP="$(date -u +%Y%m%d-%H%M%S)"
TEMP_DIR="$(mktemp -d)"
EVIDENCE_DIR="$ROOT_DIR/evidence/${COMMIT_HASH}-${RUN_STAMP}"

write_evidence() {
    local status="$1"
    local exit_code="$2"

    mkdir -p "$EVIDENCE_DIR"
    if compgen -G "$TEMP_DIR/*.log" >/dev/null; then
        cp "$TEMP_DIR"/*.log "$EVIDENCE_DIR/"
    fi
    cat > "$EVIDENCE_DIR/SUMMARY.txt" <<EOF
commit=${COMMIT_HASH}
timestamp=${RUN_STAMP}
status=${status}
exit_code=${exit_code}
EOF
}

cleanup() {
    rm -rf "$TEMP_DIR"
}

finish() {
    local exit_code=$?
    # Preserve the original verification result even if evidence storage is
    # unavailable. Failed runs need their logs for CI diagnosis as well.
    set +e
    if (( exit_code == 0 )); then
        write_evidence passed 0
    else
        write_evidence failed "$exit_code"
    fi
    cleanup
    trap - EXIT
    exit "$exit_code"
}
trap finish EXIT

run_stage() {
    local name="$1"
    shift
    echo "==> ${name}"
    if ! "$@" 2>&1 | tee "$TEMP_DIR/${name}.log"; then
        echo "FAILED: ${name}" >&2
        exit 1
    fi
}

wait_for_parallel_batch() {
    local failed=0
    local pid
    for pid in "$@"; do
        if ! wait "$pid"; then
            failed=1
        fi
    done
    return "$failed"
}

run_parallel_pytest_files() {
    local name="$1"
    local test_root="$2"
    local group_size="$3"
    local max_parallel="${VERIFY_PARALLEL_JOBS:-4}"
    local stage_failed=0
    local shard=0
    local start=0
    local count
    local test_file
    local log_file
    local total
    local -a test_files=()
    local -a pids=()

    case "$max_parallel" in
        ''|*[!0-9]*|0)
            echo "verification refused: VERIFY_PARALLEL_JOBS must be a positive integer" >&2
            return 2
            ;;
    esac

    # Keep test roots separate. The repository contains archived release trees
    # and legacy helpers imported as top-level modules.
    while IFS= read -r test_file; do
        test_files+=("$test_file")
    done < <(find "$test_root" -maxdepth 1 -type f -name 'test_*.py' -print | sort)

    total="${#test_files[@]}"
    while (( start < total )); do
        count="$group_size"
        if (( start + count > total )); then
            count=$((total - start))
        fi
        log_file="$TEMP_DIR/${name}-${shard}.log"
        echo "==> ${name}-${shard}: ${test_files[*]:start:count}"
        "$PYTHON_BIN" -m pytest -q "${test_files[@]:start:count}" >"$log_file" 2>&1 &
        pids+=("$!")
        shard=$((shard + 1))
        start=$((start + count))

        if (( ${#pids[@]} >= max_parallel )); then
            if ! wait_for_parallel_batch "${pids[@]}"; then
                stage_failed=1
            fi
            pids=()
        fi
    done

    if (( ${#pids[@]} > 0 )); then
        if ! wait_for_parallel_batch "${pids[@]}"; then
            stage_failed=1
        fi
    fi

    for log_file in "$TEMP_DIR"/"${name}"-*.log; do
        echo "---- ${log_file##*/} ----"
        cat "$log_file"
    done

    if (( stage_failed != 0 )); then
        echo "FAILED: ${name}" >&2
        return 1
    fi
}

# Unit files are grouped to reduce interpreter startup overhead. Integration
# files stay separate so each contract retains its own bounded timeout.
run_parallel_pytest_files unit-tests tests/unit 8
run_parallel_pytest_files integration-tests tests/integration 1
run_stage contract-tests "$PYTHON_BIN" -m pytest -q tests/contracts
run_stage ruff "$PYTHON_BIN" -m ruff check src tests scripts

if [[ -n "${WATCH_ASSISTANT_FRONTEND_NODE:-}" ]]; then
    FRONTEND_PM=""
elif [[ -n "${WATCH_ASSISTANT_FRONTEND_PM:-}" ]]; then
    FRONTEND_PM="$WATCH_ASSISTANT_FRONTEND_PM"
elif command -v npm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v npm)"
elif command -v pnpm >/dev/null 2>&1; then
    FRONTEND_PM="$(command -v pnpm)"
else
    FRONTEND_PM=""
fi

if [[ -n "$FRONTEND_PM" ]]; then
    run_stage frontend-tests "$FRONTEND_PM" --prefix frontend test -- --run
    run_stage frontend-build "$FRONTEND_PM" --prefix frontend run build
else
    if [[ -n "${WATCH_ASSISTANT_FRONTEND_NODE:-}" ]]; then
        FRONTEND_NODE="$WATCH_ASSISTANT_FRONTEND_NODE"
    elif command -v node >/dev/null 2>&1; then
        FRONTEND_NODE="$(command -v node)"
    else
        echo "verification refused: npm, pnpm, or node is required for frontend checks" >&2
        exit 2
    fi
    if [[ ! -f "$ROOT_DIR/frontend/node_modules/vitest/vitest.mjs" || \
        ! -f "$ROOT_DIR/frontend/node_modules/vite/bin/vite.js" ]]; then
        echo "verification refused: frontend dependencies are required for direct node checks" >&2
        exit 2
    fi
    (
        cd "$ROOT_DIR/frontend"
        run_stage frontend-tests "$FRONTEND_NODE" node_modules/vitest/vitest.mjs run
        run_stage frontend-build "$FRONTEND_NODE" node_modules/vite/bin/vite.js build
    )
fi
echo "Verification passed; evidence: $EVIDENCE_DIR"
