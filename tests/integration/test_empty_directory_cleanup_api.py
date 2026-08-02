import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
)
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.security import SecurityManager
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupStatus,
)

WEB_PASSWORD = "empty-cleanup-web-password"


class _FakeClient:
    async def aclose(self):
        return None


@pytest.mark.integration
async def test_empty_directory_cleanup_api_is_preview_confirm_idempotent_and_reversible(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'empty-cleanup-api.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="受管媒体库",
                root_directory_id="100",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-one",
                library_id="library-one",
                root_directory_id="100",
                idempotency_key="scan-one-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
                pages_read=1,
                items_seen=3,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="scan-one",
                    object_type="directory",
                    object_id="100",
                    parent_id=None,
                    name="根目录",
                    path="",
                    is_directory=True,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-one",
                    object_type="directory",
                    object_id="200",
                    parent_id="100",
                    name="受管来源",
                    path="受管来源",
                    is_directory=True,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-one",
                    object_type="directory",
                    object_id="300",
                    parent_id="200",
                    name="空目录",
                    path="受管来源/空目录",
                    is_directory=True,
                ),
            ]
        )
        await session.commit()

    password_hash = PasswordHash.recommended()
    capabilities = frozenset(
        {
            OrganizationWriteCapability.READ_SCOPE,
            OrganizationWriteCapability.RECYCLE,
            OrganizationWriteCapability.POSTCONDITION,
        }
    )
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(WEB_PASSWORD),
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
        organization_execution_enabled=True,
        organization_write_enabled=True,
        organization_write_contract_verified=True,
        organization_contract=P115OrganizationContract(
            verified=True,
            capabilities=capabilities,
            timeout_enforced=True,
            evidence=OrganizationContractEvidence(
                evidence_id="c03-fixture-recycle-v1",
                capabilities=capabilities,
                timeout_enforced=True,
            ),
        ),
        frontend_dir=tmp_path / "missing",
        system_created_directory_ids=("300",),
    )
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(candidate):
        calls.append(candidate["directory_id"])
        started.set()
        await release.wait()
        return EmptyDirectoryCleanupStatus.SUCCESS

    app.state.empty_directory_cleanup_executor = execute
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    first_task = None
    try:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        settings = await client.get("/api/v1/settings/organization")
        assert settings.status_code == 200
        enabled = await client.patch(
            "/api/v1/settings/organization",
            json={
                "cleanup_empty_directories": True,
                "source_directory_ids": ["200"],
                "revision": settings.json()["revision"],
            },
            headers=headers,
        )
        assert enabled.status_code == 200

        preview = await client.post(
            "/api/v1/libraries/library-one/empty-directory-cleanup-plan",
            json={"source_scan_run_id": "scan-one"},
            headers=headers,
        )
        assert preview.status_code == 200
        plan = preview.json()
        assert plan["candidate_count"] == 1
        assert plan["candidates"] == [
            {
                "directory_id": "300",
                "parent_id": "200",
                "name": "空目录",
                "path": "受管来源/空目录",
                "state": "ready",
            }
        ]
        assert "root-one" not in preview.text

        apply_payload = {
            "expected_revision": plan["revision"],
            "digest": plan["plan_hash"],
            "confirm": True,
            "idempotency_key": "empty-cleanup-key-1",
        }
        first_task = asyncio.create_task(
            client.post(
                f"/api/v1/empty-directory-cleanup-plans/{plan['plan_id']}/apply",
                json=apply_payload,
                headers=headers,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        running = await client.post(
            f"/api/v1/empty-directory-cleanup-plans/{plan['plan_id']}/apply",
            json=apply_payload,
            headers=headers,
        )
        assert running.status_code == 409
        detail = running.json()["detail"]
        assert detail["code"] == "empty_cleanup_in_progress"
        assert detail["status"] == "running"
        assert detail["reused"] is True
        assert detail["operation_id"].startswith("strm_op_")
        assert "lease_owner" not in running.text

        release.set()
        applied = await first_task
        assert applied.status_code == 200
        assert applied.json()["deleted"] == 1
        repeated = await client.post(
            f"/api/v1/empty-directory-cleanup-plans/{plan['plan_id']}/apply",
            json=apply_payload,
            headers=headers,
        )
        assert repeated.status_code == 200
        assert repeated.json()["deleted"] == 1
        assert calls == ["300"]
    finally:
        release.set()
        if first_task is not None and not first_task.done():
            await first_task
        await client.aclose()
        await database.engine.dispose()
