from pathlib import Path

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
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
