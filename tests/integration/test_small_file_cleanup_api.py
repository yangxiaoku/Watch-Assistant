"""Small-file cleanup apply is async: returns an operation and completes in background."""

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
from watch_assistant.services.strm_operations import StrmOperationService

WEB_PASSWORD = "small-file-cleanup-web-password"


class _FakeClient:
    async def aclose(self):
        return None


class _FakeSmallFileCleanupService:
    """Fake that records the apply call and returns a fixed (deleted, failed)."""

    def __init__(self, *, deleted: int, failed: int) -> None:
        self.deleted = deleted
        self.failed = failed
        self.apply_calls: list[dict] = []

    async def apply(self, library, *, scan_run_id, file_ids, confirm, operation_delay_seconds):
        self.apply_calls.append(
            {
                "library_id": library.id,
                "scan_run_id": scan_run_id,
                "file_ids": file_ids,
                "confirm": confirm,
                "operation_delay_seconds": operation_delay_seconds,
            }
        )
        return self.deleted, self.failed


@pytest.mark.integration
async def test_small_file_cleanup_apply_returns_operation_and_completes_in_background(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'small-file-cleanup-api.db'}")
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
                items_seen=1,
                expected_total=1,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-one",
                object_type="file",
                object_id="file-1",
                parent_id="100",
                name="sample.srt",
                path="sample.srt",
                is_directory=False,
                size_bytes=1024,
            )
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
        system_created_directory_ids=("100",),
    )
    fake_service = _FakeSmallFileCleanupService(deleted=2, failed=1)
    app.state.small_file_cleanup_service = fake_service
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    try:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        settings = await client.get("/api/v1/settings/organization")
        assert settings.status_code == 200
        enabled = await client.patch(
            "/api/v1/settings/organization",
            json={
                "small_file_threshold_mb": 5,
                "revision": settings.json()["revision"],
            },
            headers=headers,
        )
        assert enabled.status_code == 200

        apply_payload = {
            "source_scan_run_id": "scan-one",
            "file_ids": ["file-1", "file-2"],
            "confirm": True,
        }
        applied = await client.post(
            "/api/v1/libraries/library-one/small-file-cleanup-apply",
            json=apply_payload,
            headers=headers,
        )
        assert applied.status_code == 200
        body = applied.json()
        assert body["status"] == "running"
        operation_id = body["operation_id"]
        assert operation_id.startswith("strm_op_")
        assert set(body.keys()) == {"operation_id", "status"}

        operations = StrmOperationService(database.session_factory)
        terminal = None
        for _ in range(200):
            await asyncio.sleep(0.01)
            current = await operations.get(operation_id)
            if current.status in {"succeeded", "failed", "timeout", "cancelled"}:
                terminal = current
                break
        assert terminal is not None, "background operation never reached a terminal state"
        assert terminal.status == "succeeded"
        assert terminal.kind == "small_file_cleanup"
        assert terminal.retired == 2
        assert terminal.failed == 1

        assert fake_service.apply_calls == [
            {
                "library_id": "library-one",
                "scan_run_id": "scan-one",
                "file_ids": ["file-1", "file-2"],
                "confirm": True,
                "operation_delay_seconds": 0.3,
            }
        ]
    finally:
        await client.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_small_file_cleanup_apply_rejects_second_concurrent_apply(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'small-file-cleanup-api.db'}")
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
                items_seen=1,
                expected_total=1,
                created_at=now,
                updated_at=now,
            )
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
        system_created_directory_ids=("100",),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingService:
        async def apply(self, library, *, scan_run_id, file_ids, confirm, operation_delay_seconds):
            started.set()
            await release.wait()
            return 1, 0

    app.state.small_file_cleanup_service = _BlockingService()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    try:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        settings = await client.get("/api/v1/settings/organization")
        assert settings.status_code == 200
        enabled = await client.patch(
            "/api/v1/settings/organization",
            json={
                "small_file_threshold_mb": 5,
                "revision": settings.json()["revision"],
            },
            headers=headers,
        )
        assert enabled.status_code == 200

        apply_payload = {
            "source_scan_run_id": "scan-one",
            "file_ids": ["file-1"],
            "confirm": True,
        }
        first = await client.post(
            "/api/v1/libraries/library-one/small-file-cleanup-apply",
            json=apply_payload,
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["status"] == "running"
        await asyncio.wait_for(started.wait(), timeout=2)

        second = await client.post(
            "/api/v1/libraries/library-one/small-file-cleanup-apply",
            json=apply_payload,
            headers=headers,
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "strm_library_operation_conflict"
    finally:
        release.set()
        await client.aclose()
        await database.engine.dispose()
