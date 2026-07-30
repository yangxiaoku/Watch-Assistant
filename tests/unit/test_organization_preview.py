import json
from pathlib import Path

import pytest
from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.schemas import MediaType
from watch_assistant.services.media_matcher import MediaKind, TmdbCandidate
from watch_assistant.services.organization_plan import (
    OrganizationPlanService,
    OrganizationPlanStatus,
)
from watch_assistant.services.organization_preview import OrganizationPreviewService


class _TmdbClient:
    def __init__(self) -> None:
        self.calls = 0

    async def search_candidates(self, _query):
        self.calls += 1
        return [
            TmdbCandidate(
                tmdb_id=42,
                media_type=MediaType.MOVIE,
                title="The Office",
                kind=MediaKind.MOVIE,
                release_year=2005,
                origin_countries=("US",),
            )
        ]


async def _database(tmp_path: Path, *, target_exists: bool):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'preview.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-preview",
                name="Preview library",
                root_directory_id="root-preview",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id="scan-preview",
                library_id="library-preview",
                root_directory_id="root-preview",
                idempotency_key="preview-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.flush()
        if target_exists:
            session.add(
                LibraryScanEntry(
                    scan_run_id="scan-preview",
                    object_type="directory",
                    object_id="target-preview",
                    parent_id="root-preview",
                    name="The Office",
                    path="library/movie/western/The Office (2005) {tmdb-42}",
                    is_directory=True,
                )
            )
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-preview",
                object_type="file",
                object_id="file-preview",
                parent_id="root-preview",
                name="The.Office.2005.1080p.mkv",
                path="incoming/The.Office.2005.1080p.mkv",
                is_directory=False,
            )
        )
        await session.commit()
    return database


@pytest.mark.asyncio
@pytest.mark.parametrize(("target_exists", "expected"), ((True, OrganizationPlanStatus.PLANNED), (False, OrganizationPlanStatus.NEEDS_REVIEW)))
async def test_preview_uses_scan_snapshot_and_requires_verified_target(
    tmp_path: Path, target_exists: bool, expected: OrganizationPlanStatus
):
    database = await _database(tmp_path, target_exists=target_exists)
    client = _TmdbClient()
    plan_service = OrganizationPlanService(database.session_factory)
    service = OrganizationPreviewService(database.session_factory, client, plan_service)

    result = await service.create_preview(
        library_id="library-preview", scan_run_id="scan-preview"
    )

    assert result.status is expected
    assert result.source_count == 1
    assert client.calls == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_preview_preserves_original_name_when_rename_is_disabled(tmp_path: Path):
    database = await _database(tmp_path, target_exists=True)
    service = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), OrganizationPlanService(database.session_factory)
    )

    result = await service.create_preview(
        library_id="library-preview",
        scan_run_id="scan-preview",
        target_directory_id="target-root",
        target_directories={
            "library/movie/western/The Office (2005) {tmdb-42}": "target-preview"
        },
        video_extensions=["mkv"],
        rename_enabled=False,
    )

    assert result.status is OrganizationPlanStatus.PLANNED
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
    assert plan is not None
    action = json.loads(plan.actions_json)[0]
    assert action["target"] == "library/movie/western/The Office (2005) {tmdb-42}/The.Office.2005.1080p.mkv"
    assert json.loads(plan.preconditions_json)["target_directory_id"] == "target-root"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_preview_moves_configured_metadata_companion_with_primary(tmp_path: Path):
    database = await _database(tmp_path, target_exists=True)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-preview",
                object_type="file",
                object_id="nfo-preview",
                parent_id="root-preview",
                name="The.Office.2005.nfo",
                path="incoming/The.Office.2005.nfo",
                is_directory=False,
            )
        )
        await session.commit()
    service = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), OrganizationPlanService(database.session_factory)
    )

    result = await service.create_preview(
        library_id="library-preview",
        scan_run_id="scan-preview",
        metadata_extensions=["nfo"],
    )

    assert result.status is OrganizationPlanStatus.PLANNED
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
    assert plan is not None
    members = json.loads(plan.actions_json)[0]["execution"]["members"]
    assert [member["object_id"] for member in members] == [
        "file-preview",
        "nfo-preview",
    ]
    await database.engine.dispose()
