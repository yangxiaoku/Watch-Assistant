"""Task creation, polling, history, and manual retry routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import TaskAction, TaskCreateRequest, TaskResponse
from watch_assistant.security import require_api_auth
from watch_assistant.services.tasks import (
    InvalidRetryState,
    PushKindUnsupported,
    ResourceNotFound,
    TaskService,
)

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
    try:
        task, _reused = await service.create(
            request.resource_id,
            force=request.force,
            allowed_actions=allowed_push_actions(raw_request),
        )
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="resource_not_found") from exc
    except PushKindUnsupported as exc:
        raise HTTPException(status_code=503, detail="push_kind_unsupported") from exc
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
        raise HTTPException(status_code=409, detail="task_not_retryable") from exc
    except PushKindUnsupported as exc:
        raise HTTPException(status_code=503, detail="push_kind_unsupported") from exc
