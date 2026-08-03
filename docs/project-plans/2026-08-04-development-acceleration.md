# Development Acceleration And Acceptance Closure

## Why progress slowed

The main delay was coordination rather than one missing feature:

1. The desktop checkout was on an old branch with local changes while several
   worktrees contained overlapping integration attempts. Test results therefore
   did not consistently describe the same runnable code.
2. A shared editable Python installation could resolve imports from a different
   checkout. A green test run was not sufficient evidence that the current
   worktree had been tested.
3. Python and frontend dependency setup was repeated manually, and macOS often
   had no project-local `npm` or `.venv`.
4. Production Prowlarr and deployment prerequisites were treated as coding
   tasks. They are external gates and cannot be made green by adding more local
   features.
5. Inventory, planning, fixture writes, and STRM checks had separate commands
   and separate evidence. There was no single user-verifiable finish line.

## Implemented controls

- `codex/publish-main` remains the only release branch. Feature work starts from
  its current remote ref and uses one short-lived branch.
- `scripts/verify.sh` uses the active worktree `src` first and refuses to run
  without that worktree's `.venv`.
- `scripts/bootstrap_dev.sh` creates the local Python environment, installs the
  lock-derived constraints, installs frontend dependencies with npm or the
  bundled pnpm fallback, verifies that `cryptography` can load OpenSSL 3, and
  can run the offline gate with `--verify`. On macOS, a bundled Python whose
  native library directory is separate must be started with
  `WATCH_ASSISTANT_NATIVE_LIBRARY_PATH` pointing at that OpenSSL 3 `lib`
  directory.
- `scripts/acceptance_closure.py` is the single bounded acceptance entry point.
  Its `preview` phase performs production read-only inventory and organization
  plan generation. Its `execute` phase requires an explicit fixture
  confirmation, all existing C03 gates, separate one-shot authorizations, and
  stops on an uncertain result before the next write. Its `offline` phase
  validates the fake C03 lifecycle and organization/STRM integration contracts.
- Every stage writes a small public JSON report and `SUMMARY.json`; runner
  stdout and stderr are never copied into evidence.

## Daily loop

From a clean worktree based on the current publish ref:

```bash
# Only needed when the selected Python runtime does not bundle OpenSSL 3.
export WATCH_ASSISTANT_NATIVE_LIBRARY_PATH=/path/to/openssl-3/lib
# Only needed when Node is installed outside PATH (for example, a bundled runtime).
export WATCH_ASSISTANT_FRONTEND_NODE=/path/to/node
bash scripts/bootstrap_dev.sh --verify

# Check the complete acceptance plan without contacting 115.
.venv/bin/python scripts/acceptance_closure.py dry-run --output-dir /tmp/watch-assistant-acceptance

# Run the offline closure after an explicit fixture confirmation.
.venv/bin/python scripts/acceptance_closure.py offline \
  --confirm-fixture \
  --output-dir /tmp/watch-assistant-acceptance
```

The live `preview` and `execute` modes require explicit IDs, a managed scope
file, the persistent cookie path, and the existing one-shot authorization
files. They must be run only after the operator has checked the scope and the
authorization artifacts. No mode deploys, restarts systemd, permanently
deletes, or changes a production root automatically.

## Completion definition

This workstream is complete only when one `SUMMARY.json` contains successful
evidence for production read-only inventory, organization plan preview, a
manually confirmed and fully restored managed fixture, and STRM output under a
temporary root. Prowlarr live-source configuration, production deployment, and
real media-server playback remain separate external gates.
