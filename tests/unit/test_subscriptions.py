from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource
from watch_assistant.schemas import (
    MediaType,
    ResourceKind,
    SubscriptionCreateRequest,
    SubscriptionMutationRequest,
    SubscriptionStatus,
)
from watch_assistant.services.subscriptions import (
    SubscriptionConflict,
    SubscriptionNotFound,
    SubscriptionService,
)


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.resource_ids = ["res_match"]

    async def search(self, tmdb_id, *, media_type, refresh, season_number):
        self.calls.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "refresh": refresh,
                "season_number": season_number,
            }
        )
        return SimpleNamespace(
            results=[SimpleNamespace(resource_id=resource_id) for resource_id in self.resource_ids],
        )


async def _add_resource(database, resource_id: str, canonical_key: str) -> None:
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id=resource_id,
                kind=ResourceKind.MAGNET,
                canonical_key=canonical_key,
                encrypted_url="encrypted",
                name=resource_id,
                source="test",
                captured_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_subscription_lifecycle_and_manual_check(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    service = SubscriptionService(database.session_factory, search)

    created = await service.create(
        SubscriptionCreateRequest(
            tmdb_id=123,
            media_type=MediaType.TV,
            season_number=2,
        )
    )
    assert created.status == SubscriptionStatus.ACTIVE
    checked = await service.check(created.id)
    assert checked.matched_count == 1
    assert checked.resource_ids == ["res_match"]
    assert checked.new_resource_ids == ["res_match"]
    assert checked.subscription.status == SubscriptionStatus.MATCHED
    checked_again = await service.check(created.id)
    assert checked_again.new_resource_ids == []
    observations = await service.list_observations(created.id)
    assert len(observations) == 1
    assert observations[0].resource_id == "res_match"
    assert observations[0].seen_count == 2
    with pytest.raises(ValueError, match="limit"):
        await service.list_observations(created.id, limit=0)
    with pytest.raises(SubscriptionNotFound):
        await service.list_observations("sub_missing")
    assert search.calls == [
        {
            "tmdb_id": 123,
            "media_type": MediaType.TV,
            "refresh": True,
            "season_number": 2,
        },
        {
            "tmdb_id": 123,
            "media_type": MediaType.TV,
            "refresh": True,
            "season_number": 2,
        },
    ]

    paused = await service.mutate(
        created.id,
        SubscriptionMutationRequest(revision=checked_again.subscription.revision),
        "pause",
    )
    assert paused.status == SubscriptionStatus.PAUSED
    with pytest.raises(SubscriptionConflict, match="subscription_not_active"):
        await service.check(created.id)
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_subscription_observations_dedupe_by_canonical_key_when_resource_id_changes(
    tmp_path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions-canonical.db'}")
    await initialize_database(database.engine)
    search = FakeSearch()
    await _add_resource(database, "res_old", "magnet:stable-key")
    search.resource_ids = ["res_old"]
    service = SubscriptionService(database.session_factory, search)
    created = await service.create(SubscriptionCreateRequest(tmdb_id=321))
    first = await service.check(created.id)
    assert first.new_resource_ids == ["res_old"]

    async with database.session_factory() as session:
        old = await session.get(Resource, "res_old")
        await session.delete(old)
        await session.commit()
    await _add_resource(database, "res_new", "magnet:stable-key")
    search.resource_ids = ["res_new"]
    second = await service.check(created.id)
    assert second.resource_ids == ["res_new"]
    assert second.new_resource_ids == []
    observations = await service.list_observations(created.id)
    assert observations[0].resource_id == "res_new"
    assert observations[0].seen_count == 2
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_subscription_scope_is_idempotently_unique(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}")
    await initialize_database(database.engine)
    service = SubscriptionService(database.session_factory, FakeSearch())
    request = SubscriptionCreateRequest(tmdb_id=123)
    await service.create(request)
    with pytest.raises(SubscriptionConflict, match="subscription_exists"):
        await service.create(request)
    await database.engine.dispose()
