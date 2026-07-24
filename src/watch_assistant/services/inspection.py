"""Persistent, single-process magnet metadata inspection batches."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.qbittorrent import QbittorrentInspectionResult
from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import InspectionBatch, InspectionItem, Resource
from watch_assistant.schemas import (
    InspectionBatchResponse,
    InspectionBatchStatus,
    InspectionItemStatus,
    InspectionResultResponse,
    ResourceKind,
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
    InspectionItemStatus.UNSUPPORTED,
)
KNOWN_ERROR_CODES = {
    "invalid_magnet",
    "authentication_failed",
    "login_unavailable",
    "existing_torrent",
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


class InspectionResourceInvalid(LookupError):
    pass


class InspectionBatchNotFound(LookupError):
    pass


class InspectionClient(Protocol):
    async def inspect(self, magnets: list[str]) -> list[QbittorrentInspectionResult]: ...


class InspectionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._create_lock = asyncio.Lock()

    async def create(self, resource_ids: list[str]) -> InspectionBatchResponse:
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

            batch = InspectionBatch(
                id="inspect_" + uuid4().hex,
                status=InspectionBatchStatus.QUEUED,
                created_at=now,
                updated_at=now,
                expires_at=now + INSPECTION_RETENTION,
            )
            session.add(batch)
            session.add_all(
                InspectionItem(
                    batch_id=batch.id,
                    resource_id=resource_id,
                    position=position,
                    status=InspectionItemStatus.QUEUED,
                )
                for position, resource_id in enumerate(resource_ids)
            )
            await session.commit()
            return InspectionBatchResponse(
                batch_id=batch.id,
                status=InspectionBatchStatus.QUEUED,
                submitted_count=len(resource_ids),
                completed_count=0,
                results=[],
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
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._client = client
        self._write_lock = asyncio.Lock()

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

    async def run_forever(self, stop_event: asyncio.Event, *, interval: float = 0.2) -> None:
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
                        await session.commit()
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
        encrypted_magnet = await self._claim_item(batch_id, resource_id)
        if encrypted_magnet is None:
            return
        try:
            magnet = self._crypto.decrypt(encrypted_magnet)
        except Exception:  # noqa: BLE001 - ciphertext failures are not user-facing
            await self._store_result(
                batch_id,
                resource_id,
                _failed_result("internal_error"),
            )
            return
        try:
            results = await self._client.inspect([magnet])
            result = results[0] if len(results) == 1 else _failed_result("internal_error")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - adapter errors must not expose details
            result = _failed_result("internal_error")
        await self._store_result(batch_id, resource_id, result)

    async def _claim_item(self, batch_id: str, resource_id: str) -> str | None:
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
            return resource.encrypted_url

    async def _store_result(
        self,
        batch_id: str,
        resource_id: str,
        result: QbittorrentInspectionResult,
    ) -> None:
        status = _item_status(result)
        async with self._write_lock, self._session_factory() as session:
            item = await session.get(InspectionItem, (batch_id, resource_id))
            if item is None or item.status in TERMINAL_ITEM_STATUSES:
                return
            item.status = status
            item.infohash = _infohash(result.infohash)
            item.error_code = (
                None if status == InspectionItemStatus.VERIFIED else _error_code(result)
            )
            if status == InspectionItemStatus.VERIFIED:
                item.total_size_bytes = max(0, result.total_size_bytes)
                item.file_count = max(0, result.file_count)
                item.video_file_count = max(0, result.video_file_count)
                item.video_size_bytes = max(0, result.video_size_bytes)
                item.subtitle_count = max(0, result.subtitle_count)
                item.sample_count = max(0, result.sample_count)
                item.largest_video_name = result.largest_video_name
                item.content_summary = result.content_summary
            else:
                item.total_size_bytes = 0
                item.file_count = 0
                item.video_file_count = 0
                item.video_size_bytes = 0
                item.subtitle_count = 0
                item.sample_count = 0
                item.largest_video_name = None
                item.content_summary = None
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
            await session.commit()


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
    )


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
    return result.error_code if result.error_code in KNOWN_ERROR_CODES else "internal_error"


def _infohash(value: str | None) -> str | None:
    if isinstance(value, str) and INFOHASH_PATTERN.fullmatch(value.casefold()):
        return value.casefold()
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
