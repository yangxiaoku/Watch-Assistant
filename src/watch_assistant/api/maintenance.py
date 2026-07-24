"""Cache warming, watch-list, and source reliability routes."""

from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)

from watch_assistant.schemas import (
    CacheRetryResponse,
    CacheWarmStatusResponse,
    MovieWatchSummary,
    SourceReliabilitySummary,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.cache_warm import CacheWarmer
from watch_assistant.services.maintenance import MaintenanceService

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_maintenance_service(request: Request) -> MaintenanceService:
    return request.app.state.maintenance_service


def get_cache_warmer(request: Request) -> CacheWarmer:
    warmer = getattr(request.app.state, "cache_warmer", None)
    if warmer is None:
        raise HTTPException(status_code=503, detail="cache_warm_disabled")
    return warmer


MaintenanceDependency = Annotated[
    MaintenanceService, Depends(get_maintenance_service)
]
CacheWarmerDependency = Annotated[CacheWarmer, Depends(get_cache_warmer)]


@router.get("/cache/status", response_model=CacheWarmStatusResponse)
async def cache_status(
    service: MaintenanceDependency,
    warmer: CacheWarmerDependency,
) -> CacheWarmStatusResponse:
    state = await service.get_warm_state()
    return CacheWarmStatusResponse(
        running=warmer.is_running,
        last_started_at=state.last_started_at if state else None,
        last_completed_at=state.last_completed_at if state else None,
        next_run_at=warmer.next_run_at(),
        total_count=state.total_count if state else 0,
        success_count=state.success_count if state else 0,
        failure_count=state.failure_count if state else 0,
        skipped_count=state.skipped_count if state else 0,
        failed_media=await service.failed_media(),
    )


@router.post(
    "/cache/retry",
    response_model=CacheRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_cache(
    background_tasks: BackgroundTasks,
    service: MaintenanceDependency,
    warmer: CacheWarmerDependency,
) -> CacheRetryResponse:
    if warmer.is_running:
        raise HTTPException(status_code=409, detail="cache_warm_running")
    failed_media = await service.failed_media()
    if failed_media:
        background_tasks.add_task(warmer.retry_failed, failed_media)
    return CacheRetryResponse(
        scheduled=bool(failed_media),
        count=len(failed_media),
    )


@router.get("/watchlist", response_model=list[MovieWatchSummary])
async def watchlist(
    service: MaintenanceDependency,
    active: bool | None = Query(default=True),
) -> list[MovieWatchSummary]:
    return await service.list_watches(active=active)


@router.get("/sources/reliability", response_model=list[SourceReliabilitySummary])
async def source_reliability(
    service: MaintenanceDependency,
) -> list[SourceReliabilitySummary]:
    return await service.list_sources()
