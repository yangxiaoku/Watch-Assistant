from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

import watch_assistant.services.organization_automation as automation_module
from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.api.settings import _organization_result_response
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary, OrganizationPlan
from watch_assistant.schemas import MediaType, OrganizationSettingsResponse
from watch_assistant.services.library_index import LibraryScanResult, ScanRunState
from watch_assistant.services.organization_automation import (
    OrganizationAutomationService,
)
from watch_assistant.services.organization_plan import OrganizationPlanService
from watch_assistant.services.organization_preview import OrganizationPreviewService


class _TmdbClient:
    async def search_candidates(self, _query):
        from watch_assistant.services.media_matcher import MediaKind, TmdbCandidate

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


class _ReviewTmdbClient(_TmdbClient):
    async def search_candidates(self, _query):
        candidates = await super().search_candidates(_query)
        return [
            {
                "id": candidates[0].tmdb_id,
                "media_type": candidates[0].media_type.value,
                "title": candidates[0].title,
                "release_year": candidates[0].release_year,
                "origin_countries": list(candidates[0].origin_countries),
                "kind": "movie",
            },
            {
                "id": 43,
                "media_type": "movie",
                "title": "The Office",
                "release_year": 2005,
                "origin_countries": ["US"],
                "kind": "movie",
            },
        ]


class _Gateway:
    def __init__(self):
        self.calls: list[tuple[str, int, int]] = []
        self.pages = {
            ("9000", 1): _page(
                _entry(name="library", directory_id="8000", parent_id="9000")
            ),
            ("8000", 1): _page(
                _entry(name="movie", directory_id="8001", parent_id="8000")
            ),
            ("8001", 1): _page(
                _entry(name="western", directory_id="8002", parent_id="8001")
            ),
            ("8002", 1): _page(
                _entry(
                    name="The Office (2005) {tmdb-42}",
                    directory_id="8003",
                    parent_id="8002",
                )
            ),
            ("8003", 1): _page(),
            ("1000", 1): _page(
                _entry(
                    name="The.Office.2005.1080p.mkv",
                    file_id="7000",
                    parent_id="1000",
                    path="incoming/The.Office.2005.1080p.mkv",
                )
            ),
        }

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        self.calls.append((directory_id, page, page_size))
        return self.pages[(directory_id, page)]


class _Settings:
    def __init__(self, *, configured: bool):
        self.value = OrganizationSettingsResponse(
            schedule_enabled=True,
            scan_interval_minutes=5,
            source_directory_ids=["1000"] if configured else [],
            target_directory_id="9000" if configured else None,
            video_extensions=["mkv"],
            metadata_extensions=["srt"],
            rename_enabled=True,
            media_probe_enabled=True,
            ai_identification_enabled=False,
            small_file_threshold_mb=0,
            cleanup_empty_directories=False,
            strm_linkage_enabled=False,
            operation_delay_seconds=1.5,
            include_children_category=False,
            include_concert_category=False,
            region_grouping_enabled=True,
            year_grouping_enabled=False,
            prefer_remux=True,
            prefer_resolution=True,
            prefer_dolby=False,
            conflict_mode=2,
            multi_version_enabled=False,
            revision=0,
        )

    async def get_organization(self):
        return self.value


class _Events:
    def __init__(self):
        self.events: list[tuple[str, dict[str, object]]] = []

    async def log_event(self, event, *, fields=None, counts=None, **_kwargs):
            self.events.append((event, {**(fields or {}), **(counts or {})}))


class _Operations:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create(self, plan_id: str, **kwargs):
        self.calls.append((plan_id, kwargs))


def _entry(
    *,
    name: str,
    parent_id: str,
    directory_id: str | None = None,
    file_id: str | None = None,
    path: str | None = None,
) -> LibraryEntry:
    return LibraryEntry(
        directory_id=directory_id,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
        is_directory=directory_id is not None,
        size_bytes=10_000_000 if file_id else None,
        modified_at=None,
        pickcode=None,
        path=path,
    )


def _page(*items: LibraryEntry) -> DirectoryPage:
    return DirectoryPage(
        items=tuple(items),
        page=1,
        page_count=1,
        total=len(items),
        scan_complete=True,
        state=ScanState.COMPLETE,
        has_more=False,
        next_page=None,
        terminal=True,
    )


@pytest.mark.asyncio
async def test_automation_scans_source_and_freezes_target_catalog(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation.db'}")
    await initialize_database(database.engine)
    gateway = _Gateway()
    events = _Events()
    settings = _Settings(configured=True)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        settings,
        preview,
        plan_service,
        lambda _authorized: gateway,
        event_logger=events,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.scanned_count == 1
    assert service.last_result.plan_count == 1
    assert service.last_result.blocked_count == 0
    assert gateway.calls[:4] == [
        ("9000", 1, 1),
        ("8000", 1, 1),
        ("8001", 1, 1),
        ("8002", 1, 1),
    ]
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
        assert plan is not None
        library = await session.scalar(
            select(MediaLibrary).where(MediaLibrary.root_directory_id == "1000")
        )
        assert library is not None
        assert library.scope_verified is True
        assert library.enabled is True
    assert events.events[0][0] == "organize.preview.created"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_does_not_read_when_settings_are_unconfigured(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-empty.db'}")
    await initialize_database(database.engine)
    settings = _Settings(configured=False)
    called = False

    def gateway_factory(_authorized):
        nonlocal called
        called = True
        return _Gateway()

    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        settings,
        preview,
        plan_service,
        gateway_factory,
    )

    assert await service.run_once() is False
    assert called is False
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_confirms_and_queues_planned_work_when_write_gate_is_open(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-write.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )

    class _ConfirmedPreview:
        async def create_preview(self, **kwargs):
            plan = await preview.create_preview(**kwargs)
            return await plan_service.confirm_plan(
                plan.plan_id, expected_revision=plan.revision
            )

    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True),
        _ConfirmedPreview(),
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 1
    assert len(operations.calls) == 1
    assert operations.calls[0][1]["idempotency_key"].startswith("organization-auto:")
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_manual_automation_confirms_selected_tmdb_candidate_once(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-manual.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _ReviewTmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    assert await service.run_once(manual_confirmation=True) is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 1
    assert len(operations.calls) == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_exposes_incomplete_scan_block_reason_and_event(
    tmp_path: Path, monkeypatch
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-blocked.db'}")
    await initialize_database(database.engine)
    events = _Events()

    class _IncompleteScanner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def scan_tree(self, _idempotency_key):
            return LibraryScanResult(
                run_id="scan-blocked",
                state=ScanRunState.FAILED,
                complete=False,
                pages_read=1,
                items_seen=0,
                snapshot_revision=None,
                added_count=0,
                changed_count=0,
                removed_count=0,
                changes=(),
                error_code="scan_incomplete",
            )

    monkeypatch.setattr(automation_module, "LibraryIndexService", _IncompleteScanner)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        event_logger=events,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.blocked_count == 1
    assert service.last_result.blocked_details[0].source_directory_id == "1000"
    assert service.last_result.blocked_details[0].error_code == "scan_incomplete"
    assert "扫描未完成" in service.last_result.blocked_details[0].message_zh
    response = _organization_result_response(service.last_result)
    assert response.blocked_details[0].source_directory_id == "1000"
    assert response.blocked_details[0].message_zh == "源目录扫描未完成，已阻止生成整理预览。"
    assert events.events == [
        (
            "organize.automation.blocked",
            {
                "status": "blocked",
                "error_code": "scan_incomplete",
                "source_directory_id": "1000",
                "message_zh": "源目录扫描未完成，已阻止生成整理预览。",
                "count": 0,
            },
        )
    ]
    await database.engine.dispose()
