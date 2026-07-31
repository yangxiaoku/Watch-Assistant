"""Durable top-level workflow aggregation for cross-module task tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import Workflow, WorkflowStage
from watch_assistant.schemas import (
    MediaType,
    WorkflowApprovalRequest,
    WorkflowCancelRequest,
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowStageName,
    WorkflowStagePatch,
    WorkflowStageResponse,
    WorkflowStageStatus,
    WorkflowStatus,
)
from watch_assistant.services.observability import EventLogger, emit_event

_STAGE_ORDER = tuple(WorkflowStageName)
_TERMINAL_STAGE_STATUSES = {
    WorkflowStageStatus.SUCCEEDED,
    WorkflowStageStatus.SKIPPED,
    WorkflowStageStatus.FAILED,
    WorkflowStageStatus.UNCERTAIN,
    WorkflowStageStatus.CANCELLED,
}
_STATUS_ZH = {
    WorkflowStatus.IN_PROGRESS: "进行中",
    WorkflowStatus.WAITING_USER_CONFIRMATION: "等待用户确认",
    WorkflowStatus.WAITING_EXTERNAL: "等待外部服务",
    WorkflowStatus.PARTIAL: "部分完成",
    WorkflowStatus.COMPLETED: "已完成",
    WorkflowStatus.CANCELLED: "已取消",
    WorkflowStatus.FAILED: "已失败",
    WorkflowStatus.RESULT_PENDING_CONFIRMATION: "结果待确认",
}
_STAGE_STATUS_ZH = {
    WorkflowStageStatus.PENDING: "待开始",
    WorkflowStageStatus.RUNNING: "进行中",
    WorkflowStageStatus.WAITING_CONFIRMATION: "等待确认",
    WorkflowStageStatus.WAITING_EXTERNAL: "等待外部服务",
    WorkflowStageStatus.SUCCEEDED: "已完成",
    WorkflowStageStatus.SKIPPED: "已跳过",
    WorkflowStageStatus.FAILED: "已失败",
    WorkflowStageStatus.UNCERTAIN: "结果待确认",
    WorkflowStageStatus.CANCELLED: "已取消",
}


class WorkflowNotFound(LookupError):
    pass


class WorkflowConflict(ValueError):
    pass


class WorkflowService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def create(self, request: WorkflowCreateRequest) -> WorkflowResponse:
        now = datetime.now(UTC)
        workflow = Workflow(
            id="wf_" + uuid4().hex,
            correlation_id="corr_" + uuid4().hex,
            media_type=request.media_type,
            tmdb_id=request.tmdb_id,
            subscription_id=request.subscription_id,
            status=WorkflowStatus.IN_PROGRESS,
            state_reason="workflow_created",
            created_at=now,
            updated_at=now,
        )
        workflow.stages = [
            WorkflowStage(
                id=f"{workflow.id}_{stage.value}",
                stage=stage,
                stage_key=(
                    "search" if stage == WorkflowStageName.DISCOVERY else stage.value
                ),
                sequence=sequence,
                status=WorkflowStageStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            for sequence, stage in enumerate(_STAGE_ORDER)
        ]
        async with self._session_factory() as session:
            session.add(workflow)
            await session.commit()
        await emit_event(
            self._event_logger,
            "workflow.created",
            fields={"status": workflow.status.value},
            correlation_id=workflow.correlation_id,
            task_id=workflow.id,
        )
        return _response(workflow)

    async def get(self, workflow_id: str) -> WorkflowResponse:
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            await session.refresh(workflow, ["stages"])
            return _response(workflow)

    async def list(
        self,
        *,
        status: WorkflowStatus | None = None,
        media_type: MediaType | None = None,
        subscription_id: str | None = None,
        stage: WorkflowStageName | None = None,
        stage_status: WorkflowStageStatus | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> WorkflowListResponse:
        filters = []
        if status is not None:
            filters.append(Workflow.status == status)
        if media_type is not None:
            filters.append(Workflow.media_type == media_type)
        if subscription_id is not None:
            filters.append(Workflow.subscription_id == subscription_id)
        if stage is not None and stage_status is not None:
            filters.append(
                Workflow.stages.any(
                    and_(
                        WorkflowStage.stage == stage,
                        WorkflowStage.status == stage_status,
                    )
                )
            )
        elif stage is not None:
            filters.append(Workflow.stages.any(WorkflowStage.stage == stage))
        elif stage_status is not None:
            filters.append(Workflow.stages.any(WorkflowStage.status == stage_status))
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Workflow).where(*filters)
            )
            rows = list(
                await session.scalars(
                    select(Workflow)
                    .where(*filters)
                    .order_by(Workflow.updated_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            for workflow in rows:
                await session.refresh(workflow, ["stages"])
        return WorkflowListResponse(
            items=[_response(item) for item in rows],
            page=page,
            page_size=page_size,
            total=int(total or 0),
        )

    async def patch_stage(
        self,
        workflow_id: str,
        stage_name: WorkflowStageName,
        patch: WorkflowStagePatch,
    ) -> WorkflowResponse:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            stage = await session.scalar(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_id == workflow_id,
                    WorkflowStage.stage == stage_name,
                )
            )
            if stage is None:
                raise WorkflowNotFound(f"{workflow_id}:{stage_name.value}")
            stage.status = patch.status
            stage.reason = patch.reason
            stage.error_code = patch.error_code
            stage.child_type = patch.child_type or stage.child_type
            stage.child_id = patch.child_id or stage.child_id
            stage.updated_at = now
            if patch.status == WorkflowStageStatus.RUNNING and stage.started_at is None:
                stage.started_at = now
            if patch.status in _TERMINAL_STAGE_STATUSES:
                stage.completed_at = now
            stages = list(
                await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow_id
                    )
                )
            )
            workflow.status, workflow.state_reason = _derive_status(stages)
            workflow.updated_at = now
            await session.commit()
            await session.refresh(workflow, ["stages"])
            response = _response(workflow)
        await emit_event(
            self._event_logger,
            "workflow.stage_changed",
            fields={"status": patch.status.value, "stage": stage_name.value},
            correlation_id=response.correlation_id,
            task_id=response.id,
        )
        return response

    async def decide_approval(
        self,
        workflow_id: str,
        request: WorkflowApprovalRequest,
        *,
        actor_id: str,
    ) -> WorkflowResponse:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            approval = await session.scalar(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_id == workflow_id,
                    WorkflowStage.stage == WorkflowStageName.APPROVAL,
                )
            )
            if approval is None:
                raise WorkflowNotFound(f"{workflow_id}:approval")
            if approval.status != WorkflowStageStatus.WAITING_CONFIRMATION:
                raise WorkflowConflict("workflow_not_awaiting_confirmation")
            approved = request.decision == "approve"
            approval.status = (
                WorkflowStageStatus.SUCCEEDED
                if approved
                else WorkflowStageStatus.CANCELLED
            )
            approval.reason = request.reason or (
                "approved" if approved else "rejected"
            )
            approval.error_code = None
            approval.updated_at = now
            approval.completed_at = now
            if not approved:
                # A rejection cancels only work that has not started. A running
                # or uncertain child remains visible and cannot be rewritten.
                for stage in await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow_id
                    )
                ):
                    if stage.stage != WorkflowStageName.APPROVAL and stage.status in {
                        WorkflowStageStatus.PENDING,
                        WorkflowStageStatus.WAITING_CONFIRMATION,
                        WorkflowStageStatus.WAITING_EXTERNAL,
                    }:
                        stage.status = WorkflowStageStatus.CANCELLED
                        stage.reason = "approval_rejected"
                        stage.updated_at = now
                        stage.completed_at = now
            stages = list(
                await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow_id
                    )
                )
            )
            workflow.status, workflow.state_reason = _derive_status(stages)
            workflow.updated_at = now
            await session.commit()
            await session.refresh(workflow, ["stages"])
            response = _response(workflow)
        await emit_event(
            self._event_logger,
            "workflow.approval_decided",
            fields={"status": request.decision},
            correlation_id=response.correlation_id,
            task_id=response.id,
            actor_id=actor_id,
        )
        return response

    async def cancel(
        self,
        workflow_id: str,
        request: WorkflowCancelRequest,
        *,
        actor_id: str,
    ) -> WorkflowResponse:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            stages = list(
                await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow_id
                    )
                )
            )
            if workflow.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.FAILED,
            }:
                raise WorkflowConflict("workflow_not_cancellable")
            cancellable = [
                stage
                for stage in stages
                if stage.status
                in {
                    WorkflowStageStatus.PENDING,
                    WorkflowStageStatus.WAITING_CONFIRMATION,
                    WorkflowStageStatus.WAITING_EXTERNAL,
                }
            ]
            if not cancellable:
                raise WorkflowConflict("workflow_not_cancellable")
            reason = request.reason or "workflow_cancelled"
            for stage in cancellable:
                stage.status = WorkflowStageStatus.CANCELLED
                stage.reason = reason
                stage.updated_at = now
                stage.completed_at = now
            workflow.status, workflow.state_reason = _derive_status(stages)
            workflow.updated_at = now
            await session.commit()
            await session.refresh(workflow, ["stages"])
            response = _response(workflow)
        await emit_event(
            self._event_logger,
            "workflow.cancelled",
            fields={"status": response.status.value},
            correlation_id=response.correlation_id,
            task_id=response.id,
            actor_id=actor_id,
        )
        return response


async def link_child(
    session: AsyncSession,
    workflow_id: str,
    stage_name: WorkflowStageName,
    child_type: str,
    child_id: str,
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    stage = await session.scalar(
        select(WorkflowStage).where(
            WorkflowStage.workflow_id == workflow_id,
            WorkflowStage.stage == stage_name,
        )
    )
    if stage is None:
        raise WorkflowNotFound(f"{workflow_id}:{stage_name.value}")
    stage.child_type = child_type
    stage.child_id = child_id
    now = datetime.now(UTC)
    stage.status = WorkflowStageStatus.RUNNING
    stage.started_at = stage.started_at or now
    stage.completed_at = None
    stage.reason = "child_started"
    stage.error_code = None
    stage.updated_at = now
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    workflow.status, workflow.state_reason = _derive_status(stages)
    workflow.updated_at = now
    return workflow


async def sync_child_stage(
    session: AsyncSession,
    workflow_id: str,
    stage_name: WorkflowStageName,
    *,
    child_type: str,
    child_id: str,
    status: WorkflowStageStatus,
    reason: str | None = None,
    error_code: str | None = None,
) -> Workflow:
    """Persist a child task's stage state in the same transaction as its outcome."""
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    stage = await session.scalar(
        select(WorkflowStage).where(
            WorkflowStage.workflow_id == workflow_id,
            WorkflowStage.stage == stage_name,
        )
    )
    if stage is None:
        raise WorkflowNotFound(f"{workflow_id}:{stage_name.value}")
    now = datetime.now(UTC)
    stage.child_type = child_type
    stage.child_id = child_id
    stage.status = status
    stage.reason = reason
    stage.error_code = error_code
    if status == WorkflowStageStatus.RUNNING and stage.started_at is None:
        stage.started_at = now
    if status in _TERMINAL_STAGE_STATUSES:
        stage.completed_at = now
    stage.updated_at = now
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    workflow.status, workflow.state_reason = _derive_status(stages)
    workflow.updated_at = now
    return workflow


async def sync_child_stage_in_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_id: str,
    stage_name: WorkflowStageName,
    *,
    child_type: str,
    child_id: str,
    status: WorkflowStageStatus,
    reason: str | None = None,
    error_code: str | None = None,
    event_logger: EventLogger | None = None,
) -> None:
    """Update a child stage for workers that do not own an open transaction."""
    async with session_factory() as session:
        workflow = await sync_child_stage(
            session,
            workflow_id,
            stage_name,
            child_type=child_type,
            child_id=child_id,
            status=status,
            reason=reason,
            error_code=error_code,
        )
        await session.commit()
    await emit_event(
        event_logger,
        "workflow.stage_changed",
        fields={"status": status.value, "stage": stage_name.value},
        correlation_id=workflow.correlation_id,
        task_id=workflow.id,
    )


def _derive_status(
    stages: list[WorkflowStage],
) -> tuple[WorkflowStatus, str | None]:
    statuses = {stage.status for stage in stages}
    if WorkflowStageStatus.UNCERTAIN in statuses:
        return WorkflowStatus.RESULT_PENDING_CONFIRMATION, "stage_uncertain"
    if WorkflowStageStatus.WAITING_CONFIRMATION in statuses:
        return WorkflowStatus.WAITING_USER_CONFIRMATION, "waiting_confirmation"
    if WorkflowStageStatus.WAITING_EXTERNAL in statuses:
        return WorkflowStatus.WAITING_EXTERNAL, "waiting_external"
    if WorkflowStageStatus.RUNNING in statuses or WorkflowStageStatus.PENDING in statuses:
        if WorkflowStageStatus.FAILED in statuses:
            return WorkflowStatus.PARTIAL, "stage_failed_with_pending_work"
        return WorkflowStatus.IN_PROGRESS, "stage_in_progress"
    if WorkflowStageStatus.FAILED in statuses:
        if WorkflowStageStatus.SUCCEEDED in statuses:
            return WorkflowStatus.PARTIAL, "stage_failed"
        return WorkflowStatus.FAILED, "stage_failed"
    if statuses and statuses <= {WorkflowStageStatus.CANCELLED}:
        return WorkflowStatus.CANCELLED, "workflow_cancelled"
    if WorkflowStageStatus.CANCELLED in statuses:
        return WorkflowStatus.PARTIAL, "workflow_cancelled"
    return WorkflowStatus.COMPLETED, "all_stages_terminal"


def _response(workflow: Workflow) -> WorkflowResponse:
    stages = sorted(workflow.stages, key=lambda item: _STAGE_ORDER.index(item.stage))
    return WorkflowResponse(
        id=workflow.id,
        correlation_id=workflow.correlation_id,
        media_type=workflow.media_type,
        tmdb_id=workflow.tmdb_id,
        subscription_id=workflow.subscription_id,
        status=workflow.status,
        status_zh=_STATUS_ZH[workflow.status],
        state_reason=workflow.state_reason,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        stages=[
            WorkflowStageResponse(
                id=stage.id,
                stage=stage.stage,
                sequence=stage.sequence,
                status=stage.status,
                status_zh=_STAGE_STATUS_ZH[stage.status],
                reason=stage.reason,
                error_code=stage.error_code,
                child_type=stage.child_type,
                child_id=stage.child_id,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
                updated_at=stage.updated_at,
            )
            for stage in stages
        ],
    )
