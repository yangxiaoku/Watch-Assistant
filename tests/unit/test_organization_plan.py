import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.schemas import MediaType
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingPlan,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchStatus,
    MediaKind,
    TmdbCandidate,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanError,
    OrganizationPlanItem,
    OrganizationPlanService,
    OrganizationPlanStatus,
    PlanSource,
)

LIBRARY_ID = "library-1"
ROOT_ID = "7000"
SCAN_ID = "scan-1"
SECRET_NAME = "private-title.mkv"
SECRET_PATH = "/private/cloud/private-title.mkv"
SECRET_PICKCODE = "private-pickcode"


def _candidate() -> TmdbCandidate:
    return TmdbCandidate(
        tmdb_id=1,
        media_type=MediaType.MOVIE,
        title="Safe title",
        kind=MediaKind.MOVIE,
        release_year=2024,
        origin_countries=("US",),
    )


def _item(
    object_id: str = "100",
    *,
    path: str = SECRET_PATH,
    confidence: MatchConfidence = MatchConfidence.HIGH,
    naming_status: ClassificationStatus = ClassificationStatus.PLANNED,
    display_name: str = SECRET_NAME,
    reasons: tuple[str, ...] = ("tmdb_match_accepted",),
) -> OrganizationPlanItem:
    return OrganizationPlanItem(
        source=PlanSource(
            object_type="file",
            object_id=object_id,
            parent_id=ROOT_ID,
            path=path,
            remote_version="remote-v1",
        ),
        naming_plan=NamingPlan(
            status=naming_status,
            target_path="movie/safe-title.mkv",
            display_name=display_name,
            reasons=reasons,
            rule_version="i06-v1",
        ),
        decision=MatchDecision(
            status=MatchStatus.ACCEPTED,
            selected=_candidate(),
            confidence=confidence,
        ),
    )


async def _database(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'organization.db'}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id=LIBRARY_ID,
                name="private library",
                root_directory_id=ROOT_ID,
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()
        session.add(
            LibraryScanRun(
                id=SCAN_ID,
                library_id=LIBRARY_ID,
                root_directory_id=ROOT_ID,
                idempotency_key="scan-key",
                state="completed",
                complete=True,
                snapshot_revision=1,
            )
        )
        await session.commit()
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="file",
                object_id="100",
                parent_id=ROOT_ID,
                name=SECRET_NAME,
                path=SECRET_PATH,
                is_directory=False,
            )
        )
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="file",
                object_id="101",
                parent_id=ROOT_ID,
                name="second-title.mkv",
                path=SECRET_PATH,
                is_directory=False,
            )
        )
        await session.commit()
    return database


@pytest.mark.asyncio
async def test_plan_hash_is_stable_and_persistence_is_idempotent(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    first = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    second = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(display_name="different display", reasons=("other",)),),
    )

    assert first.plan_id == second.plan_id
    assert first.plan_hash == second.plan_hash
    assert first.status is OrganizationPlanStatus.PLANNED
    async with database.session_factory() as session:
        plans = list(await session.scalars(select(OrganizationPlan)))
    assert len(plans) == 1
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_plan_requires_current_complete_verified_scan(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, SCAN_ID)
        assert run is not None
        run.complete = False
        await session.commit()
    with pytest.raises(OrganizationPlanError) as error:
        await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
    assert error.value.code == "scan_not_current"
    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, LIBRARY_ID)
        assert library is not None
        library.scope_verified = False
        run = await session.get(LibraryScanRun, SCAN_ID)
        assert run is not None
        run.complete = True
        await session.commit()
    with pytest.raises(OrganizationPlanError) as error:
        await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
    assert error.value.code == "scan_not_current"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_low_confidence_and_target_conflict_are_needs_review(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    low = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(confidence=MatchConfidence.LOW),),
    )
    assert low.status is OrganizationPlanStatus.NEEDS_REVIEW

    conflict = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(object_id="101"),),
        target_conflicts=("movie/safe-title.mkv",),
    )
    assert conflict.status is OrganizationPlanStatus.NEEDS_REVIEW
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_invalid_target_and_source_scope_fail_closed(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    for target in ("../outside.mkv", "/absolute.mkv", "C:/outside.mkv", "https://x"):
        with pytest.raises(OrganizationPlanError) as error:
            await service.create_plan(
                library_id=LIBRARY_ID,
                scan_run_id=SCAN_ID,
                items=(
                    OrganizationPlanItem(
                        source=_item().source,
                        naming_plan=NamingPlan(
                            status=ClassificationStatus.PLANNED,
                            target_path=target,
                            rule_version="i06-v1",
                        ),
                        decision=_item().decision,
                    ),
                ),
            )
        assert error.value.code == "invalid_target_path"
    with pytest.raises(OrganizationPlanError) as error:
        await service.create_plan(
            library_id=LIBRARY_ID,
            scan_run_id=SCAN_ID,
            items=(
                OrganizationPlanItem(
                    source=PlanSource("file", "999", ROOT_ID, SECRET_PATH, "remote-v1"),
                    naming_plan=_item().naming_plan,
                    decision=_item().decision,
                ),
            ),
        )
    assert error.value.code == "source_snapshot_mismatch"
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_refresh_invalidates_snapshot_versions_and_expiry(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    plan = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(),),
        expires_at=now + timedelta(hours=1),
        now=now,
    )
    changed_source = _item(path="/private/cloud/renamed.mkv")
    refreshed = await service.refresh_plan(plan.plan_id, source_items=(changed_source,))
    assert refreshed.status is OrganizationPlanStatus.INVALIDATED

    second = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(object_id="100"),),
        target_root="revised",
        expires_at=now + timedelta(hours=1),
        now=now,
    )
    expired = await service.refresh_plan(second.plan_id, now=now + timedelta(hours=2))
    assert expired.status is OrganizationPlanStatus.INVALIDATED

    third = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(object_id="100"),),
        target_root="rule-check",
        expires_at=now + timedelta(hours=1),
        now=now,
    )
    rule_changed = await service.refresh_plan(third.plan_id, rule_version="i06-v2")
    assert rule_changed.status is OrganizationPlanStatus.INVALIDATED
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_public_and_error_outputs_are_redacted_and_migration_is_idempotent(
    tmp_path,
):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    public = repr(plan) + repr(plan.to_public_dict())
    assert SECRET_NAME not in public
    assert SECRET_PATH not in public
    assert SECRET_PICKCODE not in public
    assert "object_id" not in public
    with pytest.raises(OrganizationPlanError) as error:
        await service.create_plan(
            library_id=LIBRARY_ID,
            scan_run_id=SCAN_ID,
            items=(
                _item(
                    path="\x00",
                ),
            ),
        )
    assert str(error.value) == "invalid_source_snapshot"
    assert not re.search(r"private|pickcode|https?://", str(error.value), re.IGNORECASE)
    await initialize_database(database.engine)
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
    assert "organization_plans" in tables
    await database.engine.dispose()
