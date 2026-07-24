import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select, text

from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.qbittorrent import (
    InspectionStatus,
    QbittorrentInspectionResult,
)
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import InspectionBatch, InspectionItem, Resource
from watch_assistant.schemas import (
    InspectionBatchStatus,
    InspectionItemStatus,
    ResourceKind,
)
from watch_assistant.security import SecurityManager
from watch_assistant.services.inspection import InspectionService, InspectionWorker

WEB_PASSWORD = "inspection-web-password"
SCRIPT_TOKEN = "inspection-script-token"


def _magnet(letter: str) -> str:
    return f"magnet:?xt=urn:btih:{letter * 40}&dn=private-test-{letter}"


def _result(
    infohash: str | None,
    status: InspectionStatus,
    *,
    error_code: str | None = None,
    total_size: int = 0,
) -> QbittorrentInspectionResult:
    return QbittorrentInspectionResult(
        infohash=infohash,
        status=status,
        total_size_bytes=total_size,
        file_count=3 if total_size else 0,
        video_file_count=1 if total_size else 0,
        video_size_bytes=total_size if total_size else 0,
        subtitle_count=1 if total_size else 0,
        sample_count=0,
        largest_video_name="movie.mkv" if total_size else None,
        content_summary="1 video(s), 1 subtitle(s), 0 suspicious file(s)"
        if total_size
        else None,
        error_code=error_code,
    )


class FakeInspectionClient:
    def __init__(
        self,
        results: dict[str, QbittorrentInspectionResult],
        *,
        dependency_error: bool = False,
        blocked_magnets: set[str] | None = None,
    ) -> None:
        self.results = results
        self.dependency_error = dependency_error
        self.blocked_magnets = blocked_magnets or set()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.magnets: list[str] = []
        self.closed = False

    async def ensure_available(self) -> None:
        if self.dependency_error:
            raise RuntimeError("qB connection details must not be exposed")

    async def inspect(self, magnets: list[str]) -> list[QbittorrentInspectionResult]:
        magnet = magnets[0]
        self.magnets.append(magnet)
        self.started.set()
        if magnet in self.blocked_magnets:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return [self.results[magnet]]

    async def aclose(self) -> None:
        self.closed = True


async def _make_app(
    tmp_path,
    *,
    client: FakeInspectionClient,
    authenticated: bool = False,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'inspection.db'}")
    await initialize_database(database.engine)
    crypto = SecretCrypto(Fernet.generate_key().decode("ascii"))
    magnets = {
        resource_id: _magnet(letter)
        for resource_id, letter in (("res_a", "a"), ("res_b", "b"), ("res_c", "c"))
    }
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add_all(
            [
                Resource(
                    id=resource_id,
                    kind=ResourceKind.MAGNET,
                    canonical_key=f"magnet:{resource_id}",
                    encrypted_url=crypto.encrypt(magnet),
                    name=resource_id,
                    source="test",
                    captured_at=now,
                    expires_at=now + timedelta(days=1),
                )
                for resource_id, magnet in magnets.items()
            ]
            + [
                Resource(
                    id="res_share",
                    kind=ResourceKind.SHARE,
                    canonical_key="share:inspection",
                    encrypted_url=crypto.encrypt("https://115.com/inspection"),
                    name="share",
                    source="test",
                    captured_at=now,
                    expires_at=now + timedelta(days=1),
                )
            ]
        )
        await session.commit()
    security = None
    if authenticated:
        password_hash = PasswordHash.recommended()
        security = SecurityManager(
            web_password_hash=password_hash.hash(WEB_PASSWORD),
            script_token_hash=password_hash.hash(SCRIPT_TOKEN),
        )
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=crypto,
        tmdb_client=tmdb,
        pansou_client=pansou,
        security_manager=security,
        qbittorrent_client=client,
        push_supported=False,
    )
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return app, http_client, database, tmdb, pansou, crypto, magnets


async def _close(http_client, database, tmdb, pansou):
    await http_client.aclose()
    await tmdb.aclose()
    await pansou.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inspection_post_requires_auth_csrf_and_rejects_the_entire_batch(tmp_path):
    fake = FakeInspectionClient({})
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake, authenticated=True
    )

    unauthorized = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_a"]}
    )
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    missing_csrf = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_a"]}
    )
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    duplicate = await client.post(
        "/api/v1/resources/inspect",
        json={"resource_ids": ["res_a", "res_a"]},
        headers=headers,
    )
    too_many = await client.post(
        "/api/v1/resources/inspect",
        json={"resource_ids": ["res_a"] * 31},
        headers=headers,
    )
    unknown = await client.post(
        "/api/v1/resources/inspect",
        json={"resource_ids": ["res_a", "res_missing"]},
        headers=headers,
    )
    share = await client.post(
        "/api/v1/resources/inspect",
        json={"resource_ids": ["res_share"]},
        headers=headers,
    )
    raw_magnet = await client.post(
        "/api/v1/resources/inspect",
        json={"resource_ids": ["res_a"], "magnet": _magnet("z")},
        headers=headers,
    )

    assert unauthorized.status_code == 401
    assert login.status_code == 200
    assert missing_csrf.status_code == 403
    assert all(response.status_code == 422 for response in (duplicate, too_many, unknown, share, raw_magnet))
    async with database.session_factory() as session:
        assert list(await session.scalars(select(InspectionBatch))) == []
    assert app.state.inspection_supported is True
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_inspection_response_is_queued_then_cumulative_and_ordered(tmp_path):
    fake = FakeInspectionClient(
        {
            _magnet("a"): _result("a" * 40, InspectionStatus.VERIFIED, total_size=11),
            _magnet("b"): _result("b" * 40, InspectionStatus.UNSUPPORTED, error_code="existing_torrent"),
            _magnet("c"): _result("c" * 40, InspectionStatus.TIMEOUT, error_code="metadata_timeout", total_size=999),
        },
        blocked_magnets={_magnet("c")},
    )
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )

    accepted = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_c", "res_a", "res_b"]}
    )
    initial = await client.get(f"/api/v1/resources/inspect/{accepted.json()['batch_id']}")
    worker_task = asyncio.create_task(app.state.inspection_worker.run_once())
    await asyncio.wait_for(fake.started.wait(), timeout=1)
    cumulative = None
    for _ in range(100):
        cumulative = await client.get(
            f"/api/v1/resources/inspect/{accepted.json()['batch_id']}"
        )
        if cumulative.json()["completed_count"] == 2:
            break
        await asyncio.sleep(0.01)
    fake.release.set()
    await worker_task
    final = await client.get(f"/api/v1/resources/inspect/{accepted.json()['batch_id']}")

    assert accepted.status_code == 202
    assert accepted.json() == {
        "batch_id": accepted.json()["batch_id"],
        "status": "queued",
        "submitted_count": 3,
        "completed_count": 0,
        "results": [],
    }
    assert initial.json()["completed_count"] == 0
    assert cumulative is not None
    assert cumulative.json()["completed_count"] == len(cumulative.json()["results"]) == 2
    assert [item["resource_id"] for item in cumulative.json()["results"]] == [
        "res_a",
        "res_b",
    ]
    assert final.json()["status"] == "partial"
    assert final.json()["completed_count"] == len(final.json()["results"]) == 3
    assert [item["resource_id"] for item in final.json()["results"]] == [
        "res_c",
        "res_a",
        "res_b",
    ]
    timeout = final.json()["results"][0]
    assert timeout["status"] == "timeout"
    assert {key: timeout[key] for key in ("total_size_bytes", "file_count", "video_file_count", "video_size_bytes", "subtitle_count", "sample_count")} == {
        "total_size_bytes": 0,
        "file_count": 0,
        "video_file_count": 0,
        "video_size_bytes": 0,
        "subtitle_count": 0,
        "sample_count": 0,
    }
    assert timeout["largest_video_name"] is None
    assert timeout["content_summary"] is None
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("results", "expected_status"),
    [
        (
            [
                _result("a" * 40, InspectionStatus.VERIFIED, total_size=10),
                _result("b" * 40, InspectionStatus.UNSUPPORTED, error_code="invalid_magnet"),
            ],
            "completed",
        ),
        (
            [
                _result("a" * 40, InspectionStatus.FAILED, error_code="api_unavailable"),
                _result("b" * 40, InspectionStatus.TIMEOUT, error_code="metadata_timeout"),
            ],
            "failed",
        ),
    ],
)
async def test_terminal_priority_for_item_derived_results(tmp_path, results, expected_status):
    fake = FakeInspectionClient({_magnet("a"): results[0], _magnet("b"): results[1]})
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )
    accepted = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_a", "res_b"]}
    )
    assert await app.state.inspection_worker.run_once()
    response = await client.get(f"/api/v1/resources/inspect/{accepted.json()['batch_id']}")

    assert response.json()["status"] == expected_status
    assert response.json()["completed_count"] == response.json()["submitted_count"] == 2
    assert len(response.json()["results"]) == 2
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_dependency_failure_is_a_batch_failure_without_results(tmp_path):
    fake = FakeInspectionClient({}, dependency_error=True)
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )
    accepted = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_a"]}
    )

    assert await app.state.inspection_worker.run_once()
    response = await client.get(f"/api/v1/resources/inspect/{accepted.json()['batch_id']}")

    assert response.json() == {
        "batch_id": accepted.json()["batch_id"],
        "status": "failed",
        "submitted_count": 1,
        "completed_count": 0,
        "results": [],
    }
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_lifespan_runs_the_single_worker_and_closes_the_qb_client(tmp_path):
    fake = FakeInspectionClient({})
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )

    async with app.router.lifespan_context(app):
        health = await client.get("/api/v1/health")
        assert health.json() == {
            "status": "ok",
            "push_supported": False,
            "inspection_supported": True,
        }
        assert fake.closed is False

    assert fake.closed is True
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_worker_recovers_only_unfinished_items_and_cleans_up_on_cancel(tmp_path):
    fake = FakeInspectionClient(
        {_magnet("a"): _result("a" * 40, InspectionStatus.VERIFIED, total_size=10)},
        blocked_magnets={_magnet("a")},
    )
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )
    service: InspectionService = app.state.inspection_service
    batch = await service.create(["res_a"])
    task = asyncio.create_task(app.state.inspection_worker.run_once())
    await asyncio.wait_for(fake.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.cancelled.is_set()

    recovered = InspectionWorker(database.session_factory, _crypto, fake)
    assert await recovered.recover_after_restart() == 1
    async with database.session_factory() as session:
        stored_batch = await session.get(InspectionBatch, batch.batch_id)
        stored_item = await session.get(InspectionItem, (batch.batch_id, "res_a"))
        assert stored_batch.status == InspectionBatchStatus.QUEUED
        assert stored_item.status == InspectionItemStatus.QUEUED

    fake.release.set()
    assert await recovered.run_once()
    complete = await service.get(batch.batch_id)
    assert complete.status == InspectionBatchStatus.COMPLETED
    assert complete.completed_count == len(complete.results) == 1
    await _close(client, database, tmdb, pansou)


@pytest.mark.integration
async def test_inspection_tables_and_logs_do_not_contain_plaintext_magnets(tmp_path, caplog):
    magnet = _magnet("a")
    fake = FakeInspectionClient(
        {magnet: _result("a" * 40, InspectionStatus.VERIFIED, total_size=10)}
    )
    app, client, database, tmdb, pansou, _crypto, _magnets = await _make_app(
        tmp_path, client=fake
    )
    accepted = await client.post(
        "/api/v1/resources/inspect", json={"resource_ids": ["res_a"]}
    )
    assert accepted.status_code == 202
    assert await app.state.inspection_worker.run_once()
    async with database.session_factory() as session:
        rows = await session.execute(
            text("SELECT * FROM inspection_batches JOIN inspection_items ON inspection_batches.id = inspection_items.batch_id")
        )
        stored = "\n".join(str(row) for row in rows)

    assert magnet not in stored
    assert magnet not in caplog.text
    await _close(client, database, tmdb, pansou)
