"""Daily background warming for movie and TV catalogs."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.db import cleanup_expired
from watch_assistant.models import CacheWarmState
from watch_assistant.schemas import (
    HomeCatalogResponse,
    MediaIdentity,
    MediaType,
    MovieMetadata,
)
from watch_assistant.services.search import SearchService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmRun:
    total: int
    succeeded: int
    failed: int
    skipped: int
    failed_media: tuple[MovieMetadata, ...]


class CacheWarmer:
    def __init__(
        self,
        search_service: SearchService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone_name: str = "Asia/Hong_Kong",
        retry_delays: tuple[float, ...] = (1800, 3600, 7200),
        concurrency: int = 3,
    ) -> None:
        self._search = search_service
        self._session_factory = session_factory
        self._timezone = ZoneInfo(timezone_name)
        self._retry_delays = retry_delays
        self._concurrency = max(1, concurrency)
        self._run_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._run_lock.locked()

    def next_run_at(self) -> datetime:
        now = datetime.now(UTC)
        return now + timedelta(
            seconds=seconds_until_next_midnight(now, self._timezone)
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = datetime.now(UTC)
            since = local_midnight(now, self._timezone)
            await self._warm_with_retries(since, stop_event)
            if await _wait_or_stop(
                stop_event,
                seconds_until_next_midnight(datetime.now(UTC), self._timezone),
            ):
                return

    async def warm_once(self, *, since: datetime) -> WarmRun:
        async with self._run_lock:
            return await self._warm_once(since=since)

    async def retry_failed(self, failed_media: list[MediaIdentity]) -> WarmRun:
        async with self._run_lock:
            catalog = await self._search.get_home_catalog()
            media = await self._catalog_and_watches(catalog)
            failed_identities = {
                (item.media_type, item.tmdb_id) for item in failed_media
            }
            selected = [
                item for item in media if _identity(item) in failed_identities
            ]
            await self._record_started(len(selected))
            failed = await self._warm_media(selected)
            run = WarmRun(
                total=len(selected),
                succeeded=len(selected) - len(failed),
                failed=len(failed),
                skipped=0,
                failed_media=tuple(failed),
            )
            await self._cleanup()
            await self._record_completed(run)
            return run

    async def _warm_once(self, *, since: datetime) -> WarmRun:
        started = monotonic()
        catalog = await self._search.get_home_catalog()
        media = await self._catalog_and_watches(catalog)
        await self._record_started(len(media))
        pending: list[MovieMetadata] = []
        skipped = 0
        for item in media:
            if await self._search.has_cache_since(
                item.tmdb_id, item.media_type, since
            ):
                skipped += 1
            else:
                pending.append(item)
        failed = await self._warm_media(pending)
        await self._cleanup()
        run = WarmRun(
            total=len(media),
            succeeded=len(pending) - len(failed),
            failed=len(failed),
            skipped=skipped,
            failed_media=tuple(failed),
        )
        await self._record_completed(run)
        logger.info(
            "cache warm completed",
            extra={
                "media_count": run.total,
                "success_count": run.succeeded,
                "failure_count": run.failed,
                "skipped_count": run.skipped,
                "elapsed_seconds": round(monotonic() - started, 3),
            },
        )
        return run

    async def _warm_with_retries(
        self,
        since: datetime,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            run = await self.warm_once(since=since)
        except Exception:
            logger.exception("cache warm catalog request failed")
            return
        failed = list(run.failed_media)
        for delay in self._retry_delays:
            if not failed or await _wait_or_stop(stop_event, delay):
                return
            async with self._run_lock:
                failed = await self._warm_media(failed)
                run = WarmRun(
                    total=run.total,
                    succeeded=run.total - run.skipped - len(failed),
                    failed=len(failed),
                    skipped=run.skipped,
                    failed_media=tuple(failed),
                )
                await self._record_completed(run)
        if failed:
            logger.warning(
                "cache warm retries exhausted",
                extra={"failure_count": len(failed)},
            )

    async def _warm_media(
        self, media: list[MovieMetadata]
    ) -> list[MovieMetadata]:
        failed: list[MovieMetadata] = []
        queue: asyncio.Queue[MovieMetadata] = asyncio.Queue()
        for item in media:
            queue.put_nowait(item)

        async def worker() -> None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    try:
                        refreshed = await self._search.warm_media(item)
                    except Exception:
                        logger.exception(
                            "cache warm media failed",
                            extra={
                                "media_type": item.media_type.value,
                                "tmdb_id": item.tmdb_id,
                            },
                        )
                        refreshed = False
                    if not refreshed:
                        failed.append(item)
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self._concurrency, len(media)))
        ]
        if workers:
            await asyncio.gather(*workers)
        failed_ids = {_identity(item) for item in failed}
        return [item for item in media if _identity(item) in failed_ids]

    async def _catalog_and_watches(
        self, catalog: HomeCatalogResponse
    ) -> list[MovieMetadata]:
        media = {_identity(item): item for item in _dedupe_catalog(catalog)}
        for item in await self._search.list_active_watches():
            media.setdefault(_identity(item), item)
        return list(media.values())

    async def _record_started(self, total: int) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            state = await session.get(CacheWarmState, "home")
            if state is None:
                state = CacheWarmState(id="home")
                session.add(state)
            state.last_started_at = now
            state.total_count = total
            state.updated_at = now
            await session.commit()

    async def _record_completed(self, run: WarmRun) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            state = await session.get(CacheWarmState, "home")
            if state is None:
                state = CacheWarmState(id="home", last_started_at=now)
                session.add(state)
            state.last_completed_at = now
            state.total_count = run.total
            state.success_count = run.succeeded
            state.failure_count = run.failed
            state.skipped_count = run.skipped
            state.failed_ids_json = json.dumps(
                [
                    {
                        "media_type": item.media_type.value,
                        "tmdb_id": item.tmdb_id,
                    }
                    for item in run.failed_media
                ]
            )
            state.updated_at = now
            await session.commit()

    async def _cleanup(self) -> None:
        async with self._session_factory() as session:
            await cleanup_expired(session)
            await session.commit()


def local_midnight(now: datetime, timezone: ZoneInfo) -> datetime:
    local = now.astimezone(timezone)
    midnight = datetime.combine(local.date(), time.min, tzinfo=timezone)
    return midnight.astimezone(UTC)


def seconds_until_next_midnight(now: datetime, timezone: ZoneInfo) -> float:
    local = now.astimezone(timezone)
    next_date = local.date() + timedelta(days=1)
    next_midnight = datetime.combine(next_date, time.min, tzinfo=timezone)
    return max(0.0, (next_midnight - local).total_seconds())


def _dedupe_catalog(catalog: HomeCatalogResponse) -> list[MovieMetadata]:
    media: dict[tuple[MediaType, int], MovieMetadata] = {}
    for section in (
        catalog.popular,
        catalog.now_playing,
        catalog.upcoming,
        catalog.top_rated,
        catalog.tv_popular,
        catalog.tv_on_the_air,
        catalog.tv_top_rated,
    ):
        for item in section:
            media.setdefault(_identity(item), item)
    return list(media.values())


def _identity(media: MovieMetadata) -> tuple[MediaType, int]:
    return media.media_type, media.tmdb_id


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True
