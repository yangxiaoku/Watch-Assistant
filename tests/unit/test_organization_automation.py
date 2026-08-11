from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

import watch_assistant.services.organization_automation as automation_module
from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03DirectoryListing,
    C03RemoteEntry,
    C03WriteReceipt,
)
from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.adapters.p115_library_write_contract import (
    WriteOperation,
    WriteStatus,
)
from watch_assistant.api.settings import _organization_result_response
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary, OrganizationPlan
from watch_assistant.schemas import MediaType, OrganizationSettingsResponse
from watch_assistant.services.library_index import LibraryScanResult, ScanRunState
from watch_assistant.services.organization_automation import (
    OrganizationAutomationError,
    OrganizationAutomationService,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanService,
)
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


class _SelectiveTmdbClient(_TmdbClient):
    """Accept only known titles; everything else stays unrecognized (review)."""

    def __init__(self, accepted: set[str] | None = None) -> None:
        self.accepted = {value.casefold() for value in (accepted or ())}

    async def search_candidates(self, _query):
        title = getattr(_query, "title", None) or _query
        if not title or str(title).casefold() not in self.accepted:
            return []
        return await super().search_candidates(_query)


class _Gateway:
    def __init__(self, *, source_scan_complete: bool | None = True):
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
                ),
                scan_complete=source_scan_complete,
            ),
        }

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        self.calls.append((directory_id, page, page_size))
        return self.pages[(directory_id, page)]


class _Settings:
    def __init__(self, *, configured: bool, auto_execute_enabled: bool = False):
        self.value = OrganizationSettingsResponse(
            schedule_enabled=True,
            auto_execute_enabled=auto_execute_enabled,
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


class _CleanupSettings(_Settings):
    """Cleanup-enabled settings with a zero cadence so tests stay fast."""

    def __init__(self, *, threshold_mb: float = 100.0):
        super().__init__(configured=True)
        self.value = self.value.model_copy(
            update={
                "small_file_threshold_mb": threshold_mb,
                "operation_delay_seconds": 0,
            }
        )


class _FakeCleanupTransport:
    """Recorded fs_delete/recycle transport; deletions mutate the live pages."""

    def __init__(self, pages: dict[str, list[C03RemoteEntry]]):
        self.pages = {
            directory_id: list(entries) for directory_id, entries in pages.items()
        }
        self.executed: list[tuple[WriteOperation, str]] = []
        self.closed = False
        self._client = self  # _close_transport seam (aclose on the client)

    async def execute(self, request, *, timeout_seconds):
        file_id = request.payload["file_id"]
        self.executed.append((request.operation, file_id))
        for entries in self.pages.values():
            entries[:] = [
                entry for entry in entries if entry.file_id != file_id
            ]
        return C03WriteReceipt(WriteStatus.SUCCESS)

    async def list_children(self, parent_id, *, timeout_seconds):
        return C03DirectoryListing(tuple(self.pages.get(parent_id, ())))

    async def aclose(self):
        self.closed = True


class _CleanupGateway(_Gateway):
    """Source with unrecognized small/big files, a matched file and empty dirs."""

    def __init__(self):
        super().__init__()
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
                    name="tiny.unknown.mkv",
                    file_id="7101",
                    parent_id="1000",
                    path="tiny.unknown.mkv",
                    size_bytes=10 * 1024 * 1024,
                ),
                _entry(
                    name="big.unknown.mkv",
                    file_id="7102",
                    parent_id="1000",
                    path="big.unknown.mkv",
                    size_bytes=200 * 1024 * 1024,
                ),
                _entry(
                    name="The.Office.2005.1080p.mkv",
                    file_id="7103",
                    parent_id="1000",
                    path="The.Office.2005.1080p.mkv",
                    size_bytes=5 * 1024 * 1024,
                ),
                _entry(name="empty-a", directory_id="7001", parent_id="1000"),
                _entry(name="nest", directory_id="7002", parent_id="1000"),
            ),
            ("7001", 1): _page(),
            ("7002", 1): _page(
                _entry(name="deep", directory_id="7003", parent_id="7002")
            ),
            ("7003", 1): _page(),
        }


def _cleanup_pages() -> dict[str, list[C03RemoteEntry]]:
    return {
        "1000": [
            C03RemoteEntry("7101", "1000", "tiny.unknown.mkv", False),
            C03RemoteEntry("7102", "1000", "big.unknown.mkv", False),
            C03RemoteEntry("7103", "1000", "The.Office.2005.1080p.mkv", False),
            C03RemoteEntry("7001", "1000", "empty-a", True),
            C03RemoteEntry("7002", "1000", "nest", True),
        ],
        "7001": [],
        "7002": [C03RemoteEntry("7003", "7002", "deep", True)],
        "7003": [],
    }


def _entry(
    *,
    name: str,
    parent_id: str,
    directory_id: str | None = None,
    file_id: str | None = None,
    path: str | None = None,
    size_bytes: int | None = None,
) -> LibraryEntry:
    return LibraryEntry(
        directory_id=directory_id,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
        is_directory=directory_id is not None,
        size_bytes=(
            size_bytes if size_bytes is not None else (10_000_000 if file_id else None)
        ),
        modified_at=None,
        pickcode=None,
        path=path,
    )


def _page(*items: LibraryEntry, scan_complete: bool | None = True) -> DirectoryPage:
    return DirectoryPage(
        items=tuple(items),
        page=1,
        page_count=1,
        total=len(items),
        scan_complete=scan_complete,
        state=ScanState.PARTIAL if scan_complete is False else ScanState.COMPLETE,
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
    assert service.last_result.run_id is not None
    assert gateway.calls[:4] == [
        ("9000", 1, 50),
        ("8000", 1, 50),
        ("8001", 1, 50),
        ("8002", 1, 50),
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
@pytest.mark.parametrize("scan_complete", [None, False])
async def test_automation_requires_explicit_complete_source_scope(
    tmp_path: Path, scan_complete
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-scope.db'}")
    await initialize_database(database.engine)
    gateway = _Gateway(source_scan_complete=scan_complete)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True),
        preview,
        plan_service,
        lambda _authorized: gateway,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.scanned_count == 0
    assert service.last_result.plan_count == 0
    assert service.last_result.blocked_count == 1
    assert service.last_result.blocked_details[0].error_code == "source_scope_unverified"
    assert gateway.calls[-1] == ("1000", 1, 1)
    assert gateway.calls.count(("1000", 1, 1)) == 1
    async with database.session_factory() as session:
        assert await session.scalar(select(OrganizationPlan)) is None
        assert (
            await session.scalar(
                select(MediaLibrary).where(MediaLibrary.root_directory_id == "1000")
            )
            is None
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_scope_verification_reads_all_source_pages(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-pages.db'}")
    await initialize_database(database.engine)

    class _MultiPageSourceGateway:
        def __init__(self):
            self.calls: list[tuple[str, int, int]] = []

        async def list_directory(self, directory_id, *, page, page_size):
            self.calls.append((directory_id, page, page_size))
            item = _entry(
                name=f"source-{page}.mkv",
                parent_id=directory_id,
                file_id=str(7000 + page),
                path=f"source-{page}.mkv",
            )
            return DirectoryPage(
                items=(item,),
                page=page,
                page_count=2,
                total=2,
                scan_complete=None if page == 1 else True,
                state=ScanState.COMPLETE,
                has_more=page == 1,
                next_page=2 if page == 1 else None,
                terminal=page == 2,
            )

    gateway = _MultiPageSourceGateway()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True),
        preview,
        plan_service,
        lambda _authorized: gateway,
    )

    assert (
        await service._ensure_source_library(gateway, "1000")
        == automation_module._source_library_id("1000")
    )
    assert gateway.calls == [("1000", 1, 1), ("1000", 2, 1)]
    async with database.session_factory() as session:
        library = await session.get(
            MediaLibrary, automation_module._source_library_id("1000")
        )
        assert library is not None
        assert library.scope_verified is True
        assert library.enabled is True
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
async def test_automation_preserves_stable_inner_failure_code(
    tmp_path: Path, monkeypatch
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-error.db'}")
    await initialize_database(database.engine)
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
    )

    async def fail(_settings, *, manual_confirmation=False):
        raise OrganizationAutomationError("source_scope_unverified")

    monkeypatch.setattr(service, "_run", fail)

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.blocked_details[0].error_code == "source_scope_unverified"
    assert "范围未通过校验" in service.last_result.blocked_details[0].message_zh
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_auto_executes_planned_partition_and_never_provisions(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-write.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    events = _Events()
    plan_service = OrganizationPlanService(
        database.session_factory, event_logger=events
    )
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )

    provision_calls: list[object] = []

    async def provision(*args):
        provision_calls.append(args)

    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
        directory_provisioner=provision,
        event_logger=events,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 1
    assert service.last_result.blocked_count == 0
    assert len(operations.calls) == 1
    assert provision_calls == []
    assert operations.calls[0][1]["idempotency_key"].startswith("auto-")
    assert not any(
        event == "organize.plan.awaiting_confirmation"
        for event, _fields in events.events
    )
    assert any(
        event == "organize.plan.confirmed" for event, _fields in events.events
    )
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
        assert plan is not None
        assert plan.status == "planned"
        assert plan.revision == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_manual_automation_never_queues_even_when_write_gate_is_open(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-manual-write.db'}")
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

    assert await service.run_once(manual_confirmation=True) is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert operations.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_manual_automation_keeps_ambiguous_tmdb_candidate_for_review(
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
    assert service.last_result.queued_count == 0
    assert len(operations.calls) == 0
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
        assert plan is not None
        assert plan.status == "needs_review"
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
                "phase": "scan",
                "message_zh": "源目录扫描未完成，已阻止生成整理预览。",
                "next_step_zh": "请重新执行一次完整扫描；扫描未完成前不会生成或执行计划。",
                "count": 0,
            },
        )
    ]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_auto_execute_disabled_by_setting_queues_nothing(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-no-setting.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=False),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert operations.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_without_operation_service_never_queues(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-no-ops.db'}")
    await initialize_database(database.engine)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=None,
        auto_execute=True,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_write_path_disabled_never_queues(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-write-off.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=False,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert operations.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_manual_run_never_queues_auto_partition(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-manual-auto.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    assert await service.run_once(manual_confirmation=True) is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert operations.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_stale_revision_blocks_plan_queue(tmp_path: Path, monkeypatch):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-stale.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    async def failing_confirm(plan_id, *, expected_revision):
        raise OrganizationPlanError("stale_revision")

    monkeypatch.setattr(plan_service, "confirm_plan", failing_confirm)

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert service.last_result.blocked_count == 1
    assert service.last_result.blocked_details[0].error_code == "stale_revision"
    assert operations.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_operation_conflict_blocks_queue(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-conflict.db'}")
    await initialize_database(database.engine)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )

    class _ConflictingOperations(_Operations):
        async def create(self, plan_id, **kwargs):
            self.calls.append((plan_id, kwargs))
            from watch_assistant.services.organization_operations import (
                OrganizationOperationConflict,
            )

            raise OrganizationOperationConflict("operation_plan_conflict")

    operations = _ConflictingOperations()
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 0
    assert service.last_result.blocked_count == 1
    assert (
        service.last_result.blocked_details[0].error_code
        == "operation_plan_conflict"
    )
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
        assert plan is not None
        assert plan.status == "planned"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_queue_is_idempotent_across_runs(
    tmp_path: Path, monkeypatch
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-idempotent.db'}")
    await initialize_database(database.engine)
    operations = _Operations()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory, _TmdbClient(), plan_service
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _Settings(configured=True, auto_execute_enabled=True),
        preview,
        plan_service,
        lambda _authorized: _Gateway(),
        operation_service=operations,
        auto_execute=True,
    )
    # A stable scan idempotency key makes the second run reuse the same scan
    # snapshot, so plan creation dedupes to the same plan and the same
    # deterministic operation key.
    monkeypatch.setattr(
        automation_module,
        "_scan_idempotency_key",
        lambda _source_id: "fixed-scan-key",
    )

    assert await service.run_once() is True
    assert service.last_result is not None
    assert service.last_result.queued_count == 1
    first_key = operations.calls[0][1]["idempotency_key"]

    assert await service.run_once() is True
    assert service.last_result.queued_count == 1
    assert operations.calls[1][1]["idempotency_key"] == first_key
    async with database.session_factory() as session:
        plan = await session.scalar(select(OrganizationPlan))
        assert plan is not None
        assert plan.status == "planned"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_auto_cleans_unrecognized_small_files_and_empty_dirs(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-clean.db'}")
    await initialize_database(database.engine)
    gateway = _CleanupGateway()
    events = _Events()
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory,
        _SelectiveTmdbClient(accepted={"The Office"}),
        plan_service,
    )
    transport = _FakeCleanupTransport(_cleanup_pages())
    service = OrganizationAutomationService(
        database.session_factory,
        _CleanupSettings(threshold_mb=100),
        preview,
        plan_service,
        lambda _authorized: gateway,
        event_logger=events,
        cleanup_transport_factory=lambda: transport,
    )

    assert await service.run_once() is True
    result = service.last_result
    assert result is not None
    assert result.scanned_count == 1
    assert result.plan_count == 2
    assert result.blocked_count == 0
    assert result.cleaned_small_files == 1
    # 叶子(7003)先删,其父(7002)随之变空再删,最后 7001
    assert result.cleaned_empty_dirs == 3
    assert transport.closed is True
    assert [(operation, file_id) for operation, file_id in transport.executed] == [
        (WriteOperation.DELETE, "7101"),
        (WriteOperation.DELETE, "7003"),
        (WriteOperation.DELETE, "7001"),
        (WriteOperation.DELETE, "7002"),
    ]
    touched = {file_id for _, file_id in transport.executed}
    assert "7102" not in touched  # 超过阈值的小文件删除线
    assert "7103" not in touched  # 已识别(move)文件绝不删除
    assert "1000" not in touched  # 源根目录自身保留
    assert any(
        event == "organize.automation.cleaned"
        for event, _fields in events.events
    )
    async with database.session_factory() as session:
        plans = list((await session.scalars(select(OrganizationPlan))).all())
        assert len(plans) == 2
        assert sorted(plan.status for plan in plans) == ["invalidated", "planned"]
        review_plan = next(plan for plan in plans if plan.status == "invalidated")
        move_plan = next(plan for plan in plans if plan.status == "planned")
        assert {
            action["object_id"] for action in json.loads(review_plan.actions_json)
        } == {"7101", "7102"}
        assert {
            action["object_id"] for action in json.loads(move_plan.actions_json)
        } == {"7103"}
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_cleanup_requires_injected_transport(tmp_path: Path):
    # 未注入清理 transport 时自动清理整体跳过:不删除、不失效计划。
    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'automation-clean-off.db'}"
    )
    await initialize_database(database.engine)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory,
        _SelectiveTmdbClient(accepted={"The Office"}),
        plan_service,
    )
    service = OrganizationAutomationService(
        database.session_factory,
        _CleanupSettings(threshold_mb=100),
        preview,
        plan_service,
        lambda _authorized: _CleanupGateway(),
    )

    assert await service.run_once() is True
    result = service.last_result
    assert result is not None
    assert result.cleaned_small_files == 0
    assert result.cleaned_empty_dirs == 0
    async with database.session_factory() as session:
        plans = list((await session.scalars(select(OrganizationPlan))).all())
        assert sorted(plan.status for plan in plans) == ["needs_review", "planned"]
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_automation_zero_threshold_skips_small_files_but_cleans_dirs(
    tmp_path: Path,
):
    # 阈值 0 表示“不自动删除小文件”,但空目录清理仍然执行。
    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'automation-clean-zero.db'}"
    )
    await initialize_database(database.engine)
    plan_service = OrganizationPlanService(database.session_factory)
    preview = OrganizationPreviewService(
        database.session_factory,
        _SelectiveTmdbClient(accepted={"The Office"}),
        plan_service,
    )
    transport = _FakeCleanupTransport(_cleanup_pages())
    service = OrganizationAutomationService(
        database.session_factory,
        _CleanupSettings(threshold_mb=0),
        preview,
        plan_service,
        lambda _authorized: _CleanupGateway(),
        cleanup_transport_factory=lambda: transport,
    )

    assert await service.run_once() is True
    result = service.last_result
    assert result is not None
    assert result.cleaned_small_files == 0
    assert result.cleaned_empty_dirs == 3
    assert [
        (operation, file_id) for operation, file_id in transport.executed
    ] == [
        (WriteOperation.DELETE, "7003"),
        (WriteOperation.DELETE, "7001"),
        (WriteOperation.DELETE, "7002"),
    ]
    async with database.session_factory() as session:
        plans = list((await session.scalars(select(OrganizationPlan))).all())
        # 没有删除任何文件 → 相关计划保持 needs_review,不失效
        assert sorted(plan.status for plan in plans) == ["needs_review", "planned"]
    await database.engine.dispose()
