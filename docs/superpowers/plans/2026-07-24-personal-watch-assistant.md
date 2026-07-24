# Personal Watch Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted FastAPI/Vue service and TMDB userscript that searches the existing PanSou instance, normalizes resources, and submits idempotent 115 tasks through a verified TgtoDrive adapter.

**Architecture:** One runtime container serves FastAPI and the compiled Vue app, with one SQLite WAL database and one background worker. PanSou, TMDB, and TgtoDrive are isolated behind adapters. The browser submits only TMDB IDs and resource IDs; all sensitive links and credentials remain server-side.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async + aiosqlite, Pydantic Settings, HTTPX, Fernet encryption, Vue 3, TypeScript, Vite, Vitest, Pytest, Playwright, Docker Compose.

---

## File Map

Create these focused units:

- `pyproject.toml`: Python dependencies, test and lint commands.
- `src/watch_assistant/config.py`: environment configuration and secret validation.
- `src/watch_assistant/db.py`: async engine, session factory, migrations/bootstrap.
- `src/watch_assistant/models.py`: SQLAlchemy tables and state enums.
- `src/watch_assistant/schemas.py`: Pydantic API and adapter DTOs.
- `src/watch_assistant/crypto.py`: Fernet encryption/decryption helpers.
- `src/watch_assistant/adapters/tmdb.py`: TMDB client.
- `src/watch_assistant/adapters/pansou.py`: PanSou client.
- `src/watch_assistant/adapters/tgto.py`: verified TgtoDrive client contract.
- `src/watch_assistant/services/normalize.py`: resource parsing, canonical keys, dedupe, sorting.
- `src/watch_assistant/services/search.py`: cache/resource persistence and search orchestration.
- `src/watch_assistant/services/tasks.py`: idempotency, state transitions, retry rules.
- `src/watch_assistant/worker.py`: single SQLite-leased task worker.
- `src/watch_assistant/api/auth.py`: Web session and userscript token endpoints.
- `src/watch_assistant/api/search.py`: movie and search routes.
- `src/watch_assistant/api/tasks.py`: task and history routes.
- `src/watch_assistant/app.py`: FastAPI lifespan, router registration, static files.
- `tests/unit/`: pure normalization, crypto, state-machine, and auth tests.
- `tests/integration/`: adapter contract tests with HTTP fakes.
- `scripts/tgto_contract_probe.py`: read-only discovery of TgtoDrive candidate endpoints.
- `scripts/tgto_contract_check.py`: no-side-effect configured endpoint smoke check.
- `frontend/package.json`, `frontend/vite.config.ts`, `frontend/src/`: Vue app and userscript source.
- `frontend/tests/`: Vitest component tests and Playwright fixtures.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`: runtime and deployment.

### Task 1: Bootstrap the Repository and Test Harness

**Files:** Create `pyproject.toml`, `src/watch_assistant/__init__.py`, `tests/unit/test_bootstrap.py`, `frontend/package.json`, `frontend/vite.config.ts`.

- [ ] **Step 1: Write the failing Python bootstrap test**

```python
def test_package_imports():
    import watch_assistant

    assert watch_assistant.__name__ == "watch_assistant"
```

- [ ] **Step 2: Add the minimal package and dependencies**

Declare FastAPI, Uvicorn, SQLAlchemy, aiosqlite, httpx, pydantic-settings, cryptography, pwdlib[argon2], python-multipart, pytest, pytest-asyncio, respx, and ruff in `pyproject.toml`. Configure `pytest` with `asyncio_mode = "auto"`.

- [ ] **Step 3: Run the bootstrap checks**

Run: `python -m pytest tests/unit/test_bootstrap.py -q`  
Expected: `1 passed`.

- [ ] **Step 4: Add the frontend package**

Pin Vue 3, TypeScript, Vite, `@lucide/vue`, Vitest, `@vue/test-utils`, and Playwright. Add scripts `dev`, `build`, `test`, and `test:e2e`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src tests frontend
git commit -m "chore: bootstrap watch assistant"
```

### Task 2: Verify the TgtoDrive Contract Before Building Push Logic

**Files:** Create `scripts/tgto_contract_probe.py`, `scripts/tgto_contract_check.py`, `tests/integration/test_tgto_contract.py`, `config/tgto-contract.example.json`.

- [ ] **Step 1: Implement the read-only route probe**

The probe must read `TGTO_BASE_URL`, `TGTO_WEB_USER`, and `TGTO_WEB_PASSWORD`, POST credentials only to the existing `/api/login`, fetch `/static/script.js`, extract API path strings beginning with `/api/`, and print only paths and HTTP status codes. Never print environment values or cookies.

```python
paths = sorted(set(re.findall(r"/api/[A-Za-z0-9_/?${}.=&-]+", script_text)))
for path in paths:
    if any(word in path.lower() for word in ("115", "transfer", "download", "token")):
        print(path)
```

- [ ] **Step 2: Run discovery without side effects**

Run: `python scripts/tgto_contract_probe.py` with a read-only management account.  
Expected: candidate paths are printed; no task is submitted and no database file is modified.

- [ ] **Step 3: Record only verified routes**

Copy confirmed method/path pairs into `config/tgto-contract.json` using the example schema. If no route can submit a magnet/share and return or query a remote reference, record `"supported": false` and stop before implementing real push behavior.

Define the probe result used by the next step:

```python
@dataclass(frozen=True)
class ContractResult:
    login_ok: bool
    submit_supported: bool
    status_supported: bool
    uncertain_fallback: bool
```

Implement `async def run_contract_check() -> ContractResult` in `scripts/tgto_contract_check.py`; it reads the contract JSON, performs login plus harmless GET/metadata checks, and sets `uncertain_fallback=True` only when submission is supported but status lookup is not.

- [ ] **Step 4: Add the contract test**

```python
@pytest.mark.integration
async def test_configured_tgto_contract_is_readable():
    result = await run_contract_check()
    assert result.login_ok is True
    assert result.submit_supported is True
    assert result.status_supported is True or result.uncertain_fallback is True
```

- [ ] **Step 5: Commit the verified contract or the explicit unsupported gate**

```bash
git add scripts config tests/integration
git commit -m "test: verify TgtoDrive contract"
```

### Task 3: Add Configuration, Encryption, Database Models, and Retention Rules

**Files:** Create `src/watch_assistant/config.py`, `src/watch_assistant/crypto.py`, `src/watch_assistant/db.py`, `src/watch_assistant/models.py`, `src/watch_assistant/schemas.py`, `tests/unit/test_crypto.py`, `tests/unit/test_models.py`.

- [ ] **Step 1: Test encryption and state values**

```python
def test_secret_round_trip(crypto):
    token = "magnet:?xt=urn:btih:ABC"
    assert crypto.decrypt(crypto.encrypt(token)) == token


def test_task_states_are_explicit():
    assert {s.value for s in TaskState} == {
        "queued",
        "submitting",
        "accepted",
        "needs_auth",
        "failed",
        "uncertain",
    }
```

Declare `TaskState` in `models.py` as a string enum and expose a pytest `crypto` fixture from `tests/unit/conftest.py` that constructs the configured Fernet helper.

- [ ] **Step 2: Implement settings validation**

Require `DATABASE_URL`, `ENCRYPTION_KEY`, `TMDB_API_KEY`, `WEB_PASSWORD_HASH`, `SCRIPT_TOKEN_HASH`, `PANSOU_BASE_URL`, and `TGTO_BASE_URL`. Fail startup with a named error when any required secret is absent.

- [ ] **Step 3: Implement the three tables**

`resources` stores encrypted URL/password, `canonical_key`, nullable `size_bytes`/`seeders`, source, timestamps, and metadata. `search_cache` stores only `resource_ids_json` and `warnings_json`. `tasks` stores encrypted snapshots, state, attempts, remote reference, error fields, lease owner, and lease expiry.

- [ ] **Step 4: Add retention behavior**

Keep resources for 30 days; do not delete resources referenced by non-terminal tasks. Keep terminal task history for 90 days. Add a daily cleanup function and test it against a resource referenced by `uncertain`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/unit/test_crypto.py tests/unit/test_models.py -q`  
Expected: all tests pass. Commit with `feat: add encrypted sqlite persistence`.

### Task 4: Implement TMDB, PanSou, and Resource Normalization Adapters

**Files:** Create `src/watch_assistant/adapters/tmdb.py`, `src/watch_assistant/adapters/pansou.py`, `src/watch_assistant/services/normalize.py`, `tests/unit/test_normalize.py`, `tests/integration/test_pansou_adapter.py`.

- [ ] **Step 1: Add fixture-driven failing tests**

```python
def test_magnet_results_dedupe_by_infohash(pansou_fixture):
    resources = normalize_pansou(pansou_fixture)
    magnets = [r for r in resources if r.kind == "magnet"]
    assert len(magnets) == 1
    assert magnets[0].size_bytes is None
    assert magnets[0].seeders is None
```

- [ ] **Step 2: Implement strict parsing**

Accept only `magnet:` URLs and configured 115 share domains. Derive magnet canonical keys from normalized lowercase infohash. Derive 115 keys from the share ID plus normalized domain. Preserve `source`, `note`, and `datetime` as display metadata; map missing size/seeders to `None`.

- [ ] **Step 3: Implement TMDB query generation**

Fetch title, original title, and release year. Generate at most two unique queries, preferring `title + year` and `original_title + year` when distinct.

- [ ] **Step 4: Test timeout and stale-cache behavior with HTTP fakes**

Run: `python -m pytest tests/unit/test_normalize.py tests/integration/test_pansou_adapter.py -q`  
Expected: normalization, timeout, and response-shape tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/watch_assistant/adapters src/watch_assistant/services/normalize.py tests
git commit -m "feat: add tmdb pansou adapters"
```

### Task 5: Build Search Persistence and API Routes

**Files:** Create `src/watch_assistant/services/search.py`, `src/watch_assistant/api/search.py`, `src/watch_assistant/app.py`, `tests/unit/test_search_service.py`, `tests/integration/test_search_api.py`.

- [ ] **Step 1: Test the response contract**

```python
async def test_search_persists_resources_and_returns_ids(client):
    response = await client.post("/api/v1/search", json={"tmdb_id": 12345})
    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["resource_id"].startswith("res_")
    assert "warnings" in body
```

- [ ] **Step 2: Implement cache lookup and persistence**

Use `cache_key = tmdb_id + normalized query version`. Return fresh cache immediately, use stale cache only when PanSou fails and it is less than 24 hours old, and persist normalized resources before returning IDs.

- [ ] **Step 3: Register movie and search routes**

Implement `GET /api/v1/movies/{tmdb_id}` and `POST /api/v1/search`. Never include encrypted fields in response schemas.

- [ ] **Step 4: Run the API tests**

Run: `python -m pytest tests/integration/test_search_api.py -q`  
Expected: `200` for a valid search and `502` with stale-cache warning when the upstream is unavailable and no cache exists.

- [ ] **Step 5: Commit**

```bash
git add src/watch_assistant/api src/watch_assistant/services/search.py tests
git commit -m "feat: add search api and resource cache"
```

### Task 6: Implement Task State Machine and SQLite-Leased Worker

**Files:** Create `src/watch_assistant/services/tasks.py`, `src/watch_assistant/worker.py`, `src/watch_assistant/api/tasks.py`, `tests/unit/test_tasks.py`, `tests/integration/test_worker_recovery.py`.

- [ ] **Step 1: Write state transition tests**

```python
def test_submitting_without_remote_confirmation_becomes_uncertain():
    task = Task(state=TaskState.SUBMITTING)
    recover_after_restart(task, remote_status=None)
    assert task.state == TaskState.UNCERTAIN


def test_duplicate_resource_reuses_recent_task():
    existing = make_task(state=TaskState.ACCEPTED, age_hours=2)
    assert choose_existing_task([existing], resource_id=existing.resource_id)
```

Implement these testable service functions in `services/tasks.py`:

```python
def recover_after_restart(task: Task, remote_status: RemoteStatus | None) -> None:
    task.lease_owner = None
    task.lease_expires_at = None
    if remote_status == RemoteStatus.ACCEPTED:
        task.state = TaskState.ACCEPTED
    elif remote_status == RemoteStatus.NEEDS_AUTH:
        task.state = TaskState.NEEDS_AUTH
    else:
        task.state = TaskState.UNCERTAIN


def choose_existing_task(tasks: Sequence[Task], resource_id: str) -> Task | None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    reusable = {TaskState.QUEUED, TaskState.SUBMITTING, TaskState.ACCEPTED}
    candidates = [
        task
        for task in tasks
        if task.resource_id == resource_id
        and task.state in reusable
        and task.created_at >= cutoff
    ]
    return max(candidates, key=lambda task: task.created_at, default=None)
```

Define `make_task` as a test factory in `tests/unit/factories.py`; it must set `created_at` from the supplied `age_hours` and default to `TaskState.QUEUED`.

- [ ] **Step 2: Implement transactional task creation**

Lock the resource row, check a 24-hour active/accepted task, copy encrypted URL/password into the task, and commit `queued` before returning `202`.

- [ ] **Step 3: Implement the worker lease**

Claim one queued task by setting `lease_owner` and `lease_expires_at` in a transaction. Transition to `submitting`, call the adapter, save `remote_ref`, and transition to `accepted`, `needs_auth`, `failed`, or `uncertain` according to the adapter result.

- [ ] **Step 4: Implement recovery and manual retry**

On startup inspect expired leases. Query `get_status(remote_ref)` when possible; otherwise set `uncertain`. Allow `/api/v1/tasks/{id}/retry` only for `failed`, `needs_auth`, or `uncertain`, and never automatically retry `uncertain`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/unit/test_tasks.py tests/integration/test_worker_recovery.py -q`  
Expected: all transition and duplicate-submission tests pass.

```bash
git add src/watch_assistant/services/tasks.py src/watch_assistant/worker.py src/watch_assistant/api/tasks.py tests
git commit -m "feat: add durable push task worker"
```

### Task 7: Add the Verified TgtoDrive Adapter

**Files:** Create `src/watch_assistant/adapters/tgto.py`, `tests/integration/test_tgto_adapter.py`, modify `src/watch_assistant/config.py` to load `config/tgto-contract.json`.

- [ ] **Step 1: Implement fake-contract tests**

```python
async def test_submit_magnet_returns_remote_ref(fake_tgto):
    result = await client.submit_magnet("magnet:?xt=urn:btih:ABC")
    assert result.accepted is True
    assert result.remote_ref == "remote-123"
```

`client` is a `TgtoDriveClient` fixture pointed at the fake HTTP server; `fake_tgto` is the `respx` route fixture that returns the configured contract response. Define `SubmissionResult` and `RemoteStatus` in `schemas.py` before writing this test.

- [ ] **Step 2: Implement only the verified transport**

Use the method/path recorded by Task 2. Map authentication failures to `needs_auth`, accepted submissions to `accepted`, and network ambiguity to `uncertain`. Redact URLs and passwords from exception messages.

- [ ] **Step 3: Run the real test with one disposable resource**

Run: `python -m pytest -m integration tests/integration/test_tgto_adapter.py -q`  
Expected: one known test magnet and one known 115 share are accepted, or the configured unsupported gate remains explicit and push tests stay disabled.

- [ ] **Step 4: Commit**

```bash
git add src/watch_assistant/adapters/tgto.py tests/integration config
git commit -m "feat: integrate verified tgto adapter"
```

### Task 8: Add Authentication and Security Controls

**Files:** Create `src/watch_assistant/api/auth.py`, `src/watch_assistant/security.py`, `tests/unit/test_security.py`, `tests/integration/test_auth_api.py`.

- [ ] **Step 1: Test login and token boundaries**

```python
async def test_invalid_web_password_returns_401(client):
    response = await client.post("/api/v1/auth/login", json={"password": "wrong"})
    assert response.status_code == 401


async def test_script_token_cannot_be_used_as_cookie(client):
    response = await client.get(
        "/api/v1/tasks", headers={"Authorization": "Bearer bad"}
    )
    assert response.status_code == 401
```

- [ ] **Step 2: Implement Web sessions and script-token auth**

Use Argon2 password verification, an HttpOnly SameSite session cookie, a separate hashed Bearer token, CSRF protection for cookie-authenticated writes, and per-token rate limits.

- [ ] **Step 3: Add secret redaction tests**

Assert that encrypted URL/password, TMDB API Key, session cookie, and Bearer Token never appear in structured logs.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/unit/test_security.py tests/integration/test_auth_api.py -q`  
Expected: authentication, CSRF, rate-limit, and redaction tests pass.

### Task 9: Implement the Vue Web UI

**Files:** Create `frontend/src/router.ts`, `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/views/SearchView.vue`, `frontend/src/views/MovieView.vue`, `frontend/src/components/ResourceTable.vue`, `frontend/src/components/TaskDrawer.vue`, `frontend/src/components/PushButton.vue`, `frontend/tests/ResourceTable.spec.ts`.

- [ ] **Step 1: Test the resource row contract**

```ts
it("renders missing size and seeders as unknown", () => {
  const wrapper = mount(ResourceTable, { props: { resources: [magnetWithoutStats] } });
  expect(wrapper.text()).toContain("未知");
});
```

Define `magnetWithoutStats` in the test file as a complete `ResourceSummary` fixture with `size_bytes: null` and `seeders: null`; define `ResourceTable.vue` before mounting it.

- [ ] **Step 2: Implement typed API clients**

Define `ResourceSummary`, `SearchResponse`, and `TaskResponse` types matching the FastAPI schemas. The task client accepts only `{ resource_id, force? }`.

- [ ] **Step 3: Implement desktop and mobile result surfaces**

Use a table above 760px and cards below it. Render source, captured time, kind, nullable size/seeders, and a Lucide-icon push control. Use text labels for status and never expose encrypted fields.

- [ ] **Step 4: Implement task polling**

Poll every 2 seconds only while a task is `queued` or `submitting`; stop on terminal status. Show `accepted` as “已推送到 115”.

- [ ] **Step 5: Run frontend tests and commit**

Run: `npm --prefix frontend test -- --run`  
Expected: component tests pass. Commit with `feat: add watch assistant web ui`.

### Task 10: Implement and Test the TMDB Userscript

**Files:** Create `frontend/src/userscript.ts`, `frontend/src/userscript.css`, `frontend/tests/userscript.spec.ts`, modify `frontend/vite.config.ts` for a userscript output.

- [ ] **Step 1: Test ID extraction and route changes**

```ts
it("extracts movie ids from TMDB SPA routes", () => {
  expect(extractMovieId("/movie/12345-inception")).toBe(12345);
  expect(extractMovieId("/tv/12345-show")).toBeNull();
});
```

Export `extractMovieId(path: string): number | null` from `frontend/src/userscript.ts` so the fixture test and userscript runtime use the same parser.

- [ ] **Step 2: Implement Shadow DOM panel**

Observe URL mutations, create one isolated panel, call `GM.xmlHttpRequest` with the configured API base and Bearer Token, and render the same resource fields as the Web UI. Do not write to the TMDB page's global DOM except for the panel host.

- [ ] **Step 3: Implement failure isolation**

On API failure, show a compact error state inside the panel and leave the TMDB page usable. Debounce route changes by 300ms and do not issue duplicate searches for the same movie ID within 10 minutes.

- [ ] **Step 4: Run userscript tests and commit**

Run: `npm --prefix frontend test -- --run userscript.spec.ts`  
Expected: route, dedupe, and failure-isolation tests pass.

### Task 11: Package and Deploy the Single Runtime Container

**Files:** Create `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/backup_db.ps1`, `README.md`.

- [ ] **Step 1: Build a multi-stage image**

Build the Vue app in a Node stage, copy `frontend/dist` into the Python image, install the locked Python dependencies, and run `uvicorn watch_assistant.app:app --host 0.0.0.0 --port 8000 --workers 1`.

- [ ] **Step 2: Attach existing Docker networks**

Use external network names supplied by `.env` after `docker network ls` verification. The service calls `http://pansou-app:80` and `http://TgtoDrive:12366` internally; publish only the new user-facing port.

- [ ] **Step 3: Add runtime protections**

Mount a persistent SQLite directory, mount secrets read-only, set `restart: unless-stopped`, add a healthcheck for `/api/v1/health`, and do not publish PanSou/TgtoDrive ports from the new compose file.

- [ ] **Step 4: Verify deployment**

Run: `docker compose config`  
Expected: valid configuration with no interpolated secret values printed. Then run `docker compose up -d --build` and verify `curl http://127.0.0.1:<port>/api/v1/health` returns HTTP 200.

- [ ] **Step 5: Document HTTPS and backup**

Document Tailscale Serve as the recommended HTTPS path, the explicit warning for LAN HTTP, and a daily SQLite backup retaining seven copies.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example scripts/backup_db.ps1 README.md
git commit -m "ops: package watch assistant"
```

### Task 12: Full Verification and Acceptance

**Files:** Modify `README.md` only if commands or prerequisites change; add `tests/e2e/` fixtures as needed.

- [ ] **Step 1: Run all Python checks**

Run: `python -m pytest -q` and `ruff check src tests`.  
Expected: zero failures and zero lint errors.

- [ ] **Step 2: Run frontend checks**

Run: `npm --prefix frontend test -- --run && npm --prefix frontend run build`.  
Expected: tests pass and Vite emits both Web assets and the userscript bundle.

- [ ] **Step 3: Run browser acceptance tests**

Run: `npm --prefix frontend run test:e2e`.  
Expected: desktop/mobile search, filter, push, polling, and TMDB fixture injection pass.

- [ ] **Step 4: Run operational checks**

Verify a service restart preserves queued tasks, a submitting task becomes accepted or uncertain, duplicate clicks reuse a task, and logs contain no secret values.

- [ ] **Step 5: Record final acceptance evidence**

Record the PanSou fixture result, one disposable magnet submission, one disposable 115 share transfer, the TgtoDrive remote reference behavior, and the exact deployed image digest in `README.md`.

- [ ] **Step 6: Commit verification evidence**

```bash
git add README.md tests
git commit -m "test: verify watch assistant acceptance"
```

## Plan Self-Review

- Spec coverage: TMDB metadata, PanSou search, missing metadata handling, resource persistence, 115 push, task states, recovery, Web UI, userscript, authentication, encryption, Docker networking, backups, and desktop/mobile tests each have a task.
- Placeholder scan: no unresolved marker words or vague error-handling steps are used.
- Type consistency: API tasks accept `resource_id` and `force`; adapter methods accept magnet URL or share URL/password; task state names are identical in models, API, worker, and tests.
- Scope control: no second search engine, external queue, public multi-user layer, or download-progress promise was added.
- Blocking dependency: Task 2 must produce a verified TgtoDrive contract or explicitly keep real push disabled; later tasks can still build against the adapter fake, but acceptance cannot pass until the gate is resolved.
