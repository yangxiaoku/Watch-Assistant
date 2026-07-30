"""Authenticated STRM manifest, generation, and playback routes."""

import ipaddress
import math
from collections.abc import Collection
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from watch_assistant.adapters.p115_playback_contract import (
    PlaybackContractError,
    PlaybackGate,
    PlaybackStatus,
    make_playback_request,
)
from watch_assistant.schemas import (
    StrmGenerationRequest,
    StrmGenerationResponse,
    StrmManifestItemResponse,
    StrmManifestListResponse,
)
from watch_assistant.security import AuthContext, require_api_auth, require_scope
from watch_assistant.services.strm_manifest import (
    StrmManifestError,
    StrmManifestService,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])
AuthDependency = Annotated[AuthContext, Depends(require_api_auth)]


async def require_strm_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_full_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_full_disabled", "message": "STRM 功能未启用"},
        )


async def require_strm_incremental_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_incremental_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strm_incremental_disabled",
                "message": "STRM 增量同步未启用",
            },
        )


async def require_strm_cleanup_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_cleanup_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_cleanup_disabled", "message": "STRM 失效清理未启用"},
        )


async def require_strm_playback_enabled(request: Request) -> None:
    if not getattr(request.app.state, "strm_playback_enabled", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_playback_disabled", "message": "STRM 播放未启用"},
        )
    if not getattr(request.app.state, "strm_playback_contract_verified", False):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strm_playback_unverified",
                "message": "STRM 播放契约尚未验收",
            },
        )
    if not getattr(request.app.state, "strm_playback_supported", False):
        raise HTTPException(
            status_code=503,
            detail={"code": "strm_playback_unavailable", "message": "STRM 播放暂不可用"},
        )
    _require_playback_network(request)


def _service(request: Request) -> StrmManifestService:
    service = getattr(request.app.state, "strm_manifest_service", None)
    if not isinstance(service, StrmManifestService):
        raise HTTPException(status_code=503, detail="strm_unavailable")
    return service


ServiceDependency = Annotated[StrmManifestService, Depends(_service)]


@router.get(
    "/libraries/{library_id}/strm-manifest",
    response_model=StrmManifestListResponse,
    dependencies=[Depends(require_strm_enabled)],
)
async def list_manifest(
    library_id: str,
    service: ServiceDependency,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> StrmManifestListResponse:
    try:
        items, total = await service.list_current(
            library_id, page=page, page_size=page_size
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmManifestListResponse(
        items=[
            StrmManifestItemResponse(
                manifest_id=item.manifest_id,
                library_id=item.library_id,
                cloud_file_id=item.cloud_file_id,
                cloud_relative_path=item.cloud_relative_path,
                local_relative_path=item.local_relative_path,
                status=item.status,
                source_version=item.source_version,
            )
            for item in items
        ],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.post(
    "/libraries/{library_id}/strm-generation",
    response_model=StrmGenerationResponse,
    dependencies=[
        Depends(require_strm_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def generate_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    service: ServiceDependency,
    request: Request,
) -> StrmGenerationResponse:
    try:
        summary = await service.generate(
            library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=Path(
                getattr(request.app.state, "strm_output_root", "./data/strm")
            ),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmGenerationResponse(
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


@router.post(
    "/libraries/{library_id}/strm-incremental",
    response_model=StrmGenerationResponse,
    dependencies=[
        Depends(require_strm_incremental_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def incremental_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    service: ServiceDependency,
    request: Request,
) -> StrmGenerationResponse:
    try:
        summary = await service.incremental(
            library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=Path(
                getattr(request.app.state, "strm_output_root", "./data/strm")
            ),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
            retire_removed=False,
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmGenerationResponse(
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


@router.post(
    "/libraries/{library_id}/strm-cleanup",
    response_model=StrmGenerationResponse,
    dependencies=[
        Depends(require_strm_cleanup_enabled),
        Depends(require_scope("strm:write")),
    ],
)
async def cleanup_manifest(
    library_id: str,
    payload: StrmGenerationRequest,
    service: ServiceDependency,
    request: Request,
) -> StrmGenerationResponse:
    try:
        summary = await service.cleanup(
            library_id,
            source_scan_run_id=payload.source_scan_run_id,
            output_root=Path(
                getattr(request.app.state, "strm_output_root", "./data/strm")
            ),
            playback_url_prefix=getattr(
                request.app.state,
                "strm_playback_url_prefix",
                "http://127.0.0.1:8115/api/v1/strm/play",
            ),
        )
    except StrmManifestError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return StrmGenerationResponse(
        library_id=summary.library_id,
        scan_run_id=summary.scan_run_id,
        generated=summary.generated,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
        retired=summary.retired,
    )


@router.api_route(
    "/strm/play/{manifest_id}",
    methods=["GET", "HEAD"],
    dependencies=[Depends(require_strm_playback_enabled)],
)
async def play_manifest(
    manifest_id: str,
    request: Request,
    context: AuthDependency,
) -> Response:
    try:
        playback_request = make_playback_request(
            manifest_id,
            request.method,
            request.headers.get("range"),
        )
    except PlaybackContractError as error:
        raise HTTPException(status_code=416, detail="invalid_playback_request") from error
    gateway = getattr(request.app.state, "strm_playback_gateway", None)
    if gateway is None or not callable(getattr(gateway, "resolve", None)):
        raise HTTPException(status_code=503, detail="strm_playback_unavailable")
    allowed_library_ids = (
        context.library_ids
        if context.via_bearer and context.library_ids
        else None
    )
    outcome = await gateway.resolve(
        playback_request,
        gate=PlaybackGate(
            enabled=bool(getattr(request.app.state, "strm_playback_enabled", False)),
            contract_verified=bool(
                getattr(request.app.state, "strm_playback_contract_verified", False)
            ),
        ),
        allowed_library_ids=allowed_library_ids,
    )
    if outcome.status is not PlaybackStatus.READY or outcome.url is None:
        raise _playback_error(outcome.status)
    return await _proxy_playback(request, playback_request, outcome.url, outcome.request_headers)


async def _proxy_playback(
    request: Request,
    playback_request,
    upstream_url: str,
    upstream_headers: tuple[tuple[str, str], ...],
) -> Response:
    headers = {name: value for name, value in upstream_headers}
    if playback_request.method.value == "GET" and playback_request.byte_range is not None:
        headers["Range"] = _range_header(playback_request.byte_range)
    client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
    try:
        upstream = await client.send(
            client.build_request(playback_request.method.value, upstream_url, headers=headers),
            stream=True,
        )
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(status_code=504, detail="playback_timeout") from None
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="playback_remote_failed") from None
    if upstream.status_code not in {200, 206, 404, 410, 416}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="playback_remote_failed")
    if upstream.status_code in {404, 410}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=404, detail="playback_file_not_found")
    response_headers = _allowed_upstream_headers(upstream.headers)
    if playback_request.method.value == "HEAD":
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=upstream.status_code, headers=response_headers)

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=response_headers
    )


def _playback_error(status: PlaybackStatus) -> HTTPException:
    if status is PlaybackStatus.NOT_FOUND:
        return HTTPException(status_code=404, detail="playback_file_not_found")
    if status is PlaybackStatus.UNCERTAIN:
        return HTTPException(status_code=504, detail="playback_timeout")
    if status is PlaybackStatus.DISABLED:
        return HTTPException(status_code=503, detail="strm_playback_disabled")
    if status is PlaybackStatus.UNVERIFIED:
        return HTTPException(status_code=503, detail="strm_playback_unverified")
    return HTTPException(status_code=502, detail="playback_remote_failed")


def _range_header(byte_range) -> str:
    start = "" if byte_range.start is None else str(byte_range.start)
    end = "" if byte_range.end is None else str(byte_range.end)
    return f"bytes={start}-{end}"


def _allowed_upstream_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "accept-ranges",
        "cache-control",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
    }
    return {key: value for key, value in headers.items() if key.casefold() in allowed}


def _require_playback_network(request: Request) -> None:
    client = request.client
    if client is None:
        raise HTTPException(status_code=403, detail="playback_network_forbidden")
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        raise HTTPException(status_code=403, detail="playback_network_forbidden") from None
    networks: Collection[object] = getattr(
        request.app.state, "strm_playback_allowed_networks", ()
    )
    if not any(address in network for network in networks):
        raise HTTPException(status_code=403, detail="playback_network_forbidden")


__all__ = ["router"]
