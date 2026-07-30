"""Durable consumer for organization completion events and STRM updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from watch_assistant.library_models import (
    LibraryScanEntry,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.models import (
    DirectoryDirtyEvent,
    OrganizationOperation,
)
from watch_assistant.services.empty_directory_cleanup import (
    EmptyDirectoryCleanupError,
    EmptyDirectoryCleanupStatus,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
)
from watch_assistant.services.organization_outbox import (
    DirectoryDirtyLease,
    DirectoryDirtyOutboxService,
)
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    StrmManifestService,
)

IndexFactory = Callable[[str, str], LibraryIndexService]
EmptyDirectoryCleaner = Callable[[str, str, str], Awaitable[EmptyDirectoryCleanupStatus]]


@dataclass(frozen=True, slots=True)
class _CleanupCandidate:
    directory_id: str
    parent_id: str
    name: str


class DirectoryDirtyWorker:
    """Reconcile one durable dirty event at a time.

    The first implementation uses a complete, verified tree snapshot as the
    reconciliation boundary. This is deliberately conservative: it gives the
    manifest the same deletion safety proof as manual full generation while
    still making organization completion events automatic and retryable.
    """

    def __init__(
        self,
        session_factory,
        strm_service: StrmManifestService,
        index_factory: IndexFactory,
        *,
        output_root: Path | str,
        playback_url_prefix: str,
        cleanup_enabled: bool = False,
        outbox: DirectoryDirtyOutboxService | None = None,
        settings_service=None,
        empty_directory_cleaner: EmptyDirectoryCleaner | None = None,
        poll_interval_seconds: float = 5.0,
        max_attempts: int = 5,
        event_logger=None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_poll_interval")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise ValueError("invalid_max_attempts")
        self._session_factory = session_factory
        self._strm = strm_service
        self._index_factory = index_factory
        self._output_root = Path(output_root)
        self._playback_url_prefix = playback_url_prefix
        self._cleanup_enabled = bool(cleanup_enabled)
        self._outbox = outbox or DirectoryDirtyOutboxService()
        self._settings_service = settings_service
        self._empty_directory_cleaner = empty_directory_cleaner
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_attempts = max_attempts
        self._event_logger = event_logger
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        lease = await self._outbox.claim_generation(self._session_factory)
        if lease is None:
            return False
        try:
            context = await self._load_context(lease)
            if context is None:
                await self._outbox.complete(
                    self._session_factory,
                    lease,
                    status="failed",
                    error_code="event_scope_unverified",
                )
                return True
            library_id, root_directory_id, actions_json = context
            strm_linkage_enabled = True
            cleanup_empty_directories = False
            if self._settings_service is not None:
                try:
                    organization_settings = await self._settings_service.get_organization()
                except Exception:  # noqa: BLE001 - do not lose a dirty event on settings failure
                    await self._outbox.retry(
                        self._session_factory,
                        lease,
                        error_code="settings_unavailable",
                        max_attempts=self._max_attempts,
                    )
                    return True
                strm_linkage_enabled = organization_settings.strm_linkage_enabled
                cleanup_empty_directories = organization_settings.cleanup_empty_directories
                if not strm_linkage_enabled and not cleanup_empty_directories:
                    await self._outbox.complete(
                        self._session_factory,
                        lease,
                        error_code="strm_linkage_disabled",
                    )
                    await self._audit("strm.dirty_skipped", "整理完成后未启用 STRM 联动")
                    return True
            index = self._index_factory(library_id, root_directory_id)
            scan = await index.scan_tree(_dirty_idempotency_key(lease))
            if not scan.complete:
                await self._outbox.retry(
                    self._session_factory,
                    lease,
                    error_code="scan_incomplete",
                    max_attempts=self._max_attempts,
                )
                return True
            if strm_linkage_enabled:
                await self._strm.incremental(
                    library_id,
                    source_scan_run_id=scan.run_id,
                    output_root=self._output_root,
                    playback_url_prefix=self._playback_url_prefix,
                    retire_removed=self._cleanup_enabled,
                )
            if cleanup_empty_directories:
                candidates = await self._cleanup_candidates(
                    scan.run_id, root_directory_id, actions_json
                )
                if candidates and self._empty_directory_cleaner is None:
                    await self._outbox.retry(
                        self._session_factory,
                        lease,
                        error_code="empty_directory_cleanup_unavailable",
                        max_attempts=self._max_attempts,
                    )
                    return True
                for candidate in candidates:
                    try:
                        result = await self._empty_directory_cleaner(
                            candidate.directory_id,
                            candidate.parent_id,
                            candidate.name,
                        )
                    except EmptyDirectoryCleanupError:
                        await self._outbox.retry(
                            self._session_factory,
                            lease,
                            error_code="empty_directory_cleanup_failed",
                            max_attempts=self._max_attempts,
                        )
                        return True
                    if result is EmptyDirectoryCleanupStatus.UNCERTAIN:
                        await self._outbox.retry(
                            self._session_factory,
                            lease,
                            error_code="empty_directory_cleanup_uncertain",
                            max_attempts=self._max_attempts,
                        )
                        return True
            await self._outbox.complete(self._session_factory, lease)
            await self._audit("strm.dirty_consumed", "目录变更已完成增量对账")
        except asyncio.CancelledError:
            raise
        except (LibraryIndexError, StrmManifestError):
            await self._outbox.retry(
                self._session_factory,
                lease,
                error_code="reconcile_failed",
                max_attempts=self._max_attempts,
            )
        except Exception:  # noqa: BLE001 - details never cross the worker boundary
            await self._outbox.retry(
                self._session_factory,
                lease,
                error_code="worker_failed",
                max_attempts=self._max_attempts,
            )
        return True

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or self._stop
        while not stop.is_set():
            claimed = await self.run_once()
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue

    async def _load_context(
        self, lease: DirectoryDirtyLease
    ) -> tuple[str, str, str] | None:
        async with self._session_factory() as session:
            row = await session.execute(
                select(MediaLibrary.id, MediaLibrary.root_directory_id, OrganizationPlan.actions_json)
                .join(OrganizationPlan, OrganizationPlan.library_id == MediaLibrary.id)
                .join(OrganizationOperation, OrganizationOperation.plan_id == OrganizationPlan.id)
                .join(DirectoryDirtyEvent, DirectoryDirtyEvent.operation_id == OrganizationOperation.id)
                .where(
                    DirectoryDirtyEvent.id == lease.event_id,
                    MediaLibrary.enabled.is_(True),
                    MediaLibrary.scope_verified.is_(True),
                )
            )
            item = row.one_or_none()
        if item is None:
            return None
        library_id, root_directory_id, actions_json = item
        if not _directory_in_plan_scope(lease.directory_id, root_directory_id, actions_json):
            return None
        return library_id, root_directory_id, actions_json

    async def _cleanup_candidates(
        self, scan_run_id: str, root_directory_id: str, actions_json: str
    ) -> tuple[_CleanupCandidate, ...]:
        source_parent_ids = _source_parent_ids(actions_json)
        if not source_parent_ids:
            return ()
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == scan_run_id
                        )
                    )
                ).all()
            )
        directories = {
            row.object_id: row
            for row in rows
            if row.is_directory and row.object_id in source_parent_ids
        }
        occupied = {row.parent_id for row in rows if row.parent_id is not None}
        return tuple(
            _CleanupCandidate(row.object_id, row.parent_id, row.name)
            for row in sorted(directories.values(), key=lambda item: item.object_id)
            if row.object_id != root_directory_id
            and isinstance(row.parent_id, str)
            and row.object_id not in occupied
        )

    async def _audit(self, event: str, status: str) -> None:
        logger = self._event_logger
        log_event = getattr(logger, "log_event", None)
        if not callable(log_event):
            return
        try:
            await log_event(event, fields={"status": status})
        except Exception:  # noqa: BLE001 - audit failure cannot block reconciliation
            return


def _directory_in_plan_scope(directory_id: str, root_directory_id: str, actions_json: str) -> bool:
    if directory_id == root_directory_id:
        return True
    try:
        payload = json.loads(actions_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, list):
        return False
    for action in payload:
        if not isinstance(action, dict):
            continue
        for record in (action, action.get("execution")):
            if not isinstance(record, dict):
                continue
            for key in (
                "source_parent_id",
                "target_parent_id",
                "source_directory_id",
                "target_directory_id",
            ):
                if record.get(key) == directory_id:
                    return True
            members = record.get("members")
            if not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, dict) and any(
                    member.get(key) == directory_id
                    for key in ("source_parent_id", "target_parent_id")
                ):
                    return True
    return False


def _dirty_idempotency_key(lease: DirectoryDirtyLease) -> str:
    return f"dirty-{lease.event_id}-{lease.attempts}"


def _source_parent_ids(actions_json: str) -> set[str]:
    try:
        actions = json.loads(actions_json)
    except (TypeError, ValueError):
        return set()
    if not isinstance(actions, list):
        return set()
    result: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        members = action.get("members")
        if not isinstance(members, list):
            execution = action.get("execution")
            members = execution.get("members") if isinstance(execution, dict) else None
        if not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, dict) and isinstance(member.get("source_parent_id"), str):
                result.add(member["source_parent_id"])
    return result


__all__ = ["DirectoryDirtyWorker"]
