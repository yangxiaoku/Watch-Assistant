"""Task creation, polling, history, and manual retry routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from watch_assistant.schemas import TaskCreateRequest, TaskResponse
from watch_assistant.security import require_api_auth
from watch_assistant.services.tasks import (
    InvalidRetryState,
    ResourceNotFound,
    TaskService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]


def ensure_push_supported(request: Request) -> None:
    if getattr(request.app.state, "push_supported", True) is not True:
        raise HTTPException(status_code=503, detail="push_unsupported")


@router.post(
    "/tasks", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_task(
    request: TaskCreateRequest,
    raw_request: Request,
    service: TaskServiceDependency,
) -> TaskResponse:
    ensure_push_supported(raw_request)
    try:
        task, _reused = await service.create(request.resource_id, force=request.force)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="resource_not_found") from exc
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
    ensure_push_supported(raw_request)
    try:
        return await service.retry(task_id)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except InvalidRetryState as exc:
        raise HTTPException(status_code=409, detail="task_not_retryable") from exc
