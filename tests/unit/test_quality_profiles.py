from datetime import UTC, datetime

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import Resource
from watch_assistant.schemas import (
    QualityProfileCreateRequest,
    QualityProfilePatch,
    ResourceKind,
)
from watch_assistant.services.quality_profiles import (
    QualityProfileConflict,
    QualityProfileService,
)


@pytest.mark.asyncio
async def test_quality_profile_version_and_zero_side_effect_simulation(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'quality.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            Resource(
                id="res_quality",
                kind=ResourceKind.MAGNET,
                canonical_key="magnet:quality",
                encrypted_url="encrypted",
                name="Movie 2024 1080p WEB-DL HEVC",
                source="test",
                captured_at=datetime.now(UTC),
                expires_at=datetime.now(UTC),
                seeders=5,
            )
        )
        await session.commit()
    service = QualityProfileService(database.session_factory)
    profile = await service.create(
        QualityProfileCreateRequest(
            name="1080 均衡",
            rules={"hard": {"min_resolution": 1080}, "prefer": {"source": "WEB-DL"}},
        )
    )
    simulated = await service.simulate(profile.id, ["res_quality"])
    assert simulated.items[0].eligible is True
    assert "preferred_source" in simulated.items[0].reasons
    updated = await service.update(
        profile.id,
        QualityProfilePatch(
            revision=profile.revision,
            rules={"hard": {"min_resolution": 2160}},
        ),
    )
    assert updated.revision == profile.revision + 1
    with pytest.raises(QualityProfileConflict):
        await service.update(
            profile.id,
            QualityProfilePatch(revision=profile.revision, name="stale"),
        )
    await database.engine.dispose()
