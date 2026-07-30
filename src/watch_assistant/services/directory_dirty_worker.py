"""Durable consumer for organization completion events and STRM updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from watch_assistant.library_models import MediaLibrary, OrganizationPlan
from watch_assistant.models import (
    DirectoryDirtyEvent,
    OrganizationOperation,
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
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_attempts = max_attempts
        self._event_logger = event_logger
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        lease = await self._outbox.claim_next(self._session_factory)
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
            library_id, root_directory_id = context
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
            await self._strm.incremental(
                library_id,
                source_scan_run_id=scan.run_id,
                output_root=self._output_root,
                playback_url_prefix=self._playback_url_prefix,
                retire_removed=self._cleanup_enabled,
            )
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
    ) -> tuple[str, str] | None:
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
        return library_id, root_directory_id

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
        for key in ("source_parent_id", "target_parent_id", "source_directory_id", "target_directory_id"):
            if action.get(key) == directory_id:
                return True
        for member in action.get("members", ()) if isinstance(action.get("members"), list) else ():
            if isinstance(member, dict) and any(
                member.get(key) == directory_id
                for key in ("source_parent_id", "target_parent_id")
            ):
                return True
    return False


def _dirty_idempotency_key(lease: DirectoryDirtyLease) -> str:
    return f"dirty-{lease.event_id}-{lease.attempts}"


__all__ = ["DirectoryDirtyWorker"]
