import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.security import SecurityManager
from watch_assistant.services.strm_manifest import (
    StrmGenerationSummary,
    StrmManifestService,
)
from watch_assistant.services.strm_operations import (
    StrmOperationKind,
    StrmOperationService,
)


class _FakeClient:
    async def aclose(self):
        return None


@pytest.mark.integration
async def test_strm_operations_are_visible_and_legacy_cleanup_is_preview_only(
    tmp_path: Path,
    monkeypatch,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm-api.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="测试媒体库",
                root_directory_id="1000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id="scan-one",
                library_id="library-one",
                root_directory_id="1000",
                idempotency_key="scan-one-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.commit()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-one",
                object_type="file",
                object_id="file-one",
                parent_id="1000",
                name="Episode.mkv",
                path="Shows/Episode.mkv",
                is_directory=False,
                size_bytes=10,
            )
        )
        await session.commit()

    password = token_urlsafe(16)
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(password),
            script_token_hash=password_hash.hash(token_urlsafe(16)),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
        strm_full_enabled=True,
        strm_incremental_enabled=True,
        strm_cleanup_enabled=True,
        strm_output_root=tmp_path / "strm-output",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    try:
        login = await client.post("/api/v1/auth/login", json={"password": password})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        generated = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={"source_scan_run_id": "scan-one"},
            headers={**headers, "Idempotency-Key": "api-full-one"},
        )
        assert generated.status_code == 200
        body = generated.json()
        assert body["operation_id"].startswith("strm_op_")
        assert body["generated"] == 1
        assert "lease_owner" not in generated.text

        repeated = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={"source_scan_run_id": "scan-one"},
            headers={**headers, "Idempotency-Key": "api-full-one"},
        )
        assert repeated.status_code == 200
        assert repeated.json() == body

        conflicting = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={
                "source_scan_run_id": "scan-one",
                "workflow_id": "different-workflow",
            },
            headers={**headers, "Idempotency-Key": "api-full-one"},
        )
        assert conflicting.status_code == 409
        assert conflicting.json()["detail"] == "idempotency_key_conflict"
        strm_path = tmp_path / "strm-output" / "Shows" / "Episode.strm"
        assert strm_path.exists()

        operation = await client.get(
            f"/api/v1/strm-operations/{body['operation_id']}"
        )
        assert operation.status_code == 200
        assert operation.json()["status"] == "succeeded"
        assert "lease_owner" not in operation.text
        manifest = await client.get(
            "/api/v1/libraries/library-one/strm-manifest", headers=headers
        )
        assert manifest.status_code == 200
        manifest_item = manifest.json()["items"][0]
        assert manifest_item["local_relative_path"] == "Shows/Episode.strm"
        assert strm_path.read_text(encoding="utf-8") == (
            "http://127.0.0.1:8115/api/v1/strm/play/"
            f"{manifest_item['manifest_id']}\n"
        )

        history = await client.get(
            "/api/v1/libraries/library-one/strm-operations",
            headers=headers,
        )
        assert history.status_code == 200
        assert history.json()["items"][0]["operation_id"] == body["operation_id"]
        assert "lease_owner" not in history.text

        scoped_token = await client.post(
            "/api/v1/agent/tokens",
            json={
                "name": "strm-scoped",
                "scopes": ["library:read", "strm:read", "strm:write"],
                "library_ids": ["library-other"],
            },
            headers=headers,
        )
        assert scoped_token.status_code == 201
        scoped_headers = {
            "Authorization": f"Bearer {scoped_token.json()['token']}"
        }
        denied_manifest = await client.get(
            "/api/v1/libraries/library-one/strm-manifest",
            headers=scoped_headers,
        )
        denied_generation = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={"source_scan_run_id": "scan-one"},
            headers=scoped_headers,
        )
        denied_plan = await client.post(
            "/api/v1/libraries/library-one/strm-cleanup-plan",
            json={"source_scan_run_id": "scan-one"},
            headers=scoped_headers,
        )
        assert denied_manifest.status_code == 404
        assert denied_generation.status_code == 404
        assert denied_plan.status_code == 404

        legacy = await client.post(
            "/api/v1/libraries/library-one/strm-cleanup",
            json={"source_scan_run_id": "scan-one"},
            headers=headers,
        )
        assert legacy.status_code == 409
        assert legacy.json()["detail"]["code"] == "cleanup_plan_required"
        assert "retired" not in legacy.text

        orphan = await StrmOperationService(database.session_factory).create(
            library_id="library-one",
            source_scan_run_id="scan-one",
            kind=StrmOperationKind.FULL,
        )
        await StrmOperationService(database.session_factory).start(
            orphan.operation_id,
            now=datetime.now(UTC) - timedelta(hours=1),
            lease_duration=timedelta(minutes=5),
        )
        async with app.router.lifespan_context(app):
            recovered = await StrmOperationService(database.session_factory).get(
                orphan.operation_id
            )
            assert recovered.status == "failed"
            assert recovered.error_code == "strm_operation_recovered"

        async def cancelled_generate(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(StrmManifestService, "generate", cancelled_generate)
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await client.post(
                "/api/v1/libraries/library-one/strm-generation",
                json={"source_scan_run_id": "scan-one"},
                headers={**headers, "Idempotency-Key": "api-cancelled-one"},
            )
        cancelled_history = await client.get(
            "/api/v1/libraries/library-one/strm-operations",
            headers=headers,
        )
        assert cancelled_history.status_code == 200
        assert cancelled_history.json()["items"][0]["status"] == "cancelled"
        assert cancelled_history.json()["items"][0]["error_code"] == "strm_operation_cancelled"
        assert (
            cancelled_history.json()["items"][0]["error_code"]
            == "strm_operation_cancelled"
        )
        cancelled_repeat = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={"source_scan_run_id": "scan-one"},
            headers={**headers, "Idempotency-Key": "api-cancelled-one"},
        )
        assert cancelled_repeat.status_code == 409
        assert cancelled_repeat.json()["detail"] == "strm_operation_cancelled"
        monkeypatch.undo()

        async with database.session_factory() as session:
            session.add(
                LibraryScanRun(
                    id="scan-two",
                    library_id="library-one",
                    root_directory_id="1000",
                    idempotency_key="scan-two-key",
                    state="completed",
                    complete=True,
                    snapshot_revision=2,
                )
            )
            await session.flush()
            session.add(
                LibraryScanEntry(
                    scan_run_id="scan-two",
                    object_type="file",
                    object_id="file-two",
                    parent_id="1000",
                    name="Resumed.mkv",
                    path="Shows/Resumed.mkv",
                    is_directory=False,
                    size_bytes=20,
                )
            )
            await session.commit()

        resumable = await StrmOperationService(database.session_factory).create(
            library_id="library-one",
            source_scan_run_id="scan-two",
            kind=StrmOperationKind.FULL,
        )
        await StrmOperationService(database.session_factory).start(
            resumable.operation_id
        )
        cancelled = await client.post(
            f"/api/v1/strm-operations/{resumable.operation_id}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        resumed = await client.post(
            f"/api/v1/strm-operations/{resumable.operation_id}/resume",
            headers=headers,
        )
        assert resumed.status_code == 200
        resumed_body = resumed.json()
        assert resumed_body["status"] == "succeeded"
        assert resumed_body["generated"] == 1
        assert (tmp_path / "strm-output" / "Shows" / "Resumed.strm").exists()
    finally:
        await client.aclose()
        await database.engine.dispose()


@pytest.mark.integration
async def test_same_idempotency_key_reuses_running_operation_explicitly(
    tmp_path: Path,
    monkeypatch,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'strm-running.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="测试媒体库",
                root_directory_id="1000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id="scan-one",
                library_id="library-one",
                root_directory_id="1000",
                idempotency_key="scan-one-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_generate(self, *args, **kwargs):
        del self, args, kwargs
        started.set()
        await release.wait()
        return StrmGenerationSummary(
            "library-one", "scan-one", 0, 0, 0, 0, 0
        )

    monkeypatch.setattr(StrmManifestService, "generate", blocking_generate)
    password = token_urlsafe(16)
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(password),
            script_token_hash=password_hash.hash(token_urlsafe(16)),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
        strm_full_enabled=True,
        strm_incremental_enabled=True,
        strm_output_root=tmp_path / "strm-output",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    first_task = None
    try:
        login = await client.post("/api/v1/auth/login", json={"password": password})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        request_headers = {**headers, "Idempotency-Key": "running-key"}
        first_task = asyncio.create_task(
            client.post(
                "/api/v1/libraries/library-one/strm-generation",
                json={"source_scan_run_id": "scan-one"},
                headers=request_headers,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        repeated = await client.post(
            "/api/v1/libraries/library-one/strm-generation",
            json={"source_scan_run_id": "scan-one"},
            headers=request_headers,
        )
        assert repeated.status_code == 409
        detail = repeated.json()["detail"]
        assert detail["code"] == "strm_operation_in_progress"
        assert detail["status"] == "running"
        assert detail["reused"] is True
        assert detail["operation_id"].startswith("strm_op_")
        assert "lease_owner" not in repeated.text

        release.set()
        first = await asyncio.wait_for(first_task, timeout=2)
        assert first.status_code == 200
        assert first.json()["operation_id"] == detail["operation_id"]
    finally:
        release.set()
        if first_task is not None and not first_task.done():
            await first_task
        await client.aclose()
        await database.engine.dispose()
