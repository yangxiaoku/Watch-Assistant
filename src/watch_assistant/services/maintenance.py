"""Read models used by maintenance and diagnostics APIs."""

import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import CacheWarmState, MovieWatch, SourceReliability
from watch_assistant.schemas import (
    MediaIdentity,
    MovieWatchSummary,
    SourceReliabilitySummary,
)


class MaintenanceService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_warm_state(self) -> CacheWarmState | None:
        async with self._session_factory() as session:
            return await session.get(CacheWarmState, "home")

    async def failed_media(self) -> list[MediaIdentity]:
        state = await self.get_warm_state()
        if state is None:
            return []
        return _media_identities(state.failed_ids_json)

    async def list_watches(self, *, active: bool | None) -> list[MovieWatchSummary]:
        async with self._session_factory() as session:
            statement = select(MovieWatch).order_by(MovieWatch.last_checked_at.desc())
            if active is not None:
                statement = statement.where(MovieWatch.active.is_(active))
            rows = await session.scalars(statement)
            return [MovieWatchSummary.model_validate(item) for item in rows]

    async def list_sources(self) -> list[SourceReliabilitySummary]:
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(SourceReliability)))
        summaries = [
            SourceReliabilitySummary(
                source=item.source,
                accepted_count=item.accepted_count,
                rejected_count=item.rejected_count,
                link_ok_count=item.link_ok_count,
                link_bad_count=item.link_bad_count,
                penalty=_source_penalty(item),
            )
            for item in rows
        ]
        return sorted(
            summaries,
            key=lambda item: (item.penalty, item.rejected_count + item.link_bad_count),
            reverse=True,
        )


def _media_identities(value: str) -> list[MediaIdentity]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    identities: list[MediaIdentity] = []
    for item in parsed:
        try:
            identities.append(MediaIdentity.model_validate(item))
        except ValidationError:
            continue
    return identities


def _source_penalty(source: SourceReliability) -> int:
    total = (
        source.accepted_count
        + source.rejected_count
        + source.link_ok_count
        + source.link_bad_count
    )
    if total < 10:
        return 0
    bad = source.rejected_count + source.link_bad_count
    return min(30, round(30 * bad / total))
