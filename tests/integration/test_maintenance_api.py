from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI

from watch_assistant.api.maintenance import router
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import CacheWarmState, MovieWatch, SourceReliability
from watch_assistant.schemas import MediaIdentity, MediaType
from watch_assistant.security import require_api_auth
from watch_assistant.services.maintenance import MaintenanceService


class FakeCacheWarmer:
    def __init__(self) -> None:
        self.is_running = False
        self.retried: list[MediaIdentity] = []

    def next_run_at(self) -> datetime:
        return datetime(2026, 7, 26, tzinfo=UTC)

    async def retry_failed(self, failed_media: list[MediaIdentity]) -> None:
        self.retried = failed_media


async def test_maintenance_preserves_media_type_for_watches_and_retries(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'maintenance.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    failed = (
        '[{"media_type":"movie","tmdb_id":303},'
        '{"media_type":"tv","tmdb_id":303}]'
    )
    async with database.session_factory() as session:
        session.add_all(
            [
                CacheWarmState(
                    id="home",
                    last_started_at=now - timedelta(minutes=5),
                    last_completed_at=now,
                    total_count=3,
                    success_count=1,
                    failure_count=2,
                    failed_ids_json=failed,
                ),
                MovieWatch(
                    media_type=MediaType.MOVIE,
                    tmdb_id=404,
                    title="movie watch",
                    active=True,
                    first_empty_at=now,
                    last_checked_at=now,
                ),
                MovieWatch(
                    media_type=MediaType.TV,
                    tmdb_id=404,
                    title="tv watch",
                    active=True,
                    first_empty_at=now,
                    last_checked_at=now,
                ),
                SourceReliability(
                    source="plugin:noisy",
                    accepted_count=2,
                    rejected_count=8,
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_auth] = lambda: None
    app.state.maintenance_service = MaintenanceService(database.session_factory)
    warmer = FakeCacheWarmer()
    app.state.cache_warmer = warmer

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        status = await client.get("/api/v1/cache/status")
        watches = await client.get("/api/v1/watchlist")
        sources = await client.get("/api/v1/sources/reliability")
        retry = await client.post("/api/v1/cache/retry")

    assert status.status_code == 200
    assert status.json()["failed_media"] == [
        {"media_type": "movie", "tmdb_id": 303},
        {"media_type": "tv", "tmdb_id": 303},
    ]
    assert {(item["media_type"], item["tmdb_id"]) for item in watches.json()} == {
        ("movie", 404),
        ("tv", 404),
    }
    assert sources.json()[0]["penalty"] == 24
    assert retry.status_code == 202
    assert retry.json() == {"scheduled": True, "count": 2}
    assert [(item.media_type, item.tmdb_id) for item in warmer.retried] == [
        (MediaType.MOVIE, 303),
        (MediaType.TV, 303),
    ]
    await database.engine.dispose()


async def test_retry_rejects_when_cache_warm_is_running(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'running.db'}")
    await initialize_database(database.engine)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_auth] = lambda: None
    app.state.maintenance_service = MaintenanceService(database.session_factory)
    warmer = FakeCacheWarmer()
    warmer.is_running = True
    app.state.cache_warmer = warmer

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.post("/api/v1/cache/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "cache_warm_running"
    await database.engine.dispose()
