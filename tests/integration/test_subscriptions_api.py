from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from tests.unit.factories import make_security_manager
from watch_assistant.adapters.pansou import PanSouClient
from watch_assistant.adapters.tmdb import TmdbClient
from watch_assistant.api.subscriptions import get_subscription_service
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import SubscriptionResourceObservationResponse
from watch_assistant.services.subscriptions import SubscriptionNotFound


class _ObservationService:
    async def list_observations(self, subscription_id: str, *, limit: int):
        if subscription_id == "missing":
            raise SubscriptionNotFound(subscription_id)
        return [
            SubscriptionResourceObservationResponse(
                resource_id="res_observed",
                first_seen_at=datetime(2026, 7, 29, tzinfo=UTC),
                last_seen_at=datetime(2026, 7, 29, tzinfo=UTC),
                seen_count=2,
            )
        ][:limit]


@pytest.mark.integration
async def test_subscription_observations_api_handles_paging_and_missing_subscription(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions-api.db'}")
    await initialize_database(database.engine)
    tmdb = TmdbClient("unused")
    pansou = PanSouClient("http://pansou.test")
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb,
        pansou_client=pansou,
    security_manager=make_security_manager(),
    )
    app.dependency_overrides[get_subscription_service] = lambda: _ObservationService()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    try:
        listed = await client.get(
            "/api/v1/subscriptions/sub_demo/observations", params={"limit": 1}
        )
        assert listed.status_code == 200
        assert listed.json()[0]["resource_id"] == "res_observed"

        invalid = await client.get(
            "/api/v1/subscriptions/sub_demo/observations", params={"limit": 0}
        )
        assert invalid.status_code == 422

        missing = await client.get("/api/v1/subscriptions/missing/observations")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "subscription_not_found"
    finally:
        await client.aclose()
        await tmdb.aclose()
        await pansou.aclose()
        await database.engine.dispose()
