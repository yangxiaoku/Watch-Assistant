"""Scheduled, read-first organization planning and optional execution queueing."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_library import P115LibraryGateway
from watch_assistant.library_models import LibraryScanEntry, MediaLibrary
from watch_assistant.schemas import OrganizationSettingsResponse
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanService,
    OrganizationPlanStatus,
)
from watch_assistant.services.organization_preview import (
    OrganizationPreviewError,
    OrganizationPreviewService,
)
from watch_assistant.services.organization_target import (
    OrganizationTargetError,
    read_target_catalog,
)
from watch_assistant.services.settings import SettingsService


class OrganizationAutomationError(ValueError):
    """Stable local automation error without remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationAutomationResult:
    source_count: int
    scanned_count: int
    plan_count: int
    queued_count: int
    blocked_count: int

    def __repr__(self) -> str:
        return (
            "OrganizationAutomationResult(source_count="
            f"{self.source_count}, scanned_count={self.scanned_count}, "
            f"plan_count={self.plan_count}, queued_count={self.queued_count}, "
            f"blocked_count={self.blocked_count})"
        )


GatewayFactory = Callable[[Collection[str]], P115LibraryGateway]


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
        event_logger: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings_service
        self._preview = preview_service
        self._plans = plan_service
        self._gateway_factory = gateway_factory
        self._operations = operation_service
        self._auto_execute = auto_execute
        self._event_logger = event_logger or settings_service
        self._lock = asyncio.Lock()
        self.last_result: OrganizationAutomationResult | None = None

    async def run_once(self) -> bool:
        """Run one bounded planning pass; return whether work was attempted."""

        async with self._lock:
            settings = await self._settings.get_organization()
            if not settings.source_directory_ids or not settings.target_directory_id:
                self.last_result = OrganizationAutomationResult(
                    source_count=len(settings.source_directory_ids),
                    scanned_count=0,
                    plan_count=0,
                    queued_count=0,
                    blocked_count=1,
                )
                return False
            try:
                result = await self._run(settings)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - scheduler remains alive
                await self._log_blocked(_stable_error_code(error))
                self.last_result = OrganizationAutomationResult(
                    source_count=len(settings.source_directory_ids),
                    scanned_count=0,
                    plan_count=0,
                    queued_count=0,
                    blocked_count=1,
                )
                return True
            self.last_result = result
            return True

    async def _run(
        self, settings: OrganizationSettingsResponse
    ) -> OrganizationAutomationResult:
        target_id = settings.target_directory_id
        if target_id is None:
            raise OrganizationAutomationError("target_directory_missing")
        authorized_ids = tuple(dict.fromkeys((*settings.source_directory_ids, target_id)))
        gateway = self._gateway_factory(authorized_ids)
        catalog = await read_target_catalog(gateway, target_id)
        if any(source_id in catalog.by_path.values() for source_id in settings.source_directory_ids):
            raise OrganizationAutomationError("source_target_overlap")

        scanned = plans = queued = blocked = 0
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
                    page_size=1,
                )
                scan = await scanner.scan_tree(_scan_idempotency_key(source_id))
                if not scan.complete:
                    blocked += 1
                    continue
                if await self._scan_contains_directory(
                    scan.run_id,
                    (set(settings.source_directory_ids) | {target_id}) - {source_id},
                ):
                    blocked += 1
                    await self._log_blocked("source_target_overlap")
                    continue
                scanned += 1
                plan = await self._preview.create_preview(
                    library_id=library_id,
                    scan_run_id=scan.run_id,
                    target_directory_id=target_id,
                    target_directories=catalog.by_path,
                    existing_target_files=catalog.files,
                    video_extensions=settings.video_extensions,
                    metadata_extensions=settings.metadata_extensions,
                    small_file_threshold_mb=settings.small_file_threshold_mb,
                    rename_enabled=settings.rename_enabled,
                    region_grouping_enabled=settings.region_grouping_enabled,
                    year_grouping_enabled=settings.year_grouping_enabled,
                    include_children_category=settings.include_children_category,
                    include_concert_category=settings.include_concert_category,
                    media_probe_enabled=settings.media_probe_enabled,
                    ai_identification_enabled=settings.ai_identification_enabled,
                    cleanup_empty_directories=settings.cleanup_empty_directories,
                    strm_linkage_enabled=settings.strm_linkage_enabled,
                    prefer_remux=settings.prefer_remux,
                    prefer_resolution=settings.prefer_resolution,
                    prefer_dolby=settings.prefer_dolby,
                    conflict_mode=settings.conflict_mode,
                    multi_version_enabled=settings.multi_version_enabled,
                )
                plans += 1
                if (
                    self._auto_execute
                    and self._operations is not None
                    and plan.status is OrganizationPlanStatus.PLANNED
                ):
                    confirmed = await self._plans.confirm_plan(
                        plan.plan_id, expected_revision=plan.revision
                    )
                    await self._operations.create(
                        plan.plan_id,
                        idempotency_key=f"organization-auto:{plan.plan_id}",
                        expected_plan_revision=confirmed.revision,
                    )
                    queued += 1
                await self._log_preview(plan.status.value, plan.source_count)
            except asyncio.CancelledError:
                raise
            except (
                LibraryIndexError,
                OrganizationPlanError,
                OrganizationPreviewError,
                OrganizationTargetError,
            ) as error:
                blocked += 1
                await self._log_blocked(_stable_error_code(error))
        return OrganizationAutomationResult(
            source_count=len(settings.source_directory_ids),
            scanned_count=scanned,
            plan_count=plans,
            queued_count=queued,
            blocked_count=blocked,
        )

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
                page = await gateway.list_directory(source_id, page=1, page_size=1)
                if page.state.value != "complete" or page.scan_complete is False:
                    raise OrganizationAutomationError("source_scope_unverified")
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

    async def _log_preview(self, status: str, count: int) -> None:
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if callable(method):
            await method(
                "organize.preview.created",
                fields={"status": status},
                counts={"count": count},
            )

    async def _log_blocked(self, error_code: str) -> None:
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if callable(method):
            await method(
                "organize.preview.created",
                fields={"status": "blocked", "error_code": error_code},
                counts={"count": 0},
            )


def _source_library_id(source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("ascii")).hexdigest()[:32]
    return f"org-source-{digest}"


def _scan_idempotency_key(source_id: str) -> str:
    bucket = int(datetime.now(UTC).timestamp())
    return f"organization-scan:{source_id}:{bucket}:{uuid.uuid4().hex[:8]}"


def _stable_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "automation_failed"


__all__ = [
    "OrganizationAutomationError",
    "OrganizationAutomationResult",
    "OrganizationAutomationService",
]
