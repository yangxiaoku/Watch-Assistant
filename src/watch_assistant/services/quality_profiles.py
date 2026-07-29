"""Versioned quality rules and side-effect-free resource simulation."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import QualityProfile, Resource
from watch_assistant.schemas import (
    QualityProfileCreateRequest,
    QualityProfilePatch,
    QualityProfileResponse,
    QualitySimulationItem,
    QualitySimulationResponse,
)
from watch_assistant.services.observability import EventLogger, emit_event

_ALLOWED_RULES = frozenset({"hard", "prefer"})
_ALLOWED_GROUP_RULES = {
    "hard": frozenset({"min_resolution", "allowed_sources"}),
    "prefer": frozenset({"resolution", "source", "codec"}),
}
_RESOLUTION = re.compile(r"(?<!\d)(2160|1440|1080|720|480)[pi]?", re.IGNORECASE)
_SOURCE = re.compile(r"\b(REMUX|BLURAY|WEB[- .]?DL|WEBRIP|HDTV)\b", re.IGNORECASE)
_CODEC = re.compile(r"\b(AV1|HEVC|H\.265|H265|H\.264|H264)\b", re.IGNORECASE)


class QualityProfileNotFound(LookupError):
    pass


class QualityProfileConflict(ValueError):
    pass


class QualityProfileValidationError(ValueError):
    pass


class QualityProfileService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_logger = event_logger

    async def create(
        self, request: QualityProfileCreateRequest
    ) -> QualityProfileResponse:
        rules = _validate_rules(request.rules)
        now = datetime.now(UTC)
        item = QualityProfile(
            id="quality_" + uuid4().hex,
            name=request.name,
            scope=request.scope,
            scope_key=request.scope_key,
            rules_json=json.dumps(rules, ensure_ascii=False, separators=(",", ":")),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(item)
            await session.commit()
        await emit_event(
            self._event_logger,
            "quality_profile.created",
            fields={"status": "active"},
            resource_type="quality_profile",
            resource_id=item.id,
        )
        return _response(item)

    async def list(self) -> list[QualityProfileResponse]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(QualityProfile).order_by(QualityProfile.updated_at.desc())
            )
            return [_response(row) for row in rows]

    async def get(self, profile_id: str) -> QualityProfileResponse:
        async with self._session_factory() as session:
            item = await session.get(QualityProfile, profile_id)
            if item is None:
                raise QualityProfileNotFound(profile_id)
            return _response(item)

    async def update(
        self, profile_id: str, patch: QualityProfilePatch
    ) -> QualityProfileResponse:
        async with self._session_factory() as session:
            item = await session.get(QualityProfile, profile_id)
            if item is None:
                raise QualityProfileNotFound(profile_id)
            if item.revision != patch.revision:
                raise QualityProfileConflict("quality_profile_conflict")
            if patch.name is not None:
                item.name = patch.name
            if patch.rules is not None:
                item.rules_json = json.dumps(
                    _validate_rules(patch.rules),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
            response = _response(item)
        await emit_event(
            self._event_logger,
            "quality_profile.changed",
            fields={"status": "active"},
            resource_type="quality_profile",
            resource_id=response.id,
        )
        return response

    async def simulate(
        self, profile_id: str, resource_ids: list[str]
    ) -> QualitySimulationResponse:
        async with self._session_factory() as session:
            profile = await session.get(QualityProfile, profile_id)
            if profile is None:
                raise QualityProfileNotFound(profile_id)
            resources = list(
                await session.scalars(
                    select(Resource).where(Resource.id.in_(resource_ids))
                )
            )
        by_id = {resource.id: resource for resource in resources}
        rules = _decode_rules(profile.rules_json)
        items = [
            _evaluate(resource_id, by_id.get(resource_id), rules)
            for resource_id in resource_ids
        ]
        return QualitySimulationResponse(profile=_response(profile), items=items)


def _validate_rules(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value) <= _ALLOWED_RULES:
        raise QualityProfileValidationError("invalid_quality_rules")
    normalized: dict[str, object] = {}
    for group in _ALLOWED_RULES:
        raw = value.get(group, {})
        if not isinstance(raw, dict):
            raise QualityProfileValidationError("invalid_quality_rules")
        if not set(raw) <= _ALLOWED_GROUP_RULES[group]:
            raise QualityProfileValidationError("unknown_quality_rule")
        normalized[group] = dict(raw)
    hard = normalized["hard"]
    if not isinstance(hard, dict):
        raise QualityProfileValidationError("invalid_quality_rules")
    if "min_resolution" in hard and (
        not isinstance(hard["min_resolution"], int)
        or hard["min_resolution"] not in {480, 720, 1080, 1440, 2160}
    ):
        raise QualityProfileValidationError("invalid_min_resolution")
    return normalized


def _decode_rules(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {"hard": {}, "prefer": {}}
    return _validate_rules(decoded) if isinstance(decoded, dict) else {"hard": {}, "prefer": {}}


def _evaluate(
    resource_id: str, resource: Resource | None, rules: dict[str, object]
) -> QualitySimulationItem:
    if resource is None:
        return QualitySimulationItem(
            resource_id=resource_id, eligible=False, score=0, reasons=["resource_not_found"]
        )
    hard = rules.get("hard", {})
    prefer = rules.get("prefer", {})
    if not isinstance(hard, dict) or not isinstance(prefer, dict):
        return QualitySimulationItem(
            resource_id=resource_id, eligible=False, score=0, reasons=["invalid_quality_rules"]
        )
    resolution_match = _RESOLUTION.search(resource.name)
    resolution = int(resolution_match.group(1)) if resolution_match else None
    source_match = _SOURCE.search(resource.name)
    source = source_match.group(1).replace(" ", "-").upper() if source_match else None
    reasons: list[str] = []
    eligible = True
    minimum = hard.get("min_resolution")
    if isinstance(minimum, int) and (resolution is None or resolution < minimum):
        eligible = False
        reasons.append("min_resolution_not_met" if resolution is not None else "resolution_unknown")
    allowed_sources = hard.get("allowed_sources")
    if isinstance(allowed_sources, list) and allowed_sources:
        normalized = {str(item).upper().replace(" ", "-") for item in allowed_sources}
        if source is None:
            eligible = False
            reasons.append("source_unknown")
        elif source not in normalized:
            eligible = False
            reasons.append("source_not_allowed")
    score = 50
    preferred_resolution = prefer.get("resolution")
    if isinstance(preferred_resolution, int) and resolution == preferred_resolution:
        score += 25
        reasons.append("preferred_resolution")
    preferred_source = prefer.get("source")
    if isinstance(preferred_source, str) and source == preferred_source.upper():
        score += 15
        reasons.append("preferred_source")
    if resource.seeders is not None and resource.seeders > 0:
        score += 10
    return QualitySimulationItem(
        resource_id=resource_id,
        eligible=eligible,
        score=min(100, score),
        reasons=reasons,
    )


def _response(item: QualityProfile) -> QualityProfileResponse:
    return QualityProfileResponse(
        id=item.id,
        name=item.name,
        scope=item.scope,
        scope_key=item.scope_key,
        rules=_decode_rules(item.rules_json),
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
