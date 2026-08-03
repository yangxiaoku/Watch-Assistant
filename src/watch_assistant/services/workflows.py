"""Durable top-level workflow aggregation for cross-module task tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import (
    DirectoryDirtyGeneration,
    InspectionBatch,
    OrganizationOperation,
    Resource,
    StrmOperation,
    Task,
    TaskState,
    Workflow,
    WorkflowEvidence,
    WorkflowStage,
)
from watch_assistant.schemas import (
    EvidenceSource,
    EvidenceStatus,
    MediaType,
    WorkflowApprovalRequest,
    WorkflowCancelRequest,
    WorkflowCreateRequest,
    WorkflowDiscoveryRequest,
    WorkflowEvidenceListResponse,
    WorkflowEvidenceResponse,
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
_TERMINAL_PREREQUISITE_STATUSES = {
    WorkflowStageStatus.SUCCEEDED,
    WorkflowStageStatus.SKIPPED,
}
_STALE_STAGE_AFTER = timedelta(hours=24)
_REASON_ZH = {
    "workflow_created": "工作流已创建，等待第一个阶段开始。",
    "stage_in_progress": "有阶段正在处理，后续阶段会在前置完成后开始。",
    "stage_failed_with_pending_work": "前置阶段失败，仍有阶段尚未完成。",
    "stage_failed": "工作流阶段处理失败。",
    "stage_uncertain": "远端结果暂时无法确认，需要先核对后再继续。",
    "waiting_confirmation": "工作流等待人工确认。",
    "waiting_external": "工作流等待外部服务返回结果。",
    "all_stages_terminal": "所有阶段均已结束。",
    "workflow_cancelled": "工作流已取消。",
    "child_started": "关联任务已开始。",
    "organization_queued": "整理操作已排队，等待后台执行。",
    "organization_started": "整理操作正在执行。",
    "organization_finished": "整理操作已结束。",
    "organization_uncertain": "整理结果暂时无法确认，请先核对 115。",
    "organization_retried": "整理操作已重新排队。",
    "organization_cancelled": "整理操作已取消。",
    "task_retry": "推送任务已重新开始。",
    "task_cancelled": "推送任务已取消。",
    "task_accepted": "115 已受理，等待文件可用证据。",
    "task_submitted": "115 已受理，等待文件可用证据。",
    "task_downloading": "115 正在下载，等待文件可用证据。",
    "task_available": "已取得只读文件可用证据。",
    "task_uncertain": "推送结果暂时无法确认，请先核对远端状态。",
    "task_failed": "推送任务处理失败。",
    "discovery_evidence": "已根据资源记录完成发现阶段。",
    "availability_evidence": "已根据只读文件可用证据完成确认。",
    "stage_timeout": "阶段长时间没有更新，已暂停并等待核对。",
    "workflow_prerequisite_not_met": "前置阶段尚未完成，当前阶段不能开始。",
}
_ERROR_ZH = {
    "workflow_prerequisite_not_met": "前置阶段尚未完成",
    "workflow_stage_regression": "阶段状态不能回退",
    "workflow_stage_terminal": "已结束阶段不能改写",
    "workflow_evidence_required": "该阶段必须由受信生产者提交证据",
    "workflow_discovery_conflict": "发现证据与当前工作流资源不一致",
    "stage_timeout": "阶段超过允许等待时间",
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
            if request.resource_id is not None:
                resource = await session.get(Resource, request.resource_id)
                if resource is None:
                    raise WorkflowConflict("resource_not_found")
                await record_discovery_evidence(
                    session,
                    workflow,
                    request.resource_id,
                )
            await session.commit()
            await session.refresh(workflow, ["stages"])
        await emit_event(
            self._event_logger,
            "workflow.created",
            fields={"status": workflow.status.value},
            correlation_id=workflow.correlation_id,
            task_id=workflow.id,
        )
        if request.resource_id is not None:
            await emit_event(
                self._event_logger,
                "workflow.discovery_verified",
                fields={
                    "status": "succeeded",
                    "stage": WorkflowStageName.DISCOVERY.value,
                },
                correlation_id=workflow.correlation_id,
                task_id=workflow.id,
            )
        return _response(workflow)

    async def record_discovery(
        self,
        workflow_id: str,
        request: WorkflowDiscoveryRequest,
    ) -> WorkflowResponse:
        """Advance discovery only from a persisted resource record."""

        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            resource = await session.get(Resource, request.resource_id)
            if resource is None:
                raise WorkflowConflict("resource_not_found")
            await record_discovery_evidence(session, workflow, request.resource_id)
            await session.commit()
            await session.refresh(workflow, ["stages"])
            response = _response(workflow)
        await emit_event(
            self._event_logger,
            "workflow.discovery_verified",
            fields={"status": "succeeded", "stage": WorkflowStageName.DISCOVERY.value},
            correlation_id=response.correlation_id,
            task_id=response.id,
        )
        return response

    async def list_evidence(
        self, workflow_id: str
    ) -> WorkflowEvidenceListResponse:
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFound(workflow_id)
            rows = list(
                await session.scalars(
                    select(WorkflowEvidence)
                    .where(WorkflowEvidence.workflow_id == workflow_id)
                    .order_by(WorkflowEvidence.observed_at, WorkflowEvidence.id)
                )
            )
        return WorkflowEvidenceListResponse(items=[_evidence_response(row) for row in rows])

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
            if stage_name in {
                WorkflowStageName.DISCOVERY,
                WorkflowStageName.AVAILABILITY,
            } and patch.status is not WorkflowStageStatus.PENDING:
                raise WorkflowConflict("workflow_evidence_required")
            replayed_response: WorkflowResponse | None = None
            original_stage_status = stage.status
            original_stage_updated_at = stage.updated_at
            stages = list(
                await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_id == workflow_id
                    )
                )
            )
            _validate_stage_transition(stages, stage, patch.status)
            child_patch_supplied = (
                patch.child_type is not None or patch.child_id is not None
            )
            child_bound = False
            if child_patch_supplied:
                requested_child_type = patch.child_type or stage.child_type
                requested_child_id = patch.child_id or stage.child_id
                if requested_child_type is None or requested_child_id is None:
                    raise WorkflowConflict("workflow_conflict")
                await _validate_child_ownership(
                    session,
                    workflow_id,
                    stage_name,
                    child_type=requested_child_type,
                    child_id=requested_child_id,
                )
                before_binding = (stage.child_type, stage.child_id)
                if before_binding != (None, None) and before_binding != (
                    requested_child_type,
                    requested_child_id,
                ):
                    raise WorkflowConflict("workflow_conflict")
                child_bound = before_binding != (
                    requested_child_type,
                    requested_child_id,
                )
            terminal_replay = (
                patch.status in _TERMINAL_STAGE_STATUSES
                and stage.status is patch.status
            )
            stage_values = {}
            if child_patch_supplied and child_bound:
                stage_values.update(
                    child_type=requested_child_type,
                    child_id=requested_child_id,
                )
            if not terminal_replay:
                stage_values.update(
                    status=patch.status,
                    reason=patch.reason,
                    error_code=patch.error_code,
                    updated_at=now,
                )
                if patch.status in {
                    WorkflowStageStatus.RUNNING,
                    WorkflowStageStatus.WAITING_EXTERNAL,
                    WorkflowStageStatus.WAITING_CONFIRMATION,
                } and stage.started_at is None:
                    stage_values["started_at"] = now
                if patch.status in _TERMINAL_STAGE_STATUSES:
                    stage_values["completed_at"] = now
            elif child_bound:
                stage_values["updated_at"] = now
            if stage_values:
                result = await session.execute(
                    update(WorkflowStage)
                    .where(
                        WorkflowStage.id == stage.id,
                        WorkflowStage.status == original_stage_status,
                        WorkflowStage.updated_at == original_stage_updated_at,
                    )
                    .values(**stage_values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    current_stage = await session.scalar(
                        select(WorkflowStage)
                        .where(WorkflowStage.id == stage.id)
                        .execution_options(populate_existing=True)
                    )
                    if current_stage is None:
                        raise WorkflowNotFound(f"{workflow_id}:{stage_name.value}")
                    if (
                        current_stage.status is patch.status
                        and patch.status in _TERMINAL_STAGE_STATUSES
                        and (
                            not child_patch_supplied
                            or (
                                current_stage.child_type == requested_child_type
                                and current_stage.child_id == requested_child_id
                            )
                        )
                    ):
                        workflow = await session.get(Workflow, workflow_id)
                        if workflow is None:
                            raise WorkflowNotFound(workflow_id)
                        await session.refresh(
                            workflow,
                            ["status", "state_reason", "updated_at", "stages"],
                        )
                        replayed_response = _response(workflow)
                    else:
                        if (
                            current_stage.status in _TERMINAL_STAGE_STATUSES
                            and current_stage.status is patch.status
                            and patch.status in _TERMINAL_STAGE_STATUSES
                            and child_patch_supplied
                        ):
                            raise WorkflowConflict("workflow_conflict")
                        if current_stage.status in _TERMINAL_STAGE_STATUSES:
                            raise WorkflowConflict("workflow_stage_terminal")
                        raise WorkflowConflict("workflow_conflict")
                else:
                    await session.refresh(stage)
            if replayed_response is None:
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
            else:
                response = replayed_response
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

    async def recover_stale(self, *, now: datetime | None = None) -> int:
        """Stop orphaned stages after restart without inventing a remote result."""

        current_time = now or datetime.now(UTC)
        cutoff = current_time - _STALE_STAGE_AFTER
        recovered = 0
        async with self._session_factory() as session:
            stages = list(
                await session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.status == WorkflowStageStatus.RUNNING,
                        WorkflowStage.updated_at < cutoff,
                    )
                )
            )
            for stage in stages:
                stage.status = WorkflowStageStatus.UNCERTAIN
                stage.reason = "stage_timeout"
                stage.error_code = "stage_timeout"
                stage.completed_at = current_time
                stage.updated_at = current_time
                workflow = await session.get(Workflow, stage.workflow_id)
                if workflow is not None:
                    all_stages = list(
                        await session.scalars(
                            select(WorkflowStage).where(
                                WorkflowStage.workflow_id == workflow.id
                            )
                        )
                    )
                    workflow.status, workflow.state_reason = _derive_status(all_stages)
                    workflow.updated_at = current_time
                recovered += 1
            if recovered:
                await session.commit()
        return recovered


async def record_evidence(
    session: AsyncSession,
    *,
    workflow_id: str | None,
    task_id: str | None,
    stage: WorkflowStageName | None,
    evidence_type: str,
    source: EvidenceSource,
    subject_id: str,
    status: EvidenceStatus,
    verified: bool,
    observed_at: datetime | None = None,
) -> WorkflowEvidence:
    """Atomically insert or refresh one safe, idempotent evidence record."""

    workflow = None
    workflow_stage = None
    if workflow_id is not None:
        workflow = await session.get(Workflow, workflow_id)
        if workflow is None:
            raise WorkflowNotFound(workflow_id)
        if stage is None:
            raise WorkflowConflict("workflow_evidence_required")
        workflow_stage = await session.scalar(
            select(WorkflowStage).where(
                WorkflowStage.workflow_id == workflow_id,
                WorkflowStage.stage == stage,
            )
        )
        if workflow_stage is None:
            raise WorkflowConflict("workflow_evidence_required")

    task = None
    if task_id is not None:
        task = await session.get(Task, task_id)
        if task is None:
            raise WorkflowConflict("workflow_evidence_required")
        if task.workflow_id != workflow_id or subject_id != task_id:
            raise WorkflowConflict("workflow_evidence_required")
    elif workflow_id is None:
        raise WorkflowConflict("workflow_evidence_required")

    if source is EvidenceSource.RESOURCE_RECORD:
        resource = await session.get(Resource, subject_id)
        valid_discovery = (
            workflow is not None
            and workflow_stage is not None
            and task is None
            and resource is not None
            and stage is WorkflowStageName.DISCOVERY
            and evidence_type == "discovery"
            and status is EvidenceStatus.DISCOVERED
            and workflow_stage.child_type == "resource"
            and workflow_stage.child_id == subject_id
        )
        if not valid_discovery:
            raise WorkflowConflict("workflow_evidence_required")
    elif task is not None:
        valid_task_stage = stage in {
            WorkflowStageName.PUSH,
            WorkflowStageName.AVAILABILITY,
        }
        valid_type = (
            evidence_type == "availability_receipt"
            if stage is WorkflowStageName.AVAILABILITY
            else evidence_type == "remote_status"
        )
        valid_push_status = (
            status
            in {
                EvidenceStatus.SUBMITTED,
                EvidenceStatus.DOWNLOADING,
                EvidenceStatus.FAILED,
                EvidenceStatus.UNCERTAIN,
            }
            if stage is WorkflowStageName.PUSH
            else True
        )
        valid_availability = (
            stage is WorkflowStageName.AVAILABILITY
            and evidence_type == "availability_receipt"
            and source is EvidenceSource.READONLY_RECONCILIATION
            and status is EvidenceStatus.AVAILABLE
            and task.state is TaskState.AVAILABLE
        )
        valid_source = source in {
            EvidenceSource.SUBMISSION_RECEIPT,
            EvidenceSource.READONLY_RECONCILIATION,
        }
        if (
            not valid_task_stage
            or not valid_type
            or not valid_source
            or not valid_push_status
        ):
            raise WorkflowConflict("workflow_evidence_required")
        if stage is WorkflowStageName.AVAILABILITY and not valid_availability:
            raise WorkflowConflict("workflow_evidence_required")
    else:
        raise WorkflowConflict("workflow_evidence_required")

    unique_filters = [
        WorkflowEvidence.evidence_type == evidence_type,
        WorkflowEvidence.source == source.value,
        WorkflowEvidence.subject_id == subject_id,
        WorkflowEvidence.status == status.value,
    ]
    unique_filters.append(
        WorkflowEvidence.workflow_id.is_(None)
        if workflow_id is None
        else WorkflowEvidence.workflow_id == workflow_id
    )
    unique_filters.append(
        WorkflowEvidence.task_id.is_(None)
        if task_id is None
        else WorkflowEvidence.task_id == task_id
    )
    unique_filters.append(
        WorkflowEvidence.stage.is_(None)
        if stage is None
        else WorkflowEvidence.stage == stage
    )
    current_time = observed_at or datetime.now(UTC)
    values = {
        "id": "evidence_" + uuid4().hex,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "stage": stage,
        "evidence_type": evidence_type,
        "source": source.value,
        "subject_id": subject_id,
        "status": status.value,
        "verified": verified,
        "observed_at": current_time,
        "created_at": current_time,
    }
    statement = insert(WorkflowEvidence).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                WorkflowEvidence.workflow_id,
                WorkflowEvidence.stage,
                WorkflowEvidence.evidence_type,
                WorkflowEvidence.source,
                WorkflowEvidence.subject_id,
                WorkflowEvidence.status,
            ],
            set_={
                "task_id": statement.excluded.task_id,
                "verified": statement.excluded.verified,
                "observed_at": statement.excluded.observed_at,
            },
        )
    )
    evidence = await session.scalar(select(WorkflowEvidence).where(*unique_filters))
    if evidence is None:
        raise RuntimeError("workflow evidence upsert did not return a row")
    return evidence


async def record_discovery_evidence(
    session: AsyncSession,
    workflow: Workflow,
    resource_id: str,
) -> WorkflowEvidence:
    """Use an existing local resource row as the discovery producer."""

    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow.id)
        )
    )
    stage = next(item for item in stages if item.stage is WorkflowStageName.DISCOVERY)
    if stage.status in _TERMINAL_STAGE_STATUSES:
        if (
            stage.status is WorkflowStageStatus.SUCCEEDED
            and stage.child_id == resource_id
            and stage.child_type == "resource"
        ):
            evidence = await session.scalar(
                select(WorkflowEvidence).where(
                    WorkflowEvidence.workflow_id == workflow.id,
                    WorkflowEvidence.stage == WorkflowStageName.DISCOVERY,
                    WorkflowEvidence.subject_id == resource_id,
                    WorkflowEvidence.evidence_type == "discovery",
                    WorkflowEvidence.source == EvidenceSource.RESOURCE_RECORD.value,
                    WorkflowEvidence.status == EvidenceStatus.DISCOVERED.value,
                )
            )
            if evidence is not None:
                return evidence
        raise WorkflowConflict("workflow_discovery_conflict")
    _validate_stage_transition(stages, stage, WorkflowStageStatus.RUNNING)
    _ensure_child_binding(
        stage,
        child_type="resource",
        child_id=resource_id,
        conflict_code="workflow_discovery_conflict",
    )
    now = datetime.now(UTC)
    stage.status = WorkflowStageStatus.RUNNING
    stage.started_at = stage.started_at or now
    stage.updated_at = now
    stage.status = WorkflowStageStatus.SUCCEEDED
    stage.reason = "discovery_evidence"
    stage.error_code = None
    stage.child_type = "resource"
    stage.child_id = resource_id
    stage.completed_at = now
    stage.updated_at = now
    workflow.status, workflow.state_reason = _derive_status([*stages])
    workflow.updated_at = now
    return await record_evidence(
        session,
        workflow_id=workflow.id,
        task_id=None,
        stage=WorkflowStageName.DISCOVERY,
        evidence_type="discovery",
        source=EvidenceSource.RESOURCE_RECORD,
        subject_id=resource_id,
        status=EvidenceStatus.DISCOVERED,
        verified=True,
        observed_at=now,
    )


async def advance_availability_from_evidence(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    evidence: WorkflowEvidence,
) -> Workflow:
    """Close push and open downstream stages only after an available receipt."""

    stored_evidence = await session.get(WorkflowEvidence, evidence.id)
    if stored_evidence is None:
        raise WorkflowConflict("workflow_evidence_required")
    evidence = stored_evidence
    if (
        evidence.workflow_id != workflow_id
        or evidence.stage is not WorkflowStageName.AVAILABILITY
        or evidence.evidence_type != "availability_receipt"
        or evidence.source != EvidenceSource.READONLY_RECONCILIATION.value
        or evidence.status != EvidenceStatus.AVAILABLE.value
        or not evidence.verified
        or evidence.task_id != task_id
        or evidence.subject_id != task_id
    ):
        raise WorkflowConflict("workflow_evidence_required")
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    task = await session.get(Task, task_id)
    if (
        task is None
        or task.workflow_id != workflow_id
        or task.state is not TaskState.AVAILABLE
    ):
        raise WorkflowConflict("workflow_evidence_required")
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    push = next(item for item in stages if item.stage is WorkflowStageName.PUSH)
    availability = next(
        item for item in stages if item.stage is WorkflowStageName.AVAILABILITY
    )
    if push.status is not WorkflowStageStatus.SUCCEEDED:
        _validate_stage_transition(stages, push, WorkflowStageStatus.SUCCEEDED)
    _ensure_child_binding(
        push,
        child_type="task",
        child_id=task_id,
    )
    if availability.status is WorkflowStageStatus.SUCCEEDED:
        _ensure_child_binding(
            availability,
            child_type="workflow_evidence",
            child_id=evidence.id,
            conflict_code="workflow_stage_terminal",
        )
        return workflow
    if push.status is not WorkflowStageStatus.SUCCEEDED:
        now = datetime.now(UTC)
        push.status = WorkflowStageStatus.SUCCEEDED
        push.reason = "task_available"
        push.error_code = None
        push.completed_at = now
        push.updated_at = now
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    _validate_stage_transition(stages, availability, WorkflowStageStatus.SUCCEEDED)
    _ensure_child_binding(
        availability,
        child_type="workflow_evidence",
        child_id=evidence.id,
        conflict_code="workflow_stage_terminal",
    )
    now = datetime.now(UTC)
    availability.status = WorkflowStageStatus.SUCCEEDED
    availability.reason = "availability_evidence"
    availability.error_code = None
    availability.child_type = "workflow_evidence"
    availability.child_id = evidence.id
    availability.started_at = availability.started_at or now
    availability.completed_at = now
    availability.updated_at = now
    workflow.status, workflow.state_reason = _derive_status(stages)
    workflow.updated_at = now
    return workflow


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
    await _validate_child_ownership(
        session,
        workflow_id,
        stage_name,
        child_type=child_type,
        child_id=child_id,
    )
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    _validate_stage_transition(stages, stage, WorkflowStageStatus.RUNNING)
    _ensure_child_binding(stage, child_type=child_type, child_id=child_id)
    if stage.status is WorkflowStageStatus.RUNNING:
        return workflow
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
    await _validate_child_ownership(
        session,
        workflow_id,
        stage_name,
        child_type=child_type,
        child_id=child_id,
    )
    stages = list(
        await session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id)
        )
    )
    _validate_stage_transition(stages, stage, status)
    original_stage_status = stage.status
    original_stage_updated_at = stage.updated_at
    current_binding = (stage.child_type, stage.child_id)
    if current_binding != (None, None) and current_binding != (
        child_type,
        child_id,
    ):
        raise WorkflowConflict("workflow_conflict")
    now = datetime.now(UTC)
    stage_values = {
        "child_type": child_type,
        "child_id": child_id,
    }
    terminal_replay = (
        stage.status in _TERMINAL_STAGE_STATUSES and stage.status is status
    )
    if terminal_replay:
        if current_binding == (child_type, child_id):
            return workflow
        stage_values["updated_at"] = now
    else:
        stage_values.update(
            status=status,
            reason=reason,
            error_code=error_code,
            updated_at=now,
        )
        if status in {
            WorkflowStageStatus.RUNNING,
            WorkflowStageStatus.WAITING_EXTERNAL,
            WorkflowStageStatus.WAITING_CONFIRMATION,
        } and stage.started_at is None:
            stage_values["started_at"] = now
        if status in _TERMINAL_STAGE_STATUSES:
            stage_values["completed_at"] = now
    result = await session.execute(
        update(WorkflowStage)
        .where(
            WorkflowStage.id == stage.id,
            WorkflowStage.status == original_stage_status,
            WorkflowStage.updated_at == original_stage_updated_at,
        )
        .values(**stage_values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        current_stage = await session.scalar(
            select(WorkflowStage)
            .where(WorkflowStage.id == stage.id)
            .execution_options(populate_existing=True)
        )
        if (
            current_stage is not None
            and current_stage.status is status
            and status in _TERMINAL_STAGE_STATUSES
            and (current_stage.child_type, current_stage.child_id)
            == (child_type, child_id)
        ):
            await session.refresh(
                workflow,
                ["status", "state_reason", "updated_at", "stages"],
            )
            return workflow
        if (
            current_stage is not None
            and current_stage.status is status
            and status in _TERMINAL_STAGE_STATUSES
        ):
            raise WorkflowConflict("workflow_conflict")
        if (
            current_stage is not None
            and current_stage.status in _TERMINAL_STAGE_STATUSES
        ):
            raise WorkflowConflict("workflow_stage_terminal")
        raise WorkflowConflict("workflow_conflict")
    await session.refresh(stage)
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


def _ensure_child_binding(
    stage: WorkflowStage,
    *,
    child_type: str,
    child_id: str,
    conflict_code: str = "workflow_conflict",
) -> None:
    """Keep one workflow stage bound to one durable child identity."""

    if not child_type or not child_id:
        raise WorkflowConflict(conflict_code)
    if stage.child_type is None and stage.child_id is None:
        stage.child_type = child_type
        stage.child_id = child_id
        return
    if stage.child_type != child_type or stage.child_id != child_id:
        raise WorkflowConflict(conflict_code)


async def _validate_child_ownership(
    session: AsyncSession,
    workflow_id: str,
    stage_name: WorkflowStageName,
    *,
    child_type: str,
    child_id: str,
) -> None:
    """Require every workflow child binding to name a real scoped child."""

    if child_type == "resource":
        valid = stage_name is WorkflowStageName.DISCOVERY and (
            await session.get(Resource, child_id)
        ) is not None
    elif child_type == "task":
        task = await session.get(Task, child_id)
        valid = stage_name is WorkflowStageName.PUSH and (
            task is not None and task.workflow_id == workflow_id
        )
    elif child_type == "inspection_batch":
        batch = await session.get(InspectionBatch, child_id)
        valid = stage_name is WorkflowStageName.INSPECTION and (
            batch is not None and batch.workflow_id == workflow_id
        )
    elif child_type == "workflow_evidence":
        evidence = await session.get(WorkflowEvidence, child_id)
        valid = stage_name is WorkflowStageName.AVAILABILITY and (
            evidence is not None
            and evidence.workflow_id == workflow_id
            and evidence.stage is WorkflowStageName.AVAILABILITY
        )
    elif child_type == "organization_operation":
        operation = await session.get(OrganizationOperation, child_id)
        valid = stage_name is WorkflowStageName.ORGANIZATION and (
            operation is not None and operation.workflow_id == workflow_id
        )
    elif child_type == "strm_operation":
        operation = await session.get(StrmOperation, child_id)
        valid = stage_name is WorkflowStageName.STRM and (
            operation is not None and operation.workflow_id == workflow_id
        )
    elif child_type == "strm_dirty_generation":
        generation = await session.get(DirectoryDirtyGeneration, child_id)
        operation = (
            await session.get(OrganizationOperation, generation.operation_id)
            if generation is not None
            else None
        )
        valid = stage_name is WorkflowStageName.STRM and (
            operation is not None and operation.workflow_id == workflow_id
        )
    else:
        valid = False
    if not valid:
        raise WorkflowConflict("workflow_conflict")


def _validate_stage_transition(
    stages: list[WorkflowStage],
    stage: WorkflowStage,
    requested: WorkflowStageStatus,
) -> None:
    """Enforce the ordered workflow contract at every child update boundary."""

    current = stage.status
    if requested is WorkflowStageStatus.PENDING:
        if current is not WorkflowStageStatus.PENDING:
            raise WorkflowConflict("workflow_stage_regression")
        return
    if current in {
        WorkflowStageStatus.SUCCEEDED,
        WorkflowStageStatus.SKIPPED,
        WorkflowStageStatus.CANCELLED,
    } and requested is not current:
        raise WorkflowConflict("workflow_stage_terminal")
    if current is WorkflowStageStatus.UNCERTAIN and requested not in {
        WorkflowStageStatus.SUCCEEDED,
        WorkflowStageStatus.FAILED,
        WorkflowStageStatus.UNCERTAIN,
    }:
        raise WorkflowConflict("workflow_stage_regression")
    if current is WorkflowStageStatus.FAILED and requested not in {
        WorkflowStageStatus.RUNNING,
        WorkflowStageStatus.FAILED,
    }:
        raise WorkflowConflict("workflow_stage_regression")
    later_stages = [item for item in stages if item.sequence > stage.sequence]
    if any(item.status is not WorkflowStageStatus.PENDING for item in later_stages):
        raise WorkflowConflict("workflow_stage_regression")
    if stage.sequence == 0:
        return
    previous = {item.sequence: item for item in stages}.get(stage.sequence - 1)
    if previous is None or previous.status not in _TERMINAL_PREREQUISITE_STATUSES:
        raise WorkflowConflict("workflow_prerequisite_not_met")


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
        state_reason_zh=_reason_zh(workflow.state_reason),
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
                reason_zh=_reason_zh(stage.reason),
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


def _evidence_response(evidence: WorkflowEvidence) -> WorkflowEvidenceResponse:
    return WorkflowEvidenceResponse(
        id=evidence.id,
        workflow_id=evidence.workflow_id,
        task_id=evidence.task_id,
        stage=evidence.stage,
        evidence_type=evidence.evidence_type,
        source=EvidenceSource(evidence.source),
        subject_id=evidence.subject_id,
        status=EvidenceStatus(evidence.status),
        verified=evidence.verified,
        observed_at=evidence.observed_at,
    )


def _reason_zh(reason: str | None) -> str | None:
    if reason is None:
        return None
    return _REASON_ZH.get(reason, _ERROR_ZH.get(reason, "工作流状态已更新，请查看诊断信息。"))
