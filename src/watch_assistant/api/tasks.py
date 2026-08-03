"""Task creation, polling, history, and manual retry routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import (
    TaskAction,
    TaskCreateRequest,
    TaskReconciliationResponse,
    TaskResponse,
    WorkflowEvidenceResponse,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.tasks import (
    InvalidCancelState,
    InvalidRetryState,
    PushKindUnsupported,
    ReconciliationUnavailable,
    ResourceNotFound,
    TaskNotReconcilable,
    TaskService,
)
from watch_assistant.services.workflows import WorkflowConflict, WorkflowNotFound

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]


def allowed_push_actions(request: Request) -> frozenset[TaskAction]:
    capabilities = getattr(request.app.state, "push_capabilities", None)
    if isinstance(capabilities, dict):
        actions: set[TaskAction] = set()
        if capabilities.get("magnet") is True:
            actions.add(TaskAction.OFFLINE_DOWNLOAD)
        if capabilities.get("share") is True:
            actions.add(TaskAction.SAVE_SHARE)
        return frozenset(actions)
    return frozenset()


@router.post(
    "/tasks", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_task(
    request: TaskCreateRequest,
    raw_request: Request,
    service: TaskServiceDependency,
) -> TaskResponse:
    if request.target_directory_id is not None:
        root_id = getattr(raw_request.app.state, "organization_target_root_id", None)
        browsed_ids = getattr(raw_request.app.state, "p115_browsed_directory_ids", set())
        if not isinstance(root_id, str) or not root_id:
            raise HTTPException(status_code=503, detail="p115_directory_scope_unavailable")
        if request.target_directory_id not in {root_id, *browsed_ids}:
            raise HTTPException(status_code=403, detail="p115_directory_out_of_scope")
    try:
        task, _reused = await service.create(
            request.resource_id,
            force=request.force,
            allowed_actions=allowed_push_actions(raw_request),
            workflow_id=request.workflow_id,
            target_directory_id=request.target_directory_id,
        )
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="resource_not_found") from exc
    except PushKindUnsupported as exc:
        raise HTTPException(status_code=503, detail="push_kind_unsupported") from exc
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail="workflow_not_found") from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code not in {
            "workflow_prerequisite_not_met",
            "workflow_stage_regression",
            "workflow_stage_terminal",
            "workflow_evidence_required",
        }:
            code = "workflow_conflict"
        raise HTTPException(status_code=409, detail=code) from None
    return task


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, service: TaskServiceDependency) -> TaskResponse:
    task = await service.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    return task


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(service: TaskServiceDependency) -> list[TaskResponse]:
    return await service.list_recent()


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: str, raw_request: Request, service: TaskServiceDependency
) -> TaskResponse:
    try:
        return await service.retry(
            task_id, allowed_actions=allowed_push_actions(raw_request)
        )
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except InvalidRetryState as exc:
        code = str(exc)
        if code not in {"uncertain_requires_verification"}:
            code = "task_not_retryable"
        raise HTTPException(status_code=409, detail=code) from exc
    except PushKindUnsupported as exc:
        raise HTTPException(status_code=503, detail="push_kind_unsupported") from exc


@router.post(
    "/tasks/{task_id}/reconcile", response_model=TaskReconciliationResponse
)
async def reconcile_task(
    task_id: str,
    raw_request: Request,
    service: TaskServiceDependency,
) -> TaskReconciliationResponse:
    adapter = getattr(raw_request.app.state, "task_adapter", None)
    if adapter is None or not callable(getattr(adapter, "get_status_for_task", None)):
        raise HTTPException(status_code=503, detail="reconciliation_unavailable")
    try:
        task, evidence = await service.reconcile(task_id, adapter)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except TaskNotReconcilable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReconciliationUnavailable as exc:
        code = str(exc)
        if code not in {"reconciliation_unavailable", "reconciliation_conflict"}:
            code = "reconciliation_unavailable"
        raise HTTPException(status_code=503, detail=code) from exc
    except WorkflowConflict as exc:
        code = str(exc)
        if code not in {"workflow_evidence_required", "workflow_stage_terminal"}:
            code = "reconciliation_conflict"
        raise HTTPException(status_code=409, detail=code) from exc
    return TaskReconciliationResponse(
        task=TaskResponse.model_validate(task, from_attributes=True),
        evidence=WorkflowEvidenceResponse.model_validate(
            evidence, from_attributes=True
        ),
    )


@router.get(
    "/tasks/{task_id}/evidence", response_model=list[WorkflowEvidenceResponse]
)
async def list_task_evidence(
    task_id: str, service: TaskServiceDependency
) -> list[WorkflowEvidenceResponse]:
    try:
        rows = await service.evidence(task_id)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    return [
        WorkflowEvidenceResponse.model_validate(row, from_attributes=True)
        for row in rows
    ]


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str, raw_request: Request, service: TaskServiceDependency
) -> TaskResponse:
    try:
        return await service.cancel(
            task_id, allowed_actions=allowed_push_actions(raw_request)
        )
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except InvalidCancelState as exc:
        raise HTTPException(status_code=409, detail="task_not_cancellable") from exc
    except PushKindUnsupported as exc:
        raise HTTPException(status_code=503, detail="push_kind_unsupported") from exc
