"""Offline-only P115 organization transport with an explicit plan scope."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import (
    OrganizationTransportOperation,
    OrganizationTransportResult,
    OrganizationTransportStatus,
)


class P115OrganizationTransportError(RuntimeError):
    """Stable, redacted error raised by the offline transport boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class P115OrganizationMethod(StrEnum):
    READ_OBJECT = "read_object"
    READ_TARGET = "read_target"
    MOVE = "move"
    RENAME = "rename"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationObjectIntent:
    """Immutable identity and scope copied from one approved plan member."""

    object_id: str
    source_parent_id: str
    source_name: str
    target_parent_id: str
    target_name: str

    def __post_init__(self) -> None:
        if any(
            _stable_id(value) is None
            for value in (
                self.object_id,
                self.source_parent_id,
                self.target_parent_id,
            )
        ) or any(
            not _safe_name(value) for value in (self.source_name, self.target_name)
        ):
            raise ValueError("invalid_organization_intent")

    def __repr__(self) -> str:
        return "OrganizationObjectIntent(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationTransportCall:
    operation: P115OrganizationMethod

    def __repr__(self) -> str:
        return f"OrganizationTransportCall(operation={self.operation.value!r})"


Outcome = (
    RemoteObjectState | OrganizationTransportResult | BaseException | None | object
)
_MISSING = object()


class OfflineP115OrganizationTransport:
    """In-memory implementation of ``OrganizationExecutorTransport``.

    No credential source, P115 client, network hook, or retry loop exists here.
    Every read and write must match the scope frozen at construction time.
    """

    def __init__(
        self,
        *,
        intents: Collection[OrganizationObjectIntent],
        managed_directory_ids: Collection[str],
        scope_confirmed: bool,
        states: Mapping[str, RemoteObjectState] | None = None,
        outcomes: Mapping[tuple[P115OrganizationMethod, str], Outcome] | None = None,
    ) -> None:
        intent_items = tuple(intents)
        managed_ids = frozenset(managed_directory_ids)
        if not intent_items or len({item.object_id for item in intent_items}) != len(
            intent_items
        ):
            raise ValueError("invalid_organization_scope")
        if not managed_ids or any(_stable_id(value) is None for value in managed_ids):
            raise ValueError("invalid_organization_scope")
        if any(
            intent.source_parent_id not in managed_ids
            or intent.target_parent_id not in managed_ids
            for intent in intent_items
        ):
            raise ValueError("invalid_organization_scope")
        targets = {
            (intent.target_parent_id, intent.target_name) for intent in intent_items
        }
        if len(targets) != len(intent_items):
            raise ValueError("invalid_organization_scope")
        if not isinstance(scope_confirmed, bool):
            raise TypeError("invalid_organization_scope")

        initial_states = dict(states or {})
        if any(
            key != state.object_id or _stable_id(key) is None
            for key, state in initial_states.items()
            if isinstance(state, RemoteObjectState)
        ) or any(
            not isinstance(state, RemoteObjectState)
            for state in initial_states.values()
        ):
            raise ValueError("invalid_remote_state")

        self._intents = {item.object_id: item for item in intent_items}
        self._targets = targets
        self._managed_directory_ids = managed_ids
        self._scope_confirmed = scope_confirmed
        self._states = initial_states
        self._outcomes = dict(outcomes or {})
        self._calls: list[OrganizationTransportCall] = []

    def __repr__(self) -> str:
        return (
            "OfflineP115OrganizationTransport(mode='offline_fake', "
            f"call_count={len(self._calls)})"
        )

    @property
    def calls(self) -> tuple[OrganizationTransportCall, ...]:
        return tuple(self._calls)

    async def read_object(self, object_id: str) -> RemoteObjectState | None:
        intent = self._intent(object_id)
        self._calls.append(
            OrganizationTransportCall(P115OrganizationMethod.READ_OBJECT)
        )
        outcome = self._injected(P115OrganizationMethod.READ_OBJECT, object_id)
        state = self._states.get(intent.object_id) if outcome is _MISSING else outcome
        if state is None:
            return None
        if not isinstance(state, RemoteObjectState) or not self._valid_observation(
            state, intent=intent
        ):
            raise P115OrganizationTransportError("observation_unverified")
        return state

    async def read_target(self, parent_id: str, name: str) -> RemoteObjectState | None:
        self._require_scope()
        if (parent_id, name) not in self._targets:
            raise P115OrganizationTransportError("scope_unverified")
        self._calls.append(
            OrganizationTransportCall(P115OrganizationMethod.READ_TARGET)
        )
        outcome = self._injected(P115OrganizationMethod.READ_TARGET, parent_id)
        matches = (
            [
                state
                for state in self._states.values()
                if state.parent_id == parent_id and state.name == name
            ]
            if outcome is _MISSING
            else []
            if outcome is None
            else [outcome]
        )
        if len(matches) > 1 or any(
            not isinstance(state, RemoteObjectState)
            or state.parent_id != parent_id
            or state.name != name
            or state.parent_id not in self._managed_directory_ids
            for state in matches
        ):
            raise P115OrganizationTransportError("observation_unverified")
        return matches[0] if matches else None

    async def move(
        self, object_id: str, target_parent_id: str
    ) -> OrganizationTransportResult:
        intent = self._intent(object_id)
        if target_parent_id != intent.target_parent_id:
            raise P115OrganizationTransportError("scope_unverified")
        self._calls.append(OrganizationTransportCall(P115OrganizationMethod.MOVE))
        outcome = self._write_outcome(
            P115OrganizationMethod.MOVE,
            OrganizationTransportOperation.MOVE,
            object_id,
        )
        if outcome is not None:
            return outcome
        state = self._states.get(object_id)
        if state is None or not self._valid_observation(state, intent=intent):
            return _uncertain(OrganizationTransportOperation.MOVE)
        if state.parent_id != intent.source_parent_id:
            return _uncertain(OrganizationTransportOperation.MOVE)
        self._states[object_id] = RemoteObjectState(
            object_id, intent.target_parent_id, state.name
        )
        return _success(OrganizationTransportOperation.MOVE)

    async def rename(
        self, object_id: str, target_name: str
    ) -> OrganizationTransportResult:
        intent = self._intent(object_id)
        if target_name != intent.target_name:
            raise P115OrganizationTransportError("scope_unverified")
        self._calls.append(OrganizationTransportCall(P115OrganizationMethod.RENAME))
        outcome = self._write_outcome(
            P115OrganizationMethod.RENAME,
            OrganizationTransportOperation.RENAME,
            object_id,
        )
        if outcome is not None:
            return outcome
        state = self._states.get(object_id)
        if state is None or not self._valid_observation(state, intent=intent):
            return _uncertain(OrganizationTransportOperation.RENAME)
        if state.parent_id != intent.target_parent_id:
            return _uncertain(OrganizationTransportOperation.RENAME)
        self._states[object_id] = RemoteObjectState(
            object_id, state.parent_id, intent.target_name
        )
        return _success(OrganizationTransportOperation.RENAME)

    def _intent(self, object_id: str) -> OrganizationObjectIntent:
        self._require_scope()
        if _stable_id(object_id) is None or object_id not in self._intents:
            raise P115OrganizationTransportError("scope_unverified")
        return self._intents[object_id]

    def _require_scope(self) -> None:
        if self._scope_confirmed is not True:
            raise P115OrganizationTransportError("scope_unverified")

    def _valid_observation(
        self, state: RemoteObjectState, *, intent: OrganizationObjectIntent
    ) -> bool:
        return (
            state.object_id == intent.object_id
            and state.parent_id in {intent.source_parent_id, intent.target_parent_id}
            and state.parent_id in self._managed_directory_ids
            and state.name in {intent.source_name, intent.target_name}
        )

    def _injected(self, method: P115OrganizationMethod, identity: str) -> Outcome:
        key = (method, identity)
        if key not in self._outcomes:
            return _MISSING
        outcome = self._outcomes[key]
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, TimeoutError):
            raise outcome
        if isinstance(outcome, BaseException):
            raise P115OrganizationTransportError("outcome_unknown") from None
        return outcome

    def _write_outcome(
        self,
        method: P115OrganizationMethod,
        operation: OrganizationTransportOperation,
        object_id: str,
    ) -> OrganizationTransportResult | None:
        outcome = self._injected(method, object_id)
        if outcome is _MISSING:
            return None
        if (
            not isinstance(outcome, OrganizationTransportResult)
            or outcome.operation is not operation
            or not isinstance(outcome.status, OrganizationTransportStatus)
        ):
            return _uncertain(operation)
        if outcome.status is OrganizationTransportStatus.SUCCESS:
            return _success(operation)
        if outcome.status is OrganizationTransportStatus.FAILED:
            return OrganizationTransportResult(
                operation, OrganizationTransportStatus.FAILED, "remote_failed"
            )
        return OrganizationTransportResult(
            operation,
            OrganizationTransportStatus.UNCERTAIN,
            "timeout" if outcome.error_code == "timeout" else "outcome_unknown",
        )


def create_p115_organization_transport(
    *,
    intents: Collection[OrganizationObjectIntent],
    managed_directory_ids: Collection[str],
    scope_confirmed: bool,
    states: Mapping[str, RemoteObjectState] | None = None,
    outcomes: Mapping[tuple[P115OrganizationMethod, str], Outcome] | None = None,
    live: bool = False,
) -> OfflineP115OrganizationTransport:
    """Create the fake-only transport; a live implementation is not available."""

    if live is not False:
        raise P115OrganizationTransportError("live_transport_disabled")
    return OfflineP115OrganizationTransport(
        intents=intents,
        managed_directory_ids=managed_directory_ids,
        scope_confirmed=scope_confirmed,
        states=states,
        outcomes=outcomes,
    )


def _stable_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
        return None
    return value


def _safe_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 255
        and not any(char in value for char in ("\x00", "/", "\\"))
        and value not in {".", ".."}
    )


def _success(operation: OrganizationTransportOperation) -> OrganizationTransportResult:
    return OrganizationTransportResult(operation, OrganizationTransportStatus.SUCCESS)


def _uncertain(
    operation: OrganizationTransportOperation,
) -> OrganizationTransportResult:
    return OrganizationTransportResult(
        operation, OrganizationTransportStatus.UNCERTAIN, "outcome_unknown"
    )


__all__ = [
    "OfflineP115OrganizationTransport",
    "OrganizationObjectIntent",
    "OrganizationTransportCall",
    "P115OrganizationMethod",
    "P115OrganizationTransportError",
    "create_p115_organization_transport",
]
