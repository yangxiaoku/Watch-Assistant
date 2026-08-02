import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
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
    OrganizationPlanCompanion,
    OrganizationPlanError,
    OrganizationPlanItem,
    OrganizationPlanService,
    OrganizationPlanStatus,
    PlanSource,
    _canonical_hash,
    _entry_remote_version,
    _execution_blockers,
    load_executable_steps,
)

LIBRARY_ID = "library-1"
ROOT_ID = "7000"
SCAN_ID = "scan-1"
SECRET_NAME = "private-title.mkv"
SECRET_PATH = "/private/cloud/private-title.mkv"
SECRET_PICKCODE = "private-pickcode"


@pytest.mark.parametrize(
    ("status", "expires_at", "complete_preconditions", "strict_versions", "snapshot_changed", "expected"),
    (
        (OrganizationPlanStatus.PLANNED, datetime.now(UTC) - timedelta(seconds=1), True, True, False, "plan_expired"),
        (OrganizationPlanStatus.IGNORED, datetime.now(UTC) + timedelta(hours=1), True, True, False, "plan_status_ignored"),
        (OrganizationPlanStatus.PLANNED, datetime.now(UTC) + timedelta(hours=1), False, True, False, "plan_prerequisites_incomplete"),
        (OrganizationPlanStatus.PLANNED, datetime.now(UTC) + timedelta(hours=1), True, False, False, "source_snapshot_changed"),
        (OrganizationPlanStatus.NEEDS_REVIEW, datetime.now(UTC) + timedelta(hours=1), True, True, False, "plan_needs_review"),
    ),
)
def test_execution_blocker_matrix_exposes_a_specific_reason(
    status,
    expires_at,
    complete_preconditions,
    strict_versions,
    snapshot_changed,
    expected,
):
    blockers = _execution_blockers(
        status=status,
        expires_at=expires_at,
        now=datetime.now(UTC),
        action_count=1,
        executable_action_count=1,
        review_action_count=0,
        complete_preconditions=complete_preconditions,
        strict_source_versions=strict_versions,
        source_snapshot_changed=snapshot_changed,
    )

    assert expected in {blocker.code for blocker in blockers}
    assert all(blocker.message_zh and blocker.next_step_zh for blocker in blockers)


def _source_version(
    object_id: str,
    *,
    path: str,
    name: str,
    parent_id: str = ROOT_ID,
) -> str:
    return _entry_remote_version(
        LibraryScanEntry(
            scan_run_id=SCAN_ID,
            object_type="file",
            object_id=object_id,
            parent_id=parent_id,
            name=name,
            path=path,
            is_directory=False,
        )
    )


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
    target_parent_id: str | None = "8000",
    target_name: str = "safe-title.mkv",
    companions: tuple[OrganizationPlanCompanion, ...] = (),
) -> OrganizationPlanItem:
    source_name = SECRET_NAME if object_id == "100" else "second-title.mkv"
    return OrganizationPlanItem(
        source=PlanSource(
            object_type="file",
            object_id=object_id,
            parent_id=ROOT_ID,
            path=path,
            remote_version=_source_version(object_id, path=path, name=source_name),
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
        target_parent_id=target_parent_id,
        target_name=target_name,
        companions=companions,
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
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=1,
                expected_total=3,
                pages_read=2,
                items_seen=3,
            )
        )
        await session.commit()
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="directory",
                object_id="8000",
                parent_id=ROOT_ID,
                name="movie",
                path="movie",
                is_directory=True,
            )
        )
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
        session.add(
            LibraryScanCheckpoint(
                scan_run_id=SCAN_ID,
                page=2,
                items_seen=3,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {ROOT_ID: 3, "8000": 0},
                        "expected_total": 3,
                        "pending": [],
                        "visited": [ROOT_ID, "8000"],
                    }
                ),
            )
        )
        await session.commit()
    return database


async def _refresh_completed_tree_evidence(database):
    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, SCAN_ID)
        checkpoint = await session.get(LibraryScanCheckpoint, SCAN_ID)
        assert run is not None
        assert checkpoint is not None
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == SCAN_ID
                    )
                )
            ).all()
        )
        directory_ids = [run.root_directory_id]
        directory_totals = {run.root_directory_id: 0}
        for entry in entries:
            directory_totals[entry.parent_id] = (
                directory_totals.get(entry.parent_id, 0) + 1
            )
            if entry.is_directory:
                directory_ids.append(entry.object_id)
                directory_totals.setdefault(entry.object_id, 0)
        assert set(directory_totals) == set(directory_ids)
        run.expected_total = len(entries)
        run.items_seen = len(entries)
        checkpoint.items_seen = len(entries)
        checkpoint.cursor_json = json.dumps(
            {
                "version": 2,
                "directory_totals": directory_totals,
                "expected_total": len(entries),
                "pending": [],
                "visited": directory_ids,
            }
        )
        await session.commit()


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
    assert first.can_execute is True
    # Display/audit basis changes do not change the canonical idempotency hash.
    async with database.session_factory() as session:
        plans = list(await session.scalars(select(OrganizationPlan)))
        stored = plans[0]
    assert len(plans) == 1
    assert json.loads(stored.preconditions_json)["library"]["revision"] == 0

    ordered_first = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item("101"), _item("100")),
        target_root="ordered",
    )
    ordered_second = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item("100"), _item("101")),
        target_root="ordered",
    )
    assert ordered_first.plan_id == ordered_second.plan_id
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_plan_can_execute_requires_planned_and_unexpired_state(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    assert plan_view.can_execute is True

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        plan.status = OrganizationPlanStatus.NEEDS_REVIEW.value
        await session.commit()
    review_view = await service.get_plan(plan_view.plan_id)
    assert review_view.can_execute is False
    assert {blocker.code for blocker in review_view.execution_blockers} == {
        "plan_needs_review",
    }

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        plan.status = OrganizationPlanStatus.PLANNED.value
        plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    expired_view = await service.get_plan(plan_view.plan_id)
    assert expired_view.can_execute is False
    assert {blocker.code for blocker in expired_view.execution_blockers} == {
        "plan_expired",
    }
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_complete_execution_payload_is_persisted_and_parsed(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        steps = await load_executable_steps(database.session_factory, plan)
        assert steps is not None
        assert steps[0].scope_directory_ids == (ROOT_ID, "8000")
        member = steps[0].members[0]
        assert (member.source_parent_id, member.source_path, member.source_name) == (
            ROOT_ID,
            SECRET_PATH,
            SECRET_NAME,
        )
        assert (member.target_parent_id, member.target_name) == (
            "8000",
            "safe-title.mkv",
        )
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_non_hash_legacy_version_is_not_executable(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        source_snapshot = json.loads(plan.source_snapshot_json)
        actions = json.loads(plan.actions_json)
        preconditions = json.loads(plan.preconditions_json)
        legacy = "remote-v1"
        source_snapshot[0]["remote_version"] = legacy
        plan.source_snapshot_json = json.dumps(source_snapshot)
        plan.actions_json = json.dumps(actions)
        plan.preconditions_json = json.dumps(preconditions)
        plan.plan_hash = _canonical_hash(
            {
                "library_id": plan.library_id,
                "library_snapshot": preconditions["library"],
                "source_snapshot": source_snapshot,
                "target_root": plan.target_root,
                "actions": actions,
                "preconditions": preconditions,
                "rule_version": plan.rule_version,
                "parser_version": plan.parser_version,
                "matcher_version": plan.matcher_version,
            }
        )
        await session.commit()

    refreshed = await service.get_plan(plan_view.plan_id)
    assert refreshed.can_execute is False
    async with database.session_factory() as session:
        stored = await session.get(OrganizationPlan, plan_view.plan_id)
        assert stored is not None
        assert await load_executable_steps(database.session_factory, stored) is None
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("size_bytes", 1),
        ("modified_at", datetime(2026, 7, 28, tzinfo=UTC)),
    ),
)
async def test_source_size_or_mtime_change_blocks_execution(tmp_path, field, value):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    async with database.session_factory() as session:
        row = await session.get(
            LibraryScanEntry,
            {
                "scan_run_id": SCAN_ID,
                "object_type": "file",
                "object_id": "100",
            },
        )
        assert row is not None
        setattr(row, field, value)
        await session.commit()
        stored = await session.get(OrganizationPlan, plan_view.plan_id)
        assert stored is not None
        assert await load_executable_steps(database.session_factory, stored) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_unrelated_new_scan_invalidates_bound_plan(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-2",
                library_id=LIBRARY_ID,
                root_directory_id=ROOT_ID,
                idempotency_key="scan-key-2",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=2,
                expected_total=4,
                pages_read=2,
                items_seen=4,
            )
        )
        await session.flush()
        session.add_all(
            [
                LibraryScanEntry(
                    scan_run_id="scan-2",
                    object_type="directory",
                    object_id="8000",
                    parent_id=ROOT_ID,
                    name="movie",
                    path="movie",
                    is_directory=True,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-2",
                    object_type="file",
                    object_id="100",
                    parent_id=ROOT_ID,
                    name=SECRET_NAME,
                    path=SECRET_PATH,
                    is_directory=False,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-2",
                    object_type="file",
                    object_id="101",
                    parent_id=ROOT_ID,
                    name="second-title.mkv",
                    path=SECRET_PATH,
                    is_directory=False,
                ),
                LibraryScanEntry(
                    scan_run_id="scan-2",
                    object_type="file",
                    object_id="unrelated",
                    parent_id=ROOT_ID,
                    name="unrelated.mkv",
                    path="unrelated.mkv",
                    is_directory=False,
                ),
            ]
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-2",
                page=2,
                items_seen=4,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {ROOT_ID: 4, "8000": 0},
                        "expected_total": 4,
                        "pending": [],
                        "visited": [ROOT_ID, "8000"],
                    }
                ),
            )
        )
        await session.commit()
        stored = await session.get(OrganizationPlan, plan_view.plan_id)
        assert stored is not None
        assert await load_executable_steps(database.session_factory, stored) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_execution_scope_includes_configured_target_root(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(),),
        target_directory_id="9000",
        target_directories={"movie": "8000"},
    )
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        steps = await load_executable_steps(database.session_factory, plan)
        assert steps is not None
        assert steps[0].scope_directory_ids == (ROOT_ID, "8000", "9000")
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_companion_group_is_complete_and_target_identity_changes_hash(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="file",
                object_id="102",
                parent_id=ROOT_ID,
                name="private-title.srt",
                path="/private/cloud/private-title.srt",
                is_directory=False,
            )
        )
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="directory",
                object_id="8001",
                parent_id=ROOT_ID,
                name="movie",
                path="movie",
                is_directory=True,
            )
        )
        await session.commit()
    await _refresh_completed_tree_evidence(database)
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id=ROOT_ID,
            path="/private/cloud/private-title.srt",
            remote_version=_source_version(
                "102", path="/private/cloud/private-title.srt", name="private-title.srt"
            ),
        ),
        target_parent_id="8000",
        target_name="safe-title.srt",
    )
    plan = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(companions=(companion,)),),
    )
    assert plan.status is OrganizationPlanStatus.PLANNED
    async with database.session_factory() as session:
        stored = await session.get(OrganizationPlan, plan.plan_id)
        assert stored is not None
        steps = await load_executable_steps(database.session_factory, stored)
        assert steps is not None
        assert len(steps[0].members) == 2
        assert steps[0].members[1].source_path == "/private/cloud/private-title.srt"
    alternate = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(target_parent_id="8001"),),
    )
    assert alternate.plan_hash != plan.plan_hash
    await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_index", "field"),
    (
        (0, "source_version"),
        (0, "source_path"),
        (1, "source_version"),
        (1, "source_path"),
    ),
)
async def test_executable_loader_rejects_synced_member_version_or_path_tampering(
    tmp_path, member_index, field
):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    async with database.session_factory() as session:
        session.add(
            LibraryScanEntry(
                scan_run_id=SCAN_ID,
                object_type="file",
                object_id="102",
                parent_id=ROOT_ID,
                name="private-title.srt",
                path="/private/cloud/private-title.srt",
                is_directory=False,
            )
        )
        await session.commit()
    await _refresh_completed_tree_evidence(database)
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="102",
            parent_id=ROOT_ID,
            path="/private/cloud/private-title.srt",
            remote_version=_source_version(
                "102", path="/private/cloud/private-title.srt", name="private-title.srt"
            ),
        ),
        target_parent_id="8000",
        target_name="safe-title.srt",
    )
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(companions=(companion,)),),
    )
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        actions = json.loads(plan.actions_json)
        preconditions = json.loads(plan.preconditions_json)
        execution = actions[0]["execution"]
        replacement = (
            "remote-v2" if field == "source_version" else "/private/cloud/changed"
        )
        execution["members"][member_index][field] = replacement
        preconditions["items"][0]["execution"]["members"][member_index][field] = (
            replacement
        )
        if member_index == 0:
            if field == "source_version":
                actions[0]["source_version"] = replacement
                preconditions["items"][0]["remote_version"] = replacement
            else:
                actions[0]["source_path"] = replacement
                preconditions["items"][0]["source_path"] = replacement
        plan.actions_json = json.dumps(actions)
        plan.preconditions_json = json.dumps(preconditions)
        plan.plan_hash = _canonical_hash(
            {
                "library_id": plan.library_id,
                "library_snapshot": preconditions["library"],
                "source_snapshot": json.loads(plan.source_snapshot_json),
                "target_root": plan.target_root,
                "actions": actions,
                "preconditions": preconditions,
                "rule_version": plan.rule_version,
                "parser_version": plan.parser_version,
                "matcher_version": plan.matcher_version,
            }
        )
        await session.commit()
        stored = await session.get(OrganizationPlan, plan.id)
        assert stored is not None
        assert await load_executable_steps(database.session_factory, stored) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_missing_target_mapping_or_companion_identity_needs_review(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    missing_target = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(target_parent_id="9999"),),
        target_root="missing-target",
    )
    assert missing_target.status is OrganizationPlanStatus.NEEDS_REVIEW
    async with database.session_factory() as session:
        stored = await session.get(OrganizationPlan, missing_target.plan_id)
        assert stored is not None
        assert (
            await load_executable_steps(
                database.session_factory, stored, allow_unconfirmed=True
            )
            is None
        )
    companion = OrganizationPlanCompanion(
        source=PlanSource(
            object_type="file",
            object_id="missing-companion",
            parent_id=ROOT_ID,
            path="/private/subtitle.srt",
            remote_version=_source_version(
                "missing-companion", path="/private/subtitle.srt", name="subtitle.srt"
            ),
        ),
        target_parent_id="8000",
        target_name="safe-title.srt",
    )
    incomplete = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(companions=(companion,)),),
        target_root="companion",
    )
    assert incomplete.status is OrganizationPlanStatus.NEEDS_REVIEW
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_old_plan_payload_is_readable_but_not_executable(tmp_path):
    database = await _database(tmp_path)
    service = OrganizationPlanService(database.session_factory)
    plan_view = await service.create_plan(
        library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
    )
    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_view.plan_id)
        assert plan is not None
        actions = json.loads(plan.actions_json)
        actions[0].pop("execution", None)
        plan.actions_json = json.dumps(actions)
        await session.commit()
        refreshed = await session.get(OrganizationPlan, plan_view.plan_id)
        assert refreshed is not None
        assert await load_executable_steps(database.session_factory, refreshed) is None
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_executable_loader_rejects_persisted_tampering(tmp_path):
    mutations = ("hash", "target", "scope", "snapshot", "member")
    for mutation in mutations:
        case_dir = tmp_path / mutation
        case_dir.mkdir()
        database = await _database(case_dir)
        service = OrganizationPlanService(database.session_factory)
        plan_view = await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
        async with database.session_factory() as session:
            plan = await session.get(OrganizationPlan, plan_view.plan_id)
            assert plan is not None
            if mutation == "hash":
                plan.plan_hash = "0" * 64
            else:
                actions = json.loads(plan.actions_json)
                preconditions = json.loads(plan.preconditions_json)
                snapshot = json.loads(plan.source_snapshot_json)
                if mutation == "target":
                    actions[0]["target"] = "movie/other-title.mkv"
                elif mutation == "scope":
                    scope = ["7000", "8000", "9999"]
                    actions[0]["execution"]["scope_directory_ids"] = scope
                    preconditions["items"][0]["execution"]["scope_directory_ids"] = (
                        scope
                    )
                elif mutation == "snapshot":
                    snapshot[0]["name"] = "other-title.mkv"
                else:
                    member_name = "other-title.mkv"
                    actions[0]["execution"]["members"][0]["source_name"] = member_name
                    preconditions["items"][0]["execution"]["members"][0][
                        "source_name"
                    ] = member_name
                plan.actions_json = json.dumps(actions)
                plan.preconditions_json = json.dumps(preconditions)
                plan.source_snapshot_json = json.dumps(snapshot)
                plan.plan_hash = _canonical_hash(
                    {
                        "library_id": plan.library_id,
                        "library_snapshot": preconditions["library"],
                        "source_snapshot": snapshot,
                        "target_root": plan.target_root,
                        "actions": actions,
                        "preconditions": preconditions,
                        "rule_version": plan.rule_version,
                        "parser_version": plan.parser_version,
                        "matcher_version": plan.matcher_version,
                    }
                )
            await session.commit()
            stored = await session.get(OrganizationPlan, plan.id)
            assert stored is not None
            assert await load_executable_steps(database.session_factory, stored) is None
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_executable_loader_rejects_library_and_scan_changes(tmp_path):
    changes = (
        ("library_revision",),
        ("library_scope",),
        ("library_root",),
        ("scan_incomplete",),
    )
    for (change,) in changes:
        case_dir = tmp_path / change
        case_dir.mkdir()
        database = await _database(case_dir)
        service = OrganizationPlanService(database.session_factory)
        plan_view = await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
        async with database.session_factory() as session:
            if change.startswith("library"):
                library = await session.get(MediaLibrary, LIBRARY_ID)
                assert library is not None
                if change == "library_revision":
                    library.revision += 1
                elif change == "library_scope":
                    library.scope_verified = False
                else:
                    library.root_directory_id = "7001"
            else:
                run = await session.get(LibraryScanRun, SCAN_ID)
                assert run is not None
                run.complete = False
            await session.commit()
            stored = await session.get(OrganizationPlan, plan_view.plan_id)
            assert stored is not None
            assert await load_executable_steps(database.session_factory, stored) is None
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
                    source=PlanSource(
                        "file",
                        "999",
                        ROOT_ID,
                        SECRET_PATH,
                        _source_version("999", path=SECRET_PATH, name=SECRET_NAME),
                    ),
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
    basis_plan = await service.create_plan(
        library_id=LIBRARY_ID,
        scan_run_id=SCAN_ID,
        items=(_item(),),
        target_root="basis-check",
        now=now,
    )
    basis_changed = await service.refresh_plan(
        basis_plan.plan_id,
        source_items=(_item(reasons=("basis_changed",)),),
    )
    assert basis_changed.status is OrganizationPlanStatus.INVALIDATED

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
async def test_refresh_invalidates_library_scope_changes(tmp_path):
    changes = (
        ("revision", 2),
        ("enabled", False),
        ("scope_verified", False),
        ("root_directory_id", "7001"),
    )
    for index, (field, value) in enumerate(changes):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        database = await _database(case_dir)
        service = OrganizationPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
        async with database.session_factory() as session:
            library = await session.get(MediaLibrary, LIBRARY_ID)
            assert library is not None
            setattr(library, field, value)
            await session.commit()
        refreshed = await service.refresh_plan(plan.plan_id)
        assert refreshed.status is OrganizationPlanStatus.INVALIDATED
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_refresh_invalidates_corrupt_source_snapshot_without_leaking_json(
    tmp_path,
):
    corruptions = (
        "{broken-json",
        "[]",
        '[{"object_type":"file","parent_id":"7000","path":"/private"}]',
    )
    for index, corruption in enumerate(corruptions):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        database = await _database(case_dir)
        service = OrganizationPlanService(database.session_factory)
        plan = await service.create_plan(
            library_id=LIBRARY_ID, scan_run_id=SCAN_ID, items=(_item(),)
        )
        async with database.session_factory() as session:
            stored = await session.get(OrganizationPlan, plan.plan_id)
            assert stored is not None
            stored.source_snapshot_json = corruption
            await session.commit()

        refreshed = await service.refresh_plan(plan.plan_id)
        assert refreshed.status is OrganizationPlanStatus.INVALIDATED
        assert refreshed.source_count == 0
        assert corruption not in repr(refreshed)
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
