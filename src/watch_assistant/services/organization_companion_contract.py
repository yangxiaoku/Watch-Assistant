"""Pure all-or-nothing checks for a media item and its companion files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    OrganizationStepCheckResult,
    OrganizationStepExpectation,
    RemoteObjectState,
    check_after_timeout,
    check_after_write,
    check_before_write,
)


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationCompanionGroup:
    """Immutable expectations for one primary file and its companions."""

    main: OrganizationStepExpectation
    companions: tuple[OrganizationStepExpectation, ...] = ()

    def __post_init__(self) -> None:
        steps = self.steps
        if not all(_valid_expectation(step) for step in steps):
            raise ValueError("invalid_group_member")
        object_ids = [step.object_id for step in steps]
        if len(set(object_ids)) != len(object_ids):
            raise ValueError("duplicate_group_object")
        targets = {
            (step.target_parent_id, step.target_name.casefold()) for step in steps
        }
        if len(targets) != len(steps):
            raise ValueError("duplicate_group_target")

    @property
    def steps(self) -> tuple[OrganizationStepExpectation, ...]:
        return (self.main, *self.companions)

    def __repr__(self) -> str:
        return f"OrganizationCompanionGroup(step_count={len(self.steps)})"


def resolve_organization_companion_group(
    main: OrganizationStepExpectation,
    *,
    companions: Sequence[OrganizationStepExpectation] = (),
) -> OrganizationCompanionGroup | None:
    """Build one deterministic group or reject incomplete plan input."""

    if (
        not isinstance(main, OrganizationStepExpectation)
        or not isinstance(companions, Sequence)
        or isinstance(companions, (str, bytes))
        or not all(isinstance(step, OrganizationStepExpectation) for step in companions)
    ):
        return None
    try:
        ordered = tuple(sorted(companions, key=lambda step: step.object_id))
        return OrganizationCompanionGroup(main, ordered)
    except (TypeError, ValueError):
        return None


resolve_companion_group = resolve_organization_companion_group


def check_group_before_write(
    group: OrganizationCompanionGroup,
    *,
    sources: Mapping[str, RemoteObjectState | None],
    targets: Mapping[tuple[str, str], RemoteObjectState | None],
) -> OrganizationStepCheckResult:
    """Require every member to be ready before any member can be written."""

    shape = _validate_observation_maps(group, sources=sources, targets=targets)
    if shape is not None:
        return shape
    statuses = tuple(
        _check_step_before_write(
            step,
            source=sources.get(step.object_id),
            target=targets.get((step.target_parent_id, step.target_name)),
        )
        for step in group.steps
    )
    return _aggregate_before_write(statuses)


def check_group_after_write(
    group: OrganizationCompanionGroup,
    *,
    observed: Mapping[str, RemoteObjectState | None],
) -> OrganizationStepCheckResult:
    """Accept a write only when every member has its exact target state."""

    shape = _validate_observed_map(group, observed)
    if shape is not None:
        return shape
    statuses = tuple(
        check_after_write(step, observed=observed.get(step.object_id)).status
        for step in group.steps
    )
    if all(status is OrganizationStepCheck.ALREADY_APPLIED for status in statuses):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    if any(status is OrganizationStepCheck.CONFLICT for status in statuses):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "group_postcondition_conflict"
        )
    return OrganizationStepCheckResult(
        OrganizationStepCheck.UNCERTAIN, "group_postcondition_unverified"
    )


def check_group_after_timeout(
    group: OrganizationCompanionGroup,
    *,
    observed: Mapping[str, RemoteObjectState | None],
) -> OrganizationStepCheckResult:
    """Resolve a timeout only when every member reached its exact target."""

    shape = _validate_observed_map(group, observed)
    if shape is not None:
        return shape
    statuses = tuple(
        check_after_timeout(step, observed=observed.get(step.object_id)).status
        for step in group.steps
    )
    if all(status is OrganizationStepCheck.ALREADY_APPLIED for status in statuses):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    return OrganizationStepCheckResult(OrganizationStepCheck.UNCERTAIN, "timeout")


def _check_step_before_write(
    step: OrganizationStepExpectation,
    *,
    source: RemoteObjectState | None,
    target: RemoteObjectState | None,
) -> OrganizationStepCheck:
    if target is not None and not _same_target(step, source):
        return OrganizationStepCheck.CONFLICT
    return check_before_write(step, source=source, target=target).status


def _aggregate_before_write(
    statuses: Sequence[OrganizationStepCheck],
) -> OrganizationStepCheckResult:
    if all(status is OrganizationStepCheck.READY for status in statuses):
        return OrganizationStepCheckResult(OrganizationStepCheck.READY)
    if all(status is OrganizationStepCheck.ALREADY_APPLIED for status in statuses):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    if any(status is OrganizationStepCheck.CONFLICT for status in statuses):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "group_precondition_conflict"
        )
    if any(status is OrganizationStepCheck.UNCERTAIN for status in statuses):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_precondition_unverified"
        )
    return OrganizationStepCheckResult(
        OrganizationStepCheck.CONFLICT, "group_partial_state"
    )


def _validate_observation_maps(
    group: OrganizationCompanionGroup,
    *,
    sources: Mapping[str, RemoteObjectState | None],
    targets: Mapping[tuple[str, str], RemoteObjectState | None],
) -> OrganizationStepCheckResult | None:
    if not isinstance(sources, Mapping) or not isinstance(targets, Mapping):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_precondition_unverified"
        )
    expected_ids = {step.object_id for step in group.steps}
    expected_targets = {
        (step.target_parent_id, step.target_name) for step in group.steps
    }
    if set(sources) - expected_ids or set(targets) - expected_targets:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "group_member_set_mismatch"
        )
    if any(
        value is not None and not _valid_state(value)
        for value in (*sources.values(), *targets.values())
    ):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_precondition_unverified"
        )
    return None


def _validate_observed_map(
    group: OrganizationCompanionGroup,
    observed: Mapping[str, RemoteObjectState | None],
) -> OrganizationStepCheckResult | None:
    if not isinstance(observed, Mapping):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_postcondition_unverified"
        )
    expected_ids = {step.object_id for step in group.steps}
    if set(observed) - expected_ids:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_member_set_mismatch"
        )
    if any(
        value is not None and not _valid_state(value) for value in observed.values()
    ):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "group_postcondition_unverified"
        )
    return None


def _same_target(
    expectation: OrganizationStepExpectation,
    observed: RemoteObjectState | None,
) -> bool:
    return (
        observed is not None
        and observed.object_id == expectation.object_id
        and observed.parent_id == expectation.target_parent_id
        and observed.name == expectation.target_name
    )


def _valid_expectation(value: object) -> bool:
    return (
        isinstance(value, OrganizationStepExpectation)
        and all(
            _safe_identity(item)
            for item in (
                value.object_id,
                value.source_parent_id,
                value.target_parent_id,
            )
        )
        and _safe_name(value.source_name)
        and _safe_name(value.target_name)
    )


def _valid_state(value: object) -> bool:
    return (
        isinstance(value, RemoteObjectState)
        and _safe_identity(value.object_id)
        and _safe_identity(value.parent_id)
        and _safe_name(value.name)
    )


def _safe_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and value.isascii()
        and not any(character.isspace() for character in value)
        and not any(marker in value for marker in ("/", "\\", "\x00", "://"))
    )


def _safe_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4096
        and value not in {".", ".."}
        and not any(marker in value for marker in ("/", "\\", "\x00", "://"))
    )


__all__ = [
    "OrganizationCompanionGroup",
    "check_group_after_timeout",
    "check_group_after_write",
    "check_group_before_write",
    "resolve_companion_group",
    "resolve_organization_companion_group",
]
