"""A workflow may legitimately bind multiple same-type children over time.

The frontend inspects magnets in several batches and can push several
resources while reusing the per-media workflow.  The workflow fences must
accept a NEW child of the SAME type (a later inspection batch or push task)
while still rejecting a foreign or cross-type child.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import InspectionBatch, InspectionBatchStatus, Resource
from watch_assistant.schemas import (
    WorkflowCreateRequest,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.workflows import (
    WorkflowConflict,
    WorkflowService,
    link_child,
)


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'multi-child.db'}")
    await initialize_database(database.engine)
    return database


async def _add_resource(database, crypto):
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_multi",
                kind="magnet",
                canonical_key="magnet:abcdef0123456789abcdef0123456789abcdef01",
                encrypted_url=crypto.encrypt(
                    "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
                ),
                name="Movie",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await session.commit()


async def _add_batch(database, batch_id: str, workflow_id: str):
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            InspectionBatch(
                id=batch_id,
                workflow_id=workflow_id,
                status=InspectionBatchStatus.QUEUED,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()


@pytest.mark.integration
async def test_inspection_stage_accepts_multiple_batches(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    workflows = WorkflowService(database.session_factory)
    try:
        workflow = await workflows.create(
            WorkflowCreateRequest(
                media_type="movie", tmdb_id=27205, resource_id="res_multi"
            )
        )
        await _add_batch(database, "inspect_batch_one", workflow.id)
        await _add_batch(database, "inspect_batch_two", workflow.id)

        async with database.session_factory() as session:
            await link_child(
                session,
                workflow.id,
                WorkflowStageName.INSPECTION,
                "inspection_batch",
                "inspect_batch_one",
            )
            await session.commit()
        # A second batch for the same workflow must not conflict.
        async with database.session_factory() as session:
            await link_child(
                session,
                workflow.id,
                WorkflowStageName.INSPECTION,
                "inspection_batch",
                "inspect_batch_two",
            )
            await session.commit()

        detail = await workflows.get(workflow.id)
        inspection = next(
            stage for stage in detail.stages if stage.stage is WorkflowStageName.INSPECTION
        )
        assert inspection.child_type == "inspection_batch"
        assert inspection.child_id == "inspect_batch_two"
        assert inspection.status is WorkflowStageStatus.RUNNING
    finally:
        await database.engine.dispose()


@pytest.mark.integration
async def test_inspection_stage_rejects_foreign_batch(tmp_path):
    database = await _database(tmp_path)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    await _add_resource(database, crypto)
    workflows = WorkflowService(database.session_factory)
    try:
        workflow = await workflows.create(
            WorkflowCreateRequest(
                media_type="movie", tmdb_id=27205, resource_id="res_multi"
            )
        )
        # The batch belongs to a different workflow.
        foreign = await workflows.create(
            WorkflowCreateRequest(media_type="movie", tmdb_id=27206)
        )
        await _add_batch(database, "inspect_foreign", foreign.id)

        with pytest.raises(WorkflowConflict, match="workflow_conflict"):
            async with database.session_factory() as session:
                await link_child(
                    session,
                    workflow.id,
                    WorkflowStageName.INSPECTION,
                    "inspection_batch",
                    "inspect_foreign",
                )
                await session.commit()
    finally:
        await database.engine.dispose()
