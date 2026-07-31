"""Persistent, single-process magnet metadata inspection batches."""

import asyncio
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.qbittorrent import QbittorrentInspectionResult
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import (
    InspectionBatch,
    InspectionItem,
    MagnetMetadataCache,
    Resource,
)
from watch_assistant.schemas import (
    InspectionBatchResponse,
    InspectionBatchStatus,
    InspectionItemStatus,
    InspectionResultResponse,
    LoggingLevel,
    ResourceKind,
    WorkflowStageName,
    WorkflowStageStatus,
)
from watch_assistant.services.observability import EventLogger, emit_event
from watch_assistant.services.workflows import (
    emit_workflow_stage_changed,
    link_child,
    sync_child_stage,
)

INSPECTION_RETENTION = timedelta(days=7)
TERMINAL_ITEM_STATUSES = (
    InspectionItemStatus.VERIFIED,
    InspectionItemStatus.TIMEOUT,
    InspectionItemStatus.FAILED,
    InspectionItemStatus.UNSUPPORTED,
)
SUCCESSFUL_ITEM_STATUSES = (
    InspectionItemStatus.VERIFIED,
)
KNOWN_ERROR_CODES = {
    "invalid_magnet",
    "authentication_failed",
    "login_unavailable",
    "existing_torrent",
    "existing_torrent_unreadable",
    "incompatible_qbittorrent",
    "metadata_stop_unsupported",
    "metadata_stop_failed",
    "ownership_conflict",
    "metadata_timeout",
    "api_unavailable",
    "malformed_response",
    "add_failed",
    "cleanup_failed",
    "internal_error",
}
INFOHASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")
INSPECTION_CACHE_SCHEMA_VERSION = 1
METADATA_TIMEOUT_CACHE_TTL = timedelta(minutes=10)
CACHEABLE_ITEM_STATUSES = (
    InspectionItemStatus.VERIFIED,
    InspectionItemStatus.TIMEOUT,
)


class InspectionResourceInvalid(LookupError):
    pass


class InspectionBatchNotFound(LookupError):
    pass


class InspectionClient(Protocol):
    async def inspect(
        self, magnets: list[str]
    ) -> list[QbittorrentInspectionResult]: ...


class InspectionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._create_lock = asyncio.Lock()
        self._event_logger = event_logger

    async def create(
        self,
        resource_ids: list[str],
        *,
        force: bool = False,
        workflow_id: str | None = None,
    ) -> InspectionBatchResponse:
        now = datetime.now(UTC)
        async with self._create_lock, self._session_factory() as session:
            resources = list(
                await session.scalars(
                    select(Resource).where(Resource.id.in_(resource_ids))
                )
            )
            by_id = {resource.id: resource for resource in resources}
            if len(by_id) != len(resource_ids) or any(
                resource.kind != ResourceKind.MAGNET
                or _as_utc(resource.expires_at) <= now
                for resource in by_id.values()
            ):
                raise InspectionResourceInvalid("resource_not_inspectable")

            infohash_by_resource = {
                resource_id: _resource_infohash(by_id[resource_id].canonical_key)
                for resource_id in resource_ids
            }
            infohashes = {
                infohash
                for infohash in infohash_by_resource.values()
                if infohash is not None
            }
            cached_by_infohash = {}
            if infohashes:
                cached_by_infohash = {
                    cache.infohash: cache
                    for cache in await session.scalars(
                        select(MagnetMetadataCache).where(
                            MagnetMetadataCache.infohash.in_(infohashes)
                        )
                    )
                }

            batch = InspectionBatch(
                id="inspect_" + uuid4().hex,
                workflow_id=workflow_id,
                status=InspectionBatchStatus.QUEUED,
                created_at=now,
                updated_at=now,
                expires_at=now + INSPECTION_RETENTION,
            )
            items = []
            for position, resource_id in enumerate(resource_ids):
                infohash = infohash_by_resource[resource_id]
                item = InspectionItem(
                    batch_id=batch.id,
                    resource_id=resource_id,
                    position=position,
                    status=InspectionItemStatus.QUEUED,
                )
                cache = cached_by_infohash.get(infohash)
                if (
                    infohash is not None
                    and cache is not None
                    and not force
                    and _cache_is_valid(cache, now)
                ):
                    _apply_cache_to_item(item, cache, infohash)
                items.append(item)
            batch.status = _batch_status(item.status for item in items)
            session.add(batch)
            session.add_all(items)
            stage_workflow = None
            stage_status = None
            stage_error_code = None
            if workflow_id is not None:
                if batch.status in {
                    InspectionBatchStatus.COMPLETED,
                    InspectionBatchStatus.PARTIAL,
                    InspectionBatchStatus.FAILED,
                }:
                    stage_status = _inspection_stage_status(batch.status)
                    stage_error_code = (
                        "inspection_partial"
                        if batch.status == InspectionBatchStatus.PARTIAL
                        else "inspection_failed"
                        if batch.status == InspectionBatchStatus.FAILED
                        else None
                    )
                    stage_workflow = await sync_child_stage(
                        session,
                        workflow_id,
                        WorkflowStageName.INSPECTION,
                        child_type="inspection_batch",
                        child_id=batch.id,
                        status=stage_status,
                        reason=f"inspection_{batch.status.value}",
                        error_code=stage_error_code,
                    )
                else:
                    stage_status = WorkflowStageStatus.RUNNING
                    stage_workflow = await link_child(
                        session,
                        workflow_id,
                        WorkflowStageName.INSPECTION,
                        "inspection_batch",
                        batch.id,
                    )
            await session.commit()
            if stage_workflow is not None and stage_status is not None:
                await emit_workflow_stage_changed(
                    self._event_logger,
                    workflow_id=stage_workflow.id,
                    correlation_id=stage_workflow.correlation_id,
                    stage_name=WorkflowStageName.INSPECTION,
                    status=stage_status,
                    error_code=stage_error_code,
                )
            await emit_event(
                self._event_logger,
                "inspection.batch_started",
                fields={"status": "queued", "count": len(resource_ids)},
            )
            if batch.status in {
                InspectionBatchStatus.COMPLETED,
                InspectionBatchStatus.PARTIAL,
                InspectionBatchStatus.FAILED,
            }:
                await emit_event(
                    self._event_logger,
                    "inspection.batch_completed"
                    if batch.status != InspectionBatchStatus.FAILED
                    else "inspection.batch_failed",
                    level=(
                        LoggingLevel.ERROR
                        if batch.status == InspectionBatchStatus.FAILED
                        else LoggingLevel.INFO
                    ),
                    fields={
                        "status": batch.status.value,
                        "count": len(resource_ids),
                        "hidden_count": len(resource_ids),
                        "error_code": (
                            "inspection_failed"
                            if batch.status == InspectionBatchStatus.FAILED
                            else None
                        ),
                    },
                    task_id=batch.id,
                    correlation_id=(
                        stage_workflow.correlation_id
                        if stage_workflow is not None
                        else None
                    ),
                )
            return InspectionBatchResponse(
                batch_id=batch.id,
                workflow_id=batch.workflow_id,
                status=batch.status,
                submitted_count=len(resource_ids),
                completed_count=sum(
                    item.status in TERMINAL_ITEM_STATUSES for item in items
                ),
                results=[
                    _result_response(item)
                    for item in items
                    if item.status in TERMINAL_ITEM_STATUSES
                ],
            )

    async def get(self, batch_id: str) -> InspectionBatchResponse:
        async with self._session_factory() as session:
            batch = await session.get(InspectionBatch, batch_id)
            if batch is None:
                raise InspectionBatchNotFound(batch_id)
            items = list(
                await session.scalars(
                    select(InspectionItem)
                    .where(InspectionItem.batch_id == batch_id)
                    .order_by(InspectionItem.position)
                )
            )
        results = [
            _result_response(item)
            for item in items
            if item.status in TERMINAL_ITEM_STATUSES
        ]
        return InspectionBatchResponse(
            batch_id=batch.id,
            workflow_id=batch.workflow_id,
            status=batch.status,
            submitted_count=len(items),
            completed_count=len(results),
            results=results,
        )


class InspectionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        client: InspectionClient,
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._client = client
        self._write_lock = asyncio.Lock()
        self._event_logger = event_logger

    async def recover_after_restart(self) -> int:
        async with self._session_factory() as session:
            batches = list(
                await session.scalars(
                    select(InspectionBatch).where(
                        InspectionBatch.status.in_(
                            (
                                InspectionBatchStatus.QUEUED,
                                InspectionBatchStatus.RUNNING,
                            )
                        )
                    )
                )
            )
            for batch in batches:
                batch.status = InspectionBatchStatus.QUEUED
                batch.updated_at = datetime.now(UTC)
            items = list(
                await session.scalars(
                    select(InspectionItem).where(
                        InspectionItem.status == InspectionItemStatus.RUNNING
                    )
                )
            )
            for item in items:
                item.status = InspectionItemStatus.QUEUED
            await session.commit()
            return len(batches)

    async def run_forever(
        self, stop_event: asyncio.Event, *, interval: float = 0.2
    ) -> None:
        await self.recover_after_restart()
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        batch_id = await self._claim_batch()
        if batch_id is None:
            return False

        if not await self._ensure_dependency(batch_id):
            return True
        item_ids = await self._unfinished_item_ids(batch_id)
        if not item_ids:
            await self._finalize_batch(batch_id)
            return True

        tasks = [
            asyncio.create_task(self._inspect_item(batch_id, resource_id))
            for resource_id in item_ids
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        await self._finalize_batch(batch_id)
        return True

    async def _claim_batch(self) -> str | None:
        async with self._session_factory() as session:
            batch = await session.scalar(
                select(InspectionBatch)
                .where(InspectionBatch.status == InspectionBatchStatus.QUEUED)
                .order_by(InspectionBatch.created_at)
                .limit(1)
            )
            if batch is None:
                return None
            batch.status = InspectionBatchStatus.RUNNING
            batch.updated_at = datetime.now(UTC)
            await session.commit()
            return batch.id

    async def _ensure_dependency(self, batch_id: str) -> bool:
        ensure_available = getattr(self._client, "ensure_available", None)
        if ensure_available is None:
            return True
        try:
            await ensure_available()
            return True
        except Exception:  # noqa: BLE001 - dependency error is intentionally opaque
            async with self._session_factory() as session:
                completed_count = await session.scalar(
                    select(InspectionItem)
                    .where(
                        InspectionItem.batch_id == batch_id,
                        InspectionItem.status.in_(TERMINAL_ITEM_STATUSES),
                    )
                    .with_only_columns(InspectionItem.resource_id)
                )
                if completed_count is None:
                    batch = await session.get(InspectionBatch, batch_id)
                    if batch is not None:
                        batch.status = InspectionBatchStatus.FAILED
                        batch.updated_at = datetime.now(UTC)
                        stage_workflow = None
                        if batch.workflow_id is not None:
                            stage_workflow = await sync_child_stage(
                                session,
                                batch.workflow_id,
                                WorkflowStageName.INSPECTION,
                                child_type="inspection_batch",
                                child_id=batch.id,
                                status=WorkflowStageStatus.FAILED,
                                reason="inspection_dependency_failed",
                                error_code="inspection_dependency_failed",
                            )
                        await session.commit()
                        if stage_workflow is not None:
                            await emit_workflow_stage_changed(
                                self._event_logger,
                                workflow_id=stage_workflow.id,
                                correlation_id=stage_workflow.correlation_id,
                                stage_name=WorkflowStageName.INSPECTION,
                                status=WorkflowStageStatus.FAILED,
                                error_code="inspection_dependency_failed",
                            )
                        await emit_event(
                            self._event_logger,
                            "inspection.batch_failed",
                            level=LoggingLevel.ERROR,
                            fields={
                                "status": "dependency_failed",
                                "count": 0,
                                "hidden_count": 0,
                                "error_code": "inspection_dependency_failed",
                            },
                            correlation_id=(
                                stage_workflow.correlation_id
                                if stage_workflow is not None
                                else None
                            ),
                            task_id=batch.id,
                        )
                        await emit_event(
                            self._event_logger,
                            "inspection.dependency_failed",
                            level=LoggingLevel.ERROR,
                            fields={"status": "unavailable", "error_code": "inspection_dependency_failed"},
                            resource_type="dependency",
                            resource_id="qbittorrent",
                        )
                        return False
            return True

    async def _unfinished_item_ids(self, batch_id: str) -> list[str]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(InspectionItem.resource_id)
                .where(
                    InspectionItem.batch_id == batch_id,
                    InspectionItem.status.in_(
                        (InspectionItemStatus.QUEUED, InspectionItemStatus.RUNNING)
                    ),
                )
                .order_by(InspectionItem.position)
            )
            return list(rows)

    async def _inspect_item(self, batch_id: str, resource_id: str) -> None:
        claim = await self._claim_item(batch_id, resource_id)
        if claim is None:
            return
        encrypted_magnet, cache_infohash = claim
        try:
            magnet = self._crypto.decrypt(encrypted_magnet)
        except Exception:  # noqa: BLE001 - ciphertext failures are not user-facing
            await self._store_result(
                batch_id,
                resource_id,
                _failed_result("internal_error"),
                cache_infohash,
            )
            return
        try:
            results = await self._client.inspect([magnet])
            result = (
                results[0] if len(results) == 1 else _failed_result("internal_error")
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - adapter errors must not expose details
            result = _failed_result("internal_error")
        await self._store_result(batch_id, resource_id, result, cache_infohash)

    async def _claim_item(
        self, batch_id: str, resource_id: str
    ) -> tuple[str, str | None] | None:
        async with self._write_lock, self._session_factory() as session:
            item = await session.get(InspectionItem, (batch_id, resource_id))
            if item is None or item.status in TERMINAL_ITEM_STATUSES:
                return None
            resource = await session.get(Resource, resource_id)
            if resource is None or resource.kind != ResourceKind.MAGNET:
                item.status = InspectionItemStatus.FAILED
                item.error_code = "internal_error"
                await session.commit()
                return None
            item.status = InspectionItemStatus.RUNNING
            await session.commit()
            return resource.encrypted_url, _resource_infohash(resource.canonical_key)

    async def _store_result(
        self,
        batch_id: str,
        resource_id: str,
        result: QbittorrentInspectionResult,
        cache_infohash: str | None,
    ) -> None:
        status = _item_status(result)
        result_infohash = _infohash(result.infohash)
        if (
            status == InspectionItemStatus.VERIFIED
            and cache_infohash is not None
            and result_infohash != cache_infohash
        ):
            status = InspectionItemStatus.FAILED
            result = _failed_result("malformed_response")
            item_infohash = None
        elif status == InspectionItemStatus.TIMEOUT and cache_infohash is not None:
            item_infohash = cache_infohash
        else:
            item_infohash = result_infohash or cache_infohash
        async with self._write_lock, self._session_factory() as session:
            item = await session.get(InspectionItem, (batch_id, resource_id))
            if item is None or item.status in TERMINAL_ITEM_STATUSES:
                return
            item.status = status
            item.infohash = item_infohash
            item.result_source = result.result_source
            item.error_code = (
                None if status == InspectionItemStatus.VERIFIED else _error_code(result)
            )
            _apply_result_to_item(item, result, status)
            if cache_infohash is not None and status in CACHEABLE_ITEM_STATUSES:
                await _store_cache(session, cache_infohash, result, status)
            await session.commit()

    async def _finalize_batch(self, batch_id: str) -> None:
        async with self._session_factory() as session:
            batch = await session.get(InspectionBatch, batch_id)
            if batch is None or batch.status == InspectionBatchStatus.FAILED:
                return
            items = list(
                await session.scalars(
                    select(InspectionItem).where(InspectionItem.batch_id == batch_id)
                )
            )
            if any(item.status not in TERMINAL_ITEM_STATUSES for item in items):
                return
            successes = sum(item.status in SUCCESSFUL_ITEM_STATUSES for item in items)
            batch.status = (
                InspectionBatchStatus.COMPLETED
                if successes == len(items)
                else InspectionBatchStatus.PARTIAL
                if successes
                else InspectionBatchStatus.FAILED
            )
            batch.updated_at = datetime.now(UTC)
            stage_workflow = None
            stage_status = None
            stage_error_code = None
            if batch.workflow_id is not None:
                stage_status = _inspection_stage_status(batch.status)
                stage_error_code = (
                    "inspection_partial"
                    if batch.status == InspectionBatchStatus.PARTIAL
                    else "inspection_failed"
                    if batch.status == InspectionBatchStatus.FAILED
                    else None
                )
                stage_workflow = await sync_child_stage(
                    session,
                    batch.workflow_id,
                    WorkflowStageName.INSPECTION,
                    child_type="inspection_batch",
                    child_id=batch.id,
                    status=stage_status,
                    reason=f"inspection_{batch.status.value}",
                    error_code=stage_error_code,
                )
            await session.commit()
            if stage_workflow is not None and stage_status is not None:
                await emit_workflow_stage_changed(
                    self._event_logger,
                    workflow_id=stage_workflow.id,
                    correlation_id=stage_workflow.correlation_id,
                    stage_name=WorkflowStageName.INSPECTION,
                    status=stage_status,
                    error_code=stage_error_code,
                )
            await emit_event(
                self._event_logger,
                "inspection.batch_completed"
                if batch.status == InspectionBatchStatus.COMPLETED
                else "inspection.batch_failed"
                if batch.status == InspectionBatchStatus.FAILED
                else "inspection.batch_completed",
                level=(
                    LoggingLevel.ERROR
                    if batch.status == InspectionBatchStatus.FAILED
                    else LoggingLevel.INFO
                ),
                fields={
                    "status": batch.status.value,
                    "count": len(items),
                    "hidden_count": len(items) - successes,
                    "error_code": stage_error_code,
                },
                correlation_id=(
                    stage_workflow.correlation_id
                    if stage_workflow is not None
                    else None
                ),
                task_id=batch.id,
            )


def _result_response(item: InspectionItem) -> InspectionResultResponse:
    return InspectionResultResponse(
        resource_id=item.resource_id,
        infohash=item.infohash,
        status=item.status,
        total_size_bytes=item.total_size_bytes or 0,
        file_count=item.file_count or 0,
        video_file_count=item.video_file_count or 0,
        video_size_bytes=item.video_size_bytes or 0,
        subtitle_count=item.subtitle_count or 0,
        sample_count=item.sample_count or 0,
        largest_video_name=item.largest_video_name,
        content_summary=item.content_summary,
        error_code=item.error_code,
        result_source=item.result_source,
    )


def _inspection_stage_status(status: InspectionBatchStatus) -> WorkflowStageStatus:
    if status in {InspectionBatchStatus.QUEUED, InspectionBatchStatus.RUNNING}:
        return WorkflowStageStatus.RUNNING
    if status == InspectionBatchStatus.COMPLETED:
        return WorkflowStageStatus.SUCCEEDED
    return WorkflowStageStatus.FAILED


def _batch_status(statuses: Iterable[InspectionItemStatus]) -> InspectionBatchStatus:
    status_values = tuple(statuses)
    if any(status not in TERMINAL_ITEM_STATUSES for status in status_values):
        return InspectionBatchStatus.QUEUED
    successes = sum(status in SUCCESSFUL_ITEM_STATUSES for status in status_values)
    if successes == len(status_values):
        return InspectionBatchStatus.COMPLETED
    if successes:
        return InspectionBatchStatus.PARTIAL
    return InspectionBatchStatus.FAILED


def _cache_is_valid(cache: MagnetMetadataCache, now: datetime) -> bool:
    if cache.schema_version != INSPECTION_CACHE_SCHEMA_VERSION:
        return False
    if cache.status == InspectionItemStatus.VERIFIED:
        return True
    return (
        cache.status == InspectionItemStatus.TIMEOUT
        and _as_utc(cache.updated_at) + METADATA_TIMEOUT_CACHE_TTL > now
    )


def _apply_cache_to_item(
    item: InspectionItem,
    cache: MagnetMetadataCache,
    infohash: str,
) -> None:
    item.infohash = infohash
    item.result_source = cache.result_source
    item.status = cache.status
    item.error_code = (
        None if cache.status == InspectionItemStatus.VERIFIED else "metadata_timeout"
    )
    if cache.status == InspectionItemStatus.VERIFIED:
        item.total_size_bytes = cache.total_size_bytes
        item.file_count = cache.file_count
        item.video_file_count = cache.video_file_count
        item.video_size_bytes = cache.video_size_bytes
        item.subtitle_count = cache.subtitle_count
        item.sample_count = cache.sample_count
        item.largest_video_name = cache.largest_video_name
        item.content_summary = cache.content_summary
    else:
        _clear_item_details(item)


def _apply_result_to_item(
    item: InspectionItem,
    result: QbittorrentInspectionResult,
    status: InspectionItemStatus,
) -> None:
    if status == InspectionItemStatus.VERIFIED:
        item.total_size_bytes = max(0, result.total_size_bytes)
        item.file_count = max(0, result.file_count)
        item.video_file_count = max(0, result.video_file_count)
        item.video_size_bytes = max(0, result.video_size_bytes)
        item.subtitle_count = max(0, result.subtitle_count)
        item.sample_count = max(0, result.sample_count)
        item.largest_video_name = _basename(result.largest_video_name)
        item.content_summary = result.content_summary
        item.result_source = result.result_source
    else:
        _clear_item_details(item)


def _clear_item_details(item: InspectionItem) -> None:
    item.total_size_bytes = 0
    item.file_count = 0
    item.video_file_count = 0
    item.video_size_bytes = 0
    item.subtitle_count = 0
    item.sample_count = 0
    item.largest_video_name = None
    item.content_summary = None


async def _store_cache(
    session: AsyncSession,
    infohash: str,
    result: QbittorrentInspectionResult,
    status: InspectionItemStatus,
) -> None:
    cache = await session.get(MagnetMetadataCache, infohash)
    if (
        status == InspectionItemStatus.TIMEOUT
        and cache is not None
        and cache.schema_version == INSPECTION_CACHE_SCHEMA_VERSION
        and cache.status == InspectionItemStatus.VERIFIED
    ):
        return
    if cache is None:
        cache = MagnetMetadataCache(
            infohash=infohash,
            status=status,
            schema_version=INSPECTION_CACHE_SCHEMA_VERSION,
        )
        session.add(cache)
    cache.status = status
    cache.result_source = result.result_source
    cache.schema_version = INSPECTION_CACHE_SCHEMA_VERSION
    cache.updated_at = datetime.now(UTC)
    cache.expires_at = (
        cache.updated_at + METADATA_TIMEOUT_CACHE_TTL
        if status == InspectionItemStatus.TIMEOUT
        else None
    )
    if status == InspectionItemStatus.VERIFIED:
        cache.total_size_bytes = max(0, result.total_size_bytes)
        cache.file_count = max(0, result.file_count)
        cache.video_file_count = max(0, result.video_file_count)
        cache.video_size_bytes = max(0, result.video_size_bytes)
        cache.subtitle_count = max(0, result.subtitle_count)
        cache.sample_count = max(0, result.sample_count)
        cache.largest_video_name = _basename(result.largest_video_name)
        cache.content_summary = result.content_summary
    else:
        cache.total_size_bytes = 0
        cache.file_count = 0
        cache.video_file_count = 0
        cache.video_size_bytes = 0
        cache.subtitle_count = 0
        cache.sample_count = 0
        cache.largest_video_name = None
        cache.content_summary = None


def _failed_result(error_code: str) -> QbittorrentInspectionResult:
    return QbittorrentInspectionResult(
        infohash=None,
        status=InspectionItemStatus.FAILED,
        error_code=error_code,
    )


def _item_status(result: QbittorrentInspectionResult) -> InspectionItemStatus:
    try:
        status = InspectionItemStatus(result.status)
    except ValueError:
        return InspectionItemStatus.FAILED
    return status if status in TERMINAL_ITEM_STATUSES else InspectionItemStatus.FAILED


def _error_code(result: QbittorrentInspectionResult) -> str:
    return (
        result.error_code
        if result.error_code in KNOWN_ERROR_CODES
        else "internal_error"
    )


def _infohash(value: str | None) -> str | None:
    if isinstance(value, str) and INFOHASH_PATTERN.fullmatch(value.casefold()):
        return value.casefold()
    return None


def _resource_infohash(canonical_key: str) -> str | None:
    if not isinstance(canonical_key, str) or canonical_key[:7].casefold() != "magnet:":
        return None
    value = canonical_key[7:].casefold()
    return value if INFOHASH_PATTERN.fullmatch(value) else None


def _basename(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    basename = PurePosixPath(value.replace("\\", "/")).name
    return basename or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
