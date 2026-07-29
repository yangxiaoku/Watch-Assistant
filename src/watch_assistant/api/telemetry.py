"""Authenticated, bounded client-side performance telemetry."""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import PositiveInt

from watch_assistant.schemas import (
    MediaDetailMetricRequest,
    MediaDetailMetricResponse,
    MediaType,
)
from watch_assistant.security import require_api_auth
from watch_assistant.services.settings import SettingsService

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


def get_settings_service(request: Request) -> SettingsService:
    return request.app.state.settings_service


SettingsDependency = Annotated[SettingsService, Depends(get_settings_service)]


def _redacted_media_id(media_type: MediaType, tmdb_id: int) -> str:
    digest = hashlib.sha256(f"{media_type.value}:{tmdb_id}".encode("ascii")).hexdigest()
    return digest[:16]


@router.post(
    "/media/{media_type}/{tmdb_id}/performance",
    response_model=MediaDetailMetricResponse,
)
async def record_media_detail_metric(
    media_type: MediaType,
    tmdb_id: PositiveInt,
    payload: MediaDetailMetricRequest,
    settings: SettingsDependency,
    request: Request,
) -> MediaDetailMetricResponse:
    if payload.stage == "late_response":
        event = "media.detail.race_dropped"
    elif payload.stage == "request_cancelled":
        event = "media.detail.request_cancelled"
    else:
        event = "media.detail.performance"
    fields: dict[str, object] = {
        "media_type": media_type.value,
        "stage": payload.stage,
        "status": payload.status,
        "cached": payload.cached,
    }
    if payload.season_number is not None:
        fields["season"] = payload.season_number
    if payload.error_code is not None:
        fields["error_code"] = payload.error_code
    await settings.log_event(
        event,
        fields=fields,
        duration_ms=payload.duration_ms,
        error_code=payload.error_code,
        request_id=getattr(request.state, "request_id", None),
        correlation_id=getattr(request.state, "correlation_id", None),
        resource_type="media_detail",
        resource_id=_redacted_media_id(media_type, tmdb_id),
    )
    return MediaDetailMetricResponse()
