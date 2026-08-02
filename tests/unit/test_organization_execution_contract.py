import pytest

from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    OrganizationStepExpectation,
    RemoteObjectState,
    check_after_timeout,
    check_after_write,
    check_before_write,
    reconcile_uncertain,
)


def _expectation() -> OrganizationStepExpectation:
    return OrganizationStepExpectation(
        object_id="file-1",
        source_parent_id="source",
        source_name="before.mkv",
        target_parent_id="target",
        target_name="after.mkv",
    )


def _source() -> RemoteObjectState:
    return RemoteObjectState("file-1", "source", "before.mkv")


def _target() -> RemoteObjectState:
    return RemoteObjectState("file-1", "target", "after.mkv")


def test_precondition_allows_only_unchanged_source_and_absent_target():
    result = check_before_write(_expectation(), source=_source(), target=None)

    assert result.status is OrganizationStepCheck.READY
    assert result.error_code is None


def test_replay_at_exact_target_never_requests_a_second_move():
    result = check_before_write(_expectation(), source=_target(), target=None)

    assert result.status is OrganizationStepCheck.ALREADY_APPLIED


@pytest.mark.parametrize(
    "source",
    (
        RemoteObjectState("other-file", "source", "before.mkv"),
        RemoteObjectState("file-1", "other-parent", "before.mkv"),
        RemoteObjectState("file-1", "source", "other.mkv"),
    ),
)
def test_changed_source_fails_closed(source):
    result = check_before_write(_expectation(), source=source, target=None)

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "source_precondition_changed",
    )


def test_existing_target_is_a_conflict_not_an_overwrite():
    result = check_before_write(
        _expectation(),
        source=_source(),
        target=RemoteObjectState("other-file", "target", "after.mkv"),
    )

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "target_already_exists",
    )


def test_missing_source_is_uncertain_not_a_new_write_attempt():
    result = check_before_write(_expectation(), source=None, target=None)

    assert (result.status, result.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "source_not_observed",
    )


@pytest.mark.parametrize(
    "observed",
    (
        RemoteObjectState("file-1", "target", "unexpected.mkv"),
        RemoteObjectState("other-file", "target", "after.mkv"),
        RemoteObjectState("file-1", "other-parent", "after.mkv"),
    ),
)
def test_confirmed_postcondition_requires_the_same_object_parent_and_name(observed):
    assert check_after_write(_expectation(), observed=_target()).status is (
        OrganizationStepCheck.ALREADY_APPLIED
    )
    mismatch = check_after_write(_expectation(), observed=observed)
    assert (mismatch.status, mismatch.error_code) == (
        OrganizationStepCheck.CONFLICT,
        "postcondition_mismatch",
    )


def test_timeout_is_only_resolved_by_exact_postcondition():
    success = check_after_timeout(_expectation(), observed=_target())
    unknown = check_after_timeout(_expectation(), observed=_source())
    missing = check_after_timeout(_expectation(), observed=None)

    assert success.status is OrganizationStepCheck.ALREADY_APPLIED
    assert (unknown.status, unknown.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "timeout",
    )
    assert (missing.status, missing.error_code) == (
        OrganizationStepCheck.UNCERTAIN,
        "timeout",
    )


def test_uncertain_reconciliation_distinguishes_source_target_and_unknown():
    expectation = _expectation()

    assert reconcile_uncertain(expectation, observed=_target()).status is (
        OrganizationStepCheck.ALREADY_APPLIED
    )
    assert reconcile_uncertain(expectation, observed=_source()).status is (
        OrganizationStepCheck.NOT_APPLIED
    )
    assert reconcile_uncertain(expectation, observed=None).status is (
        OrganizationStepCheck.UNCERTAIN
    )
    assert reconcile_uncertain(
        expectation,
        observed=RemoteObjectState("file-1", "other-parent", "after.mkv"),
    ).status is OrganizationStepCheck.UNCERTAIN
def test_diagnostics_never_render_remote_identity_or_name():
    expectation = _expectation()
    source = _source()
    rendered = repr(expectation) + repr(source)

    for value in ("file-1", "source", "before.mkv", "target", "after.mkv"):
        assert value not in rendered


@pytest.mark.parametrize("factory", (OrganizationStepExpectation, RemoteObjectState))
def test_invalid_remote_values_are_rejected(factory):
    with pytest.raises(ValueError):
        if factory is OrganizationStepExpectation:
            factory("file-1", "source", "before.mkv", "target", "")
        else:
            factory("file-1", "", "before.mkv")
