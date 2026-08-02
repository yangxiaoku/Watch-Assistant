"""Pure O02 pre/postcondition checks for a future organization transport.

This module has no client, database, worker, or retry loop.  It only decides
whether a uniquely identified remote object is safe to execute, already at its
planned target, conflicted, or too ambiguous to touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrganizationStepCheck(StrEnum):
    READY = "ready"
    ALREADY_APPLIED = "already_applied"
    NOT_APPLIED = "not_applied"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True, repr=False)
class RemoteObjectState:
    """One redacted remote observation supplied by a future read-only gateway."""

    object_id: str
    parent_id: str
    name: str

    def __post_init__(self) -> None:
        for value in (self.object_id, self.parent_id, self.name):
            if not isinstance(value, str) or not value:
                raise ValueError("invalid_remote_state")

    def __repr__(self) -> str:
        return "RemoteObjectState(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationStepExpectation:
    """The immutable source and target state encoded by one approved plan step."""

    object_id: str
    source_parent_id: str
    source_name: str
    target_parent_id: str
    target_name: str

    def __post_init__(self) -> None:
        for value in (
            self.object_id,
            self.source_parent_id,
            self.source_name,
            self.target_parent_id,
            self.target_name,
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("invalid_step_expectation")

    def __repr__(self) -> str:
        return "OrganizationStepExpectation(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationStepCheckResult:
    status: OrganizationStepCheck
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            f"OrganizationStepCheckResult(status={self.status.value!r}, "
            f"error_code={self.error_code!r})"
        )


def check_before_write(
    expectation: OrganizationStepExpectation,
    *,
    source: RemoteObjectState | None,
    target: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    """Allow only an unchanged source and an absent target to reach transport."""

    if _matches_target(expectation, source):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    if source is None:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "source_not_observed"
        )
    if not _matches_source(expectation, source):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "source_precondition_changed"
        )
    if target is not None:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "target_already_exists"
        )
    return OrganizationStepCheckResult(OrganizationStepCheck.READY)


def check_after_write(
    expectation: OrganizationStepExpectation,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    """Recognize only the exact postcondition after a confirmed write result."""

    if _matches_target(expectation, observed):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    if observed is None:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "postcondition_not_observed"
        )
    return OrganizationStepCheckResult(
        OrganizationStepCheck.CONFLICT, "postcondition_mismatch"
    )


def check_after_timeout(
    expectation: OrganizationStepExpectation,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    """A timeout is only resolved by the exact completed postcondition."""

    result = check_after_write(expectation, observed=observed)
    if result.status is OrganizationStepCheck.ALREADY_APPLIED:
        return result
    return OrganizationStepCheckResult(OrganizationStepCheck.UNCERTAIN, "timeout")


def reconcile_uncertain(
    expectation: OrganizationStepExpectation,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    """Resolve an uncertain write using one read-only remote observation.

    The source state proves that the write was not applied and is safe to
    expose for an explicit retry.  Any other non-target state remains
    uncertain; the reconciler must never infer success from a partial read.
    """

    if _matches_target(expectation, observed):
        return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
    if _matches_source(expectation, observed):
        return OrganizationStepCheckResult(OrganizationStepCheck.NOT_APPLIED)
    if observed is None:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "reconciliation_unobserved"
        )
    return OrganizationStepCheckResult(
        OrganizationStepCheck.UNCERTAIN, "reconciliation_mismatch"
    )


def _matches_source(
    expectation: OrganizationStepExpectation, observed: RemoteObjectState | None
) -> bool:
    return observed is not None and (
        observed.object_id == expectation.object_id
        and observed.parent_id == expectation.source_parent_id
        and observed.name == expectation.source_name
    )


def _matches_target(
    expectation: OrganizationStepExpectation, observed: RemoteObjectState | None
) -> bool:
    return observed is not None and (
        observed.object_id == expectation.object_id
        and observed.parent_id == expectation.target_parent_id
        and observed.name == expectation.target_name
    )


__all__ = [
    "OrganizationStepCheck",
    "OrganizationStepCheckResult",
    "OrganizationStepExpectation",
    "RemoteObjectState",
    "check_after_timeout",
    "check_after_write",
    "check_before_write",
    "reconcile_uncertain",
]
