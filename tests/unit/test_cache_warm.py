import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import CacheWarmState
from watch_assistant.schemas import (
    HomeCatalogResponse,
    MediaIdentity,
    MediaType,
    MovieMetadata,
)
from watch_assistant.services.cache_warm import (
    CacheWarmer,
    local_midnight,
    seconds_until_next_midnight,
)


def _media(tmdb_id: int, media_type: MediaType, title: str) -> MovieMetadata:
    return MovieMetadata(tmdb_id=tmdb_id, media_type=media_type, title=title)


class FakeSearchService:
    def __init__(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        watched: list[MovieMetadata] | None = None,
    ) -> None:
        self.warmed: list[tuple[MediaType, int]] = []
        self.attempts: dict[tuple[MediaType, int], int] = {}
        self.current = {(MediaType.MOVIE, 1)}
        self.stop_event = stop_event
        self.watched = watched or []

    async def get_home_catalog(self) -> HomeCatalogResponse:
        if self.stop_event is not None:
            self.stop_event.set()
        return HomeCatalogResponse(
            popular=[_media(1, MediaType.MOVIE, "movie popular")],
            now_playing=[_media(2, MediaType.MOVIE, "movie now")],
            upcoming=[_media(3, MediaType.MOVIE, "movie upcoming")],
            top_rated=[_media(4, MediaType.MOVIE, "movie top")],
            tv_popular=[_media(1, MediaType.TV, "tv popular")],
            tv_on_the_air=[_media(2, MediaType.TV, "tv on air")],
            tv_top_rated=[_media(3, MediaType.TV, "tv top")],
        )

    async def has_cache_since(
        self,
        tmdb_id: int,
        media_type: MediaType,
        since: datetime,
    ) -> bool:
        return (media_type, tmdb_id) in self.current

    async def list_active_watches(self) -> list[MovieMetadata]:
        return self.watched

    async def warm_media(self, media: MovieMetadata) -> bool:
        identity = (media.media_type, media.tmdb_id)
        self.warmed.append(identity)
        self.attempts[identity] = self.attempts.get(identity, 0) + 1
        return identity != (MediaType.TV, 3) or self.attempts[identity] > 1


async def test_cache_warmer_uses_all_sections_and_composite_media_identity(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'warm.db'}")
    await initialize_database(database.engine)
    search = FakeSearchService()
    warmer = CacheWarmer(search, database.session_factory, retry_delays=())

    run = await warmer.warm_once(since=datetime(2026, 7, 24, tzinfo=UTC))

    assert run.total == 7
    assert run.skipped == 1
    assert run.succeeded == 5
    assert run.failed == 1
    assert (MediaType.TV, 1) in search.warmed
    assert (MediaType.MOVIE, 1) not in search.warmed
    async with database.session_factory() as session:
        state = await session.scalar(select(CacheWarmState))
    assert json.loads(state.failed_ids_json) == [{"media_type": "tv", "tmdb_id": 3}]
    await database.engine.dispose()


async def test_cache_warmer_includes_media_typed_watches(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    await initialize_database(database.engine)
    watched = _media(4, MediaType.TV, "watched tv")
    search = FakeSearchService(watched=[watched])
    warmer = CacheWarmer(search, database.session_factory, retry_delays=())

    run = await warmer.warm_once(since=datetime(2026, 7, 24, tzinfo=UTC))

    assert run.total == 8
    assert (MediaType.TV, 4) in search.warmed
    await database.engine.dispose()


async def test_retry_failed_selects_exact_media_identity(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    await initialize_database(database.engine)
    search = FakeSearchService()
    warmer = CacheWarmer(search, database.session_factory, retry_delays=())

    run = await warmer.retry_failed([MediaIdentity(media_type=MediaType.TV, tmdb_id=1)])

    assert run.total == 1
    assert search.warmed == [(MediaType.TV, 1)]
    await database.engine.dispose()


async def test_cache_warmer_retries_only_failed_media(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'retry-loop.db'}")
    await initialize_database(database.engine)
    search = FakeSearchService()
    warmer = CacheWarmer(search, database.session_factory, retry_delays=(0,))

    await warmer._warm_with_retries(datetime(2026, 7, 24, tzinfo=UTC), asyncio.Event())

    assert search.warmed.count((MediaType.TV, 3)) == 2
    assert search.warmed.count((MediaType.MOVIE, 2)) == 1
    await database.engine.dispose()


async def test_cache_warmer_stops_without_waiting_until_midnight(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'stop.db'}")
    await initialize_database(database.engine)
    stop_event = asyncio.Event()
    warmer = CacheWarmer(
        FakeSearchService(stop_event=stop_event),
        database.session_factory,
        retry_delays=(),
    )

    await asyncio.wait_for(warmer.run_forever(stop_event), timeout=1)

    await database.engine.dispose()


async def test_cache_warmer_uses_bounded_concurrency_and_continues_failures(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'parallel.db'}")
    await initialize_database(database.engine)

    class DelayedSearchService:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.warmed: list[int] = []

        async def get_home_catalog(self) -> HomeCatalogResponse:
            return HomeCatalogResponse(
                popular=[
                    _media(index, MediaType.MOVIE, f"movie {index}")
                    for index in range(1, 10)
                ],
                now_playing=[],
                upcoming=[],
                top_rated=[],
                tv_popular=[],
                tv_on_the_air=[],
                tv_top_rated=[],
            )

        async def has_cache_since(
            self, tmdb_id: int, media_type: MediaType, since: datetime
        ) -> bool:
            return False

        async def list_active_watches(self) -> list[MovieMetadata]:
            return []

        async def warm_media(self, media: MovieMetadata) -> bool:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.warmed.append(media.tmdb_id)
            await asyncio.sleep(0.03)
            self.active -= 1
            if media.tmdb_id == 4:
                raise RuntimeError("database write failed")
            return media.tmdb_id != 5

    search = DelayedSearchService()
    warmer = CacheWarmer(
        search,
        database.session_factory,
        retry_delays=(),
        concurrency=3,
    )
    started = monotonic()

    run = await warmer.warm_once(since=datetime(2026, 7, 24, tzinfo=UTC))

    elapsed = monotonic() - started
    assert search.max_active == 3
    assert len(search.warmed) == 9
    assert run.failed == 2
    assert {item.tmdb_id for item in run.failed_media} == {4, 5}
    assert elapsed < 0.2
    await database.engine.dispose()


def test_midnight_helpers_use_hong_kong_timezone():
    timezone = ZoneInfo("Asia/Hong_Kong")
    now = datetime(2026, 7, 24, 15, 30, tzinfo=UTC)

    assert local_midnight(now, timezone) == datetime(2026, 7, 23, 16, 0, tzinfo=UTC)
    assert seconds_until_next_midnight(now, timezone) == 1800
