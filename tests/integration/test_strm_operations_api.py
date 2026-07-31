from pathlib import Path

import pytest
from sqlalchemy import select
from test_settings_api import WEB_PASSWORD, _app

from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import StrmOperation


async def _seed_scan(database) -> None:
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library",
                name="STRM",
                root_directory_id="root",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-ready",
                library_id="library",
                root_directory_id="root",
                idempotency_key="scan-ready-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-ready",
                object_type="file",
                object_id="file-1",
                parent_id="root",
                name="Movie.mkv",
                path="Movie.mkv",
                is_directory=False,
                size_bytes=100,
            )
        )
        await session.commit()


@pytest.mark.integration
async def test_strm_requests_persist_independent_operations_and_workflow_link(
    tmp_path: Path,
):
    _app_obj, client, database, tmdb, pansou = await _app(
        tmp_path,
        strm_full_enabled=True,
        strm_incremental_enabled=True,
        strm_cleanup_enabled=True,
        strm_output_root=tmp_path / "strm",
    )
    try:
        await _seed_scan(database)
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        workflow = await client.post(
            "/api/v1/workflows", json={"media_type": "movie"}, headers=headers
        )
        assert workflow.status_code == 201
        workflow_id = workflow.json()["id"]

        full = await client.post(
            "/api/v1/libraries/library/strm-generation",
            json={"source_scan_run_id": "scan-ready", "workflow_id": workflow_id},
            headers=headers,
        )
        assert full.status_code == 200
        full_body = full.json()
        assert full_body["operation_id"].startswith("strm_op_")
        assert full_body["generated"] == 1

        full_operation = await client.get(
            f"/api/v1/strm-operations/{full_body['operation_id']}"
        )
        assert full_operation.status_code == 200
        assert full_operation.json()["status"] == "succeeded"
        assert full_operation.json()["kind"] == "full"

        workflow_after = await client.get(f"/api/v1/workflows/{workflow_id}")
        stage = next(
            item
            for item in workflow_after.json()["stages"]
            if item["stage"] == "strm"
        )
        assert stage["child_type"] == "strm_operation"
        assert stage["child_id"] == full_body["operation_id"]
        assert stage["status"] == "succeeded"

        incremental = await client.post(
            "/api/v1/libraries/library/strm-incremental",
            json={"source_scan_run_id": "scan-ready"},
            headers=headers,
        )
        cleanup = await client.post(
            "/api/v1/libraries/library/strm-cleanup",
            json={"source_scan_run_id": "scan-ready"},
            headers=headers,
        )
        assert incremental.status_code == 200
        assert cleanup.status_code == 200
        assert incremental.json()["operation_id"] != cleanup.json()["operation_id"]

        history = await client.get(
            "/api/v1/libraries/library/strm-operations",
            params={"limit": 2},
        )
        assert history.status_code == 200
        assert len(history.json()["items"]) == 2
        assert {item["kind"] for item in history.json()["items"]} == {
            "incremental",
            "cleanup",
        }
        assert history.json()["next_cursor"] == 2

        second_page = await client.get(
            "/api/v1/libraries/library/strm-operations",
            params={"cursor": 2, "limit": 2},
        )
        assert second_page.status_code == 200
        assert {item["kind"] for item in second_page.json()["items"]} == {"full"}
        assert second_page.json()["next_cursor"] is None

        async with database.session_factory() as session:
            operations = list((await session.scalars(select(StrmOperation))).all())
        assert {operation.kind for operation in operations} == {
            "full",
            "incremental",
            "cleanup",
        }
        missing = await client.get("/api/v1/strm-operations/missing-operation")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "strm_operation_not_found"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
