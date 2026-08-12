"""Lease fencing for STRM manifest operations.

Split from strm_manifest (phase D): bind a callback lease to a durable
conditional database write (fail-closed rowcount fence), without any
filesystem or manifest-generation logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import StrmOperation, StrmOperationKind, StrmOperationStatus
from watch_assistant.services.strm_errors import StrmManifestError
from watch_assistant.services.strm_path import valid_id as _valid_id
from watch_assistant.services.strm_scope import (
    active_strm_operation_id,
    source_snapshot_is_current,
)

CancelCheck = Callable[[], Awaitable[bool]]
LeaseCheck = Callable[[], Awaitable[bool]]
SessionFence = Callable[[AsyncSession], Awaitable[bool]]

class _LeaseFence:
    """Bind a callback lease to a durable conditional database write."""

    def __init__(
        self,
        operation_id: str | None,
        lease_check: LeaseCheck | None,
        *,
        library_id: str | None = None,
        source_scan_run_id: str | None = None,
        operation_kind: StrmOperationKind | str | None = None,
        durable_fence: SessionFence | None = None,
        source_snapshot_revision: int | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.lease_check = lease_check
        self.library_id = library_id
        self.source_scan_run_id = source_scan_run_id
        self.operation_kind = (
            StrmOperationKind(operation_kind)
            if operation_kind is not None
            else None
        )
        self.durable_fence = durable_fence
        self.source_snapshot_revision = source_snapshot_revision
        self._lease_owner: str | None = None
        self._database_lease = False

    async def bind(self, session: AsyncSession) -> None:
        await _raise_if_lease_lost(self.lease_check)
        if self.operation_id is not None:
            operation = await session.get(StrmOperation, self.operation_id)
            if (
                operation is None
                or not _operation_lease_is_current(operation)
                or not self._operation_matches_scope(operation)
            ):
                raise StrmManifestError("strm_operation_lease_lost")
            self._lease_owner = operation.lease_owner
            self._database_lease = True
        await _raise_if_lease_lost(self.lease_check)

    async def assert_current(self, session: AsyncSession) -> None:
        await _raise_if_lease_lost(self.lease_check)
        if (
            self.source_snapshot_revision is not None
            and self.library_id is not None
            and self.source_scan_run_id is not None
            and not await source_snapshot_is_current(
                session,
                library_id=self.library_id,
                source_scan_run_id=self.source_scan_run_id,
                source_snapshot_revision=self.source_snapshot_revision,
            )
        ):
            raise StrmManifestError("source_snapshot_not_current")
        if self.durable_fence is not None and not await self.durable_fence(session):
            raise StrmManifestError("strm_operation_lease_lost")
        if not self._database_lease or self.operation_id is None:
            return
        operation = await session.scalar(
            select(StrmOperation)
            .where(StrmOperation.id == self.operation_id)
            .execution_options(populate_existing=True)
        )
        if (
            operation is None
            or not _operation_lease_is_current(
                operation, expected_owner=self._lease_owner
            )
            or not self._operation_matches_scope(operation)
        ):
            raise StrmManifestError("strm_operation_lease_lost")

    async def fence_commit(
        self,
        session: AsyncSession,
        *,
        check_source_snapshot: bool = True,
    ) -> None:
        """Acquire the lease row's write lock immediately before commit.

        The conditional update and the manifest transaction commit are one
        database transaction.  A competing lease takeover therefore cannot
        commit between this check and the manifest commit.
        """

        await _raise_if_lease_lost(self.lease_check)
        if self.durable_fence is not None and not await self.durable_fence(session):
            raise StrmManifestError("strm_operation_lease_lost")
        if not self._database_lease or self.operation_id is None:
            if self.source_snapshot_revision is not None:
                # Force pending manifest/plan changes into this transaction so
                # the source check and the terminal commit have one ordering.
                await session.flush()
            if (
                check_source_snapshot
                and self.source_snapshot_revision is not None
                and self.library_id is not None
                and self.source_scan_run_id is not None
                and not await source_snapshot_is_current(
                    session,
                    library_id=self.library_id,
                    source_scan_run_id=self.source_scan_run_id,
                    source_snapshot_revision=self.source_snapshot_revision,
                )
            ):
                raise StrmManifestError("source_snapshot_not_current")
            return
        result = await session.execute(
            update(StrmOperation)
            .where(
                StrmOperation.id == self.operation_id,
                StrmOperation.status == StrmOperationStatus.RUNNING,
                StrmOperation.lease_owner == self._lease_owner,
                StrmOperation.lease_expires_at.is_not(None),
                StrmOperation.lease_expires_at > datetime.now(UTC),
                *self._scope_predicates(),
            )
            # A no-op UPDATE still takes the database row/write lock without
            # changing the externally visible lease value.
            .values(heartbeat_at=StrmOperation.heartbeat_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise StrmManifestError("strm_operation_lease_lost")
        if (
            check_source_snapshot
            and self.source_snapshot_revision is not None
            and self.library_id is not None
            and self.source_scan_run_id is not None
            and not await source_snapshot_is_current(
                session,
                library_id=self.library_id,
                source_scan_run_id=self.source_scan_run_id,
                source_snapshot_revision=self.source_snapshot_revision,
            )
        ):
            raise StrmManifestError("source_snapshot_not_current")

    async def observe_database_lease(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> bool | None:
        if not self._database_lease or self.operation_id is None:
            return True
        try:
            async with session_factory() as session:
                operation = await session.get(StrmOperation, self.operation_id)
                return bool(
                    operation is not None
                    and _operation_lease_is_current(
                        operation, expected_owner=self._lease_owner
                    )
                    and self._operation_matches_scope(operation)
                )
        except SQLAlchemyError:
            return None

    def _scope_predicates(self) -> tuple[object, ...]:
        predicates: list[object] = []
        if self.library_id is not None:
            predicates.append(StrmOperation.library_id == self.library_id)
        if self.source_scan_run_id is not None:
            predicates.append(
                StrmOperation.source_scan_run_id == self.source_scan_run_id
            )
        if self.operation_kind is not None:
            predicates.append(StrmOperation.kind == self.operation_kind)
        return tuple(predicates)

    def _operation_matches_scope(self, operation: StrmOperation) -> bool:
        return bool(
            (self.library_id is None or operation.library_id == self.library_id)
            and (
                self.source_scan_run_id is None
                or operation.source_scan_run_id == self.source_scan_run_id
            )
            and (
                self.operation_kind is None or operation.kind is self.operation_kind
            )
        )

async def _commit_fenced(
    session: AsyncSession,
    fence: _LeaseFence,
    *,
    check_source_snapshot: bool = True,
) -> None:
    await fence.fence_commit(
        session, check_source_snapshot=check_source_snapshot
    )
    await session.commit()

async def _raise_if_cancelled(
    cancel_check: CancelCheck | None,
    lease_check: LeaseCheck | None,
) -> None:
    await _raise_if_lease_lost(lease_check)
    if cancel_check is not None and await cancel_check():
        raise StrmManifestError("strm_operation_cancelled")

def _operation_lease_is_current(
    operation: StrmOperation,
    *,
    expected_owner: str | None = None,
) -> bool:
    if (
        operation.status is not StrmOperationStatus.RUNNING
        or operation.lease_owner is None
        or operation.lease_expires_at is None
    ):
        return False
    if expected_owner is not None and operation.lease_owner != expected_owner:
        return False
    expires_at = operation.lease_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)

async def _raise_if_lease_lost(lease_check: LeaseCheck | None) -> None:
    if lease_check is not None and not await lease_check():
        raise StrmManifestError("strm_operation_lease_lost")

async def _raise_if_conflicting_operation(
    session: AsyncSession,
    library_id: str,
    *,
    operation_id: str | None,
) -> None:
    if operation_id is not None and not _valid_id(operation_id):
        raise StrmManifestError("invalid_request")
    active_id = await active_strm_operation_id(
        session, library_id, exclude_operation_id=operation_id
    )
    if active_id is not None:
        raise StrmManifestError("strm_library_operation_conflict")

def _is_lease_error(error: StrmManifestError) -> bool:
    return str(error) == "strm_operation_lease_lost"

def _validate_fencing(
    operation_id: str | None,
    lease_check: LeaseCheck | None,
) -> None:
    if operation_id is not None and lease_check is None:
        raise StrmManifestError("strm_operation_lease_required")
