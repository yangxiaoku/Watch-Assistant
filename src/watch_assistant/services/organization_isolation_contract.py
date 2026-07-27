"""Pure local preconditions for managed isolation and restore plans."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    OrganizationStepCheckResult,
    OrganizationStepExpectation,
    RemoteObjectState,
    check_after_timeout,
    check_after_write,
)


class OrganizationIsolationAction(StrEnum):
    ISOLATE = "isolate"
    RESTORE = "restore"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationIsolationIntent:
    """Immutable, scoped intent for one isolation or restore step."""

    action: OrganizationIsolationAction
    object_id: str
    original_source_parent_id: str
    original_source_name: str
    isolation_parent_id: str
    isolation_name: str
    original_target_parent_id: str
    original_target_name: str
    rule_version: str
    managed_directory_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.action, OrganizationIsolationAction):
            raise TypeError("invalid_isolation_intent")
        identities = (
            self.object_id,
            self.original_source_parent_id,
            self.isolation_parent_id,
            self.original_target_parent_id,
            self.rule_version,
        )
        names = (
            self.original_source_name,
            self.isolation_name,
            self.original_target_name,
        )
        if not all(_safe_identity(value) for value in identities) or not all(
            _safe_name(value) for value in names
        ):
            raise ValueError("invalid_isolation_intent")
        if not isinstance(self.managed_directory_ids, frozenset) or not all(
            _safe_identity(value) for value in self.managed_directory_ids
        ):
            raise ValueError("invalid_isolation_scope")
        required = {
            self.original_source_parent_id,
            self.isolation_parent_id,
            self.original_target_parent_id,
        }
        if not required <= self.managed_directory_ids:
            raise ValueError("invalid_isolation_scope")

    @property
    def expectation(self) -> OrganizationStepExpectation:
        if self.action is OrganizationIsolationAction.ISOLATE:
            return OrganizationStepExpectation(
                self.object_id,
                self.original_source_parent_id,
                self.original_source_name,
                self.isolation_parent_id,
                self.isolation_name,
            )
        return OrganizationStepExpectation(
            self.object_id,
            self.isolation_parent_id,
            self.isolation_name,
            self.original_target_parent_id,
            self.original_target_name,
        )

    def __repr__(self) -> str:
        return (
            "OrganizationIsolationIntent(action="
            f"{self.action.value!r}, rule_version=<redacted>, "
            "scope=<redacted>)"
        )


def resolve_isolation_intent(
    action: OrganizationIsolationAction,
    *,
    object_id: str,
    original_source_parent_id: str,
    original_source_name: str,
    isolation_parent_id: str,
    isolation_name: str,
    original_target_parent_id: str,
    original_target_name: str,
    rule_version: str,
    managed_directory_ids: Iterable[str],
) -> OrganizationIsolationIntent | None:
    """Create a complete managed intent, or reject incomplete input."""

    if isinstance(managed_directory_ids, (str, bytes)):
        return None
    try:
        scope = frozenset(managed_directory_ids)
        return OrganizationIsolationIntent(
            action,
            object_id,
            original_source_parent_id,
            original_source_name,
            isolation_parent_id,
            isolation_name,
            original_target_parent_id,
            original_target_name,
            rule_version,
            scope,
        )
    except (TypeError, ValueError):
        return None


def check_isolation_before_write(
    intent: OrganizationIsolationIntent,
    *,
    source: RemoteObjectState | None,
    target: RemoteObjectState | None,
    timed_out: bool = False,
) -> OrganizationStepCheckResult:
    return _check_before_write(
        intent, source=source, target=target, timed_out=timed_out
    )


def check_restore_before_write(
    intent: OrganizationIsolationIntent,
    *,
    source: RemoteObjectState | None,
    target: RemoteObjectState | None,
    timed_out: bool = False,
) -> OrganizationStepCheckResult:
    return _check_before_write(
        intent, source=source, target=target, timed_out=timed_out
    )


def check_isolation_after_write(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    return _check_after_write(intent, observed=observed)


def check_restore_after_write(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    return _check_after_write(intent, observed=observed)


def check_isolation_after_timeout(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    return _check_after_timeout(intent, observed=observed)


def check_restore_after_timeout(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    return _check_after_timeout(intent, observed=observed)


def _check_before_write(
    intent: OrganizationIsolationIntent,
    *,
    source: RemoteObjectState | None,
    target: RemoteObjectState | None,
    timed_out: bool,
) -> OrganizationStepCheckResult:
    if not isinstance(intent, OrganizationIsolationIntent):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "invalid_isolation_intent"
        )
    if timed_out:
        return OrganizationStepCheckResult(OrganizationStepCheck.UNCERTAIN, "timeout")
    if not _valid_observation(source) or not _valid_observation(target):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "precondition_unverified"
        )
    if source is None:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "source_not_observed"
        )
    expectation = intent.expectation
    if _matches_target(expectation, source):
        if target is None or _matches_target(expectation, target):
            return OrganizationStepCheckResult(OrganizationStepCheck.ALREADY_APPLIED)
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "partial_state"
        )
    if not _matches_source(expectation, source):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "source_precondition_changed"
        )
    if target is None:
        return OrganizationStepCheckResult(OrganizationStepCheck.READY)
    if target.object_id == expectation.object_id:
        return OrganizationStepCheckResult(
            OrganizationStepCheck.CONFLICT, "partial_state"
        )
    return OrganizationStepCheckResult(
        OrganizationStepCheck.CONFLICT, "target_already_exists"
    )


def _check_after_write(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    if not isinstance(intent, OrganizationIsolationIntent) or not _valid_observation(
        observed
    ):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "postcondition_unverified"
        )
    return check_after_write(intent.expectation, observed=observed)


def _check_after_timeout(
    intent: OrganizationIsolationIntent,
    *,
    observed: RemoteObjectState | None,
) -> OrganizationStepCheckResult:
    if not isinstance(intent, OrganizationIsolationIntent) or not _valid_observation(
        observed
    ):
        return OrganizationStepCheckResult(
            OrganizationStepCheck.UNCERTAIN, "postcondition_unverified"
        )
    return check_after_timeout(intent.expectation, observed=observed)


def _matches_source(
    expectation: OrganizationStepExpectation, observed: RemoteObjectState | None
) -> bool:
    return (
        observed is not None
        and observed.object_id == expectation.object_id
        and observed.parent_id == expectation.source_parent_id
        and observed.name == expectation.source_name
    )


def _matches_target(
    expectation: OrganizationStepExpectation, observed: RemoteObjectState | None
) -> bool:
    return (
        observed is not None
        and observed.object_id == expectation.object_id
        and observed.parent_id == expectation.target_parent_id
        and observed.name == expectation.target_name
    )


def _valid_observation(value: RemoteObjectState | None) -> bool:
    return value is None or (
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
    "OrganizationIsolationAction",
    "OrganizationIsolationIntent",
    "check_isolation_after_timeout",
    "check_isolation_after_write",
    "check_isolation_before_write",
    "check_restore_after_timeout",
    "check_restore_after_write",
    "check_restore_before_write",
    "resolve_isolation_intent",
]
