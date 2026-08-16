"""Scheduled, read-first organization planning with bounded auto cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_c03_live_transport import P115C03LiveTransport
from watch_assistant.adapters.p115_library import (
    P115LibraryGateway,
    ScanState,
    scan_directory,
)
from watch_assistant.adapters.p115_library_gateway import (
    MAX_SCOPE_VERIFICATION_PAGES,
    P115ReadOnlyGatewayError,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_delete,
)
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import OrganizationOperation, OrganizationOperationStatus
from watch_assistant.schemas import OrganizationSettingsResponse
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
)
from watch_assistant.services.media_matcher import TmdbMatchError
from watch_assistant.services.media_parser import _is_junk_filename
from watch_assistant.services.organization_directory_provisioner import (
    OrganizationDirectoryProvisionError,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationConflict,
    OrganizationOperationPrerequisiteError,
    OrganizationOperationService,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanService,
    OrganizationPlanStatus,
    OrganizationPlanView,
)
from watch_assistant.services.organization_preview import (
    OrganizationPreviewError,
    OrganizationPreviewService,
    _scope_entries,
)
from watch_assistant.services.organization_target import (
    OrganizationTargetCatalog,
    OrganizationTargetError,
    read_target_catalog,
)
from watch_assistant.services.settings import SettingsService

logger = logging.getLogger(__name__)


class OrganizationAutomationError(ValueError):
    """Stable local automation error without remote values."""

    def __init__(
        self,
        code: str,
        *,
        phase: OrganizationAutomationPhase | None = None,
    ) -> None:
        self.code = code
        self.phase = phase
        super().__init__(code)


OrganizationAutomationPhase = Literal[
    "credentials", "directory_read", "scan", "tmdb", "plan", "execution"
]


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationBlockedDetail:
    source_directory_id: str | None
    phase: OrganizationAutomationPhase
    error_code: str
    message_zh: str
    next_step_zh: str

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "source_directory_id": self.source_directory_id,
            "phase": self.phase,
            "error_code": self.error_code,
            "message_zh": self.message_zh,
            "next_step_zh": self.next_step_zh,
        }

    def __repr__(self) -> str:
        return (
            "OrganizationBlockedDetail(source_directory_id_present="
            f"{self.source_directory_id is not None}, "
            f"phase={self.phase!r}, "
            f"error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True)
class OrganizationResultItem:
    title: str
    tmdb_id: int | None
    target: str | None
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationAutomationResult:
    source_count: int
    scanned_count: int
    plan_count: int
    queued_count: int
    blocked_count: int
    blocked_details: tuple[OrganizationBlockedDetail, ...] = ()
    plan_ids: tuple[str, ...] = ()
    finished_at: datetime | None = None
    run_id: str | None = None
    cleaned_small_files: int = 0
    cleaned_empty_dirs: int = 0

    def __repr__(self) -> str:
        return (
            "OrganizationAutomationResult(source_count="
            f"{self.source_count}, scanned_count={self.scanned_count}, "
            f"plan_count={self.plan_count}, queued_count={self.queued_count}, "
            f"blocked_count={self.blocked_count}, "
            f"blocked_detail_count={len(self.blocked_details)}, "
            f"plan_count={len(self.plan_ids)}, "
            f"cleaned_small_files={self.cleaned_small_files}, "
            f"cleaned_empty_dirs={self.cleaned_empty_dirs}, "
            f"finished_at={self.finished_at!r}, "
            f"run_id_present={self.run_id is not None})"
        )


@dataclass(frozen=True, slots=True)
class _SmallFileCandidate:
    object_id: str
    parent_id: str
    name: str


@dataclass(slots=True)
class _SourceDirectoryTree:
    """Live children of one source root, keyed by directory id."""

    files: dict[str, set[str]] = field(default_factory=dict)
    children: dict[str, set[str]] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)


CleanupTransportFactory = Callable[
    [], Awaitable[P115C03LiveTransport] | P115C03LiveTransport
]
_CLEANUP_CALL_TIMEOUT_SECONDS = 30.0


GatewayFactory = Callable[[Collection[str]], P115LibraryGateway]
DirectoryProvisioner = Callable[[str, Mapping[str, str], Collection[str]], Awaitable[None]]


class OrganizationAutomationService:
    """Build plans from configured roots and queue only approved auto plans."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings_service: SettingsService,
        preview_service: OrganizationPreviewService,
        plan_service: OrganizationPlanService,
        gateway_factory: GatewayFactory,
        *,
        operation_service: OrganizationOperationService | None = None,
        auto_execute: bool = False,
        directory_provisioner: DirectoryProvisioner | None = None,
        hydrate_file_details: bool = False,
        event_logger: object | None = None,
        cleanup_transport_factory: CleanupTransportFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings_service
        self._preview = preview_service
        self._plans = plan_service
        self._gateway_factory = gateway_factory
        self._operations = operation_service
        self._hydrate_file_details = bool(hydrate_file_details)
        # Auto-execution only confirms and queues plans; the real write gate
        # stays at the worker/transport level.  The service never provisions
        # directories and never holds write credentials.
        self._auto_execute = bool(auto_execute)
        del directory_provisioner
        # 自动清理的远端写入口:由运行时注入生产 transport
        # (P115C03ProductionTransport + p115_c03_timeout_executor)。未注入时
        # 自动清理整体跳过(读取型自动化保持可用)。
        self._cleanup_transport_factory = cleanup_transport_factory
        self._event_logger = event_logger or settings_service
        self._lock = asyncio.Lock()
        self.last_result: OrganizationAutomationResult | None = None

    async def run_once(
        self,
        *,
        manual_confirmation: bool = False,
        run_id: str | None = None,
    ) -> bool:
        """Run one bounded planning pass; return whether work was attempted."""

        run_id = run_id or f"org_{uuid.uuid4().hex}"
        async with self._lock:
            settings = await self._settings.get_organization()
            if not settings.source_directory_ids or not settings.target_directory_id:
                self.last_result = OrganizationAutomationResult(
                    source_count=len(settings.source_directory_ids),
                    scanned_count=0,
                    plan_count=0,
                    queued_count=0,
                    blocked_count=1,
                    blocked_details=(_blocked_detail(None, "organization_settings_incomplete"),),
                    finished_at=datetime.now(UTC),
                    run_id=run_id,
                )
                await self._log_blocked(None, "organization_settings_incomplete")
                return False
            try:
                result = await self._run(
                    settings, manual_confirmation=manual_confirmation
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - scheduler remains alive
                error_code, phase = _classify_error(error)
                self.last_result = OrganizationAutomationResult(
                    source_count=len(settings.source_directory_ids),
                    scanned_count=0,
                    plan_count=0,
                    queued_count=0,
                    blocked_count=1,
                    blocked_details=(_blocked_detail(None, error_code, phase=phase),),
                    finished_at=datetime.now(UTC),
                    run_id=run_id,
                )
                await self._log_blocked(None, error_code, phase=phase)
                return True
            self.last_result = OrganizationAutomationResult(
                source_count=result.source_count,
                scanned_count=result.scanned_count,
                plan_count=result.plan_count,
                queued_count=result.queued_count,
                blocked_count=result.blocked_count,
                blocked_details=result.blocked_details,
                plan_ids=result.plan_ids,
                finished_at=datetime.now(UTC),
                run_id=run_id,
                cleaned_small_files=result.cleaned_small_files,
                cleaned_empty_dirs=result.cleaned_empty_dirs,
            )
            return True

    async def _run(
        self,
        settings: OrganizationSettingsResponse,
        *,
        manual_confirmation: bool = False,
    ) -> OrganizationAutomationResult:
        target_id = settings.target_directory_id
        if target_id is None:
            raise OrganizationAutomationError("target_directory_missing")
        authorized_ids = tuple(dict.fromkeys((*settings.source_directory_ids, target_id)))
        gateway = self._gateway_factory(authorized_ids)
        try:
            catalog = await read_target_catalog(gateway, target_id)
        except asyncio.CancelledError:
            raise
        except OrganizationTargetError as error:
            raise OrganizationAutomationError(
                _stable_error_code(error), phase="directory_read"
            ) from None
        except P115ReadOnlyGatewayError as error:
            error_code, phase = _classify_error(
                error, default_phase="directory_read"
            )
            raise OrganizationAutomationError(error_code, phase=phase) from None
        except Exception:  # noqa: BLE001 - keep remote directory details private
            raise OrganizationAutomationError(
                "target_directory_read_failed", phase="directory_read"
            ) from None
        if any(source_id in catalog.by_path.values() for source_id in settings.source_directory_ids):
            raise OrganizationAutomationError("source_target_overlap")

        scanned = plans = queued = blocked = 0
        cleaned_small = cleaned_empty = 0
        plan_ids: list[str] = []
        blocked_details: list[OrganizationBlockedDetail] = []
        for source_id in settings.source_directory_ids:
            try:
                library_id = await self._ensure_source_library(
                    gateway, source_id
                )
                scanner = LibraryIndexService(
                    self._session_factory,
                    gateway,
                    library_id=library_id,
                    root_directory_id=source_id,
                    # 现场扫描用与库扫描调度器一致的默认页大小(100):
                    # page_size=1 时每条目一次 fs_files 调用,条目一多(如
                    # 目录含整季 10+ 文件)就触发 115 限流 gateway_error,
                    # 导致自动整理扫描反复失败(2026-08 实测两次)。
                    hydrate_file_details=self._hydrate_file_details,
                )
                scan = await scanner.scan_tree(_scan_idempotency_key(source_id))
                if not scan.complete:
                    error_code = scan.error_code or "scan_incomplete"
                    blocked += 1
                    blocked_details.append(
                        _blocked_detail(source_id, error_code, phase="scan")
                    )
                    await self._log_blocked(source_id, error_code, phase="scan")
                    continue
                target_tree_recorded = await self._target_tree_recorded(
                    scan.run_id, target_id
                )
                if (
                    not target_tree_recorded
                    and await self._scan_contains_directory(
                        scan.run_id,
                        (set(settings.source_directory_ids) | {target_id})
                        - {source_id},
                    )
                ):
                    blocked += 1
                    blocked_details.append(
                        _blocked_detail(
                            source_id, "source_target_overlap", phase="scan"
                        )
                    )
                    await self._log_blocked(source_id, "source_target_overlap", phase="scan")
                    continue
                try:
                    await self._record_target_tree(
                        scan.run_id, target_id, catalog
                    )
                except (
                    OrganizationAutomationError,
                    OrganizationPlanError,
                    OrganizationTargetError,
                ) as error:
                    error_code, phase = _classify_error(error)
                    blocked += 1
                    blocked_details.append(
                        _blocked_detail(source_id, error_code, phase=phase)
                    )
                    await self._log_blocked(source_id, error_code, phase=phase)
                    continue
                scanned += 1
                preview_kwargs = {
                    "library_id": library_id,
                    "scan_run_id": scan.run_id,
                    "source_directory_ids": (source_id,),
                    "target_directory_id": target_id,
                    "target_directories": catalog.by_path,
                    "existing_target_files": catalog.files,
                    "video_extensions": settings.video_extensions,
                    "metadata_extensions": settings.metadata_extensions,
                    "small_file_threshold_mb": settings.small_file_threshold_mb,
                    "rename_enabled": settings.rename_enabled,
                    "region_grouping_enabled": settings.region_grouping_enabled,
                    "year_grouping_enabled": settings.year_grouping_enabled,
                    "include_children_category": settings.include_children_category,
                    "include_concert_category": settings.include_concert_category,
                    "media_probe_enabled": settings.media_probe_enabled,
                    "ai_identification_enabled": settings.ai_identification_enabled,
                    "cleanup_empty_directories": settings.cleanup_empty_directories,
                    "strm_linkage_enabled": settings.strm_linkage_enabled,
                    "prefer_remux": settings.prefer_remux,
                    "prefer_resolution": settings.prefer_resolution,
                    "prefer_dolby": settings.prefer_dolby,
                    "conflict_mode": settings.conflict_mode,
                    "multi_version_enabled": settings.multi_version_enabled,
                    "manual_confirmation": manual_confirmation,
                }
                create_previews = getattr(self._preview, "create_previews", None)
                if callable(create_previews):
                    preview_plans = list(await create_previews(**preview_kwargs))
                else:
                    preview_plans = [await self._preview.create_preview(**preview_kwargs)]
                plans += len(preview_plans)
                queue_enabled = (
                    self._auto_execute
                    and self._operations is not None
                    and not manual_confirmation
                    and settings.auto_execute_enabled
                )
                for plan in preview_plans:
                    plan_ids.append(plan.plan_id)
                    if (
                        queue_enabled
                        and plan.status == OrganizationPlanStatus.PLANNED.value
                    ):
                        outcome = await self._queue_auto_plan(plan.plan_id)
                        if outcome is None:
                            queued += 1
                            await self._log_preview(
                                plan.status.value,
                                plan.source_count,
                                auto_queued=True,
                            )
                        else:
                            blocked += 1
                            blocked_details.append(
                                _blocked_detail(source_id, outcome, phase="plan")
                            )
                            await self._log_blocked(
                                source_id, outcome, phase="plan"
                            )
                    else:
                        await self._log_preview(plan.status.value, plan.source_count)
                cleaned_files, cleaned_dirs = await self._auto_clean_source(
                    source_id,
                    library_id=library_id,
                    scan_run_id=scan.run_id,
                    preview_plans=preview_plans,
                    small_file_threshold_mb=settings.small_file_threshold_mb,
                    cleanup_empty_directories=settings.cleanup_empty_directories,
                    operation_delay_seconds=settings.operation_delay_seconds,
                    manual_confirmation=manual_confirmation,
                    auto_cleanup_junk_files=settings.auto_cleanup_junk_files,
                )
                cleaned_small += cleaned_files
                cleaned_empty += cleaned_dirs
            except asyncio.CancelledError:
                raise
            except (
                OrganizationAutomationError,
                OrganizationDirectoryProvisionError,
                LibraryIndexError,
                OrganizationPlanError,
                OrganizationPreviewError,
                P115ReadOnlyGatewayError,
                OrganizationTargetError,
                TmdbMatchError,
            ) as error:
                error_code, phase = _classify_error(error)
                blocked += 1
                blocked_details.append(
                    _blocked_detail(source_id, error_code, phase=phase)
                )
                await self._log_blocked(source_id, error_code, phase=phase)
        return OrganizationAutomationResult(
            source_count=len(settings.source_directory_ids),
            scanned_count=scanned,
            plan_count=plans,
            queued_count=queued,
            blocked_count=blocked,
            blocked_details=tuple(blocked_details),
            plan_ids=tuple(plan_ids),
            cleaned_small_files=cleaned_small,
            cleaned_empty_dirs=cleaned_empty,
        )

    async def result_items(self) -> tuple[OrganizationResultItem, ...]:
        """Read current media outcomes for the most recent automation pass."""

        result = self.last_result
        if result is None or not result.plan_ids:
            return ()
        async with self._session_factory() as session:
            plans = list(
                (
                    await session.scalars(
                        select(OrganizationPlan).where(
                            OrganizationPlan.id.in_(result.plan_ids)
                        )
                    )
                ).all()
            )
            operations = list(
                (
                    await session.scalars(
                        select(OrganizationOperation)
                        .where(
                            OrganizationOperation.plan_id.in_(result.plan_ids)
                        )
                        .order_by(
                            OrganizationOperation.created_at.asc(),
                            OrganizationOperation.id.asc(),
                        )
                    )
                ).all()
            )
        # A plan may carry several operations (a failed attempt plus its
        # retry); the last row per plan is the newest and most relevant one.
        operation_by_plan = {item.plan_id: item for item in operations}
        items: list[OrganizationResultItem] = []
        for plan in sorted(plans, key=lambda item: item.created_at):
            actions = _json_list(plan.actions_json)
            basis = _json_list(plan.basis_json)
            operation = operation_by_plan.get(plan.id)
            operation_status = operation.status.value if operation is not None else None
            for action, evidence in zip(actions, basis, strict=False):
                if not isinstance(action, dict) or not isinstance(evidence, dict):
                    continue
                if (
                    action.get("kind") != "move"
                    or operation_status is None
                    and plan.status != OrganizationPlanStatus.PLANNED.value
                ):
                    status = "needs_review"
                elif operation_status is None:
                    status = "skipped"
                elif operation_status == OrganizationOperationStatus.ORGANIZED.value:
                    status = "success"
                elif operation_status == OrganizationOperationStatus.FAILED.value:
                    status = "failed"
                elif operation_status == OrganizationOperationStatus.UNCERTAIN.value:
                    status = "uncertain"
                elif operation_status == OrganizationOperationStatus.ORGANIZING.value:
                    status = "organizing"
                else:
                    status = "queued"
                title = evidence.get("title")
                items.append(
                    OrganizationResultItem(
                        title=title if isinstance(title, str) and title else "未识别影片",
                        tmdb_id=(
                            evidence.get("tmdb_id")
                            if isinstance(evidence.get("tmdb_id"), int)
                            else None
                        ),
                        target=(
                            action.get("target")
                            if isinstance(action.get("target"), str)
                            else None
                        ),
                        status=status,
                        error_code=(
                            operation.error_code if operation is not None else None
                        ),
                    )
                )
        return tuple(items)

    async def _ensure_source_library(
        self, gateway: P115LibraryGateway, source_id: str
    ) -> str:
        library_id = _source_library_id(source_id)
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, library_id)
            if library is None:
                library = MediaLibrary(
                    id=library_id,
                    name="115 自动整理源目录",
                    root_directory_id=source_id,
                    scope_verified=False,
                    enabled=False,
                    revision=0,
                )
                session.add(library)
                await session.flush()
            elif library.root_directory_id != source_id:
                raise OrganizationAutomationError("source_scope_changed")

            if not library.scope_verified or not library.enabled:
                try:
                    scan = await scan_directory(
                        gateway,
                        source_id,
                        page_size=1,
                        max_pages=MAX_SCOPE_VERIFICATION_PAGES,
                        require_explicit_complete=True,
                    )
                except asyncio.CancelledError:
                    raise
                except P115ReadOnlyGatewayError as error:
                    error_code, phase = _classify_error(
                        error, default_phase="directory_read"
                    )
                    raise OrganizationAutomationError(
                        error_code, phase=phase
                    ) from None
                except Exception:  # noqa: BLE001 - scope verification stays fail-closed
                    raise OrganizationAutomationError(
                        "gateway_error", phase="directory_read"
                    ) from None
                if scan.state is not ScanState.COMPLETE or scan.scan_complete is not True:
                    raise OrganizationAutomationError(
                        "source_scope_unverified", phase="directory_read"
                    )
                library.scope_verified = True
                library.enabled = True
                library.revision += 1
            await session.commit()
        return library_id

    async def _scan_contains_directory(
        self, scan_run_id: str, directory_ids: set[str]
    ) -> bool:
        async with self._session_factory() as session:
            found = await session.scalar(
                select(LibraryScanEntry.object_id)
                .where(
                    LibraryScanEntry.scan_run_id == scan_run_id,
                    LibraryScanEntry.object_type == "directory",
                    LibraryScanEntry.object_id.in_(directory_ids),
                )
                .limit(1)
            )
        return found is not None

    async def _target_tree_recorded(self, scan_run_id: str, target_id: str) -> bool:
        """Return whether this scan run already carries the target-tree evidence.

        A completed tree run may be reused across passes (same idempotency
        key); the target tree recorded by a previous pass must not be
        mistaken for a source-target overlap.
        """
        async with self._session_factory() as session:
            found = await session.scalar(
                select(LibraryScanEntry.object_id)
                .where(
                    LibraryScanEntry.scan_run_id == scan_run_id,
                    LibraryScanEntry.object_type == "directory",
                    LibraryScanEntry.object_id == target_id,
                )
                .limit(1)
            )
        return found is not None

    async def _record_target_tree(
        self,
        scan_run_id: str,
        target_id: str,
        catalog: OrganizationTargetCatalog,
    ) -> None:
        """Append the frozen target directory tree to the source scan run.

        Plan confirmation re-validates executable steps against the scan run's
        managed directory set, which must include every target directory the
        plan may write into.  The target catalog was read (read-only) earlier
        in this pass, so the tree is recorded after the source-target overlap
        check and before preview creation.
        """
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, scan_run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, scan_run_id)
            if run is None or checkpoint is None:
                raise OrganizationAutomationError(
                    "scan_not_current", phase="scan"
                )
            existing_ids = set(
                (
                    await session.scalars(
                        select(LibraryScanEntry.object_id).where(
                            LibraryScanEntry.scan_run_id == scan_run_id,
                            LibraryScanEntry.object_type == "directory",
                        )
                    )
                ).all()
            )
            if target_id in existing_ids:
                return
            by_path = catalog.by_path
            target_rows: list[tuple[str, str, str, str]] = [
                (target_id, run.root_directory_id, "target-root", "target-root")
            ]
            for path, object_id in by_path.items():
                if not path:
                    continue
                parent_path = str(PurePosixPath(path).parent)
                parent_key = "" if parent_path == "." else _index_path(parent_path)
                parent = (
                    run.root_directory_id
                    if not parent_key
                    else by_path.get(parent_key, target_id)
                )
                target_rows.append(
                    (object_id, parent, PurePosixPath(path).name, path)
                )
            for object_id, parent_id, name, path in target_rows:
                if object_id in existing_ids:
                    continue
                session.add(
                    LibraryScanEntry(
                        scan_run_id=scan_run_id,
                        object_type="directory",
                        object_id=object_id,
                        parent_id=parent_id,
                        name=name,
                        path=path,
                        is_directory=True,
                    )
                )
            await session.flush()
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id
                        )
                    )
                ).all()
            )
            visited = [run.root_directory_id]
            directory_totals = {run.root_directory_id: 0}
            for entry in entries:
                assert isinstance(entry.parent_id, str)
                directory_totals[entry.parent_id] = (
                    directory_totals.get(entry.parent_id, 0) + 1
                )
                if entry.is_directory:
                    visited.append(entry.object_id)
                    directory_totals.setdefault(entry.object_id, 0)
            assert set(directory_totals) == set(visited)
            run.scan_mode = "tree"
            run.expected_total = len(entries)
            run.items_seen = len(entries)
            checkpoint.page = run.pages_read
            checkpoint.items_seen = len(entries)
            checkpoint.cursor_json = json.dumps(
                {
                    "version": 2,
                    "directory_totals": directory_totals,
                    "expected_total": len(entries),
                    "pending": [],
                    "visited": visited,
                },
                ensure_ascii=False,
            )
            await session.commit()

    async def _queue_auto_plan(self, plan_id: str) -> str | None:
        """Confirm and queue one auto-partition plan.

        Returns None on success; a stable error code when the plan could not
        be queued.  Confirmation re-validates the executable steps so a plan
        whose snapshot prerequisites changed is never queued.
        """
        try:
            async with self._session_factory() as session:
                plan = await session.get(OrganizationPlan, plan_id)
                if plan is None:
                    return "plan_not_found"
                expected_revision = plan.revision
            view = await self._plans.confirm_plan(
                plan_id, expected_revision=expected_revision
            )
            await self._operations.create(
                plan_id,
                idempotency_key=f"auto-{plan_id}",
                expected_plan_revision=view.revision,
            )
        except (
            OrganizationPlanError,
            OrganizationOperationConflict,
            OrganizationOperationPrerequisiteError,
        ) as error:
            return _stable_error_code(error)
        except Exception:  # noqa: BLE001 - a queue failure never fails the pass
            return "auto_queue_failed"
        return None

    async def _auto_clean_source(
        self,
        source_id: str,
        *,
        library_id: str,
        scan_run_id: str,
        preview_plans: Sequence[OrganizationPlanView],
        small_file_threshold_mb: float,
        cleanup_empty_directories: bool,
        operation_delay_seconds: float,
        manual_confirmation: bool,
        auto_cleanup_junk_files: bool,
    ) -> tuple[int, int]:
        """Delete unrecognized small files/junk and prune empty source dirs.

        Runs after preview generation, per source, strictly best-effort: any
        failure leaves the pass result intact (the blocked-detail bookkeeping
        stays untouched) and cleanup simply does not count that pass.

        删除必须与移动/重命名共享同一人工确认门禁:人工确认模式下所有
        远端删除一并挂起;空目录清理还额外受 ``cleanup_empty_directories``
        配置开关控制(默认关闭),不得仅凭写开关注入就执行。

        广告垃圾文件清理 (``auto_cleanup_junk_files``) 同样受此门禁约束,且
        仅在这些前提下执行,任一不满足则整体跳过(fail-closed):
        1. 仅在完整扫描通过后执行 —— ``_auto_clean_source`` 只在预览成功后
           调用,预览要求已验证的完整快照 (validate_complete_scan_evidence)。
        2. 仅限受管源目录范围 —— 垃圾候选经 ``_scope_entries`` 限定到当前
           source 子树,绝不越出配置范围。
        3. 仅走 115 回收站(fs_delete/prepare_delete),不永久删除。
        4. manual_confirmation 为真时跳过全部远端删除。
        """
        if manual_confirmation:
            return 0, 0
        if self._cleanup_transport_factory is None:
            return 0, 0
        try:
            transport = await _resolve_transport(self._cleanup_transport_factory())
        except Exception:  # noqa: BLE001 - cleanup stays best-effort
            logger.warning(
                "auto-clean transport unavailable; skipping cleanup for source"
            )
            return 0, 0
        cleaned_files = cleaned_dirs = cleaned_junk = 0
        deleted_ids: set[str] = set()
        try:
            if small_file_threshold_mb > 0:
                candidates = await self._small_review_file_candidates(
                    scan_run_id,
                    preview_plans,
                    small_file_threshold_mb=small_file_threshold_mb,
                )
                if candidates:
                    deleted_ids = await self._delete_small_files(
                        transport,
                        candidates,
                        operation_delay_seconds=operation_delay_seconds,
                    )
                    cleaned_files = len(deleted_ids)
                    if deleted_ids:
                        await self._invalidate_plans_for_deleted(
                            library_id, deleted_ids
                        )
            if auto_cleanup_junk_files:
                junk_candidates = await self._junk_file_candidates(
                    scan_run_id,
                    source_id,
                    library_id=library_id,
                    target_directory_id=None,
                )
                if junk_candidates:
                    junk_deleted = await self._delete_small_files(
                        transport,
                        junk_candidates,
                        operation_delay_seconds=operation_delay_seconds,
                    )
                    cleaned_junk = len(junk_deleted)
                    if junk_deleted:
                        await self._invalidate_plans_for_deleted(
                            library_id, junk_deleted
                        )
            if cleanup_empty_directories:
                cleaned_dirs = await self._cleanup_empty_directories(
                    transport,
                    source_id,
                    operation_delay_seconds=operation_delay_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("auto-clean failed; the pass continues without cleanup")
        finally:
            try:
                await _close_transport(transport)
            except Exception:  # client teardown never fails the pass
                logger.warning("auto-clean transport close failed", exc_info=True)
        if cleaned_files or cleaned_dirs or cleaned_junk:
            await self._log_cleaned(
                cleaned_files, cleaned_dirs, junk_files=cleaned_junk
            )
        return cleaned_files, cleaned_dirs

    async def _small_review_file_candidates(
        self,
        scan_run_id: str,
        preview_plans: Sequence[OrganizationPlanView],
        *,
        small_file_threshold_mb: float,
    ) -> tuple[_SmallFileCandidate, ...]:
        """Collect review-only sources from the just-created plans that are
        smaller than the threshold.

        Only ``review`` actions qualify (matched/move files are never touched).
        The scan snapshot rows provide the authoritative size; the plan
        snapshot carries the name/parent pair the plan committed to, and both
        must agree before a file may be considered for cleanup.
        """
        threshold_bytes = small_file_threshold_mb * 1024 * 1024
        plan_ids = tuple(plan.plan_id for plan in preview_plans)
        if not plan_ids:
            return ()
        review_sources: dict[str, tuple[str, str]] = {}
        move_sources: set[str] = set()
        async with self._session_factory() as session:
            plans = list(
                (
                    await session.scalars(
                        select(OrganizationPlan).where(
                            OrganizationPlan.id.in_(plan_ids)
                        )
                    )
                ).all()
            )
            for plan in plans:
                for action in _json_list(plan.actions_json):
                    if not isinstance(action, dict):
                        continue
                    object_id = action.get("object_id")
                    if not isinstance(object_id, str) or not object_id:
                        continue
                    if action.get("kind") == "move":
                        move_sources.add(object_id)
                        continue
                    parent_id = action.get("source_parent_id")
                    name = action.get("source_name")
                    if (
                        isinstance(parent_id, str)
                        and isinstance(name, str)
                        and name
                    ):
                        review_sources.setdefault(object_id, (parent_id, name))
            if not review_sources:
                return ()
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id,
                            LibraryScanEntry.object_id.in_(tuple(review_sources)),
                            LibraryScanEntry.is_directory.is_(False),
                        )
                    )
                ).all()
            )
        candidates: list[_SmallFileCandidate] = []
        for row in rows:
            if row.object_id in move_sources:
                continue
            if (
                row.size_bytes is None
                or row.size_bytes < 0
                or row.size_bytes >= threshold_bytes
                or not isinstance(row.parent_id, str)
                or row.parent_id != review_sources[row.object_id][0]
                or row.name != review_sources[row.object_id][1]
            ):
                continue
            candidates.append(
                _SmallFileCandidate(row.object_id, row.parent_id, row.name)
            )
        return tuple(candidates)

    async def _junk_file_candidates(
        self,
        scan_run_id: str,
        source_id: str,
        *,
        library_id: str,
        target_directory_id: str | None,
    ) -> tuple[_SmallFileCandidate, ...]:
        """Collect advertisement junk files directly from the scan snapshot.

        广告垃圾文件在预览阶段被 ``_is_junk_filename`` 过滤,永远不会进入
        plan actions,因此不能像小文件那样从计划快照收集,必须直接从
        LibraryScanEntry 快照收集。候选仅限非目录且文件名含广告特征且
        无任何媒体特征(``media_parser._is_junk_filename``)的文件。

        范围门禁与预览一致:用 ``_scope_entries`` 限定到 source 子树 (source
        作为受管源目录时即整个已验证快照;快照里额外录入了 target 目录树,
        但那些都是目录,不构成文件候选)。source 不在扫描快照内时
        ``_scope_entries`` 抛 OrganizationPreviewError (fail-closed),由
        ``_auto_clean_source`` 外层容错,不破坏 pass。

        MediaLibrary 不存在或 root_directory_id 为空时整体跳过(返回空)。
        """
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, library_id)
            if library is None or not library.root_directory_id:
                return ()
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id
                        )
                    )
                ).all()
            )
        scoped = _scope_entries(
            entries,
            source_directory_ids=(source_id,),
            root_directory_id=library.root_directory_id,
            target_directory_id=target_directory_id,
        )
        candidates = [
            _SmallFileCandidate(
                entry.object_id, entry.parent_id, entry.name
            )
            for entry in scoped
            if (
                not entry.is_directory
                and isinstance(entry.parent_id, str)
                and entry.parent_id
                and _is_junk_filename(entry.name)
            )
        ]
        return tuple(candidates)

    async def _delete_small_files(
        self,
        transport: P115C03LiveTransport,
        candidates: Sequence[_SmallFileCandidate],
        *,
        operation_delay_seconds: float,
    ) -> set[str]:
        """Recycle (fs_delete, recoverable) one candidate at a time.

        候选身份由调用方从"已验证完整扫描快照"构建(id/name/parent 与快照
        一致)。删除前尽力做实时父目录核对:小目录可完整读取时严格核对
        (唯一非目录同名同 id),文件已移动/消失则跳过——fail-closed;目录
        条目超过 C03 分页实测上限(每页 1 条 × 8 页)导致列表不完整时,若
        部分列表未显示该 id 名字已变化则降级为快照身份直接回收站删除,
        文件已不存在时 prepare_delete 由 115 侧返回失败,不会误删。
        """
        deleted_ids: set[str] = set()
        for candidate in candidates:
            await _pace(operation_delay_seconds)
            listing = await transport.list_children(
                candidate.parent_id,
                timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS,
            )
            if listing.complete:
                matches = [
                    entry
                    for entry in listing.entries
                    if entry.file_id == candidate.object_id
                    and not entry.is_directory
                    and entry.name == candidate.name
                ]
                if len(matches) != 1:
                    continue
            elif listing.entries and any(
                entry.file_id == candidate.object_id
                and entry.name != candidate.name
                for entry in listing.entries
            ):
                # 部分列表已看到该 id 且名字不符:文件已变化,快照身份失效。
                continue
            await _pace(operation_delay_seconds)
            receipt = await transport.execute(
                prepare_delete(candidate.object_id),
                timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS,
            )
            if receipt.status is WriteStatus.SUCCESS:
                deleted_ids.add(candidate.object_id)
        return deleted_ids

    async def _invalidate_plans_for_deleted(
        self, library_id: str, deleted_ids: Collection[str]
    ) -> None:
        """Invalidate every active plan whose source snapshot references a
        deleted file, so the pending list never shows review items whose file
        is already in the 115 recycle bin."""
        if not deleted_ids:
            return
        deleted = set(deleted_ids)
        async with self._session_factory() as session:
            plans = list(
                (
                    await session.scalars(
                        select(OrganizationPlan).where(
                            OrganizationPlan.library_id == library_id,
                            OrganizationPlan.status.in_(
                                (
                                    OrganizationPlanStatus.NEEDS_REVIEW.value,
                                    OrganizationPlanStatus.PLANNED.value,
                                )
                            ),
                        )
                    )
                ).all()
            )
        for plan in plans:
            snapshot = _json_list(plan.source_snapshot_json)
            if not any(
                isinstance(item, dict) and item.get("object_id") in deleted
                for item in snapshot
            ):
                continue
            try:
                await self._plans.invalidate_plan(plan.id)
            except OrganizationPlanError:
                continue

    async def _cleanup_empty_directories(
        self,
        transport: P115C03LiveTransport,
        source_id: str,
        *,
        operation_delay_seconds: float,
    ) -> int:
        """Read the live source tree once, then recycle empty directories
        deepest-first.

        Only directories that were empty in the full tree read AND are still
        empty in a fresh listing right before deletion are recycled; the source
        root itself is never deleted.  Removing a child directory re-opens its
        parent for the same check (leaf-upward pruning).
        """
        tree = await self._read_directory_tree(transport, source_id, operation_delay_seconds)
        if tree is None:
            return 0
        depths = _directory_depths(tree, source_id)
        candidates = sorted(
            (
                directory_id
                for directory_id in tree.files
                if directory_id != source_id
                and _directory_empty(tree, directory_id)
            ),
            key=lambda item: (-depths.get(item, 0), item),
        )
        cleaned = 0
        deleted: set[str] = set()
        processed = 0
        while processed < len(candidates):
            directory_id = candidates[processed]
            processed += 1
            if directory_id in deleted or not _directory_empty(tree, directory_id):
                continue
            await _pace(operation_delay_seconds)
            listing = await transport.list_children(
                directory_id, timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS
            )
            if not listing.complete or listing.entries:
                # Repopulated (or unverifiable) since the tree read: leave it.
                continue
            await _pace(operation_delay_seconds)
            receipt = await transport.execute(
                prepare_delete(directory_id),
                timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS,
            )
            if receipt.status is not WriteStatus.SUCCESS:
                continue
            cleaned += 1
            deleted.add(directory_id)
            tree.files.pop(directory_id, None)
            tree.children.pop(directory_id, None)
            parent_id = tree.parents.get(directory_id)
            if parent_id is not None and parent_id in tree.files:
                tree.children[parent_id].discard(directory_id)
                if (
                    parent_id != source_id
                    and _directory_empty(tree, parent_id)
                    and parent_id not in deleted
                ):
                    candidates.append(parent_id)
        return cleaned

    async def _read_directory_tree(
        self,
        transport: P115C03LiveTransport,
        source_id: str,
        operation_delay_seconds: float,
    ) -> _SourceDirectoryTree | None:
        """Read the full live subtree below one source root.

        Returns ``None`` (and deletes nothing) when any listing is incomplete,
        so cleanup only ever runs against a fully observed tree.
        """
        tree = _SourceDirectoryTree()
        pending = [source_id]
        while pending:
            directory_id = pending.pop()
            if directory_id in tree.files or directory_id in tree.children:
                continue  # cycle guard: each directory is read once
            await _pace(operation_delay_seconds)
            listing = await transport.list_children(
                directory_id, timeout_seconds=_CLEANUP_CALL_TIMEOUT_SECONDS
            )
            if not listing.complete:
                return None
            tree.files.setdefault(directory_id, set())
            tree.children.setdefault(directory_id, set())
            for entry in listing.entries:
                if entry.is_directory:
                    tree.children[directory_id].add(entry.file_id)
                    tree.parents[entry.file_id] = directory_id
                    if (
                        entry.file_id not in tree.files
                        and entry.file_id not in tree.children
                    ):
                        pending.append(entry.file_id)
                else:
                    tree.files[directory_id].add(entry.file_id)
        return tree

    async def _log_cleaned(
        self, cleaned_files: int, cleaned_dirs: int, *, junk_files: int = 0
    ) -> None:
        """记录自动清理审计事件。

        小文件/空目录沿用 ``organize.automation.cleaned``(counts 原样),
        仅当确有这两个维度被清理时才发,避免仅清理垃圾时发出零计数旧事件;
        广告垃圾文件单独用 ``library.auto_cleanup.applied``,任一清理发生
        即发,三个值都走 ``fields``——通知 sink 只转发 fields(见
        settings.log_event),counts 不进 handle_event,故垃圾清理结果需在
        fields 里才能生成站内通知。
        """
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if not callable(method):
            return
        if cleaned_files or cleaned_dirs:
            await method(
                "organize.automation.cleaned",
                counts={"small_files": cleaned_files, "empty_dirs": cleaned_dirs},
            )
        await method(
            "library.auto_cleanup.applied",
            fields={
                "small_files": cleaned_files,
                "empty_dirs": cleaned_dirs,
                "junk_files": junk_files,
            },
        )

    async def _log_preview(
        self, status: str, count: int, *, auto_queued: bool = False
    ) -> None:
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if callable(method):
            await method(
                "organize.preview.created",
                fields={"status": status},
                counts={"count": count},
            )
            if status == "needs_review":
                await method(
                    "organize.needs_review",
                    fields={"status": status, "count": count},
                )
            if (
                not auto_queued
                and status
                in {
                    OrganizationPlanStatus.PLANNED.value,
                    OrganizationPlanStatus.NEEDS_REVIEW.value,
                }
            ):
                await method(
                    "organize.plan.awaiting_confirmation",
                    fields={"status": status},
                    counts={"count": count},
                )

    async def _log_blocked(
        self,
        source_directory_id: str | None,
        error_code: str,
        *,
        phase: OrganizationAutomationPhase | None = None,
    ) -> None:
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if callable(method):
            await method(
                "organize.automation.blocked",
                fields={
                    "status": "blocked",
                    "error_code": error_code,
                    "source_directory_id": source_directory_id or "未指定",
                    "phase": phase or _phase_for_code(error_code),
                    "message_zh": _blocked_message_zh(error_code, phase=phase),
                    "next_step_zh": _next_step_zh(error_code, phase=phase),
                },
                counts={"count": 0},
            )


def _source_library_id(source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("ascii")).hexdigest()[:32]
    return f"org-source-{digest}"


def _json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


async def _resolve_transport(
    value: P115C03LiveTransport | Awaitable[P115C03LiveTransport],
) -> P115C03LiveTransport:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_transport(transport: P115C03LiveTransport) -> None:
    """Release the p115 client owned by an injected cleanup transport."""
    client = getattr(transport, "_client", None)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _pace(seconds: float) -> None:
    """Wind up the remote-call cadence (风控间隔) between cleanup calls."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _directory_empty(tree: _SourceDirectoryTree, directory_id: str) -> bool:
    return not tree.files.get(directory_id) and not tree.children.get(directory_id)


def _directory_depths(tree: _SourceDirectoryTree, root_id: str) -> dict[str, int]:
    depths = {root_id: 0}
    pending = [root_id]
    while pending:
        parent_id = pending.pop()
        for child_id in tree.children.get(parent_id, ()):
            if child_id in depths:
                continue
            depths[child_id] = depths[parent_id] + 1
            pending.append(child_id)
    return depths


def _scan_idempotency_key(source_id: str) -> str:
    bucket = int(datetime.now(UTC).timestamp())
    return f"organization-scan:{source_id}:{bucket}:{uuid.uuid4().hex[:8]}"


def _index_path(value: str) -> str:
    return value.strip("/").replace("\\", "/")


def _stable_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(error, P115ReadOnlyGatewayError):
        if code in {
            "credentials_missing",
            "credentials_unavailable",
            "client_unavailable",
            "blocked_environment",
        }:
            return code
        return "gateway_error"
    if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
        return code
    if isinstance(
        error,
        (
            LibraryIndexError,
            OrganizationDirectoryProvisionError,
            OrganizationOperationConflict,
            OrganizationOperationPrerequisiteError,
            OrganizationPlanError,
            OrganizationPreviewError,
            OrganizationTargetError,
            TmdbMatchError,
        ),
    ):
        message = str(error)
        if _SAFE_ERROR_CODE.fullmatch(message):
            return message
    return "automation_failed"


def _classify_error(
    error: Exception,
    *,
    default_phase: OrganizationAutomationPhase = "plan",
) -> tuple[str, OrganizationAutomationPhase]:
    """Reduce internal failures to a safe code and an actionable workflow phase."""

    code = _stable_error_code(error)
    explicit_phase = getattr(error, "phase", None)
    if explicit_phase in _AUTOMATION_PHASES:
        return code, explicit_phase
    if isinstance(error, P115ReadOnlyGatewayError):
        return code, "credentials" if code in _CREDENTIAL_CODES else default_phase
    if isinstance(error, LibraryIndexError):
        return code, "scan"
    if isinstance(error, TmdbMatchError):
        return code, "tmdb"
    if isinstance(error, OrganizationDirectoryProvisionError):
        return code, "execution"
    if isinstance(error, OrganizationTargetError):
        return code, "directory_read"
    if isinstance(error, OrganizationPlanError):
        return code, "plan"
    if isinstance(error, OrganizationPreviewError):
        return code, _phase_for_code(code, default="plan")
    return code, _phase_for_code(code, default=default_phase)


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_AUTOMATION_PHASES = frozenset(
    {"credentials", "directory_read", "scan", "tmdb", "plan", "execution"}
)
_CREDENTIAL_CODES = frozenset(
    {
        "credentials_missing",
        "credentials_unavailable",
        "client_unavailable",
        "blocked_environment",
    }
)
_SCAN_CODES = frozenset(
    {
        "scan_incomplete",
        "partial_page",
        "storage_error",
        "entry_path_invalid",
        "directory_cycle",
        "directory_limit_exceeded",
        "total_mismatch",
        "cancelled",
        "scan_not_current",
        "scan_entry_invalid",
        "source_directory_not_found",
        "entry_scope_unverified",
        "pagination_unverified",
    }
)
_TMDB_CODES = frozenset(
    {"tmdb_unavailable", "tmdb_timeout", "tmdb_rate_limited", "tmdb_malformed_response"}
)
_EXECUTION_CODES = frozenset(
    {"target_directory_create_failed", "target_directory_parent_missing"}
)


_STABLE_AUTOMATION_ERROR_CODES = frozenset(
    {
        "auto_queue_failed",
        "automation_failed",
        "blocked_environment",
        "candidate_target_unavailable",
        "client_unavailable",
        "credentials_missing",
        "credentials_unavailable",
        "gateway_error",
        "operation_creation_conflict",
        "operation_plan_conflict",
        "organization_settings_incomplete",
        "plan_expired",
        "plan_not_found",
        "plan_not_reviewable",
        "plan_prerequisites_changed",
        "plan_revision_changed",
        "source_scope_changed",
        "source_scope_unverified",
        "source_target_overlap",
        "stale_revision",
        "target_directory_create_failed",
        "target_directory_id_invalid",
        "target_directory_incomplete",
        "target_directory_parent_missing",
        "target_directory_read_failed",
        "target_entry_identity_unverified",
        "target_entry_scope_unverified",
        "target_entry_type_unverified",
        "target_file_identity_conflict",
    }
)


_BLOCKED_MESSAGES_ZH = {
    "automation_failed": "自动整理执行失败，原因暂不可用。",
    "auto_queue_failed": "自动排队未完成，已跳过该计划；请刷新后重新生成计划。",
    "stale_revision": "计划版本已变化，自动排队已跳过；请刷新后重试。",
    "plan_revision_changed": "计划版本已变化，自动排队已跳过；请刷新后重试。",
    "plan_expired": "计划已过期，自动排队已跳过；请重新扫描生成新计划。",
    "plan_not_reviewable": "计划状态不可排队，自动排队已跳过。",
    "plan_prerequisites_changed": "计划前置条件已变化，自动排队已跳过；请重新扫描生成新计划。",
    "plan_not_found": "计划已不存在，自动排队已跳过。",
    "operation_plan_conflict": "该计划已有整理操作，自动排队已跳过；请到“整理”页查看操作状态。",
    "operation_creation_conflict": "整理操作创建冲突，自动排队已跳过。",
    "organization_settings_incomplete": "整理来源或目标目录未配置。",
    "scan_incomplete": "源目录扫描未完成，已阻止生成整理预览。",
    "partial_page": "源目录分页扫描不完整，已阻止生成整理预览。",
    "gateway_error": "读取源目录失败，已阻止生成整理预览。",
    "blocked_environment": "115 当前不可执行安全读取，已阻止整理。",
    "client_unavailable": "115 客户端不可用，已阻止整理。",
    "credentials_missing": "115 登录凭据未配置，已阻止整理。",
    "credentials_unavailable": "115 登录状态不可用，已阻止整理。",
    "storage_error": "保存源目录扫描结果失败，已阻止生成整理预览。",
    "entry_path_invalid": "源目录扫描发现无效条目，已阻止生成整理预览。",
    "directory_cycle": "源目录扫描发现目录循环，已阻止生成整理预览。",
    "directory_limit_exceeded": "源目录超出扫描上限，已阻止生成整理预览。",
    "total_mismatch": "源目录扫描总数校验失败，已阻止生成整理预览。",
    "cancelled": "源目录扫描被取消，已阻止生成整理预览。",
    "source_scope_changed": "源目录范围已变化，需要重新确认。",
    "source_scope_unverified": "源目录范围未通过校验，已阻止整理。",
    "source_target_overlap": "扫描来源与整理目标重叠，已阻止生成整理预览。",
    "scan_not_current": "扫描快照不是最新完整快照，已阻止生成整理预览。",
    "scan_entry_invalid": "扫描快照包含无效条目，已阻止生成整理预览。",
    "source_directory_not_found": "扫描来源不存在，已阻止生成整理预览。",
    "no_video_files": "扫描来源没有可整理的视频文件。",
    "target_directory_parent_missing": "归档目录父级不存在，已阻止整理。",
    "target_directory_create_failed": "归档目录创建失败，已阻止整理。",
    "target_directory_id_invalid": "归档目录 ID 无效，已阻止整理。",
    "target_directory_incomplete": "归档目录扫描未完成，已阻止整理。",
    "target_directory_read_failed": "读取归档目录失败，115 当前未返回完整目录；已停止本轮整理，请确认账号可访问该目录后重试。",
    "target_entry_identity_unverified": "归档目录条目缺少可靠 ID，已阻止整理。",
    "target_entry_scope_unverified": "归档目录条目的父目录证据不一致，已阻止整理。",
    "target_entry_type_unverified": "归档目录条目类型证据不可靠，已阻止整理。",
    "target_file_identity_conflict": "归档目录发现重复文件 ID，已阻止整理。",
}


def _phase_for_code(
    error_code: str,
    *,
    default: OrganizationAutomationPhase = "plan",
) -> OrganizationAutomationPhase:
    if error_code in _CREDENTIAL_CODES:
        return "credentials"
    if error_code in _SCAN_CODES:
        return "scan"
    if error_code in _TMDB_CODES:
        return "tmdb"
    if error_code in _EXECUTION_CODES:
        return "execution"
    if error_code in {
        "target_directory_read_failed",
        "target_directory_incomplete",
        "target_entry_identity_unverified",
        "target_entry_scope_unverified",
        "target_entry_type_unverified",
        "target_file_identity_conflict",
    }:
        return "directory_read"
    return default


_PHASE_MESSAGES_ZH = {
    "credentials": "115 登录凭据或连接状态不可用，自动整理已停止。",
    "directory_read": "读取 115 目录失败，自动整理已停止。",
    "scan": "源目录扫描未完成，自动整理已停止。",
    "tmdb": "影视资料识别失败，自动整理已停止。",
    "plan": "整理计划生成失败，自动整理已停止。",
    "execution": "整理操作执行失败，自动整理已停止。",
}
_PHASE_NEXT_STEPS_ZH = {
    "credentials": "请到设置检查 115 登录状态和凭据，再重新发起整理。",
    "directory_read": "请确认目标目录仍可访问、范围未变化，再重新读取目录。",
    "scan": "请重新执行一次完整扫描；扫描未完成前不会生成或执行计划。",
    "tmdb": "请检查影视资料服务配置或稍后重试，识别不确定时需要人工确认。",
    "plan": "请刷新扫描快照并重新生成整理计划，确认前不会执行移动。",
    "execution": "请先核对远端状态；结果不确定时不要重复提交。",
}


def _blocked_message_zh(
    error_code: str,
    *,
    phase: OrganizationAutomationPhase | None = None,
) -> str:
    return _BLOCKED_MESSAGES_ZH.get(
        error_code,
        _PHASE_MESSAGES_ZH[phase or _phase_for_code(error_code)],
    )


def _next_step_zh(
    error_code: str,
    *,
    phase: OrganizationAutomationPhase | None = None,
) -> str:
    if error_code in _CREDENTIAL_CODES:
        return _PHASE_NEXT_STEPS_ZH["credentials"]
    return _PHASE_NEXT_STEPS_ZH[phase or _phase_for_code(error_code)]


def _blocked_detail(
    source_directory_id: str | None,
    error_code: str,
    *,
    phase: OrganizationAutomationPhase | None = None,
) -> OrganizationBlockedDetail:
    resolved_phase = phase or _phase_for_code(error_code)
    return OrganizationBlockedDetail(
        source_directory_id=source_directory_id,
        phase=resolved_phase,
        error_code=error_code,
        message_zh=_blocked_message_zh(error_code, phase=resolved_phase),
        next_step_zh=_next_step_zh(error_code, phase=resolved_phase),
    )


__all__ = [
    "CleanupTransportFactory",
    "OrganizationAutomationError",
    "OrganizationAutomationResult",
    "OrganizationAutomationService",
    "OrganizationBlockedDetail",
    "OrganizationResultItem",
]
