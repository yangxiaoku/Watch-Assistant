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
  can run the offline gate with `--verify`. After a successful run it records
  lock-file fingerprints inside ignored dependency directories and reuses
  healthy `.venv` and `frontend/node_modules` on later runs; a changed lock
  file or failed import/check invalidates that fast path. On macOS, a bundled
  Python whose native library directory is separate is resolved automatically
  from the Python runtime when the Codex runtime exposes its OpenSSL 3
  libraries; `WATCH_ASSISTANT_NATIVE_LIBRARY_PATH` remains the explicit
  override. The same runtime discovery supplies Node.js when it is not on
  `PATH`.
- `scripts/acceptance_closure.py` is the single bounded acceptance entry point.
  Its `preview` phase performs production read-only inventory and organization
  plan generation. Its `execute` phase requires an explicit fixture
  confirmation, all existing C03 gates, separate one-shot authorizations, and
  stops on an uncertain result before the next write. Its `offline` phase
  validates the fake C03 lifecycle and organization/STRM integration contracts.
- The systemd unit keeps the external dependency virtualenv for execution but
  sets `PYTHONPATH=/opt/watch-assistant/current/src`, and the unit contract
  requires that release-source path. This keeps the running application and
  release smoke checks on the published source instead of a stale installed
  package from the shared virtualenv.
- Every stage writes a small public JSON report and `SUMMARY.json`; runner
  stdout and stderr are never copied into evidence.

## Daily loop

From a clean worktree based on the current publish ref:

```bash
# Usually auto-detected; set this only when the runtime is outside the known paths.
export WATCH_ASSISTANT_NATIVE_LIBRARY_PATH=/path/to/openssl-3/lib
# Usually auto-detected; set this only when Node is installed elsewhere.
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

The default `preview`, `execute`, and `offline` runtime gate accepts only the
current worktree `.venv`. A systemd release may use an external virtualenv only
when production acceptance is explicitly authorized and the interpreter is
declared, for example:

```bash
WATCH_ASSISTANT_PRODUCTION_ACCEPTANCE=1 \
WATCH_ASSISTANT_PRODUCTION_PYTHON=/opt/watch-assistant/venv/bin/python \
PYTHONPATH=/opt/watch-assistant/current/src \
/opt/watch-assistant/venv/bin/python \
  /opt/watch-assistant/current/scripts/acceptance_closure.py preview
```

The equivalent command-line authorization is
`--production-acceptance --production-python /opt/watch-assistant/venv/bin/python`.
The closure overwrites the child-stage `PYTHONPATH` with the current release's
`src` directory, so an inherited editable install or unrelated path cannot take
precedence. The summary records only the selected runtime class and authorization
source; it never records environment contents, credentials, or the supplied
interpreter path.

## Completion definition

This workstream is complete only when one `SUMMARY.json` contains successful
evidence for production read-only inventory, organization plan preview, a
manually confirmed and fully restored managed fixture, and STRM output under a
temporary root. Prowlarr live-source configuration, production deployment, and
real media-server playback remain separate external gates.
