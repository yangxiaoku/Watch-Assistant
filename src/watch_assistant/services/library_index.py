"""Resumable, read-only persistence for a managed library directory."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import delete, func, select
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
        self._session_factory = session_factory
        self._gateway = gateway
        self._library_id = library_id
        self._root_directory_id = root_directory_id
        self._page_size = page_size
        self._max_reported_changes = max_reported_changes

    async def scan(self, idempotency_key: str) -> LibraryScanResult:
        _validate_idempotency_key(idempotency_key)
        await self._verify_scope()
        run = await self._get_or_create_run(idempotency_key)
        if run.complete and run.state == ScanRunState.COMPLETED.value:
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
        run = await self._get_or_create_run(idempotency_key)
        if run.complete and run.state == ScanRunState.COMPLETED.value:
            return await self._result_for_run(run.id)
        await self._reset_tree_run(run.id)
        try:
            await self._mark_running(run.id)
            pending = [self._root_directory_id]
            visited = {self._root_directory_id}
            pages_read = 0
            while pending:
                directory_id = pending.pop(0)
                page_number = 1
                expected_page_count: int | None = None
                expected_total: int | None = None
                while True:
                    try:
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
                        expected_page_count, expected_total, terminal = _validate_page(
                            page,
                            requested_page=page_number,
                            root_directory_id=directory_id,
                            expected_page_count=expected_page_count,
                            expected_total=expected_total,
                        )
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
                    for entry in page.items:
                        if not entry.is_directory or entry.directory_id is None:
                            continue
                        child_id = entry.directory_id
                        if child_id in visited:
                            await self._finish_incomplete(
                                run.id, ScanRunState.FAILED, "directory_cycle"
                            )
                            return await self._result_for_run(run.id)
                        if len(visited) >= max_directories:
                            await self._finish_incomplete(
                                run.id, ScanRunState.FAILED, "directory_limit_exceeded"
                            )
                            return await self._result_for_run(run.id)
                        visited.add(child_id)
                        pending.append(child_id)
                    if terminal:
                        break
                    page_number += 1
            return await self._complete_run(run.id)
        except asyncio.CancelledError:
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
            await session.execute(
                delete(LibraryScanEntry).where(LibraryScanEntry.scan_run_id == run_id)
            )
            await session.execute(
                delete(LibraryScanDiff).where(LibraryScanDiff.scan_run_id == run_id)
            )
            run.state = ScanRunState.QUEUED.value
            run.error_code = None
            run.pages_read = 0
            run.items_seen = 0
            run.expected_page_count = None
            run.expected_total = None
            checkpoint.page = 0
            checkpoint.items_seen = 0
            await session.commit()

    async def _persist_tree_page(
        self, run_id: str, page: DirectoryPage, *, pages_read: int
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(LibraryScanRun, run_id)
            checkpoint = await session.get(LibraryScanCheckpoint, run_id)
            if run is None or checkpoint is None:
                raise LibraryIndexError("scan_run_missing")
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

    async def _get_or_create_run(self, idempotency_key: str) -> LibraryScanRun:
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
                return run
            run = LibraryScanRun(
                id=uuid.uuid4().hex,
                library_id=self._library_id,
                root_directory_id=self._root_directory_id,
                idempotency_key=idempotency_key,
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
                return existing
            return run

    async def _mark_running(self, run_id: str) -> None:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            run.state = ScanRunState.RUNNING.value
            run.error_code = None
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
            checkpoint.page = page.page
            checkpoint.items_seen = run.items_seen

    async def _finish_incomplete(
        self, run_id: str, state: ScanRunState, error_code: str
    ) -> None:
        async with self._session_factory() as session:
            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise LibraryIndexError("scan_run_missing")
            run.state = state.value
            run.complete = False
            run.error_code = error_code
            await session.commit()

    async def _complete_run(self, run_id: str) -> LibraryScanResult:
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(LibraryScanRun, run_id)
                if run is None:
                    raise LibraryIndexError("scan_run_missing")
                if (
                    run.expected_total is not None
                    and run.items_seen != run.expected_total
                ):
                    run.state = ScanRunState.FAILED.value
                    run.complete = False
                    run.error_code = "total_mismatch"
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
                    entries = list(
                        (
                            await session.scalars(
                                select(LibraryScanEntry).where(
                                    LibraryScanEntry.scan_run_id == run_id
                                )
                            )
                        ).all()
                    )
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
                    run.state = ScanRunState.COMPLETED.value
                    run.complete = True
                    run.error_code = None
                    run.added_count = added_count
                    run.changed_count = changed_count
                    run.removed_count = removed_count
            await session.commit()
        return await self._result_for_run(run_id)

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
    if entry.is_directory:
        if entry.directory_id is None:
            raise LibraryIndexError("missing_directory_id")
        return "directory", entry.directory_id
    if entry.file_id is None:
        raise LibraryIndexError("missing_file_id")
    return "file", entry.file_id


def _validate_page(
    page: DirectoryPage,
    *,
    requested_page: int,
    root_directory_id: str,
    expected_page_count: int | None,
    expected_total: int | None,
) -> tuple[int | None, int | None, bool]:
    if page.page != requested_page or page.page < 1:
        raise LibraryIndexError("repeated_page")
    if page.page_count is not None:
        if page.page_count < 1 or page.page_count < requested_page:
            raise LibraryIndexError("pagination_unverified")
        if expected_page_count is not None and page.page_count != expected_page_count:
            raise LibraryIndexError("page_count_changed")
        expected_page_count = page.page_count
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
        _entry_identity(entry)
    terminal = terminal_by_count or page.terminal is True or page.has_more is False
    if terminal and expected_total is not None and expected_total < 0:
        raise LibraryIndexError("invalid_total")
    if not terminal and not (
        continues_by_count or page.has_more is True or page.next_page is not None
    ):
        raise LibraryIndexError("pagination_unverified")
    return expected_page_count, expected_total, terminal


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
]
