"""Scheduled, read-first organization planning and optional execution queueing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_library import P115LibraryGateway
from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyGatewayError
from watch_assistant.library_models import (
    LibraryScanEntry,
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
from watch_assistant.services.organization_directory_provisioner import (
    OrganizationDirectoryProvisionError,
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

    def __repr__(self) -> str:
        return (
            "OrganizationAutomationResult(source_count="
            f"{self.source_count}, scanned_count={self.scanned_count}, "
            f"plan_count={self.plan_count}, queued_count={self.queued_count}, "
            f"blocked_count={self.blocked_count}, "
            f"blocked_detail_count={len(self.blocked_details)}, "
            f"plan_count={len(self.plan_ids)}, "
            f"finished_at={self.finished_at!r}, "
            f"run_id_present={self.run_id is not None})"
        )


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
        event_logger: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings_service
        self._preview = preview_service
        self._plans = plan_service
        self._gateway_factory = gateway_factory
        self._operations = operation_service
        self._auto_execute = auto_execute
        self._directory_provisioner = directory_provisioner
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
                    page_size=1,
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
                if await self._scan_contains_directory(
                    scan.run_id,
                    (set(settings.source_directory_ids) | {target_id}) - {source_id},
                ):
                    blocked += 1
                    blocked_details.append(
                        _blocked_detail(
                            source_id, "source_target_overlap", phase="scan"
                        )
                    )
                    await self._log_blocked(source_id, "source_target_overlap", phase="scan")
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
                if self._directory_provisioner is not None:
                    target_paths = tuple(
                        path
                        for preview_plan in preview_plans
                        for path in await self._plans.plan_target_directory_paths(
                            preview_plan.plan_id
                        )
                    )
                    missing_paths = tuple(
                        path for path in target_paths if path not in catalog.by_path
                    )
                    if missing_paths:
                        try:
                            await self._directory_provisioner(
                                target_id, catalog.by_path, missing_paths
                            )
                        except asyncio.CancelledError:
                            raise
                        except OrganizationDirectoryProvisionError:
                            raise
                        except Exception as error:  # noqa: BLE001 - keep remote details private
                            error_code, phase = _classify_error(
                                error, default_phase="execution"
                            )
                            raise OrganizationAutomationError(
                                error_code
                                if error_code != "automation_failed"
                                else "target_directory_create_failed",
                                phase=phase,
                            ) from None
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
                            raise OrganizationAutomationError(
                                error_code, phase=phase
                            ) from None
                        except Exception:  # noqa: BLE001 - keep remote details private
                            raise OrganizationAutomationError(
                                "target_directory_read_failed", phase="directory_read"
                            ) from None
                        preview_kwargs["target_directories"] = catalog.by_path
                        preview_kwargs["existing_target_files"] = catalog.files
                        if callable(create_previews):
                            preview_plans = list(await create_previews(**preview_kwargs))
                        else:
                            preview_plans = [
                                await self._preview.create_preview(**preview_kwargs)
                            ]
                plans += len(preview_plans)
                for plan in preview_plans:
                    plan_ids.append(plan.plan_id)
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
                        select(OrganizationOperation).where(
                            OrganizationOperation.plan_id.in_(result.plan_ids)
                        )
                    )
                ).all()
            )
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
                    page = await gateway.list_directory(source_id, page=1, page_size=1)
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
                if page.state.value != "complete" or page.scan_complete is False:
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

    async def _log_preview(self, status: str, count: int) -> None:
        logger = self._event_logger
        method = getattr(logger, "log_event", None)
        if callable(method):
            await method(
                "organize.preview.created",
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


def _scan_idempotency_key(source_id: str) -> str:
    bucket = int(datetime.now(UTC).timestamp())
    return f"organization-scan:{source_id}:{bucket}:{uuid.uuid4().hex[:8]}"


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
        "automation_failed",
        "blocked_environment",
        "candidate_target_unavailable",
        "client_unavailable",
        "credentials_missing",
        "credentials_unavailable",
        "gateway_error",
        "organization_settings_incomplete",
        "source_scope_changed",
        "source_scope_unverified",
        "source_target_overlap",
        "target_directory_create_failed",
        "target_directory_id_invalid",
        "target_directory_incomplete",
        "target_directory_parent_missing",
        "target_directory_read_failed",
    }
)


_BLOCKED_MESSAGES_ZH = {
    "automation_failed": "自动整理执行失败，原因暂不可用。",
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
    if error_code in {"target_directory_read_failed", "target_directory_incomplete"}:
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
    "OrganizationAutomationError",
    "OrganizationAutomationResult",
    "OrganizationAutomationService",
    "OrganizationBlockedDetail",
    "OrganizationResultItem",
]
