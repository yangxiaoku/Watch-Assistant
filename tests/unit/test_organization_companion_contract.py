import pytest

from watch_assistant.services.organization_companion_contract import (
    OrganizationCompanionGroup,
    check_group_after_timeout,
    check_group_after_write,
    check_group_before_write,
    resolve_organization_companion_group,
)
from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    OrganizationStepExpectation,
    RemoteObjectState,
)


def _step(
    object_id: str,
    source_name: str,
    target_name: str,
    *,
    source_parent_id: str = "source",
    target_parent_id: str = "target",
) -> OrganizationStepExpectation:
    return OrganizationStepExpectation(
        object_id,
        source_parent_id,
        source_name,
        target_parent_id,
        target_name,
    )


def _group() -> OrganizationCompanionGroup:
    return OrganizationCompanionGroup(
        _step("video", "video.mkv", "Video.mkv"),
        (_step("subtitle", "video.srt", "Video.srt"),),
    )


def _source(step: OrganizationStepExpectation) -> RemoteObjectState:
    return RemoteObjectState(step.object_id, step.source_parent_id, step.source_name)


def _target(step: OrganizationStepExpectation) -> RemoteObjectState:
    return RemoteObjectState(step.object_id, step.target_parent_id, step.target_name)


def test_group_is_ready_only_when_every_member_is_ready():
    group = _group()
    result = check_group_before_write(
        group,
        sources={step.object_id: _source(step) for step in group.steps},
        targets={},
    )

    assert result.status is OrganizationStepCheck.READY


def test_changed_source_conflicts_for_the_entire_group():
    group = _group()
    changed = RemoteObjectState("video", "source", "changed.mkv")
    result = check_group_before_write(
        group,
        sources={
            group.main.object_id: changed,
            group.companions[0].object_id: _source(group.companions[0]),
        },
        targets={},
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "group_precondition_conflict",
    )


def test_existing_target_for_one_companion_blocks_the_entire_group():
    group = _group()
    companion = group.companions[0]
    result = check_group_before_write(
        group,
        sources={step.object_id: _source(step) for step in group.steps},
        targets={
            (companion.target_parent_id, companion.target_name): RemoteObjectState(
                "other", companion.target_parent_id, companion.target_name
            )
        },
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "group_precondition_conflict",
    )


def test_missing_main_or_companion_is_uncertain():
    group = _group()
    result = check_group_before_write(
        group,
        sources={group.main.object_id: _source(group.main)},
        targets={},
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "group_precondition_unverified",
    )


def test_extra_observation_member_is_a_group_conflict():
    group = _group()
    result = check_group_before_write(
        group,
        sources={
            **{step.object_id: _source(step) for step in group.steps},
            "unexpected": _source(group.main),
        },
        targets={},
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "group_member_set_mismatch",
    )


def test_partial_replay_state_is_never_executable():
    group = _group()
    result = check_group_before_write(
        group,
        sources={
            group.main.object_id: _target(group.main),
            group.companions[0].object_id: _source(group.companions[0]),
        },
        targets={},
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "group_partial_state",
    )


def test_all_members_at_exact_targets_are_already_applied():
    group = _group()
    result = check_group_before_write(
        group,
        sources={step.object_id: _target(step) for step in group.steps},
        targets={},
    )

    assert result.status is OrganizationStepCheck.ALREADY_APPLIED


def test_after_write_requires_every_exact_target():
    group = _group()
    result = check_group_after_write(
        group,
        observed={
            group.main.object_id: _target(group.main),
            group.companions[0].object_id: _source(group.companions[0]),
        },
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "group_postcondition_conflict",
    )


def test_timeout_only_succeeds_when_the_entire_group_reached_target():
    group = _group()
    applied = check_group_after_timeout(
        group, observed={step.object_id: _target(step) for step in group.steps}
    )
    partial = check_group_after_timeout(
        group,
        observed={group.main.object_id: _target(group.main)},
    )

    assert applied.status is OrganizationStepCheck.ALREADY_APPLIED
    assert (partial.status, partial.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "timeout",
    )


def test_resolver_rejects_incomplete_or_duplicate_plan_members():
    assert (
        resolve_organization_companion_group(
            _step("video", "private/path.mkv", "Video.mkv")
        )
        is None
    )
    assert (
        resolve_organization_companion_group(
            _step("video", "video.mkv", "Video.mkv"),
            companions=(_step("video", "video.srt", "Video.srt"),),
        )
        is None
    )
    with pytest.raises(ValueError, match="duplicate_group_target"):
        OrganizationCompanionGroup(
            _step("video", "video.mkv", "Video.mkv"),
            (_step("subtitle", "video.srt", "Video.mkv"),),
        )


def test_resolver_orders_companions_deterministically():
    group = resolve_organization_companion_group(
        _step("video", "video.mkv", "Video.mkv"),
        companions=(
            _step("z-subtitle", "z.srt", "Z.srt"),
            _step("a-nfo", "a.nfo", "A.nfo"),
        ),
    )

    assert group is not None
    assert [step.object_id for step in group.steps] == [
        "video",
        "a-nfo",
        "z-subtitle",
    ]


def test_group_repr_does_not_render_remote_values():
    group = OrganizationCompanionGroup(
        _step("file-secret", "private.mkv", "Public.mkv"),
        (_step("sidecar-secret", "private.srt", "Public.srt"),),
    )

    rendered = repr(group)
    assert "file-secret" not in rendered
    assert "private.mkv" not in rendered
    assert "sidecar-secret" not in rendered
