import pytest

from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    RemoteObjectState,
)
from watch_assistant.services.organization_isolation_contract import (
    OrganizationIsolationAction,
    check_isolation_after_timeout,
    check_isolation_after_write,
    check_isolation_before_write,
    check_restore_after_timeout,
    check_restore_after_write,
    check_restore_before_write,
    resolve_isolation_intent,
)


def _intent(action=OrganizationIsolationAction.ISOLATE):
    intent = resolve_isolation_intent(
        action,
        object_id="file-1",
        original_source_parent_id="library-root",
        original_source_name="before.mkv",
        isolation_parent_id="quarantine",
        isolation_name="before.mkv",
        original_target_parent_id="movie-dir",
        original_target_name="After.mkv",
        rule_version="rules-v1",
        managed_directory_ids={"library-root", "quarantine", "movie-dir"},
    )
    assert intent is not None
    return intent


def _source(intent):
    return RemoteObjectState(
        intent.object_id,
        intent.original_source_parent_id
        if intent.action is OrganizationIsolationAction.ISOLATE
        else intent.isolation_parent_id,
        intent.original_source_name
        if intent.action is OrganizationIsolationAction.ISOLATE
        else intent.isolation_name,
    )


def _target(intent):
    return RemoteObjectState(
        intent.object_id,
        intent.isolation_parent_id
        if intent.action is OrganizationIsolationAction.ISOLATE
        else intent.original_target_parent_id,
        intent.isolation_name
        if intent.action is OrganizationIsolationAction.ISOLATE
        else intent.original_target_name,
    )


def test_isolation_requires_exact_source_and_empty_target():
    intent = _intent()
    ready = check_isolation_before_write(intent, source=_source(intent), target=None)
    assert ready.status is OrganizationStepCheck.READY

    missing = check_isolation_before_write(intent, source=None, target=None)
    assert (missing.status, missing.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "source_not_observed",
    )

    conflict = check_isolation_before_write(
        intent,
        source=_source(intent),
        target=RemoteObjectState("other", "quarantine", "before.mkv"),
    )
    assert (conflict.status, conflict.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "target_already_exists",
    )


def test_restore_reuses_same_exact_source_target_rules():
    intent = _intent(OrganizationIsolationAction.RESTORE)
    ready = check_restore_before_write(intent, source=_source(intent), target=None)
    assert ready.status is OrganizationStepCheck.READY

    changed = check_restore_before_write(
        intent,
        source=RemoteObjectState("file-1", "quarantine", "changed.mkv"),
        target=None,
    )
    assert (changed.status, changed.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "source_precondition_changed",
    )


def test_exact_final_position_is_idempotent_but_partial_state_conflicts():
    intent = _intent()
    applied = check_isolation_before_write(intent, source=_target(intent), target=None)
    assert applied.status is OrganizationStepCheck.ALREADY_APPLIED

    partial = check_isolation_before_write(
        intent,
        source=_target(intent),
        target=RemoteObjectState("other", "quarantine", "before.mkv"),
    )
    assert (partial.status, partial.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "partial_state",
    )


def test_timeout_never_returns_ready_and_postcondition_is_shared():
    intent = _intent()
    result = check_isolation_before_write(
        intent, source=_source(intent), target=None, timed_out=True
    )
    assert (result.status, result.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "timeout",
    )
    assert (
        check_isolation_after_timeout(intent, observed=_target(intent)).status
        is OrganizationStepCheck.ALREADY_APPLIED
    )
    assert (
        check_restore_after_timeout(
            _intent(OrganizationIsolationAction.RESTORE), observed=None
        ).status
        is OrganizationStepCheck.UNCERTAIN
    )


def test_write_postcondition_is_exact_for_both_actions():
    isolate = _intent()
    restore = _intent(OrganizationIsolationAction.RESTORE)
    assert (
        check_isolation_after_write(isolate, observed=_target(isolate)).status
        is OrganizationStepCheck.ALREADY_APPLIED
    )
    assert (
        check_restore_after_write(restore, observed=_target(restore)).status
        is OrganizationStepCheck.ALREADY_APPLIED
    )
    assert (
        check_restore_after_write(restore, observed=_source(restore)).status
        is OrganizationStepCheck.CONFLICT
    )


@pytest.mark.parametrize(
    "kwargs",
    (
        {"managed_directory_ids": {"library-root", "movie-dir"}},
        {"rule_version": ""},
        {"original_target_parent_id": "not/a/directory"},
    ),
)
def test_missing_or_unmanaged_scope_is_rejected(kwargs):
    values = {
        "action": OrganizationIsolationAction.ISOLATE,
        "object_id": "file-1",
        "original_source_parent_id": "library-root",
        "original_source_name": "before.mkv",
        "isolation_parent_id": "quarantine",
        "isolation_name": "before.mkv",
        "original_target_parent_id": "movie-dir",
        "original_target_name": "After.mkv",
        "rule_version": "rules-v1",
        "managed_directory_ids": {"library-root", "quarantine", "movie-dir"},
    }
    values.update(kwargs)
    assert resolve_isolation_intent(**values) is None


def test_invalid_observation_is_uncertain_and_repr_is_redacted():
    intent = resolve_isolation_intent(
        OrganizationIsolationAction.ISOLATE,
        object_id="file-secret",
        original_source_parent_id="source-secret",
        original_source_name="private.mkv",
        isolation_parent_id="quarantine-secret",
        isolation_name="private.mkv",
        original_target_parent_id="target-secret",
        original_target_name="public.mkv",
        rule_version="rules-v1",
        managed_directory_ids={"source-secret", "quarantine-secret", "target-secret"},
    )
    assert intent is not None
    assert (
        check_isolation_before_write(
            intent,
            source=RemoteObjectState("file-secret", "source-secret", "private/path"),
            target=None,
        ).status
        is OrganizationStepCheck.UNCERTAIN
    )
    rendered = repr(intent)
    for value in (
        "file-secret",
        "source-secret",
        "quarantine-secret",
        "private.mkv",
        "rules-v1",
    ):
        assert value not in rendered
