"""Authenticated, side-effect-free subtitle analysis routes."""

from fastapi import APIRouter, Depends, HTTPException

from watch_assistant.schemas import SubtitleAnalyzeRequest, SubtitleAnalyzeResponse
from watch_assistant.security import require_api_auth
from watch_assistant.services.subtitle_analysis import (
    SubtitleAnalysisError,
    analyze_subtitles,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])


@router.post("/subtitles/analyze", response_model=SubtitleAnalyzeResponse)
async def analyze_subtitle_names(
    payload: SubtitleAnalyzeRequest,
) -> SubtitleAnalyzeResponse:
    try:
        return analyze_subtitles(payload.video_name, payload.subtitle_names)
    except SubtitleAnalysisError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc


__all__ = ["router"]
