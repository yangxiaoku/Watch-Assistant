"""只读库存重复检测路由。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from watch_assistant.schemas import (
    InventoryAuditGroupResponse,
    InventoryAuditItemResponse,
    InventoryAuditReportResponse,
)
from watch_assistant.security import AuthContext, require_api_auth
from watch_assistant.services.inventory_audit import InventoryAuditReport
from watch_assistant.services.inventory_audit_service import (
    InventoryAuditError,
    InventoryAuditService,
)

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_auth)],
)
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


def _service(request: Request) -> InventoryAuditService:
    service = getattr(request.app.state, "inventory_audit_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="inventory_audit_unavailable")
    return service


def _to_response(report: InventoryAuditReport) -> InventoryAuditReportResponse:
    return InventoryAuditReportResponse(
        groups=[
            InventoryAuditGroupResponse(
                group_id=group.group_id,
                kind=group.kind,
                items=[
                    InventoryAuditItemResponse(
                        object_id=item.object_id,
                        name=item.name,
                        path=item.path,
                        size_bytes=item.size_bytes,
                        resolution=item.resolution,
                    )
                    for item in group.items
                ],
                reclaimable_bytes=group.reclaimable_bytes,
                keep_object_id=group.keep_object_id,
            )
            for group in report.groups
        ],
        duplicate_count=report.duplicate_count,
        multi_version_count=report.multi_version_count,
        reclaimable_bytes=report.reclaimable_bytes,
    )


@router.get(
    "/inventory/audit",
    response_model=InventoryAuditReportResponse,
)
async def inventory_audit(
    request: Request, _: AuthDependency
) -> InventoryAuditReportResponse:
    target_root_id = getattr(request.app.state, "organization_target_root_id", None)
    if not target_root_id:
        raise HTTPException(
            status_code=503, detail={"code": "inventory_scope_unconfigured"}
        )
    try:
        report = await _service(request).run_audit(str(target_root_id))
    except InventoryAuditError as exc:
        status_code = 503 if exc.code == "library_scope_unverified" else 409
        raise HTTPException(status_code=status_code, detail={"code": exc.code}) from None
    return _to_response(report)


__all__ = ["router"]
