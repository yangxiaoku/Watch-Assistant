"""Durable queue and worker lifecycle for read-only library scans."""

from __future__ import annotations

import asyncio
import inspect as python_inspect
import uuid
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_library import P115LibraryGateway
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.schemas import LoggingLevel
from watch_assistant.services.library_index import (
    LibraryIndexError,
    LibraryIndexService,
    LibraryScanResult,
    ScanRunState,
    validate_tree_cursor_scope,
)
from watch_assistant.services.observability import EventLogger, emit_event

# 网关取消(快照分页被活跃目录并发修改打断)时的自动重排队上限。
# 超过后转为 FAILED,避免无限热循环;由调度器冷却重试或用户手动重扫。
MAX_SCAN_REQUEUE_ATTEMPTS = 5

SCAN_STATE_LABELS_ZH = {
    "queued": "等待扫描",
    "running": "扫描中",
    "completed": "扫描完成",
    "failed": "扫描失败",
    "cancelled": "已取消",
}

SCAN_ERROR_MESSAGES_ZH = {
    "cancelled": "扫描已取消，已保存的分页结果可以继续使用。",
    "gateway_error": "115 目录读取失败，已保存已完成分页，请核对登录状态后重试。",
    "entry_scope_unverified": "文件详情的归属范围无法确认，扫描已停止。",
    "pickcode_unavailable": "文件播放标识读取失败，未生成可播放快照。",
    "storage_error": "扫描结果保存失败，远端目录未被修改，请稍后重试。",
    "total_mismatch": "115 返回的目录总数与扫描结果不一致，未生成完整快照。",
    "page_count_changed": "115 返回的分页范围发生变化，未生成完整快照。",
    "entry_out_of_scope": "发现范围外目录条目，扫描已停止且未生成删除结论。",
    "entry_path_invalid": "发现无法确认的目录路径，扫描已停止。",
    "directory_cycle": "发现目录循环，扫描已停止且未生成删除结论。",
    "directory_limit_exceeded": "目录数量超过安全上限，扫描未完成。",
    "repeated_entry": "115 返回了重复目录条目，扫描未生成完整快照。",
    "scan_worker_failed": "扫描 worker 异常退出，已保留断点，请稍后重试。",
    "scan_worker_recovered": "扫描已从上次中断位置重新排队。",
    "scan_requeue_limit": "扫描被远端多次取消，已超过自动重试上限，请检查目录活跃状态后手动重扫。",
    "lease_claim_lost": "扫描执行权已变化，已停止继续读取，请查看状态后再决定是否重试。",
    "library_scope_unverified": "媒体库范围尚未完成只读验证。",
    "checkpoint_invalid": "扫描断点无法完成校验，已停止继续读取。",
    "scan_run_missing": "扫描操作不存在。",
}


def scan_state_message_zh(state: str) -> str:
    return SCAN_STATE_LABELS_ZH.get(state, "扫描状态未知")


def scan_error_message_zh(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return SCAN_ERROR_MESSAGES_ZH.get(error_code, "扫描未完成，请查看状态后再决定是否重试。")


class LibraryScanOperationError(ValueError):
    """Stable local error for the scan operation API."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LibraryScanOperationNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class LibraryScanOperationSummary:
    run_id: str
    library_id: str
    state: str
    state_message_zh: str
    complete: bool
    snapshot_revision: int | None
    pages_read: int
    items_seen: int
    added_count: int
    changed_count: int
    removed_count: int
    attempts: int
    error_code: str | None
    error_message_zh: str | None
    cancel_requested: bool

    def __repr__(self) -> str:
        return (
            "LibraryScanOperationSummary(run_id=<redacted>, "
            "library_id=<redacted>, "
            f"state={self.state!r}, complete={self.complete!r}, "
            f"pages_read={self.pages_read}, items_seen={self.items_seen}, "
            f"attempts={self.attempts}, error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LibraryScanLease:
    run_id: str
    library_id: str
    root_directory_id: str
    idempotency_key: str
    scan_mode: str
    max_directories: int
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime

    def __repr__(self) -> str:
        return (
            "LibraryScanLease(run_id=<redacted>, library_id=<redacted>, "
            f"lease_owner=<redacted>, lease_token=<redacted>, "
            f"lease_expires_at={self.lease_expires_at!r})"
        )


class LibraryScanOperationService:
    """Persist scan requests and lease them to a read-only worker."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def enqueue(
        self,
        library_id: str,
        *,
        idempotency_key: str,
        max_directories: int = 10_000,
    ) -> LibraryScanOperationSummary:
        _validate_identifier(library_id, "invalid_library_id", maximum=128)
        _validate_idempotency_key(idempotency_key)
        if (
            not isinstance(max_directories, int)
            or isinstance(max_directories, bool)
            or not 1 <= max_directories <= 100_000
        ):
            raise LibraryScanOperationError("invalid_directory_limit")
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, library_id)
            _require_verified_library(library)
            root_directory_id = library.root_directory_id
            run = await session.scalar(
                select(LibraryScanRun).where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.idempotency_key == idempotency_key,
                )
            )
            if run is None:
                run = LibraryScanRun(
                    id=uuid.uuid4().hex,
                    library_id=library_id,
                    root_directory_id=root_directory_id,
                    idempotency_key=idempotency_key,
                    scan_mode="tree",
                    max_directories=max_directories,
                )
                session.add(run)
                try:
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    run = await session.scalar(
                        select(LibraryScanRun).where(
                            LibraryScanRun.library_id == library_id,
                            LibraryScanRun.idempotency_key == idempotency_key,
                        )
                    )
                    if run is None:
                        raise LibraryScanOperationError(
                            "library_scan_unavailable"
                        ) from None
                else:
                    from watch_assistant.library_models import LibraryScanCheckpoint

                    session.add(LibraryScanCheckpoint(scan_run_id=run.id))
            if run.root_directory_id != root_directory_id:
                raise LibraryScanOperationError("library_scope_unverified")
            elif run.complete:
                return _summary(run)
            elif run.state == ScanRunState.RUNNING.value:
                # An idempotent retry must not steal a live lease from the
                # worker already processing this operation.
                return _summary(run)
            else:
                run.max_directories = max_directories
                run.state = ScanRunState.QUEUED.value
                run.cancel_requested = False
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
                run.error_code = None
            await session.commit()
            summary = _summary(run)
        await emit_event(
            self._event_logger,
            "library.scan.queued",
            fields={
                "status": summary.state,
                "count": 1,
            },
            resource_type="library_scan",
            resource_id=summary.run_id,
        )
        return summary

    async def get(
        self, library_id: str, run_id: str
    ) -> LibraryScanOperationSummary:
        _validate_identifier(library_id, "invalid_library_id", maximum=128)
        _validate_identifier(run_id, "invalid_scan_run_id", maximum=64)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(LibraryScanRun).where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.id == run_id,
                )
            )
            if run is None:
                raise LibraryScanOperationNotFound
            return _summary(run)

    async def list(
        self,
        library_id: str,
        *,
        cursor: int = 0,
        limit: int = 50,
    ) -> tuple[list[LibraryScanOperationSummary], int | None]:
        _validate_identifier(library_id, "invalid_library_id", maximum=128)
        if cursor < 0 or limit < 1 or limit > 100:
            raise LibraryScanOperationError("invalid_pagination")
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LibraryScanRun)
                        .where(LibraryScanRun.library_id == library_id)
                        .order_by(
                            LibraryScanRun.created_at.desc(), LibraryScanRun.id.desc()
                        )
                        .offset(cursor)
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return [_summary(row) for row in rows], cursor + limit if has_more else None

    async def cancel(
        self, library_id: str, run_id: str
    ) -> LibraryScanOperationSummary:
        _validate_identifier(library_id, "invalid_library_id", maximum=128)
        _validate_identifier(run_id, "invalid_scan_run_id", maximum=64)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(LibraryScanRun).where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.id == run_id,
                )
            )
            if run is None:
                raise LibraryScanOperationNotFound
            if run.complete:
                return _summary(run)
            run.cancel_requested = True
            if run.state == ScanRunState.QUEUED.value:
                run.state = ScanRunState.CANCELLED.value
                run.error_code = "cancelled"
            if run.state == ScanRunState.CANCELLED.value:
                run.lease_owner = None
                run.lease_token = None
            run.lease_expires_at = (
                None if run.state == ScanRunState.CANCELLED.value else run.lease_expires_at
            )
            await session.commit()
            return _summary(run)

    async def claim_next(
        self,
        *,
        owner: str,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> LibraryScanLease | None:
        _validate_identifier(owner, "invalid_worker_owner", maximum=128)
        if lease_duration.total_seconds() <= 0:
            raise LibraryScanOperationError("invalid_lease_duration")
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            await self._recover_expired_in_session(session, current_time)
            await session.commit()
            while True:
                run = await session.scalar(
                    select(LibraryScanRun)
                    .where(
                        LibraryScanRun.state == ScanRunState.QUEUED.value,
                        LibraryScanRun.cancel_requested.is_(False),
                    )
                    .order_by(LibraryScanRun.created_at.asc(), LibraryScanRun.id.asc())
                    .limit(1)
                )
                if run is None:
                    return None
                token = uuid.uuid4().hex
                expires_at = current_time + lease_duration
                result = await session.execute(
                    update(LibraryScanRun)
                    .where(
                        LibraryScanRun.id == run.id,
                        LibraryScanRun.state == ScanRunState.QUEUED.value,
                        LibraryScanRun.cancel_requested.is_(False),
                    )
                    .values(
                        state=ScanRunState.RUNNING.value,
                        attempts=LibraryScanRun.attempts + 1,
                        lease_owner=owner,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        error_code=None,
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return LibraryScanLease(
                    run_id=run.id,
                    library_id=run.library_id,
                    root_directory_id=run.root_directory_id,
                    idempotency_key=run.idempotency_key,
                    scan_mode=run.scan_mode,
                    max_directories=run.max_directories,
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=expires_at,
                )

    async def _recover_expired_in_session(
        self, session: AsyncSession, current_time: datetime
    ) -> int:
        cancelled = await session.execute(
            update(LibraryScanRun)
            .where(
                LibraryScanRun.state == ScanRunState.RUNNING.value,
                LibraryScanRun.lease_expires_at.is_not(None),
                LibraryScanRun.lease_expires_at <= current_time,
                LibraryScanRun.cancel_requested.is_(True),
            )
            .values(
                state=ScanRunState.CANCELLED.value,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                error_code="cancelled",
            )
        )
        requeued = await session.execute(
            update(LibraryScanRun)
            .where(
                LibraryScanRun.state == ScanRunState.RUNNING.value,
                LibraryScanRun.lease_expires_at.is_not(None),
                LibraryScanRun.lease_expires_at <= current_time,
                LibraryScanRun.cancel_requested.is_(False),
            )
            .values(
                state=ScanRunState.QUEUED.value,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                error_code="scan_worker_recovered",
            )
        )
        return (cancelled.rowcount or 0) + (requeued.rowcount or 0)

    async def release(
        self,
        lease: LibraryScanLease,
        *,
        error_code: str | None = None,
        requeue: bool = False,
    ) -> None:
        async with self._session_factory() as session:
            current_time = datetime.now(UTC)
            run = await session.get(LibraryScanRun, lease.run_id)
            if (
                run is None
                or run.state
                not in {
                    ScanRunState.RUNNING.value,
                    ScanRunState.FAILED.value,
                    ScanRunState.CANCELLED.value,
                }
                or run.lease_owner != lease.lease_owner
                or run.lease_token != lease.lease_token
                or run.lease_expires_at is None
                or _as_utc(run.lease_expires_at) <= current_time
            ):
                return
            next_state = run.state
            next_error = run.error_code
            if run.cancel_requested and not run.complete:
                next_state = ScanRunState.CANCELLED.value
                next_error = "cancelled"
            elif requeue and not run.complete:
                if run.attempts >= MAX_SCAN_REQUEUE_ATTEMPTS:
                    # 网关反复取消(如活跃目录并发修改)时,无限 requeue 会以
                    # 满速热循环(每秒全量重读该 run 条目)且扫描永不完成。
                    # 达到上限转为 FAILED,由调度器冷却后重试或用户手动重扫。
                    next_state = ScanRunState.FAILED.value
                    next_error = "scan_requeue_limit"
                else:
                    next_state = ScanRunState.QUEUED.value
                    next_error = "scan_worker_recovered"
            if error_code is not None and not run.complete:
                if run.cancel_requested:
                    next_state = ScanRunState.CANCELLED.value
                    next_error = "cancelled"
                else:
                    next_state = ScanRunState.FAILED.value
                    next_error = error_code
            result = await session.execute(
                update(LibraryScanRun)
                .where(
                    LibraryScanRun.id == lease.run_id,
                    LibraryScanRun.state.in_(
                        (
                            ScanRunState.RUNNING.value,
                            ScanRunState.FAILED.value,
                            ScanRunState.CANCELLED.value,
                        )
                    ),
                    LibraryScanRun.lease_owner == lease.lease_owner,
                    LibraryScanRun.lease_token == lease.lease_token,
                    LibraryScanRun.lease_expires_at.is_not(None),
                    LibraryScanRun.lease_expires_at > current_time,
                )
                .values(
                    state=next_state,
                    error_code=next_error,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return
            await session.commit()

    async def renew(
        self,
        lease: LibraryScanLease,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> bool:
        if lease_duration.total_seconds() <= 0:
            raise LibraryScanOperationError("invalid_lease_duration")
        current_time = _as_utc(now or datetime.now(UTC))
        expires_at = current_time + lease_duration
        async with self._session_factory() as session:
            result = await session.execute(
                update(LibraryScanRun)
                .where(
                    LibraryScanRun.id == lease.run_id,
                    LibraryScanRun.state == ScanRunState.RUNNING.value,
                    LibraryScanRun.lease_owner == lease.lease_owner,
                    LibraryScanRun.lease_token == lease.lease_token,
                    LibraryScanRun.lease_expires_at.is_not(None),
                    LibraryScanRun.lease_expires_at > current_time,
                )
                .values(lease_expires_at=expires_at)
            )
            await session.commit()
            return result.rowcount == 1

    async def recover_expired(self, *, now: datetime | None = None) -> int:
        current_time = _as_utc(now or datetime.now(UTC))
        async with self._session_factory() as session:
            recovered = await self._recover_expired_in_session(session, current_time)
            await session.commit()
            return recovered

    async def readonly_directory_scope(
        self, lease: LibraryScanLease
    ) -> frozenset[str]:
        """Rebuild the verified directory scope needed by a resumed scan.

        A new gateway has no in-process observations from the previous worker.
        Only directory IDs that were persisted in the same validated tree
        snapshot as the cursor may be restored into its allowlist.
        """

        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, lease.run_id)
            library = await session.get(MediaLibrary, lease.library_id)
            checkpoint = await session.get(LibraryScanCheckpoint, lease.run_id)
            if (
                run is None
                or library is None
                or checkpoint is None
                or run.library_id != lease.library_id
                or run.root_directory_id != lease.root_directory_id
                or run.idempotency_key != lease.idempotency_key
                or run.scan_mode != lease.scan_mode
                or run.state != ScanRunState.RUNNING.value
                or run.lease_owner != lease.lease_owner
                or run.lease_token != lease.lease_token
                or run.lease_expires_at is None
                or _as_utc(run.lease_expires_at) <= datetime.now(UTC)
                or not library.enabled
                or not library.scope_verified
                or library.root_directory_id != lease.root_directory_id
            ):
                raise LibraryIndexError("library_scope_unverified")
            current_time = datetime.now(UTC)
            result = await session.execute(
                update(LibraryScanRun)
                .where(
                    LibraryScanRun.id == lease.run_id,
                    LibraryScanRun.state == ScanRunState.RUNNING.value,
                    LibraryScanRun.complete.is_(False),
                    LibraryScanRun.lease_owner == lease.lease_owner,
                    LibraryScanRun.lease_token == lease.lease_token,
                    LibraryScanRun.lease_expires_at.is_not(None),
                    LibraryScanRun.lease_expires_at > current_time,
                )
                .values(updated_at=current_time)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise LibraryIndexError("lease_claim_lost")
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == lease.run_id
                        )
                    )
                ).all()
            )
            scope = validate_tree_cursor_scope(
                run,
                checkpoint,
                entries,
                root_directory_id=lease.root_directory_id,
            )
            await session.commit()

        return scope


GatewayFactory = Callable[
    [str, Collection[str]], P115LibraryGateway | Awaitable[P115LibraryGateway]
]


class LibraryScanWorker:
    """Single-process poller for durable read-only scan operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        operations: LibraryScanOperationService,
        gateway_factory: GatewayFactory,
        *,
        owner: str,
        poll_interval_seconds: float = 1.0,
        lease_duration: timedelta = timedelta(minutes=5),
        hydrate_file_details: bool = False,
        event_logger: EventLogger | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_poll_interval")
        if lease_duration.total_seconds() <= 0:
            raise ValueError("invalid_lease_duration")
        self._session_factory = session_factory
        self._operations = operations
        self._gateway_factory = gateway_factory
        self._owner = owner
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._lease_duration = lease_duration
        self._hydrate_file_details = bool(hydrate_file_details)
        self._event_logger = event_logger

    async def run_once(self) -> bool:
        lease = await self._operations.claim_next(
            owner=self._owner, lease_duration=self._lease_duration
        )
        if lease is None:
            return False
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._renew_lease_forever(lease, heartbeat_stop, lease_lost),
            name=f"watch-assistant-library-scan-lease-{lease.run_id}",
        )
        scan_task = asyncio.create_task(
            self._scan_lease(lease, lease_lost),
            name=f"watch-assistant-library-scan-{lease.run_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {scan_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and lease_lost.is_set() and not scan_task.done():
                scan_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scan_task
                await self._operations.release(
                    lease, error_code="lease_claim_lost"
                )
                await self._emit_result(lease, None, "lease_claim_lost")
                return True
            result = await scan_task
        except asyncio.CancelledError:
            scan_task.cancel()
            with suppress(asyncio.CancelledError):
                await scan_task
            await self._operations.release(lease, requeue=True)
            raise
        except LibraryIndexError as error:
            await self._operations.release(lease, error_code=error.code)
            await self._emit_result(lease, None, error.code)
            return True
        except Exception:  # noqa: BLE001 - provider details never cross the boundary
            await self._operations.release(lease, error_code="scan_worker_failed")
            await self._emit_result(lease, None, "scan_worker_failed")
            return True
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        await self._operations.release(
            lease, requeue=result.state is ScanRunState.CANCELLED
        )
        await self._emit_result(lease, result, result.error_code)
        return True

    async def _scan_lease(
        self, lease: LibraryScanLease, lease_lost: asyncio.Event
    ) -> LibraryScanResult:
        if lease.scan_mode != "tree":
            raise LibraryScanOperationError("scan_mode_unsupported")
        directory_scope = await self._operations.readonly_directory_scope(lease)
        gateway = self._gateway_factory(lease.root_directory_id, directory_scope)
        if python_inspect.isawaitable(gateway):
            gateway = await gateway
        return await LibraryIndexService(
            self._session_factory,
            gateway,
            library_id=lease.library_id,
            root_directory_id=lease.root_directory_id,
            # The verified P115 contract intentionally remains one item per page.
            page_size=1,
            hydrate_file_details=self._hydrate_file_details,
            propagate_cancelled=True,
            cancel_event=lease_lost,
            lease_owner=lease.lease_owner,
            lease_token=lease.lease_token,
        ).scan_tree(
            lease.idempotency_key,
            max_directories=lease.max_directories,
        )

    async def recover_expired(self, *, now: datetime | None = None) -> int:
        return await self._operations.recover_expired(now=now)

    async def _renew_lease_forever(
        self,
        lease: LibraryScanLease,
        stop_event: asyncio.Event,
        failure_event: asyncio.Event,
    ) -> None:
        interval = max(min(self._lease_duration.total_seconds() / 3, 30.0), 0.1)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                try:
                    renewed = await self._operations.renew(
                        lease, lease_duration=self._lease_duration
                    )
                except Exception:  # noqa: BLE001 - the scan result owns recovery
                    failure_event.set()
                    return
                if not renewed:
                    failure_event.set()
                    return

    async def run_forever(
        self, stop_event: asyncio.Event, *, interval: float | None = None
    ) -> None:
        poll_interval = self._poll_interval_seconds if interval is None else interval
        while not stop_event.is_set():
            claimed = await self.run_once()
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    async def _emit_result(
        self,
        lease: LibraryScanLease,
        result: LibraryScanResult | None,
        error_code: str | None,
    ) -> None:
        state = result.state.value if result is not None else "failed"
        if state == "completed":
            fields = {
                "status": state,
                "items_seen": result.items_seen if result is not None else 0,
            }
        else:
            fields = {
                "status": state,
                "error_code": error_code or "scan_worker_failed",
            }
        await emit_event(
            self._event_logger,
            "library.scan.completed" if state == "completed" else "library.scan.failed",
            level=LoggingLevel.INFO if state == "completed" else LoggingLevel.WARNING,
            fields=fields,
            resource_type="library_scan",
            resource_id=lease.run_id,
        )


def _summary(run: LibraryScanRun) -> LibraryScanOperationSummary:
    return LibraryScanOperationSummary(
        run_id=run.id,
        library_id=run.library_id,
        state=run.state,
        state_message_zh=scan_state_message_zh(run.state),
        complete=run.complete,
        snapshot_revision=run.snapshot_revision,
        pages_read=run.pages_read,
        items_seen=run.items_seen,
        added_count=run.added_count,
        changed_count=run.changed_count,
        removed_count=run.removed_count,
        attempts=run.attempts,
        error_code=run.error_code,
        error_message_zh=scan_error_message_zh(run.error_code),
        cancel_requested=run.cancel_requested,
    )


def _require_verified_library(library: MediaLibrary | None) -> None:
    if (
        library is None
        or not library.enabled
        or not library.scope_verified
        or not library.root_directory_id
    ):
        raise LibraryScanOperationError("library_scope_unverified")


def _validate_identifier(value: str, code: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        raise LibraryScanOperationError(code)


def _validate_idempotency_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise LibraryScanOperationError("invalid_idempotency_key")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SCAN_ERROR_MESSAGES_ZH",
    "SCAN_STATE_LABELS_ZH",
    "LibraryScanLease",
    "LibraryScanOperationError",
    "LibraryScanOperationNotFound",
    "LibraryScanOperationService",
    "LibraryScanOperationSummary",
    "LibraryScanWorker",
    "scan_error_message_zh",
    "scan_state_message_zh",
]
