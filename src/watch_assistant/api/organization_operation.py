"""Authenticated local queue and cancellation routes for organization plans."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.api.organization_plan import require_organization_plan_enabled
from watch_assistant.schemas import (
    OrganizationOperationBatchRequest,
    OrganizationOperationBatchResponse,
    OrganizationOperationBatchResult,
    OrganizationOperationQueueRequest,
    OrganizationOperationResponse,
    OrganizationPlanMutationRequest,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.organization_operations import (
    OrganizationOperationNotFound,
    OrganizationOperationService,
    OrganizationOperationSummary,
)


async def require_organization_execution_enabled(request: Request) -> None:
    if not getattr(request.app.state, "organization_execution_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "organization_execution_disabled",
                "message": "整理操作功能未启用",
            },
        )


router = APIRouter(
    prefix="/api/v1",
    dependencies=[
        Depends(require_api_auth),
        Depends(require_organization_execution_enabled),
        Depends(require_organization_plan_enabled),
    ],
)


def _get_operation_service(request: Request) -> OrganizationOperationService:
    service = getattr(request.app.state, "organization_operation_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "organization_operation_unavailable",
                "message": "整理操作服务暂不可用",
            },
        )
    return service


ServiceDependency = Annotated[
    OrganizationOperationService, Depends(_get_operation_service)
]
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


@router.post(
    "/organization-plans/{plan_id}/operation",
    response_model=OrganizationOperationResponse,
)
async def queue_organization_operation(
    plan_id: str,
    payload: OrganizationOperationQueueRequest,
    context: AuthDependency,
    service: ServiceDependency,
) -> OrganizationOperationResponse:
    try:
        await _validate_agent_confirmation(
            context, service, plan_id, digest=payload.digest, confirm=payload.confirm
        )
        summary = await service.create(
            plan_id,
            idempotency_key=payload.idempotency_key,
            expected_plan_revision=payload.expected_revision,
            workflow_id=payload.workflow_id,
        )
    except Exception as exc:  # noqa: BLE001 - map only stable local errors
        raise _http_error(exc) from None
    return _response(summary)


@router.get(
    "/organization-plans/{plan_id}/operation",
    response_model=OrganizationOperationResponse,
)
async def get_plan_organization_operation(
    plan_id: str, service: ServiceDependency
) -> OrganizationOperationResponse:
    try:
        summary = await service.get_for_plan(plan_id)
    except Exception as exc:  # noqa: BLE001 - map only stable local errors
        raise _http_error(exc) from None
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "operation_not_found", "message": "整理操作不存在"},
        )
    return _response(summary)


@router.post(
    "/organization-operations/batch",
    response_model=OrganizationOperationBatchResponse,
)
async def queue_organization_operations_batch(
    payload: OrganizationOperationBatchRequest,
    context: AuthDependency,
    service: ServiceDependency,
) -> OrganizationOperationBatchResponse:
    results: list[OrganizationOperationBatchResult] = []
    for item in payload.items:
        try:
            await _validate_agent_confirmation(
                context,
                service,
                item.plan_id,
                digest=item.digest,
                confirm=item.confirm,
            )
            summary = await service.create(
                item.plan_id,
                idempotency_key=item.idempotency_key,
                expected_plan_revision=item.expected_revision,
                workflow_id=item.workflow_id,
            )
        except Exception as exc:  # noqa: BLE001 - isolate each local item
            status_code, code, message = _error_values(exc)
            del status_code
            results.append(
                OrganizationOperationBatchResult(
                    plan_id=item.plan_id,
                    status="rejected",
                    error_code=code,
                    message=message,
                )
            )
        else:
            results.append(_batch_result(summary))
    return OrganizationOperationBatchResponse(items=results)


@router.post(
    "/organization-operations/{operation_id}/cancel",
    response_model=OrganizationOperationResponse,
)
async def cancel_organization_operation(
    operation_id: str,
    payload: OrganizationPlanMutationRequest,
    service: ServiceDependency,
) -> OrganizationOperationResponse:
    try:
        summary = await service.cancel(
            operation_id, expected_revision=payload.expected_revision
        )
    except Exception as exc:  # noqa: BLE001 - map only stable local errors
        raise _http_error(exc) from None
    return _response(summary)


@router.get(
    "/organization-operations/{operation_id}",
    response_model=OrganizationOperationResponse,
)
async def get_organization_operation(
    operation_id: str, service: ServiceDependency
) -> OrganizationOperationResponse:
    try:
        summary = await service.get(operation_id)
    except Exception as exc:  # noqa: BLE001 - map only stable local errors
        raise _http_error(exc) from None
    return _response(summary)


def _response(summary: OrganizationOperationSummary) -> OrganizationOperationResponse:
    return OrganizationOperationResponse(
        operation_id=summary.operation_id,
        plan_id=summary.plan_id,
        status=summary.status.value,
        revision=summary.revision,
        attempts=summary.attempts,
        error_code=summary.error_code,
        cancel_requested=summary.cancel_requested,
    )


def _batch_result(
    summary: OrganizationOperationSummary,
) -> OrganizationOperationBatchResult:
    return OrganizationOperationBatchResult(
        plan_id=summary.plan_id,
        operation_id=summary.operation_id,
        status=summary.status.value,
        revision=summary.revision,
        attempts=summary.attempts,
        error_code=summary.error_code,
        message="整理操作已排队",
    )


def _http_error(error: Exception) -> HTTPException:
    status, code, message = _error_values(error)
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
    )


def _error_values(error: Exception) -> tuple[int, str, str]:
    if isinstance(error, OrganizationOperationNotFound):
        code = "operation_not_found"
    elif isinstance(error, ValueError):
        code = str(error) if str(error) in _MESSAGES else "operation_unavailable"
    else:
        code = "operation_unavailable"
    status = _STATUSES.get(code, 409)
    return status, code, _MESSAGES.get(code, "整理操作暂不可用")


async def _validate_agent_confirmation(
    context: AuthContext,
    service: OrganizationOperationService,
    plan_id: str,
    *,
    digest: str | None,
    confirm: bool,
) -> None:
    if not context.via_bearer:
        return
    if not confirm:
        raise ValueError("confirmation_required")
    if digest is None:
        raise ValueError("plan_digest_required")
    expected = await service.plan_digest(plan_id)
    if not hmac.compare_digest(expected, digest):
        raise ValueError("plan_digest_mismatch")


_STATUSES = {
    "invalid_plan_id": 422,
    "invalid_idempotency_key": 422,
    "invalid_operation_id": 422,
    "invalid_workflow_id": 422,
    "workflow_not_found": 404,
    "workflow_stage_missing": 409,
    "workflow_id_conflict": 409,
    "operation_not_found": 404,
    "confirmation_required": 409,
    "plan_digest_required": 422,
    "plan_digest_mismatch": 409,
}
_MESSAGES = {
    "plan_not_found": "计划不存在",
    "plan_is_not_planned": "计划尚未确认或已不可用",
    "plan_prerequisites_changed": "计划前置条件已变化，请重新确认",
    "plan_not_executable": "当前计划包含待复核项，不能执行真实整理",
    "plan_revision_changed": "计划版本已变化，请刷新后重试",
    "idempotency_key_conflict": "幂等请求与既有操作冲突",
    "plan_already_has_operation": "计划已有受控操作",
    "operation_plan_conflict": "计划已有受控操作",
    "operation_creation_conflict": "操作创建发生冲突",
    "operation_revision_changed": "操作版本已变化，请刷新后重试",
    "operation_is_not_cancellable": "操作当前状态不可取消",
    "uncertain_requires_verification": "操作结果不确定，禁止普通重试",
    "invalid_plan_id": "计划标识无效",
    "invalid_idempotency_key": "幂等标识无效",
    "invalid_operation_id": "操作标识无效",
    "invalid_workflow_id": "工作流标识无效",
    "workflow_not_found": "工作流不存在",
    "workflow_stage_missing": "工作流整理阶段不存在",
    "workflow_id_conflict": "操作已关联其他工作流",
    "operation_not_found": "操作不存在",
    "confirmation_required": "缺少操作确认",
    "plan_digest_required": "缺少计划摘要",
    "plan_digest_mismatch": "计划摘要已变化，请刷新后重试",
    "operation_unavailable": "整理操作暂不可用",
}


__all__ = ["router"]
