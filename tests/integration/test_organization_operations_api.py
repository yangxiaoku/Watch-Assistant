import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    AgentToken,
    OrganizationOperation,
    OrganizationOperationStatus,
)
from watch_assistant.schemas import MediaType
from watch_assistant.security import SecurityManager
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingPlan,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchStatus,
    MediaKind,
    TmdbCandidate,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanItem,
    OrganizationPlanService,
    PlanSource,
    _entry_remote_version,
)

WEB_PASSWORD = "organization-operation-password"
REMOTE_SECRET = "remote-id-private"


def _source_version() -> str:
    return _entry_remote_version(
        LibraryScanEntry(
            scan_run_id="scan-operation",
            object_type="file",
            object_id="source-operation",
            parent_id="root-operation",
            name="movie.mkv",
            path="/private/movie.mkv",
            is_directory=False,
        )
    )


class _FakeClient:
    async def aclose(self):
        return None


def _plan(
    plan_id: str,
    *,
    status: str = "planned",
    revision: int = 1,
    expires_at: datetime,
) -> OrganizationPlan:
    return OrganizationPlan(
        id=plan_id,
        library_id="library-operation",
        source_scan_run_id="scan-operation",
        source_snapshot_revision=1,
        source_snapshot_json=json.dumps(
            [{"object_id": REMOTE_SECRET, "object_type": "file"}]
        ),
        target_root="Movies",
        actions_json="[]",
        basis_json=json.dumps([{"reason": "pickcode-private"}]),
        preconditions_json=json.dumps(
            {
                "library": {
                    "library_id": "library-operation",
                    "revision": 1,
                    "enabled": True,
                    "scope_verified": True,
                    "root_directory_id": "root-operation",
                },
                "items": [],
            }
        ),
        rule_version="rule-v1",
        parser_version="parser-v1",
        matcher_version="matcher-v1",
        status=status,
        revision=revision,
        expires_at=expires_at,
        plan_hash=(plan_id + "0" * 64)[:64],
    )


async def _client(
    tmp_path: Path, *, execution_enabled: bool = False, plan_enabled: bool = True
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'organization.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-operation",
                name="PRIVATE_LIBRARY_NAME",
                root_directory_id="root-operation",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-operation",
                library_id="library-operation",
                root_directory_id="root-operation",
                idempotency_key="scan-operation-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=2,
                pages_read=2,
                items_seen=2,
            )
        )
        await session.flush()
        session.add_all(
            [
                _plan(
                    "plan-stale",
                    revision=2,
                    expires_at=now + timedelta(hours=1),
                ),
                _plan(
                    "plan-review",
                    status="needs_review",
                    expires_at=now + timedelta(hours=1),
                ),
                _plan(
                    "plan-invalidated",
                    status="invalidated",
                    expires_at=now + timedelta(hours=1),
                ),
                _plan(
                    "plan-expired",
                    expires_at=now - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="scan-operation",
                    object_type="directory",
                    object_id="target-operation",
                    parent_id="root-operation",
                    name="Movies",
                    path="Movies",
                    is_directory=True,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-operation",
                    object_type="file",
                    object_id="source-operation",
                    parent_id="root-operation",
                    name="movie.mkv",
                    path="/private/movie.mkv",
                    is_directory=False,
                ),
                LibraryScanCheckpoint(
                    scan_run_id="scan-operation",
                    page=2,
                    items_seen=2,
                    cursor_json=json.dumps(
                        {
                            "version": 2,
                            "directory_totals": {
                                "root-operation": 2,
                                "target-operation": 0,
                            },
                            "expected_total": 2,
                            "pending": [],
                            "visited": ["root-operation", "target-operation"],
                        }
                    ),
                ),
            ]
        )
        await session.commit()
    plan_service = OrganizationPlanService(database.session_factory)
    for plan_id in ("plan-ready", "plan-batch", "plan-cancel"):
        plan = await plan_service.create_plan(
            library_id="library-operation",
            scan_run_id="scan-operation",
            items=(
                OrganizationPlanItem(
                    source=PlanSource(
                        object_type="file",
                        object_id="source-operation",
                        parent_id="root-operation",
                        path="/private/movie.mkv",
                        remote_version=_source_version(),
                    ),
                    naming_plan=NamingPlan(
                        status=ClassificationStatus.PLANNED,
                        target_path="Movies/movie.mkv",
                        display_name="Movie",
                        reasons=("accepted",),
                        rule_version="rule-v1",
                    ),
                    decision=MatchDecision(
                        status=MatchStatus.ACCEPTED,
                        selected=TmdbCandidate(
                            tmdb_id=1,
                            media_type=MediaType.MOVIE,
                            title="Movie",
                            kind=MediaKind.MOVIE,
                            release_year=2024,
                            origin_countries=("US",),
                        ),
                        confidence=MatchConfidence.HIGH,
                    ),
                    target_parent_id="target-operation",
                    target_name="movie.mkv",
                ),
            ),
            target_directory_id="target-operation",
            parser_version=f"parser-{plan_id}",
        )
        async with database.session_factory() as session:
            stored = await session.get(OrganizationPlan, plan.plan_id)
            assert stored is not None
            stored.id = plan_id
            await session.commit()
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash("unused-script-token"),
    )
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=security,
        frontend_dir=tmp_path / "missing",
        organization_plan_enabled=plan_enabled,
        organization_execution_enabled=execution_enabled,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database


async def _auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    return {"X-CSRF-Token": login.json()["csrf_token"]}


async def _close(client: httpx.AsyncClient, database) -> None:
    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_operation_routes_require_auth_csrf_and_execution_gate(tmp_path: Path):
    client, database = await _client(tmp_path)
    unauthenticated = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={"expected_revision": 1, "idempotency_key": "key"},
    )
    assert unauthenticated.status_code == 401
    headers = await _auth_headers(client)
    missing_csrf = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={"expected_revision": 1, "idempotency_key": "key"},
    )
    assert missing_csrf.status_code == 403
    disabled = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={"expected_revision": 1, "idempotency_key": "key"},
        headers=headers,
    )
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == {
        "code": "organization_execution_disabled",
        "message": "整理操作功能未启用",
    }
    assert (await client.get("/api/v1/health")).json()[
        "organization_execution_enabled"
    ] is False
    await _close(client, database)


@pytest.mark.integration
async def test_queue_is_idempotent_and_rejects_unconfirmed_stale_or_expired_plans(
    tmp_path: Path,
):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    missing_confirmation = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={"expected_revision": 1, "idempotency_key": "missing-confirmation"},
        headers=headers,
    )
    assert missing_confirmation.status_code == 409
    assert missing_confirmation.json()["detail"]["code"] == "confirmation_required"

    payload = {
        "expected_revision": 1,
        "idempotency_key": "queue-key",
        "confirm": True,
    }
    first = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "planned"
    assert set(first_body) == {
        "operation_id",
        "plan_id",
        "status",
        "revision",
        "attempts",
        "error_code",
        "cancel_requested",
    }
    operation_detail = await client.get(
        "/api/v1/organization-plans/plan-ready/operation",
        headers=headers,
    )
    assert operation_detail.status_code == 200
    assert operation_detail.json()["operation_id"] == first_body["operation_id"]
    missing_operation = await client.get(
        "/api/v1/organization-plans/plan-review/operation",
        headers=headers,
    )
    assert missing_operation.status_code == 404
    assert missing_operation.json()["detail"]["code"] == "operation_not_found"
    repeated = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json=payload,
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["operation_id"] == first_body["operation_id"]
    conflicting = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={
            "expected_revision": 1,
            "idempotency_key": "new-queue-key",
            "confirm": True,
        },
        headers=headers,
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "operation_plan_conflict"
    stale = await client.post(
        "/api/v1/organization-plans/plan-stale/operation",
        json={
            "expected_revision": 1,
            "idempotency_key": "stale-key",
            "confirm": True,
        },
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "plan_revision_changed"
    for plan_id, key in (
        ("plan-review", "review-key"),
        ("plan-invalidated", "invalidated-key"),
        ("plan-expired", "expired-key"),
    ):
        rejected = await client.post(
            f"/api/v1/organization-plans/{plan_id}/operation",
            json={
                "expected_revision": 1,
                "idempotency_key": key,
                "confirm": True,
            },
            headers=headers,
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] in {
            "plan_is_not_planned",
            "plan_prerequisites_changed",
        }
    await _close(client, database)


@pytest.mark.integration
async def test_confirm_and_queue_is_one_action_for_review_plan(tmp_path: Path):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, "plan-ready")
        assert plan is not None
        plan.status = "needs_review"
        plan.revision = 2
        await session.commit()

    queued = await client.post(
        "/api/v1/organization-plans/plan-ready/confirm-and-operation",
        json={"expected_revision": 2, "idempotency_key": "confirm-and-queue-key", "confirm": True},
        headers=headers,
    )

    assert queued.status_code == 200
    assert queued.json()["status"] == "planned"
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, "plan-ready")
        assert plan is not None
        assert plan.status == "planned"
        assert plan.revision == 3
        operation = await session.scalar(
            select(OrganizationOperation).where(
                OrganizationOperation.plan_id == "plan-ready"
            )
        )
        assert operation is not None
        assert operation.plan_revision == 3
    await _close(client, database)


@pytest.mark.integration
async def test_confirm_and_queue_batch_isolates_review_only_plans(tmp_path: Path):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, "plan-batch")
        assert plan is not None
        plan.status = "needs_review"
        plan.revision = 2
        await session.commit()

    batch = await client.post(
        "/api/v1/organization-operations/confirm-and-batch",
        json={
            "items": [
                {
                    "plan_id": "plan-batch",
                    "expected_revision": 2,
                    "idempotency_key": "confirm-batch-good",
                    "confirm": True,
                },
                {
                    "plan_id": "plan-review",
                    "expected_revision": 1,
                    "idempotency_key": "confirm-batch-review",
                    "confirm": True,
                },
            ]
        },
        headers=headers,
    )

    assert batch.status_code == 200
    items = batch.json()["items"]
    assert items[0]["status"] == "planned"
    assert items[1]["status"] == "rejected"
    assert items[1]["error_code"] == "plan_not_executable"
    async with database.session_factory() as session:
        review_plan = await session.get(OrganizationPlan, "plan-review")
        assert review_plan is not None
        assert review_plan.status == "needs_review"
    await _close(client, database)


@pytest.mark.integration
async def test_queue_can_link_organization_operation_to_workflow(tmp_path: Path):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    workflow_response = await client.post(
        "/api/v1/workflows", json={"media_type": "movie"}, headers=headers
    )
    assert workflow_response.status_code == 201
    workflow_id = workflow_response.json()["id"]
    queued = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={
            "expected_revision": 1,
            "idempotency_key": "workflow-queue-key",
            "workflow_id": workflow_id,
            "confirm": True,
        },
        headers=headers,
    )
    assert queued.status_code == 200
    operation_id = queued.json()["operation_id"]
    async with database.session_factory() as session:
        operation = await session.get(OrganizationOperation, operation_id)
        assert operation is not None
        assert operation.workflow_id == workflow_id
    detail = await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert detail.status_code == 200
    organization = next(
        stage
        for stage in detail.json()["stages"]
        if stage["stage"] == "organization"
    )
    assert organization["status"] == "pending"
    assert organization["child_type"] == "organization_operation"
    assert organization["child_id"] == operation_id
    await _close(client, database)


@pytest.mark.integration
async def test_agent_operation_requires_confirmation_and_current_digest(tmp_path: Path):
    client, database = await _client(tmp_path, execution_enabled=True)
    raw_token = "wa_at_execute_contract"
    async with database.session_factory() as session:
        session.add(
            AgentToken(
                id="agent-execute",
                name="execute-contract",
                token_digest=hashlib.sha256(raw_token.encode()).hexdigest(),
                token_prefix=raw_token[:16],
                scopes_json=json.dumps(["organize:execute"]),
                library_ids_json="[]",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    headers = {"Authorization": f"Bearer {raw_token}"}
    base = {"expected_revision": 1, "idempotency_key": "agent-key"}
    missing = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json=base,
        headers=headers,
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "confirmation_required"
    wrong = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={**base, "digest": "0" * 64, "confirm": True},
        headers=headers,
    )
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "plan_digest_mismatch"
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, "plan-ready")
        assert plan is not None
        digest = plan.plan_hash
    accepted = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={**base, "digest": digest, "confirm": True},
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "planned"
    await _close(client, database)


@pytest.mark.integration
async def test_batch_isolates_item_failures_and_cancel_is_local(tmp_path: Path):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    batch = await client.post(
        "/api/v1/organization-operations/batch",
        json={
            "items": [
                {
                    "plan_id": "plan-batch",
                    "expected_revision": 1,
                    "idempotency_key": "batch-good",
                    "confirm": True,
                },
                {
                    "plan_id": "plan-review",
                    "expected_revision": 1,
                    "idempotency_key": "batch-bad",
                    "confirm": True,
                },
                {
                    "plan_id": "plan-cancel",
                    "expected_revision": 1,
                    "idempotency_key": "batch-missing-confirmation",
                },
            ]
        },
        headers=headers,
    )
    assert batch.status_code == 200
    items = batch.json()["items"]
    assert items[0]["status"] == "planned"
    assert items[1]["status"] == "rejected"
    assert items[1]["error_code"] == "plan_is_not_planned"
    assert items[2]["status"] == "rejected"
    assert items[2]["error_code"] == "confirmation_required"
    async with database.session_factory() as session:
        operations = list((await session.scalars(select(OrganizationOperation))).all())
    assert len(operations) == 1

    queued = await client.post(
        "/api/v1/organization-plans/plan-cancel/operation",
        json={
            "expected_revision": 1,
            "idempotency_key": "cancel-key",
            "confirm": True,
        },
        headers=headers,
    )
    cancelled = await client.post(
        f"/api/v1/organization-operations/{queued.json()['operation_id']}/cancel",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["revision"] == 2

    records = [
        json.loads(line)
        for line in (tmp_path / "watch-assistant.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    log_text = " ".join(record["message"] for record in records)
    assert "整理操作已排队" in log_text
    assert "整理操作已取消" in log_text
    event_codes = {record.get("event_code") for record in records}
    assert "organize.operation.queued" in event_codes
    assert "organize.operation.cancelled" in event_codes
    assert "settings.changed" not in event_codes
    assert REMOTE_SECRET not in log_text
    assert "pickcode-private" not in log_text
    await _close(client, database)


@pytest.mark.integration
async def test_cancel_requests_organizing_and_rejects_uncertain_without_remote_calls(
    tmp_path: Path,
):
    client, database = await _client(tmp_path, execution_enabled=True)
    headers = await _auth_headers(client)
    queued = await client.post(
        "/api/v1/organization-plans/plan-ready/operation",
        json={
            "expected_revision": 1,
            "idempotency_key": "organizing-key",
            "confirm": True,
        },
        headers=headers,
    )
    operation_id = queued.json()["operation_id"]
    async with database.session_factory() as session:
        operation = await session.get(OrganizationOperation, operation_id)
        assert operation is not None
        operation.status = OrganizationOperationStatus.ORGANIZING
        operation.revision = 2
        await session.commit()
    organizing = await client.post(
        f"/api/v1/organization-operations/{operation_id}/cancel",
        json={"expected_revision": 2},
        headers=headers,
    )
    assert organizing.status_code == 200
    assert organizing.json()["status"] == "organizing"
    assert organizing.json()["cancel_requested"] is True

    repeated = await client.post(
        f"/api/v1/organization-operations/{operation_id}/cancel",
        json={"expected_revision": 2},
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["cancel_requested"] is True

    async with database.session_factory() as session:
        operation = await session.get(OrganizationOperation, operation_id)
        assert operation is not None
        operation.status = OrganizationOperationStatus.UNCERTAIN
        operation.revision = 3
        await session.commit()
    uncertain = await client.post(
        f"/api/v1/organization-operations/{operation_id}/cancel",
        json={"expected_revision": 3},
        headers=headers,
    )
    assert uncertain.status_code == 409
    assert uncertain.json()["detail"]["code"] == "operation_is_not_cancellable"
    assert "remote-id-private" not in uncertain.text
    await _close(client, database)
