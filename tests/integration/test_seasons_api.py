import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryMediaIdentity,
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.schemas import SeasonDetailResponse, SeasonEpisodeMetadata
from watch_assistant.security import SecurityManager
from watch_assistant.services.season_metadata import SeasonMetadataService


class FakeTmdb:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []
        self.empty_primary = False

    async def get_season(
        self, series_id: int, season_number: int, *, language: str
    ) -> SeasonDetailResponse:
        self.calls.append((series_id, season_number, language))
        return SeasonDetailResponse(
            series_tmdb_id=series_id,
            tmdb_season_id=456,
            season_number=season_number,
            name="第 2 季",
            overview=(
                None
                if language == "zh-CN" and self.empty_primary
                else "本季简介"
                if language == "zh-CN"
                else "Fallback overview"
            ),
            overview_language=(
                None
                if language == "zh-CN" and self.empty_primary
                else language
            ),
            poster_path="/season.jpg",
            air_date="2025-01-01",
            episode_count=2,
            episodes=[
                SeasonEpisodeMetadata(
                    episode_number=1,
                    name="第一集",
                    air_date="2025-01-01",
                ),
                SeasonEpisodeMetadata(
                    episode_number=2,
                    name="第二集",
                    air_date="2025-01-02",
                ),
            ],
            fetched_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.integration
async def test_season_metadata_is_cached_and_exposes_fallback_contract(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'season.db'}")
    await initialize_database(database.engine)
    fake = FakeTmdb()
    fake.empty_primary = True
    service = SeasonMetadataService(database.session_factory, fake)  # type: ignore[arg-type]

    first = await service.get(1399, 2)
    second = await service.get(1399, 2)

    assert first.overview == "Fallback overview"
    assert first.overview_language == "en-US"
    assert first.warnings == ["season_overview_language_fallback"]
    assert first.cached is False
    assert second.cached is True
    assert fake.calls == [(1399, 2, "zh-CN"), (1399, 2, "en-US")]
    await database.engine.dispose()


@pytest.mark.integration
async def test_season_metadata_api_requires_auth_and_returns_independent_detail(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'season-api.db'}")
    await initialize_database(database.engine)
    password_hash = PasswordHash.recommended()
    fake = FakeTmdb()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=fake,  # type: ignore[arg-type]
        pansou_client=type("PanSou", (), {"aclose": lambda self: _noop()})(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash("season-password"),
            script_token_hash=password_hash.hash("season-token"),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        denied = await client.get("/api/v1/media/tv/1399/seasons/2")
        assert denied.status_code == 401
        login = await client.post(
            "/api/v1/auth/login", json={"password": "season-password"}
        )
        response = await client.get("/api/v1/media/tv/1399/seasons/2")

    assert login.status_code == 200
    assert response.status_code == 200
    assert response.json()["series_tmdb_id"] == 1399
    assert response.json()["season_number"] == 2
    assert response.json()["overview"] == "本季简介"
    assert "season-api.db" not in response.text
    await database.engine.dispose()


@pytest.mark.integration
async def test_episode_completeness_api_joins_confirmed_inventory_identities(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'completeness.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-tv",
                name="测试媒体库",
                root_directory_id="root-tv",
                enabled=True,
                scope_verified=True,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-tv",
                library_id="library-tv",
                root_directory_id="root-tv",
                idempotency_key="scan-tv-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=3,
                pages_read=1,
                items_seen=3,
                updated_at=now,
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="scan-tv",
                    object_type="file",
                    object_id="file-ep1-a",
                    parent_id="root-tv",
                    name="Show.S02E01.1080p.mkv",
                    path="Show.S02E01.1080p.mkv",
                    is_directory=False,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-tv",
                    object_type="file",
                    object_id="file-ep1-b",
                    parent_id="root-tv",
                    name="Show.S02E01.2160p.mkv",
                    path="Show.S02E01.2160p.mkv",
                    is_directory=False,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-tv",
                    object_type="file",
                    object_id="file-ep2",
                    parent_id="root-tv",
                    name="Show.S02E02.1080p.mkv",
                    path="Show.S02E02.1080p.mkv",
                    is_directory=False,
                ),
            ]
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-tv",
                page=1,
                items_seen=3,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-tv": 3},
                        "expected_total": 3,
                        "pending": [],
                        "visited": ["root-tv"],
                    }
                ),
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryMediaIdentity(
                    id="identity-ep1-a",
                    library_id="library-tv",
                    object_id="file-ep1-a",
                    tmdb_id=1399,
                    media_type="tv",
                    season=2,
                    episode_start=1,
                    episode_end=1,
                ),
                LibraryMediaIdentity(
                    id="identity-ep1-b",
                    library_id="library-tv",
                    object_id="file-ep1-b",
                    tmdb_id=1399,
                    media_type="tv",
                    season=2,
                    episode_start=1,
                    episode_end=1,
                ),
                LibraryMediaIdentity(
                    id="identity-ep2",
                    library_id="library-tv",
                    object_id="file-ep2",
                    tmdb_id=1399,
                    media_type="tv",
                    season=2,
                    episode_start=2,
                    episode_end=2,
                ),
            ]
        )
        await session.commit()

    password_hash = PasswordHash.recommended()
    fake = FakeTmdb()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=fake,  # type: ignore[arg-type]
        pansou_client=type("PanSou", (), {"aclose": lambda self: _noop()})(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash("completeness-password"),
            script_token_hash=password_hash.hash("completeness-token"),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": "completeness-password"}
        )
        response = await client.get(
            "/api/v1/libraries/library-tv/media/tv/1399/seasons/2/completeness"
        )
        async with database.session_factory() as session:
            current = await session.get(LibraryScanRun, "scan-tv")
            assert current is not None
            assert current.created_at is not None
            assert current.updated_at is not None
            session.add(
                LibraryScanRun(
                    id="scan-tv-requeued",
                    library_id="library-tv",
                    root_directory_id="root-tv",
                    idempotency_key="scan-tv-requeued-key",
                    state="queued",
                    complete=False,
                    snapshot_revision=None,
                    created_at=current.created_at - timedelta(minutes=1),
                    updated_at=current.updated_at + timedelta(minutes=1),
                )
            )
            await session.commit()
        requeued = await client.get(
            "/api/v1/libraries/library-tv/media/tv/1399/seasons/2/completeness"
        )

    assert login.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["freshness"]["status"] == "fresh"
    assert payload["inventory_complete"] is True
    assert payload["conclusion_available"] is True
    assert [item["status"] for item in payload["items"]] == ["multiple", "owned"]
    assert payload["duplicate_episodes"] == [1]
    assert payload["missing_episodes"] == []
    assert requeued.status_code == 200
    requeued_payload = requeued.json()
    assert requeued_payload["freshness"]["status"] == "incomplete"
    assert requeued_payload["inventory_complete"] is False
    assert requeued_payload["conclusion_available"] is False
    assert requeued_payload["missing_episodes"] == []
    await database.engine.dispose()


@pytest.mark.integration
async def test_episode_completeness_api_is_unknown_without_complete_scan(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'incomplete.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-empty",
                name="空媒体库",
                root_directory_id="root-empty",
                enabled=True,
                scope_verified=True,
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    fake = FakeTmdb()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=fake,  # type: ignore[arg-type]
        pansou_client=type("PanSou", (), {"aclose": lambda self: _noop()})(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash("incomplete-password"),
            script_token_hash=password_hash.hash("incomplete-token"),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": "incomplete-password"}
        )
        response = await client.get(
            "/api/v1/libraries/library-empty/media/tv/1399/seasons/2/completeness"
        )

    assert login.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["freshness"]["status"] == "incomplete"
    assert payload["inventory_complete"] is False
    assert payload["conclusion_available"] is False
    assert {item["status"] for item in payload["items"]} == {"unknown"}
    assert payload["missing_episodes"] == []

    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-missing-checkpoint",
                library_id="library-empty",
                root_directory_id="root-empty",
                idempotency_key="scan-missing-checkpoint-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                pages_read=0,
                items_seen=0,
                expected_total=0,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as missing_client:
        missing_login = await missing_client.post(
            "/api/v1/auth/login", json={"password": "incomplete-password"}
        )
        missing_evidence = await missing_client.get(
            "/api/v1/libraries/library-empty/media/tv/1399/seasons/2/completeness"
        )
    assert missing_login.status_code == 200
    assert missing_evidence.status_code == 200
    missing_payload = missing_evidence.json()
    assert missing_payload["freshness"]["status"] == "incomplete"
    assert missing_payload["inventory_complete"] is False
    assert missing_payload["conclusion_available"] is False
    assert {item["status"] for item in missing_payload["items"]} == {"unknown"}
    assert missing_payload["missing_episodes"] == []
    await database.engine.dispose()


async def _noop():
    return None
