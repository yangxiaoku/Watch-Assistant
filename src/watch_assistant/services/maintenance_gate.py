"""Persistent maintenance gate shared by task and background workers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import (
    BackupRestoreApproval,
    DirectoryDirtyEvent,
    DirectoryDirtyGeneration,
    InspectionBatch,
    OnlineMaintenanceState,
    OrganizationOperation,
    OrganizationOperationStatus,
    Task,
)
from watch_assistant.schemas import (
    BackupRestoreApprovalResponse,
    MaintenanceStatusResponse,
)

MAINTENANCE_ID = "default"
APPROVAL_TTL_SECONDS = 300


class MaintenanceActive(RuntimeError):
    pass


class MaintenanceNotActive(RuntimeError):
    pass


class MaintenanceDrainIncomplete(RuntimeError):
    def __init__(self, status: MaintenanceStatusResponse) -> None:
        self.status = status
        super().__init__("restore_drain_incomplete")


class MaintenanceGate:
    """Single-process lock plus durable state for fail-closed maintenance."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger=None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger
        self.lock = asyncio.Lock()

    async def enter(self, *, reason: str, actor_id: str | None = None) -> MaintenanceStatusResponse:
        now = datetime.now(UTC)
        async with self.lock, self._session_factory() as session:
            state = await self._get_or_create(session)
            if not state.active:
                state.active = True
                state.generation += 1
                state.reason = reason
                state.requested_at = now
                state.entered_at = now
                state.released_at = None
                state.requested_by = actor_id
                await session.commit()
        return await self.status()

    async def leave(self, *, actor_id: str | None = None) -> MaintenanceStatusResponse:
        del actor_id
        async with self.lock, self._session_factory() as session:
            state = await self._get_or_create(session)
            state.active = False
            state.released_at = datetime.now(UTC)
            await session.commit()
        return await self.status()

    async def assert_writable(self) -> None:
        async with self._session_factory() as session:
            state = await session.get(OnlineMaintenanceState, MAINTENANCE_ID)
            if state is not None and state.active:
                raise MaintenanceActive("maintenance_mode_active")

    async def is_active(self) -> bool:
        async with self._session_factory() as session:
            state = await session.get(OnlineMaintenanceState, MAINTENANCE_ID)
            return bool(state is not None and state.active)

    async def status(self) -> MaintenanceStatusResponse:
        async with self._session_factory() as session:
            state = await self._get_or_create(session, commit=True)
            task_rows = await session.execute(
                select(Task.state, func.count(Task.id)).group_by(Task.state)
            )
            tasks = {str(status.value): int(count) for status, count in task_rows}
            queue_counts = {
                "inspection": int(
                    await session.scalar(
                        select(func.count(InspectionBatch.id)).where(
                            InspectionBatch.status.in_(("queued", "running"))
                        )
                    )
                    or 0
                ),
                "organization": int(
                    await session.scalar(
                        select(func.count(OrganizationOperation.id)).where(
                            OrganizationOperation.status.in_(
                                (
                                    OrganizationOperationStatus.PLANNED,
                                    OrganizationOperationStatus.ORGANIZING,
                                )
                            )
                        )
                    )
                    or 0
                ),
                "strm": int(
                    await session.scalar(
                        select(func.count(DirectoryDirtyEvent.id)).where(
                            DirectoryDirtyEvent.status.in_(("pending", "running"))
                        )
                    )
                    or 0
                )
                + int(
                    await session.scalar(
                        select(func.count(DirectoryDirtyGeneration.id)).where(
                            DirectoryDirtyGeneration.status.in_(("queued", "running"))
                        )
                    )
                    or 0
                ),
            }
        blocking: list[str] = []
        if any(tasks.get(status, 0) for status in ("queued", "submitting", "needs_auth", "uncertain")):
            blocking.append("tasks_not_terminal")
        if queue_counts["inspection"]:
            blocking.append("inspection_not_terminal")
        if queue_counts["organization"]:
            blocking.append("organization_not_terminal")
        if queue_counts["strm"]:
            blocking.append("strm_not_terminal")
        return MaintenanceStatusResponse(
            active=bool(state.active),
            generation=state.generation,
            reason=state.reason,
            entered_at=state.entered_at,
            released_at=state.released_at,
            task_counts=tasks,
            queue_counts=queue_counts,
            safe_point=not blocking,
            blocking_reasons=blocking,
        )

    async def require_safe_point(self) -> MaintenanceStatusResponse:
        status = await self.status()
        if not status.active:
            raise MaintenanceNotActive("maintenance_mode_required")
        if not status.safe_point:
            raise MaintenanceDrainIncomplete(status)
        return status

    async def _get_or_create(
        self, session: AsyncSession, *, commit: bool = False
    ) -> OnlineMaintenanceState:
        state = await session.get(OnlineMaintenanceState, MAINTENANCE_ID)
        if state is None:
            state = OnlineMaintenanceState(id=MAINTENANCE_ID)
            session.add(state)
            await session.flush()
            if commit:
                await session.commit()
        return state


def approval_response(
    approval: BackupRestoreApproval,
    status: MaintenanceStatusResponse,
) -> BackupRestoreApprovalResponse:
    return BackupRestoreApprovalResponse(
        approval_id=approval.id,
        backup_id=approval.backup_id,
        status=approval.status,
        created_at=approval.created_at,
        expires_at=approval.expires_at,
        requester_identity=approval.requester_identity,
        approver_identity=approval.approver_identity,
        maintenance_generation=approval.maintenance_generation,
        safe_point=status.safe_point,
        drain_report=status,
    )


def encode_drain_report(status: MaintenanceStatusResponse) -> str:
    return json.dumps(status.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def new_approval_id() -> str:
    return "restore_" + uuid4().hex
