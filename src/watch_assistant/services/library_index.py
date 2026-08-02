"""Resumable, read-only persistence for a managed library directory."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_library import (
    DirectoryPage,
    LibraryEntry,
    P115LibraryGateway,
    ScanState,
)
from watch_assistant.library_models import (
    LibraryInventoryEvent,
    LibraryObjectLedger,
    LibraryScanCheckpoint,
    LibraryScanDiff,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)


class LibraryIndexError(ValueError):
    """Stable service error that never includes remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ScanRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class ScanChange:
    object_type: str
    object_id: str
    change_kind: str
    path_changed: bool

    def __repr__(self) -> str:
        return (
            "ScanChange(object_type=<redacted>, object_id=<redacted>, "
            f"change_kind={self.change_kind!r}, path_changed={self.path_changed!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LibraryScanResult:
    run_id: str
    state: ScanRunState
    complete: bool
    pages_read: int
    items_seen: int
    snapshot_revision: int | None
    added_count: int
    changed_count: int
    removed_count: int
    changes: tuple[ScanChange, ...]
    deletion_candidates: tuple[str, ...] = ()
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "LibraryScanResult(run_id=<redacted>, "
            f"state={self.state.value!r}, complete={self.complete!r}, "
            f"pages_read={self.pages_read}, items_seen={self.items_seen}, "
            f"added_count={self.added_count}, changed_count={self.changed_count}, "
            f"removed_count={self.removed_count}, "
            f"change_count={len(self.changes)}, "
            f"deletion_candidates={len(self.deletion_candidates)}, "
            f"error_code={self.error_code!r})"
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "complete": self.complete,
            "pages_read": self.pages_read,
            "items_seen": self.items_seen,
            "snapshot_revision": self.snapshot_revision,
            "added_count": self.added_count,
            "changed_count": self.changed_count,
            "removed_count": self.removed_count,
            "change_count": len(self.changes),
            "deletion_candidates": list(self.deletion_candidates),
            "error_code": self.error_code,
        }


class LibraryIndexService:
    """Index one explicitly verified root with no write-capable gateway seam."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: P115LibraryGateway,
        *,
        library_id: str,
        root_directory_id: str,
        page_size: int = 100,
        max_reported_changes: int = 100,
        propagate_cancelled: bool = False,
        cancel_event: asyncio.Event | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        _validate_identity(library_id)
        _validate_identity(root_directory_id)
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 1000
        ):
            raise LibraryIndexError("invalid_page_size")
        if (
            not isinstance(max_reported_changes, int)
            or isinstance(max_reported_changes, bool)
            or not 0 <= max_reported_changes <= 1000
        ):
            raise LibraryIndexError("invalid_change_limit")
        if (lease_owner is None) != (lease_token is None):
            raise LibraryIndexError("invalid_lease")
        if lease_owner is not None:
            _validate_identity(lease_owner)
        if lease_token is not None:
            _validate_identity(lease_token)
        self._session_factory = session_factory
        self._gateway = gateway
        self._library_id = library_id
        self._root_directory_id = root_directory_id
        self._page_size = page_size
        self._max_reported_changes = max_reported_changes
        self._propagate_cancelled = propagate_cancelled
        self._cancel_event = cancel_event
        self._lease_owner = lease_owner
        self._lease_token = lease_token
        self._external_lease = lease_token is not None
        self._execution_active = False
        self._direct_lease_duration = timedelta(hours=1)
        # A service instance is scoped to one worker operation.  Keeping the
        # cursor here preserves the old _persist_tree_page test seam while the
        # cursor itself remains durable in LibraryScanCheckpoint.
        self._tree_cursors: dict[str, dict[str, object]] = {}

    async def scan(self, idempotency_key: str) -> LibraryScanResult:
        _validate_idempotency_key(idempotency_key)
        await self._verify_scope()
        run = await self._get_or_create_run(idempotency_key, scan_mode="root")
        if run.complete and run.state == ScanRunState.COMPLETED.value:
            return await self._result_for_run(run.id)
        if not await self._claim_execution_lease(run.id):
            return await self._result_for_run(run.id)
        try:
            await self._mark_running(run.id)
            checkpoint = await self._checkpoint_for_run(run.id)
            if (
                run.expected_page_count is not None
                and checkpoint.page == run.expected_page_count
            ):
                return await self._complete_run(run.id)
            next_page = checkpoint.page + 1
            expected_page_count = run.expected_page_count
            expected_total = run.expected_total

            while True:
                try:
                    await self._fence_before_remote_page(run.id)
                    page = await self._gateway.list_directory(
                        self._root_directory_id,
                        page=next_page,
                        page_size=self._page_size,
                    )
                except Exception:  # noqa: BLE001 - remote details never cross the boundary
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "gateway_error"
                    )
                    return await self._result_for_run(run.id)

                try:
                    expected_page_count, expected_total, terminal = _validate_page(
                        page,
                        requested_page=next_page,
                        root_directory_id=self._root_directory_id,
                        expected_page_count=expected_page_count,
                        expected_total=expected_total,
                        require_total=True,
                    )
                except LibraryIndexError as error:
                    state = (
                        ScanRunState.CANCELLED
                        if error.code == "cancelled"
                        else ScanRunState.FAILED
                    )
                    await self._finish_incomplete(run.id, state, error.code)
                    return await self._result_for_run(run.id)

                try:
                    await self._persist_page(
                        run.id,
                        page,
                        expected_page_count=expected_page_count,
                        expected_total=expected_total,
                    )
                except LibraryIndexError as error:
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, error.code
                    )
                    return await self._result_for_run(run.id)
                except Exception:  # noqa: BLE001 - storage details never cross the boundary
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "storage_error"
                    )
                    return await self._result_for_run(run.id)

                if terminal:
                    return await self._complete_run(run.id)
                next_page += 1
        except asyncio.CancelledError:
            if self._propagate_cancelled and (
                self._cancel_event is None or self._cancel_event.is_set()
            ):
                raise
            await self._finish_incomplete(run.id, ScanRunState.CANCELLED, "cancelled")
            return await self._result_for_run(run.id)

    async def scan_tree(
        self, idempotency_key: str, *, max_directories: int = 10_000
    ) -> LibraryScanResult:
        """Scan the configured root and every discovered child directory.

        Each page is validated against its own directory while the resulting
        entries remain one immutable snapshot. Incomplete runs never update
        the ledger or produce removal conclusions.
        """

        _validate_idempotency_key(idempotency_key)
        if (
            not isinstance(max_directories, int)
            or isinstance(max_directories, bool)
            or not 1 <= max_directories <= 100_000
        ):
            raise LibraryIndexError("invalid_directory_limit")
        await self._verify_scope()
        run = await self._get_or_create_run(idempotency_key, scan_mode="tree")
        if run.complete and run.state == ScanRunState.COMPLETED.value:
            return await self._result_for_run(run.id)
        if not await self._claim_execution_lease(run.id):
            return await self._result_for_run(run.id)
        await self._reset_tree_run(run.id)
        cursor = await self._tree_cursor_for_run(run.id)
        self._tree_cursors[run.id] = cursor
        try:
            await self._mark_running(run.id)
            pages_read = await self._pages_read_for_run(run.id)
            while cursor["pending"]:
                pending = cursor["pending"]
                if not isinstance(pending, list) or not pending:
                    break
                current = pending[0]
                if not isinstance(current, dict):
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "entry_path_invalid"
                    )
                    return await self._result_for_run(run.id)
                directory_id = current.get("directory_id")
                parent_path = current.get("parent_path")
                page_number = current.get("page")
                expected_page_count = current.get("page_count")
                expected_total = current.get("total")
                if (
                    not isinstance(directory_id, str)
                    or not isinstance(parent_path, str)
                    or not isinstance(page_number, int)
                    or page_number < 1
                    or not _optional_nonnegative_int(expected_page_count)
                    or not _optional_nonnegative_int(expected_total)
                ):
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "entry_path_invalid"
                    )
                    return await self._result_for_run(run.id)
                if await self._cancel_requested(run.id):
                    await self._finish_incomplete(
                        run.id, ScanRunState.CANCELLED, "cancelled"
                    )
                    return await self._result_for_run(run.id)
                try:
                    await self._fence_before_remote_page(run.id)
                    page = await self._gateway.list_directory(
                        directory_id,
                        page=page_number,
                        page_size=self._page_size,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - remote details stay private
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "gateway_error"
                    )
                    return await self._result_for_run(run.id)
                try:
                    page = _materialize_tree_paths(page, parent_path)
                    normalized_page_count, normalized_total, terminal = _validate_page(
                        page,
                        requested_page=page_number,
                        root_directory_id=directory_id,
                        expected_page_count=expected_page_count,
                        expected_total=expected_total,
                        require_total=True,
                    )
                    next_cursor = _advance_tree_cursor(
                        cursor,
                        page,
                        directory_id=directory_id,
                        parent_path=parent_path,
                        page_number=page_number,
                        page_count=normalized_page_count,
                        total=normalized_total,
                        terminal=terminal,
                        max_directories=max_directories,
                    )
                    cursor = next_cursor
                    self._tree_cursors[run.id] = next_cursor
                    await self._persist_tree_page(
                        run.id, page, pages_read=pages_read + 1
                    )
                except LibraryIndexError as error:
                    state = (
                        ScanRunState.CANCELLED
                        if error.code == "cancelled"
                        else ScanRunState.FAILED
                    )
                    await self._finish_incomplete(run.id, state, error.code)
                    return await self._result_for_run(run.id)
                except Exception:  # noqa: BLE001 - storage details stay private
                    await self._finish_incomplete(
                        run.id, ScanRunState.FAILED, "storage_error"
                    )
                    return await self._result_for_run(run.id)
                pages_read += 1
            return await self._complete_run(run.id)
        except asyncio.CancelledError:
            if self._propagate_cancelled and (
                self._cancel_event is None or self._cancel_event.is_set()
            ):
                raise
            await self._finish_incomplete(run.id, ScanRunState.CANCELLED, "cancelled")
            return await self._result_for_run(run.id)

    async def _reset_tree_run(self, run_id: str) -> None:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if run is None or checkpoint is None:
                raise LibraryIndexError("scan_run_missing")
            if run.complete:
                return
            if (
                self._lease_token is None
                and run.state == ScanRunState.RUNNING.value
                and run.lease_token is not None
                and run.lease_expires_at is not None
                and _as_utc(run.lease_expires_at) > datetime.now(UTC)
            ):
                return
            self._assert_execution_lease(run)
            await self._fence_write(session, run)
            raw_cursor = checkpoint.cursor_json
            cursor = _decode_tree_cursor(raw_cursor)
            legacy_cursor = _is_legacy_tree_cursor(raw_cursor)
            if cursor is None and raw_cursor not in {"", "{}"} and not legacy_cursor:
                raise LibraryIndexError("checkpoint_invalid")
            if cursor is None or legacy_cursor:
                await session.execute(
                    delete(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run_id
                    )
                )
                await session.execute(
                    delete(LibraryScanDiff).where(
                        LibraryScanDiff.scan_run_id == run_id
                    )
                )
                cursor = _initial_tree_cursor(self._root_directory_id)
                checkpoint.page = 0
                checkpoint.items_seen = 0
                run.pages_read = 0
                run.items_seen = 0
            checkpoint.cursor_json = _encode_tree_cursor(cursor)
            run.error_code = None
            run.expected_page_count = None
            run.expected_total = _tree_cursor_expected_total(cursor)
            await self._fence_write(session, run, refresh=False)
            await session.commit()

    async def _tree_cursor_for_run(self, run_id: str) -> dict[str, object]:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if run is None or checkpoint is None:
                raise LibraryIndexError("checkpoint_missing")
            cursor = _decode_tree_cursor(checkpoint.cursor_json)
            if cursor is None:
                raise LibraryIndexError("checkpoint_invalid")
            entries = list(
                (
                    await session.scalars(
                        select(LibraryScanEntry).where(
                            LibraryScanEntry.scan_run_id == run_id
                        )
                    )
                ).all()
            )
            _validate_tree_cursor_evidence(
                run,
                checkpoint,
                entries,
                root_directory_id=self._root_directory_id,
                require_complete=False,
            )
            return cursor

    async def _pages_read_for_run(self, run_id: str) -> int:
        async with self._session_factory() as session:
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if checkpoint is None:
                raise LibraryIndexError("checkpoint_missing")
            return checkpoint.page

    async def _cancel_requested(self, run_id: str) -> bool:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            return run is not None and run.cancel_requested

    async def _fence_before_remote_page(self, run_id: str) -> None:
        """Prove the durable lease immediately before a remote page call."""

        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            self._assert_execution_lease(run)
            await self._fence_write(session, run)
            await session.commit()

    async def _persist_tree_page(
        self, run_id: str, page: DirectoryPage, *, pages_read: int
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(LibraryScanRun, run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if run is None or checkpoint is None:
                raise LibraryIndexError("scan_run_missing")
            self._assert_execution_lease(run)
            await self._fence_write(session, run)
            seen: set[tuple[str, str]] = set()
            for entry in page.items:
                object_type, object_id = _entry_identity(entry)
                identity = (object_type, object_id)
                if identity in seen:
                    raise LibraryIndexError("repeated_entry")
                seen.add(identity)
                existing = await session.get(
                    LibraryScanEntry,
                    {
                        "scan_run_id": run_id,
                        "object_type": object_type,
                        "object_id": object_id,
                    },
                )
                if existing is not None:
                    raise LibraryIndexError("repeated_entry")
                session.add(
                    LibraryScanEntry(
                        scan_run_id=run_id,
                        object_type=object_type,
                        object_id=object_id,
                        parent_id=entry.parent_id,
                        name=entry.name,
                        path=entry.path,
                        is_directory=entry.is_directory,
                        size_bytes=entry.size_bytes,
                        modified_at=entry.modified_at,
                    )
                )
            run.expected_page_count = None
            run.expected_total = None
            run.pages_read = pages_read
            run.items_seen = checkpoint.items_seen + len(page.items)
            checkpoint.page = pages_read
            checkpoint.items_seen = run.items_seen
            cursor = self._tree_cursors.get(run_id)
            if cursor is not None:
                checkpoint.cursor_json = _encode_tree_cursor(cursor)
                run.expected_total = _tree_cursor_expected_total(cursor)
            else:
                run.expected_total = None
            await self._fence_write(session, run, refresh=False)

    async def _verify_scope(self) -> None:
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, self._library_id)
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or library.root_directory_id != self._root_directory_id
        ):
            raise LibraryIndexError("library_scope_unverified")

    async def _get_or_create_run(
        self, idempotency_key: str, *, scan_mode: str
    ) -> LibraryScanRun:
        async with self._session_factory() as session:
            run = await session.scalar(
                select(LibraryScanRun).where(
                    LibraryScanRun.library_id == self._library_id,
                    LibraryScanRun.idempotency_key == idempotency_key,
                )
            )
            if run is not None:
                if run.root_directory_id != self._root_directory_id:
                    raise LibraryIndexError("library_scope_unverified")
                if run.scan_mode != scan_mode:
                    raise LibraryIndexError("scan_mode_mismatch")
                return run
            run = LibraryScanRun(
                id=uuid.uuid4().hex,
                library_id=self._library_id,
                root_directory_id=self._root_directory_id,
                idempotency_key=idempotency_key,
                scan_mode=scan_mode,
            )
            session.add(run)
            await session.flush()
            session.add(LibraryScanCheckpoint(scan_run_id=run.id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(LibraryScanRun).where(
                        LibraryScanRun.library_id == self._library_id,
                        LibraryScanRun.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise LibraryIndexError("scan_run_conflict") from None
                if existing.root_directory_id != self._root_directory_id:
                    raise LibraryIndexError("library_scope_unverified")
                if existing.scan_mode != scan_mode:
                    raise LibraryIndexError("scan_mode_mismatch")
                return existing
            return run

    async def _claim_execution_lease(self, run_id: str) -> bool:
        """Fence direct callers and validate the queue worker's lease.

        Queue workers already own a durable lease from
        ``LibraryScanOperationService``. Direct service callers receive a
        short-lived local lease through the same columns, so concurrent
        idempotent calls cannot both mutate one snapshot.
        """

        current_time = datetime.now(UTC)
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            if run.complete:
                return False
            if self._external_lease:
                result = await session.execute(
                    update(LibraryScanRun)
                    .where(
                        LibraryScanRun.id == run_id,
                        LibraryScanRun.state == ScanRunState.RUNNING.value,
                        LibraryScanRun.complete.is_(False),
                        LibraryScanRun.lease_owner == self._lease_owner,
                        LibraryScanRun.lease_token == self._lease_token,
                        LibraryScanRun.lease_expires_at.is_not(None),
                        LibraryScanRun.lease_expires_at > current_time,
                    )
                    .values(updated_at=current_time)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    raise LibraryIndexError("lease_claim_lost")
                return True
            if self._execution_active:
                return False
            self._lease_owner = None
            self._lease_token = None

            token = uuid.uuid4().hex
            owner = "library-index"
            expires_at = current_time + self._direct_lease_duration
            result = await session.execute(
                update(LibraryScanRun)
                .where(
                    LibraryScanRun.id == run_id,
                    LibraryScanRun.complete.is_(False),
                    or_(
                        LibraryScanRun.lease_expires_at.is_(None),
                        LibraryScanRun.lease_expires_at <= current_time,
                    ),
                )
                .values(
                    state=ScanRunState.RUNNING.value,
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=expires_at,
                    error_code=None,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            await session.commit()
        self._lease_owner = owner
        self._lease_token = token
        self._execution_active = True
        return True

    async def _mark_running(self, run_id: str) -> None:
        async with self._session_factory() as session:
            current_time = datetime.now(UTC)
            conditions = [
                LibraryScanRun.id == run_id,
                LibraryScanRun.state == ScanRunState.RUNNING.value,
                LibraryScanRun.complete.is_(False),
            ]
            if self._lease_token is not None:
                conditions.extend(
                    (
                        LibraryScanRun.lease_token == self._lease_token,
                        LibraryScanRun.lease_expires_at.is_not(None),
                        LibraryScanRun.lease_expires_at > current_time,
                    )
                )
                if self._lease_owner is not None:
                    conditions.append(LibraryScanRun.lease_owner == self._lease_owner)
            result = await session.execute(
                update(LibraryScanRun)
                .where(*conditions)
                .values(error_code=None, updated_at=current_time)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise LibraryIndexError("lease_claim_lost")
            await session.commit()

    async def _checkpoint_for_run(self, run_id: str) -> LibraryScanCheckpoint:
        async with self._session_factory() as session:
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if checkpoint is None:
                raise LibraryIndexError("checkpoint_missing")
            return checkpoint

    async def _persist_page(
        self,
        run_id: str,
        page: DirectoryPage,
        *,
        expected_page_count: int | None,
        expected_total: int | None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(LibraryScanRun, run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if run is None or checkpoint is None:
                raise LibraryIndexError("scan_run_missing")
            self._assert_execution_lease(run)
            await self._fence_write(session, run)
            seen: set[tuple[str, str]] = set()
            for entry in page.items:
                object_type, object_id = _entry_identity(entry)
                identity = (object_type, object_id)
                if identity in seen:
                    raise LibraryIndexError("repeated_entry")
                seen.add(identity)
                existing = await session.get(
                    LibraryScanEntry,
                    {
                        "scan_run_id": run_id,
                        "object_type": object_type,
                        "object_id": object_id,
                    },
                )
                if existing is not None:
                    raise LibraryIndexError("repeated_entry")
                session.add(
                    LibraryScanEntry(
                        scan_run_id=run_id,
                        object_type=object_type,
                        object_id=object_id,
                        parent_id=entry.parent_id,
                        name=entry.name,
                        path=entry.path,
                        is_directory=entry.is_directory,
                        size_bytes=entry.size_bytes,
                        modified_at=entry.modified_at,
                    )
                )
            run.expected_page_count = expected_page_count
            run.expected_total = expected_total
            run.pages_read = page.page
            run.items_seen = checkpoint.items_seen + len(page.items)
            if expected_total is None:
                raise LibraryIndexError("pagination_unverified")
            if run.items_seen > expected_total:
                raise LibraryIndexError("total_mismatch")
            if page.terminal is True and run.items_seen != expected_total:
                raise LibraryIndexError("total_mismatch")
            checkpoint.page = page.page
            checkpoint.items_seen = run.items_seen
            await self._fence_write(session, run, refresh=False)

    async def _finish_incomplete(
        self, run_id: str, state: ScanRunState, error_code: str
    ) -> None:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            self._assert_execution_lease(run)
            await self._fence_write(session, run)
            run.state = state.value
            run.complete = False
            run.error_code = error_code
            await self._fence_write(session, run, refresh=False)
            if not self._external_lease:
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
            await session.commit()
        if not self._external_lease:
            self._lease_owner = None
            self._lease_token = None
            self._execution_active = False

    async def _complete_run(self, run_id: str) -> LibraryScanResult:
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(LibraryScanRun, run_id)
                if run is None:
                    raise LibraryIndexError("scan_run_missing")
                self._assert_execution_lease(run)
                await self._fence_write(session, run)
                tree_cursor_ready = True
                if run.scan_mode == "tree":
                    checkpoint = await session.get(LibraryScanCheckpoint, run_id)
                    entries = list(
                        (
                            await session.scalars(
                                select(LibraryScanEntry).where(
                                    LibraryScanEntry.scan_run_id == run_id
                                )
                            )
                        ).all()
                    )
                    if checkpoint is None:
                        tree_cursor_ready = False
                        evidence_error = "checkpoint_missing"
                    else:
                        try:
                            _validate_tree_cursor_evidence(
                                run,
                                checkpoint,
                                entries,
                                root_directory_id=self._root_directory_id,
                                require_complete=True,
                            )
                        except LibraryIndexError as error:
                            tree_cursor_ready = False
                            evidence_error = error.code
                        else:
                            evidence_error = None
                else:
                    checkpoint = await session.get(LibraryScanCheckpoint, run_id)
                    entries = list(
                        (
                            await session.scalars(
                                select(LibraryScanEntry).where(
                                    LibraryScanEntry.scan_run_id == run_id
                                )
                            )
                        ).all()
                    )
                    evidence_error = None
                    try:
                        validate_complete_scan_evidence(
                            run,
                            checkpoint,
                            entries,
                            root_directory_id=self._root_directory_id,
                        )
                    except LibraryIndexError as error:
                        tree_cursor_ready = False
                        evidence_error = error.code
                if not tree_cursor_ready or (
                    run.scan_mode == "tree" and run.expected_total is None
                ):
                    run.state = ScanRunState.FAILED.value
                    run.complete = False
                    run.error_code = evidence_error or "pagination_unverified"
                    await self._fence_write(session, run, refresh=False)
                elif (
                    run.expected_total is not None
                    and run.items_seen != run.expected_total
                ):
                    run.state = ScanRunState.FAILED.value
                    run.complete = False
                    run.error_code = "total_mismatch"
                    await self._fence_write(session, run, refresh=False)
                else:
                    previous = await session.scalar(
                        select(LibraryScanRun)
                        .where(
                            LibraryScanRun.library_id == self._library_id,
                            LibraryScanRun.root_directory_id == self._root_directory_id,
                            LibraryScanRun.complete.is_(True),
                            LibraryScanRun.state == ScanRunState.COMPLETED.value,
                            LibraryScanRun.id != run_id,
                        )
                        .order_by(LibraryScanRun.snapshot_revision.desc())
                    )
                    previous_id = None if previous is None else previous.id
                    latest_revision = await session.scalar(
                        select(func.max(LibraryScanRun.snapshot_revision)).where(
                            LibraryScanRun.library_id == self._library_id
                        )
                    )
                    run.snapshot_revision = (latest_revision or 0) + 1
                    await session.execute(
                        delete(LibraryScanDiff).where(
                            LibraryScanDiff.scan_run_id == run_id
                        )
                    )
                    changes: list[ScanChange] = []
                    added_count = 0
                    changed_count = 0
                    removed_count = 0
                    previous_entries: dict[tuple[str, str], LibraryScanEntry] = {}
                    if previous_id is not None:
                        previous_entries = {
                            (entry.object_type, entry.object_id): entry
                            for entry in (
                                await session.scalars(
                                    select(LibraryScanEntry).where(
                                        LibraryScanEntry.scan_run_id == previous_id
                                    )
                                )
                            ).all()
                        }
                    current_keys = {(entry.object_type, entry.object_id) for entry in entries}
                    for entry in entries:
                        old = previous_entries.get((entry.object_type, entry.object_id))
                        if old is None:
                            kind = "added"
                            path_changed = False
                            added_count += 1
                        elif _entry_changed(old, entry):
                            kind = "changed"
                            path_changed = old.path != entry.path
                            changed_count += 1
                        else:
                            continue
                        session.add(
                            LibraryScanDiff(
                                scan_run_id=run_id,
                                object_type=entry.object_type,
                                object_id=entry.object_id,
                                change_kind=kind,
                                path_changed=path_changed,
                            )
                        )
                        if len(changes) < self._max_reported_changes:
                            changes.append(
                                ScanChange(
                                    entry.object_type,
                                    entry.object_id,
                                    kind,
                                    path_changed,
                                )
                            )
                        await _record_ledger_observation(
                            session,
                            library_id=self._library_id,
                            scan_run_id=run_id,
                            entry=entry,
                            event_kind=kind,
                        )
                    if previous_id is not None:
                        for key, old in previous_entries.items():
                            if key in current_keys:
                                continue
                            removed_count += 1
                            session.add(
                                LibraryScanDiff(
                                    scan_run_id=run_id,
                                    object_type=old.object_type,
                                    object_id=old.object_id,
                                    change_kind="removed",
                                    path_changed=False,
                                )
                            )
                            if len(changes) < self._max_reported_changes:
                                changes.append(
                                    ScanChange(
                                        old.object_type,
                                        old.object_id,
                                        "removed",
                                        False,
                                    )
                                )
                            await _record_ledger_removal(
                                session,
                                library_id=self._library_id,
                                scan_run_id=run_id,
                                entry=old,
                            )
                    # The CAS must happen while the run is still running.  The
                    # terminal state and all dirty snapshot rows are flushed by
                    # this transaction only after the owner has been proven.
                    await self._fence_write(session, run, refresh=False)
                    run.state = ScanRunState.COMPLETED.value
                    run.complete = True
                    run.error_code = None
                    run.added_count = added_count
                    run.changed_count = changed_count
                    run.removed_count = removed_count
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
            await session.commit()
        if not self._external_lease:
            self._lease_owner = None
            self._lease_token = None
            self._execution_active = False
        return await self._result_for_run(run_id)

    def _assert_execution_lease(self, run: LibraryScanRun) -> None:
        if self._lease_token is None:
            return
        if not self._lease_is_current(run, datetime.now(UTC)):
            raise LibraryIndexError("lease_claim_lost")

    async def _fence_write(
        self, session: AsyncSession, run: LibraryScanRun, *, refresh: bool = True
    ) -> None:
        if self._lease_token is None:
            return
        current_time = datetime.now(UTC)
        conditions = [
            LibraryScanRun.id == run.id,
            LibraryScanRun.state == ScanRunState.RUNNING.value,
            LibraryScanRun.complete.is_(False),
            LibraryScanRun.lease_token == self._lease_token,
            LibraryScanRun.lease_expires_at.is_not(None),
            LibraryScanRun.lease_expires_at > current_time,
        ]
        if self._lease_owner is not None:
            conditions.append(LibraryScanRun.lease_owner == self._lease_owner)
        # The final fence is deliberately executed without autoflush.  An
        # expired worker must not flush its dirty snapshot/ledger rows before
        # the lease CAS has proved that the owner and expiry are still valid.
        with session.no_autoflush:
            result = await session.execute(
                update(LibraryScanRun)
                .where(*conditions)
                .values(updated_at=current_time)
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            raise LibraryIndexError("lease_claim_lost")
        if refresh:
            await session.refresh(run)

    def _lease_is_current(self, run: LibraryScanRun, current_time: datetime) -> bool:
        if (
            run.state != ScanRunState.RUNNING.value
            or run.complete
            or run.lease_token != self._lease_token
            or run.lease_expires_at is None
        ):
            return False
        if self._lease_owner is not None and run.lease_owner != self._lease_owner:
            return False
        return _as_utc(run.lease_expires_at) > current_time

    async def _result_for_run(self, run_id: str) -> LibraryScanResult:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            rows = []
            if run.complete:
                rows = list(
                    await session.scalars(
                        select(LibraryScanDiff)
                        .where(LibraryScanDiff.scan_run_id == run_id)
                        .order_by(
                            LibraryScanDiff.object_type, LibraryScanDiff.object_id
                        )
                        .limit(self._max_reported_changes)
                    )
                )
            return LibraryScanResult(
                run_id=run.id,
                state=ScanRunState(run.state),
                complete=run.complete,
                pages_read=run.pages_read,
                items_seen=run.items_seen,
                snapshot_revision=run.snapshot_revision,
                added_count=run.added_count,
                changed_count=run.changed_count,
                removed_count=run.removed_count,
                changes=tuple(
                    ScanChange(
                        row.object_type,
                        row.object_id,
                        row.change_kind,
                        row.path_changed,
                    )
                    for row in rows
                ),
                deletion_candidates=tuple(
                    row.object_id for row in rows if row.change_kind == "removed"
                ),
                error_code=run.error_code,
            )


async def _record_ledger_observation(
    session: AsyncSession,
    *,
    library_id: str,
    scan_run_id: str,
    entry: LibraryScanEntry,
    event_kind: str,
) -> None:
    """Upsert one observed object and record only meaningful transitions."""

    ledger = await session.scalar(
        select(LibraryObjectLedger).where(
            LibraryObjectLedger.library_id == library_id,
            LibraryObjectLedger.object_type == entry.object_type,
            LibraryObjectLedger.object_id == entry.object_id,
        )
    )
    now = datetime.now(UTC)
    if ledger is None:
        ledger = LibraryObjectLedger(
            id="ledger_" + uuid.uuid4().hex,
            library_id=library_id,
            object_type=entry.object_type,
            object_id=entry.object_id,
            parent_id=entry.parent_id,
            name=entry.name,
            path=entry.path,
            is_directory=entry.is_directory,
            size_bytes=entry.size_bytes,
            modified_at=entry.modified_at,
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            last_scan_run_id=scan_run_id,
        )
        session.add(ledger)
        await _record_inventory_event(
            session,
            library_id=library_id,
            scan_run_id=scan_run_id,
            entry=entry,
            event_kind="added",
            previous_status=None,
        )
        return

    previous_status = ledger.status
    ledger.parent_id = entry.parent_id
    ledger.name = entry.name
    ledger.path = entry.path
    ledger.is_directory = entry.is_directory
    ledger.size_bytes = entry.size_bytes
    ledger.modified_at = entry.modified_at
    ledger.last_seen_at = now
    ledger.last_scan_run_id = scan_run_id
    ledger.revision += 1
    if previous_status == "missing":
        ledger.status = "active"
        ledger.missing_since = None
        await _record_inventory_event(
            session,
            library_id=library_id,
            scan_run_id=scan_run_id,
            entry=entry,
            event_kind="restored",
            previous_status=previous_status,
        )
    elif event_kind == "changed":
        await _record_inventory_event(
            session,
            library_id=library_id,
            scan_run_id=scan_run_id,
            entry=entry,
            event_kind="changed",
            previous_status=previous_status,
        )


async def _record_ledger_removal(
    session: AsyncSession,
    *,
    library_id: str,
    scan_run_id: str,
    entry: LibraryScanEntry,
) -> None:
    ledger = await session.scalar(
        select(LibraryObjectLedger).where(
            LibraryObjectLedger.library_id == library_id,
            LibraryObjectLedger.object_type == entry.object_type,
            LibraryObjectLedger.object_id == entry.object_id,
        )
    )
    now = datetime.now(UTC)
    previous_status = None if ledger is None else ledger.status
    if ledger is None:
        ledger = LibraryObjectLedger(
            id="ledger_" + uuid.uuid4().hex,
            library_id=library_id,
            object_type=entry.object_type,
            object_id=entry.object_id,
            parent_id=entry.parent_id,
            name=entry.name,
            path=entry.path,
            is_directory=entry.is_directory,
            size_bytes=entry.size_bytes,
            modified_at=entry.modified_at,
            status="missing",
            first_seen_at=now,
            last_seen_at=now,
            missing_since=now,
            last_scan_run_id=scan_run_id,
        )
        session.add(ledger)
    else:
        ledger.status = "missing"
        ledger.missing_since = ledger.missing_since or now
        ledger.last_scan_run_id = scan_run_id
        ledger.revision += 1
    if previous_status != "missing":
        await _record_inventory_event(
            session,
            library_id=library_id,
            scan_run_id=scan_run_id,
            entry=entry,
            event_kind="removed",
            previous_status=previous_status,
        )


async def _record_inventory_event(
    session: AsyncSession,
    *,
    library_id: str,
    scan_run_id: str,
    entry: LibraryScanEntry,
    event_kind: str,
    previous_status: str | None,
) -> None:
    dedupe_key = f"{scan_run_id}:{entry.object_type}:{entry.object_id}:{event_kind}"
    existing = await session.scalar(
        select(LibraryInventoryEvent.id).where(
            LibraryInventoryEvent.dedupe_key == dedupe_key
        )
    )
    if existing is not None:
        return
    session.add(
        LibraryInventoryEvent(
            id="inv_evt_" + uuid.uuid4().hex,
            library_id=library_id,
            scan_run_id=scan_run_id,
            object_type=entry.object_type,
            object_id=entry.object_id,
            event_kind=event_kind,
            previous_status=previous_status,
            dedupe_key=dedupe_key,
        )
    )


def _validate_identity(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        raise LibraryIndexError("invalid_identity")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _materialize_tree_paths(page: DirectoryPage, parent_path: str) -> DirectoryPage:
    """Fill relative paths for gateways that intentionally omit remote paths."""

    _validate_remote_path(parent_path, allow_empty=True)
    items = []
    for entry in page.items:
        if entry.path is None:
            items.append(replace(entry, path=_join_tree_path(parent_path, entry.name)))
        else:
            _validate_remote_path(entry.path)
            items.append(entry)
    return replace(page, items=tuple(items))


def _join_tree_path(parent_path: str, name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise LibraryIndexError("entry_path_invalid")
    value = "/".join(part for part in (parent_path, name) if part)
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise LibraryIndexError("entry_path_invalid")
    normalized = "/".join(parsed.parts)
    if not normalized or len(normalized) > 4096:
        raise LibraryIndexError("entry_path_invalid")
    return normalized


def _validate_idempotency_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise LibraryIndexError("invalid_idempotency_key")


def _entry_identity(entry: LibraryEntry) -> tuple[str, str]:
    if entry.parent_id is None:
        raise LibraryIndexError("entry_scope_unverified")
    _validate_identity(entry.parent_id)
    if entry.is_directory:
        if entry.directory_id is None:
            raise LibraryIndexError("missing_directory_id")
        _validate_identity(entry.directory_id)
        return "directory", entry.directory_id
    if entry.file_id is None:
        raise LibraryIndexError("missing_file_id")
    _validate_identity(entry.file_id)
    return "file", entry.file_id


def _validate_page(
    page: DirectoryPage,
    *,
    requested_page: int,
    root_directory_id: str,
    expected_page_count: int | None,
    expected_total: int | None,
    require_total: bool = False,
) -> tuple[int | None, int | None, bool]:
    if page.page != requested_page or page.page < 1:
        raise LibraryIndexError("repeated_page")
    if page.page_count is not None:
        if page.page_count < 1 or page.page_count < requested_page:
            raise LibraryIndexError("pagination_unverified")
        if expected_page_count is not None and page.page_count != expected_page_count:
            raise LibraryIndexError("page_count_changed")
        expected_page_count = page.page_count
    if require_total and page.total is None:
        raise LibraryIndexError("pagination_unverified")
    if expected_total is not None and page.total != expected_total:
        raise LibraryIndexError("total_changed")
    if expected_total is None:
        expected_total = page.total
    if page.state != ScanState.COMPLETE or page.scan_complete is False:
        if page.state == ScanState.CANCELLED:
            raise LibraryIndexError("cancelled")
        raise LibraryIndexError(page.error_code or "partial_page")
    terminal_by_count = (
        expected_page_count is not None and requested_page == expected_page_count
    )
    continues_by_count = (
        expected_page_count is not None and requested_page < expected_page_count
    )
    if (
        (page.terminal is True and page.has_more is True)
        or (page.terminal is True and page.next_page is not None)
        or (page.has_more is False and page.next_page is not None)
        or (page.terminal is False and page.has_more is False)
        or (page.next_page is not None and page.next_page != requested_page + 1)
        or (
            terminal_by_count
            and (
                page.terminal is False
                or page.has_more is True
                or page.next_page is not None
            )
        )
        or (continues_by_count and (page.terminal is True or page.has_more is False))
    ):
        raise LibraryIndexError("pagination_unverified")
    if (
        not page.items
        and not terminal_by_count
        and page.terminal is not True
        and page.has_more is not False
    ):
        raise LibraryIndexError("empty_page")
    for entry in page.items:
        if entry.parent_id != root_directory_id:
            raise LibraryIndexError("entry_out_of_scope")
        if entry.path is not None:
            _validate_remote_path(entry.path)
        _entry_identity(entry)
    terminal = terminal_by_count or page.terminal is True or page.has_more is False
    if terminal and expected_total is not None and expected_total < 0:
        raise LibraryIndexError("invalid_total")
    if not terminal and not (
        continues_by_count or page.has_more is True or page.next_page is not None
    ):
        raise LibraryIndexError("pagination_unverified")
    return expected_page_count, expected_total, terminal


def _initial_tree_cursor(root_directory_id: str) -> dict[str, object]:
    return {
        "version": 2,
        "directory_totals": {},
        "expected_total": 0,
        "pending": [
            {
                "directory_id": root_directory_id,
                "parent_path": "",
                "page": 1,
                "page_count": None,
                "total": None,
                "items_seen": 0,
            }
        ],
        "visited": [root_directory_id],
    }


def _decode_tree_cursor(value: str | None) -> dict[str, object] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict) or decoded.get("version") != 2:
        return None
    pending = decoded.get("pending")
    visited = decoded.get("visited")
    directory_totals = decoded.get("directory_totals")
    expected_total = decoded.get("expected_total")
    if (
        not isinstance(pending, list)
        or not isinstance(visited, list)
        or not isinstance(directory_totals, dict)
        or not _optional_nonnegative_int(expected_total)
        or expected_total is None
    ):
        return None
    if len(set(visited)) != len(visited):
        return None
    for directory_id in visited:
        if not isinstance(directory_id, str):
            return None
        try:
            _validate_identity(directory_id)
        except LibraryIndexError:
            return None
    normalized_totals: dict[str, int] = {}
    for directory_id, total in directory_totals.items():
        if (
            not isinstance(directory_id, str)
            or not _optional_nonnegative_int(total)
            or total is None
            or directory_id not in visited
        ):
            return None
        try:
            _validate_identity(directory_id)
        except LibraryIndexError:
            return None
        normalized_totals[directory_id] = total
    if sum(normalized_totals.values()) != expected_total:
        return None
    normalized_pending: list[dict[str, object]] = []
    for item in pending:
        if not isinstance(item, dict):
            return None
        directory_id = item.get("directory_id")
        parent_path = item.get("parent_path")
        page = item.get("page")
        page_count = item.get("page_count")
        total = item.get("total")
        items_seen = item.get("items_seen")
        if (
            not isinstance(directory_id, str)
            or not isinstance(parent_path, str)
            or not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not _optional_nonnegative_int(page_count)
            or not _optional_nonnegative_int(total)
            or not isinstance(items_seen, int)
            or isinstance(items_seen, bool)
            or items_seen < 0
            or directory_id not in visited
        ):
            return None
        try:
            _validate_identity(directory_id)
        except LibraryIndexError:
            return None
        try:
            _validate_remote_path(parent_path, allow_empty=True)
        except LibraryIndexError:
            return None
        normalized_pending.append(
            {
                "directory_id": directory_id,
                "parent_path": parent_path,
                "page": page,
                "page_count": page_count,
                "total": total,
                "items_seen": items_seen,
            }
        )
    return {
        "version": 2,
        "directory_totals": normalized_totals,
        "expected_total": expected_total,
        "pending": normalized_pending,
        "visited": list(visited),
    }


def validate_complete_scan_evidence(
    run: LibraryScanRun,
    checkpoint: LibraryScanCheckpoint | None,
    entries: Collection[LibraryScanEntry],
    *,
    root_directory_id: str,
    require_tree: bool = False,
) -> frozenset[str]:
    """Validate the durable evidence before treating a snapshot as complete."""

    _validate_root_scope_id(root_directory_id)
    if checkpoint is None:
        raise LibraryIndexError("checkpoint_missing")
    if run.root_directory_id != root_directory_id:
        raise LibraryIndexError("library_scope_unverified")
    if run.scan_mode == "tree":
        return _validate_tree_cursor_evidence(
            run,
            checkpoint,
            entries,
            root_directory_id=root_directory_id,
            require_complete=True,
        )
    if require_tree or run.scan_mode != "root":
        raise LibraryIndexError("pagination_unverified")
    if checkpoint.cursor_json not in {"", "{}"}:
        raise LibraryIndexError("checkpoint_invalid")
    if (
        not _optional_nonnegative_int(run.expected_total)
        or run.expected_total is None
        or not _optional_nonnegative_int(run.items_seen)
        or not _optional_nonnegative_int(run.pages_read)
        or checkpoint.page != run.pages_read
        or checkpoint.items_seen != run.items_seen
        or len(entries) != run.items_seen
        or run.items_seen != run.expected_total
    ):
        raise LibraryIndexError("pagination_unverified")
    _validate_entry_records(
        entries, allowed_parents={root_directory_id}, require_paths=False
    )
    return frozenset((root_directory_id,))


def validate_tree_cursor_scope(
    run: LibraryScanRun,
    checkpoint: LibraryScanCheckpoint,
    entries: Collection[LibraryScanEntry],
    *,
    root_directory_id: str,
) -> frozenset[str]:
    """Validate the persisted directory allowlist for a resumable tree scan."""

    _validate_root_scope_id(root_directory_id)
    if checkpoint.cursor_json in {"", "{}"}:
        if (
            checkpoint.page != 0
            or checkpoint.items_seen != 0
            or run.pages_read != 0
            or run.items_seen != 0
            or entries
        ):
            raise LibraryIndexError("checkpoint_invalid")
        return frozenset((root_directory_id,))
    if _is_legacy_tree_cursor(checkpoint.cursor_json):
        raise LibraryIndexError("library_scope_unverified")
    return _validate_tree_cursor_evidence(
        run,
        checkpoint,
        entries,
        root_directory_id=root_directory_id,
        require_complete=False,
    )


def _validate_tree_cursor_evidence(
    run: LibraryScanRun,
    checkpoint: LibraryScanCheckpoint,
    entries: Collection[LibraryScanEntry],
    *,
    root_directory_id: str,
    require_complete: bool,
) -> frozenset[str]:
    cursor = _decode_tree_cursor(checkpoint.cursor_json)
    if cursor is None:
        raise LibraryIndexError("checkpoint_invalid")
    visited = cursor["visited"]
    pending = cursor["pending"]
    directory_totals = cursor["directory_totals"]
    expected_total = cursor["expected_total"]
    if (
        not isinstance(visited, list)
        or not isinstance(pending, list)
        or not isinstance(directory_totals, dict)
        or not isinstance(expected_total, int)
        or isinstance(expected_total, bool)
        or root_directory_id not in visited
        or len(set(visited)) != len(visited)
    ):
        raise LibraryIndexError("checkpoint_invalid")
    if (
        not _optional_nonnegative_int(run.items_seen)
        or not _optional_nonnegative_int(run.pages_read)
        or checkpoint.items_seen != run.items_seen
        or checkpoint.page != run.pages_read
        or len(entries) != run.items_seen
        or (
            run.expected_total is not None
            and run.expected_total != expected_total
        )
    ):
        raise LibraryIndexError("checkpoint_invalid")

    pending_by_id: dict[str, dict[str, object]] = {}
    for item in pending:
        if not isinstance(item, dict):
            raise LibraryIndexError("checkpoint_invalid")
        directory_id = item.get("directory_id")
        if not isinstance(directory_id, str) or directory_id in pending_by_id:
            raise LibraryIndexError("checkpoint_invalid")
        pending_by_id[directory_id] = item

    visited_ids = set(visited)
    if any(directory_id not in visited_ids for directory_id in pending_by_id):
        raise LibraryIndexError("library_scope_unverified")
    _validate_entry_records(entries, allowed_parents=visited_ids, require_paths=True)

    directory_rows: dict[str, LibraryScanEntry] = {}
    observed_by_parent: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry.parent_id, str):
            raise LibraryIndexError("library_scope_unverified")
        observed_by_parent[entry.parent_id] = (
            observed_by_parent.get(entry.parent_id, 0) + 1
        )
        if entry.is_directory:
            if entry.object_id == root_directory_id or entry.object_id in directory_rows:
                raise LibraryIndexError("library_scope_unverified")
            directory_rows[entry.object_id] = entry

    child_ids = visited_ids - {root_directory_id}
    if child_ids != set(directory_rows):
        raise LibraryIndexError("library_scope_unverified")
    for directory_id, entry in directory_rows.items():
        if entry.parent_id not in visited_ids:
            raise LibraryIndexError("library_scope_unverified")
        current = directory_id
        chain: set[str] = set()
        while current != root_directory_id:
            if current in chain:
                raise LibraryIndexError("library_scope_unverified")
            chain.add(current)
            parent = directory_rows.get(current)
            if parent is None:
                raise LibraryIndexError("library_scope_unverified")
            if not isinstance(parent.parent_id, str):
                raise LibraryIndexError("library_scope_unverified")
            current = parent.parent_id

    for directory_id in visited_ids:
        item = pending_by_id.get(directory_id)
        total = directory_totals.get(directory_id)
        observed = observed_by_parent.get(directory_id, 0)
        if item is not None:
            items_seen = item.get("items_seen")
            if not isinstance(items_seen, int) or isinstance(items_seen, bool):
                raise LibraryIndexError("checkpoint_invalid")
            if items_seen != observed:
                raise LibraryIndexError("checkpoint_invalid")
            if total is None:
                if item.get("total") is not None:
                    raise LibraryIndexError("checkpoint_invalid")
            elif item.get("total") != total or observed > total:
                raise LibraryIndexError("checkpoint_invalid")
            if directory_id == root_directory_id:
                if item.get("parent_path") != "":
                    raise LibraryIndexError("entry_path_invalid")
            else:
                entry = directory_rows.get(directory_id)
                if entry is None or entry.path != item.get("parent_path"):
                    raise LibraryIndexError("entry_path_invalid")
        elif total is None or observed != total:
            raise LibraryIndexError("checkpoint_invalid")

    if require_complete:
        if pending or set(directory_totals) != visited_ids:
            raise LibraryIndexError("pagination_unverified")
        if expected_total != len(entries) or run.expected_total != expected_total:
            raise LibraryIndexError("total_mismatch")
    return frozenset(visited_ids)


def _validate_root_scope_id(root_directory_id: str) -> None:
    try:
        _validate_identity(root_directory_id)
    except LibraryIndexError:
        raise LibraryIndexError("library_scope_unverified") from None


def _validate_entry_records(
    entries: Collection[LibraryScanEntry],
    *,
    allowed_parents: set[str],
    require_paths: bool,
) -> None:
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        object_type = "directory" if entry.is_directory else "file"
        if entry.object_type != object_type:
            raise LibraryIndexError("entry_scope_unverified")
        _validate_identity(entry.object_id)
        if not isinstance(entry.parent_id, str) or entry.parent_id not in allowed_parents:
            raise LibraryIndexError("entry_out_of_scope")
        if entry.path is None:
            if require_paths:
                raise LibraryIndexError("entry_path_invalid")
        elif not isinstance(entry.path, str) or not entry.path:
            raise LibraryIndexError("entry_path_invalid")
        else:
            _validate_remote_path(entry.path)
        identity = (entry.object_type, entry.object_id)
        if identity in seen:
            raise LibraryIndexError("repeated_entry")
        seen.add(identity)


def _is_legacy_tree_cursor(value: str | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, dict) and decoded.get("version") == 1


def _tree_cursor_expected_total(cursor: dict[str, object]) -> int:
    directory_totals = cursor.get("directory_totals")
    expected_total = cursor.get("expected_total")
    if (
        not isinstance(directory_totals, dict)
        or not _optional_nonnegative_int(expected_total)
        or expected_total is None
        or any(
            not isinstance(directory_id, str)
            or not directory_id
            or not _optional_nonnegative_int(total)
            or total is None
            for directory_id, total in directory_totals.items()
        )
        or sum(directory_totals.values()) != expected_total
    ):
        raise LibraryIndexError("checkpoint_invalid")
    return expected_total


def _tree_cursor_complete(cursor: dict[str, object]) -> bool:
    pending = cursor.get("pending")
    visited = cursor.get("visited")
    directory_totals = cursor.get("directory_totals")
    return (
        isinstance(pending, list)
        and not pending
        and isinstance(visited, list)
        and isinstance(directory_totals, dict)
        and set(visited) == set(directory_totals)
    )


def _encode_tree_cursor(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _optional_nonnegative_int(value: object) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _validate_remote_path(value: str, *, allow_empty: bool = False) -> None:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > 4096
        or "\x00" in value
        or "\\" in value
        or "://" in value
        or any(part == ".." for part in value.split("/"))
    ):
        raise LibraryIndexError("entry_path_invalid")


def _advance_tree_cursor(
    cursor: dict[str, object],
    page: DirectoryPage,
    *,
    directory_id: str,
    parent_path: str,
    page_number: int,
    page_count: int | None,
    total: int | None,
    terminal: bool,
    max_directories: int,
) -> dict[str, object]:
    """Advance a tree cursor only after the current page passed validation."""

    pending = cursor.get("pending")
    visited = cursor.get("visited")
    directory_totals = cursor.get("directory_totals")
    if (
        not isinstance(pending, list)
        or not pending
        or not isinstance(visited, list)
        or not isinstance(directory_totals, dict)
    ):
        raise LibraryIndexError("checkpoint_invalid")
    current_item = pending[0]
    if (
        not isinstance(current_item, dict)
        or current_item.get("directory_id") != directory_id
    ):
        raise LibraryIndexError("checkpoint_invalid")
    current_items_seen = current_item.get("items_seen")
    if (
        not isinstance(current_items_seen, int)
        or isinstance(current_items_seen, bool)
        or current_items_seen < 0
        or total is None
    ):
        raise LibraryIndexError("pagination_unverified")
    previous_total = directory_totals.get(directory_id)
    if previous_total is not None and previous_total != total:
        raise LibraryIndexError("total_changed")
    next_items_seen = current_items_seen + len(page.items)
    if next_items_seen > total or (terminal and next_items_seen != total):
        raise LibraryIndexError("total_mismatch")
    next_cursor = json.loads(_encode_tree_cursor(cursor))
    next_pending = next_cursor["pending"]
    next_visited = next_cursor["visited"]
    next_directory_totals = next_cursor["directory_totals"]
    current = next_pending[0]
    current["page_count"] = page_count
    current["total"] = total
    current["items_seen"] = next_items_seen
    next_directory_totals[directory_id] = total
    next_cursor["expected_total"] = sum(next_directory_totals.values())
    if terminal:
        next_pending.pop(0)
    else:
        current["page"] = page_number + 1
    for entry in page.items:
        if not entry.is_directory or entry.directory_id is None:
            continue
        child_id = entry.directory_id
        _validate_identity(child_id)
        if child_id in next_visited:
            raise LibraryIndexError("directory_cycle")
        if len(next_visited) >= max_directories:
            raise LibraryIndexError("directory_limit_exceeded")
        child_path = entry.path
        if not isinstance(child_path, str) or not child_path:
            raise LibraryIndexError("entry_path_invalid")
        next_visited.append(child_id)
        next_pending.append(
            {
                "directory_id": child_id,
                "parent_path": child_path,
                "page": 1,
                "page_count": None,
                "total": None,
                "items_seen": 0,
            }
        )
    return next_cursor


def _entry_changed(old: LibraryScanEntry, current: LibraryScanEntry) -> bool:
    return any(
        (
            old.parent_id != current.parent_id,
            old.name != current.name,
            old.path != current.path,
            old.is_directory != current.is_directory,
            old.size_bytes != current.size_bytes,
            old.modified_at != current.modified_at,
        )
    )


__all__ = [
    "LibraryIndexError",
    "LibraryIndexService",
    "LibraryScanResult",
    "ScanChange",
    "ScanRunState",
    "validate_complete_scan_evidence",
    "validate_tree_cursor_scope",
]
