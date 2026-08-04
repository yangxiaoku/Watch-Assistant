"""Offline-only P115 organization transport with an explicit plan scope."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from watch_assistant.adapters.p115_c03_live_transport import (
    P115C03CallExecutor,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationWriteGate,
    P115OrganizationContract,
    WriteOperation,
    WriteStatus,
    evaluate_organization_write_gate,
    prepare_move,
    prepare_recycle,
    prepare_rename,
)
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
    RECYCLE = "recycle"


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

    async def read_object_in_scope(
        self, object_id: str, parent_ids: Collection[str]
    ) -> RemoteObjectState | None:
        self._require_scope()
        scope = _observation_scope(parent_ids, self._managed_directory_ids)
        if _stable_id(object_id) is None or scope is None:
            raise P115OrganizationTransportError("scope_unverified")
        self._calls.append(
            OrganizationTransportCall(P115OrganizationMethod.READ_OBJECT)
        )
        outcome = self._injected(P115OrganizationMethod.READ_OBJECT, object_id)
        if outcome is _MISSING:
            matches = [
                state
                for state in self._states.values()
                if state.object_id == object_id and state.parent_id in scope
            ]
        elif outcome is None:
            matches = []
        else:
            matches = [outcome]
        if len(matches) > 1 or any(
            not isinstance(state, RemoteObjectState)
            or state.object_id != object_id
            or state.parent_id not in scope
            or state.is_directory is not False
            for state in matches
        ):
            raise P115OrganizationTransportError("observation_unverified")
        return matches[0] if matches else None

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
            object_id, intent.target_parent_id, state.name, state.is_directory
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
            object_id, state.parent_id, intent.target_name, state.is_directory
        )
        return _success(OrganizationTransportOperation.RENAME)

    async def recycle(
        self, object_id: str, parent_id: str, name: str
    ) -> OrganizationTransportResult:
        self._require_scope()
        if (
            _stable_id(object_id) is None
            or parent_id not in self._managed_directory_ids
            or (parent_id, name) not in self._targets
        ):
            raise P115OrganizationTransportError("scope_unverified")
        self._calls.append(OrganizationTransportCall(P115OrganizationMethod.RECYCLE))
        outcome = self._write_outcome(
            P115OrganizationMethod.RECYCLE,
            OrganizationTransportOperation.RECYCLE,
            object_id,
        )
        if outcome is not None:
            return outcome
        state = self._states.get(object_id)
        if (
            state is None
            or state.parent_id != parent_id
            or state.name != name
            or state.parent_id not in self._managed_directory_ids
            or state.is_directory is not False
        ):
            return _uncertain(OrganizationTransportOperation.RECYCLE)
        del self._states[object_id]
        return _success(OrganizationTransportOperation.RECYCLE)

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
            and state.is_directory is False
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


class LiveP115OrganizationTransport:
    """Bounded live transport for one already-approved organization scope.

    Client construction and credential loading stay outside this boundary. The
    caller must provide the fixed-version client and a call-level timeout
    executor. Reads are reduced to stable object state and writes are reduced
    to one receipt before the executor performs its read-after-write check.
    """

    def __init__(
        self,
        *,
        client: Any,
        call_executor: P115C03CallExecutor,
        intents: Collection[OrganizationObjectIntent],
        managed_directory_ids: Collection[str],
        scope_confirmed: bool,
        timeout_seconds: float = 30.0,
        live_enabled: bool = False,
        write_enabled: bool = False,
        plan_confirmed: bool = False,
        read_only: bool = False,
        organization_contract: P115OrganizationContract | None = None,
    ) -> None:
        if live_enabled is not True:
            raise P115OrganizationTransportError("live_transport_disabled")
        if client is None or not callable(call_executor):
            raise P115OrganizationTransportError("live_transport_unavailable")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid_timeout")
        intent_items = tuple(intents)
        managed_ids = frozenset(managed_directory_ids)
        _validate_live_scope(intent_items, managed_ids, scope_confirmed)
        if not isinstance(organization_contract, P115OrganizationContract):
            raise P115OrganizationTransportError("organization_contract_required")
        self._intents = {item.object_id: item for item in intent_items}
        self._targets = {
            (intent.target_parent_id, intent.target_name) for intent in intent_items
        }
        self._managed_directory_ids = managed_ids
        self._scope_confirmed = scope_confirmed
        self._timeout_seconds = float(timeout_seconds)
        self._write_enabled = write_enabled
        self._plan_confirmed = plan_confirmed
        if not isinstance(read_only, bool):
            raise TypeError("invalid_transport_mode")
        self._read_only = read_only
        self._c03 = P115C03LiveTransport(client, call_executor=call_executor)
        self._client = client
        self._call_executor = call_executor
        self._organization_contract = organization_contract
        self._receipts: list[OrganizationTransportResult] = []
        self._require_read_scope()
        if not self._read_only:
            for operation in (WriteOperation.MOVE, WriteOperation.RENAME):
                self._require_write(operation)

    def __repr__(self) -> str:
        return (
            "LiveP115OrganizationTransport(mode='live', client='injected', "
            f"receipt_count={len(self._receipts)})"
        )

    @property
    def receipts(self) -> tuple[OrganizationTransportResult, ...]:
        """Normalized write receipts, without provider response details."""

        return tuple(self._receipts)

    async def read_object(self, object_id: str) -> RemoteObjectState | None:
        intent = self._intent(object_id)
        observations: list[RemoteObjectState] = []
        for parent_id in (
            intent.source_parent_id,
            *(
                (intent.target_parent_id,)
                if intent.target_parent_id != intent.source_parent_id
                else ()
            ),
        ):
            listing = await self._c03.list_children(
                parent_id, timeout_seconds=self._timeout_seconds
            )
            if listing.complete is not True:
                raise P115OrganizationTransportError("observation_unverified")
            observations.extend(
                RemoteObjectState(
                    entry.file_id, entry.parent_id, entry.name, entry.is_directory
                )
                for entry in listing.entries
                if entry.file_id == object_id
            )
        if len(observations) > 1:
            raise P115OrganizationTransportError("observation_unverified")
        if not observations:
            return None
        state = observations[0]
        if not _valid_live_observation(
            state, intent=intent, managed_directory_ids=self._managed_directory_ids
        ):
            raise P115OrganizationTransportError("observation_unverified")
        return state

    async def read_object_in_scope(
        self, object_id: str, parent_ids: Collection[str]
    ) -> RemoteObjectState | None:
        self._require_scope()
        scope = _observation_scope(parent_ids, self._managed_directory_ids)
        if _stable_id(object_id) is None or scope is None:
            raise P115OrganizationTransportError("scope_unverified")
        observations: list[RemoteObjectState] = []
        for parent_id in sorted(scope):
            listing = await self._c03.list_children(
                parent_id, timeout_seconds=self._timeout_seconds
            )
            if listing.complete is not True:
                raise P115OrganizationTransportError("observation_unverified")
            observations.extend(
                RemoteObjectState(
                    entry.file_id, entry.parent_id, entry.name, entry.is_directory
                )
                for entry in listing.entries
                if entry.file_id == object_id
            )
        if len(observations) > 1:
            raise P115OrganizationTransportError("observation_unverified")
        if not observations:
            return None
        state = observations[0]
        if (
            state.parent_id not in scope
            or state.parent_id not in self._managed_directory_ids
            or state.is_directory is not False
        ):
            raise P115OrganizationTransportError("observation_unverified")
        return state

    async def read_target(self, parent_id: str, name: str) -> RemoteObjectState | None:
        self._require_scope()
        if (parent_id, name) not in self._targets:
            raise P115OrganizationTransportError("scope_unverified")
        listing = await self._c03.list_children(
            parent_id, timeout_seconds=self._timeout_seconds
        )
        if listing.complete is not True:
            raise P115OrganizationTransportError("observation_unverified")
        matches = [
            RemoteObjectState(
                entry.file_id, entry.parent_id, entry.name, entry.is_directory
            )
            for entry in listing.entries
            if entry.name == name
        ]
        if len(matches) > 1:
            raise P115OrganizationTransportError("observation_unverified")
        return matches[0] if matches else None

    async def move(
        self, object_id: str, target_parent_id: str
    ) -> OrganizationTransportResult:
        self._require_write(WriteOperation.MOVE)
        intent = self._intent(object_id)
        if target_parent_id != intent.target_parent_id:
            raise P115OrganizationTransportError("scope_unverified")
        receipt = await self._c03.execute(
            prepare_move(object_id, target_parent_id),
            timeout_seconds=self._timeout_seconds,
        )
        result = _organization_result(OrganizationTransportOperation.MOVE, receipt.status)
        self._receipts.append(result)
        return result

    async def recycle(
        self, object_id: str, parent_id: str, name: str
    ) -> OrganizationTransportResult:
        self._require_write(WriteOperation.RECYCLE)
        self._require_scope()
        if (
            _stable_id(object_id) is None
            or parent_id not in self._managed_directory_ids
            or (parent_id, name) not in self._targets
            or not _safe_name(name)
        ):
            raise P115OrganizationTransportError("scope_unverified")
        receipt = await self._c03.execute(
            prepare_recycle(object_id), timeout_seconds=self._timeout_seconds
        )
        result = _organization_result(
            OrganizationTransportOperation.RECYCLE, receipt.status
        )
        self._receipts.append(result)
        return result

    async def rename(
        self, object_id: str, target_name: str
    ) -> OrganizationTransportResult:
        self._require_write(WriteOperation.RENAME)
        intent = self._intent(object_id)
        if target_name != intent.target_name:
            raise P115OrganizationTransportError("scope_unverified")
        receipt = await self._c03.execute(
            prepare_rename(object_id, target_name),
            timeout_seconds=self._timeout_seconds,
        )
        result = _organization_result(
            OrganizationTransportOperation.RENAME, receipt.status
        )
        self._receipts.append(result)
        return result

    def _intent(self, object_id: str) -> OrganizationObjectIntent:
        self._require_scope()
        if _stable_id(object_id) is None or object_id not in self._intents:
            raise P115OrganizationTransportError("scope_unverified")
        return self._intents[object_id]

    def _require_scope(self) -> None:
        if self._scope_confirmed is not True:
            raise P115OrganizationTransportError("scope_unverified")

    def _require_write(self, operation: WriteOperation) -> None:
        if self._read_only:
            raise P115OrganizationTransportError("write_disabled")
        decision = evaluate_organization_write_gate(
            OrganizationWriteGate(
                write_enabled=self._write_enabled is True,
                plan_confirmed=self._plan_confirmed is True,
                scope_confirmed=self._scope_confirmed,
                contract=self._organization_contract,
            ),
            operation,
        )
        if not decision.allowed:
            raise P115OrganizationTransportError(
                decision.error_code or "capability_unverified"
            )

    def _require_read_scope(self) -> None:
        if not self._organization_contract.supports_read_scope():
            error_code = (
                "contract_unverified"
                if not self._organization_contract.verified
                or not self._organization_contract.timeout_enforced
                or self._organization_contract.evidence is None
                else "capability_unverified"
            )
            raise P115OrganizationTransportError(error_code)


def create_p115_organization_transport(
    *,
    intents: Collection[OrganizationObjectIntent],
    managed_directory_ids: Collection[str],
    scope_confirmed: bool,
    states: Mapping[str, RemoteObjectState] | None = None,
    outcomes: Mapping[tuple[P115OrganizationMethod, str], Outcome] | None = None,
    live: bool = False,
) -> OfflineP115OrganizationTransport:
    """Create the offline transport; live construction is explicit and injected."""

    if live is not False:
        raise P115OrganizationTransportError("live_transport_disabled")
    return OfflineP115OrganizationTransport(
        intents=intents,
        managed_directory_ids=managed_directory_ids,
        scope_confirmed=scope_confirmed,
        states=states,
        outcomes=outcomes,
    )


def create_live_p115_organization_transport(
    *,
    client: Any,
    call_executor: P115C03CallExecutor,
    intents: Collection[OrganizationObjectIntent],
    managed_directory_ids: Collection[str],
    scope_confirmed: bool,
    timeout_seconds: float = 30.0,
    live_enabled: bool = False,
    write_enabled: bool = False,
    plan_confirmed: bool = False,
    read_only: bool = False,
    organization_contract: P115OrganizationContract | None = None,
) -> LiveP115OrganizationTransport:
    """Build a live transport with either a write gate or an explicit read-only mode."""

    return LiveP115OrganizationTransport(
        client=client,
        call_executor=call_executor,
        intents=intents,
        managed_directory_ids=managed_directory_ids,
        scope_confirmed=scope_confirmed,
        timeout_seconds=timeout_seconds,
        live_enabled=live_enabled,
        write_enabled=write_enabled,
        plan_confirmed=plan_confirmed,
        read_only=read_only,
        organization_contract=organization_contract,
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


def _validate_live_scope(
    intents: tuple[OrganizationObjectIntent, ...],
    managed_ids: frozenset[str],
    scope_confirmed: bool,
) -> None:
    if (
        not intents
        or len({item.object_id for item in intents}) != len(intents)
        or not managed_ids
        or any(_stable_id(value) is None for value in managed_ids)
        or scope_confirmed is not True
        or any(
            intent.source_parent_id not in managed_ids
            or intent.target_parent_id not in managed_ids
            for intent in intents
        )
        or len({(intent.target_parent_id, intent.target_name) for intent in intents})
        != len(intents)
    ):
        raise ValueError("invalid_organization_scope")


def _observation_scope(
    parent_ids: Collection[str], managed_ids: frozenset[str]
) -> frozenset[str] | None:
    if isinstance(parent_ids, (str, bytes)):
        return None
    try:
        scope = frozenset(parent_ids)
    except TypeError:
        return None
    if not scope or any(_stable_id(value) is None for value in scope):
        return None
    return scope if scope <= managed_ids else None


def _normalize_live_object_response(
    requested_id: str, response: Any
) -> RemoteObjectState | None:
    if not isinstance(response, Mapping) or response.get("state") is not True:
        return None
    detail = response.get("data")
    if not isinstance(detail, Mapping):
        detail = response
    is_directory = _live_directory_marker(detail)
    if is_directory is None:
        return None
    identity_names = (
        ("file_id", "fid")
        if not is_directory
        else ("file_id", "fid", "directory_id", "category_id", "cid")
    )
    parent_names = ("parent_id", "pid", "cid") if not is_directory else ("parent_id", "pid")
    object_id = _live_single_id(detail, identity_names)
    parent_id = _live_single_id(detail, parent_names)
    name = _live_single_text(detail)
    if object_id != requested_id or parent_id is None or name is None:
        return None
    return RemoteObjectState(object_id, parent_id, name, is_directory)


def _live_directory_marker(record: Mapping[str, Any]) -> bool | None:
    values: list[bool] = []
    for name in ("is_dir", "is_directory"):
        if name in record:
            if not isinstance(record[name], bool):
                return None
            values.append(record[name])
    for name in ("fc", "file_category"):
        if name in record:
            value = record[name]
            if isinstance(value, bool) or value not in (0, 1, "0", "1"):
                return None
            values.append(value in (0, "0"))
    return values[0] if values and len(set(values)) == 1 else None


def _live_single_id(record: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for name in names:
        if name not in record:
            continue
        value = record[name]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        normalized = str(value)
        if not normalized.isdigit() or normalized.startswith("0"):
            return None
        values.append(normalized)
    return values[0] if values and len(set(values)) == 1 else None


def _live_single_text(record: Mapping[str, Any]) -> str | None:
    values: list[str] = []
    for name in ("name", "n", "fn", "file_name", "category_name"):
        if name in record:
            value = record[name]
            if not isinstance(value, str) or not value or "\x00" in value:
                return None
            values.append(value)
    return values[0] if values and len(set(values)) == 1 else None


def _valid_live_observation(
    state: RemoteObjectState,
    *,
    intent: OrganizationObjectIntent,
    managed_directory_ids: frozenset[str],
) -> bool:
    return (
        state.object_id == intent.object_id
        and state.parent_id in {intent.source_parent_id, intent.target_parent_id}
        and state.parent_id in managed_directory_ids
        and state.name in {intent.source_name, intent.target_name}
        and state.is_directory is False
    )


def _organization_result(
    operation: OrganizationTransportOperation, status: WriteStatus
) -> OrganizationTransportResult:
    if status is WriteStatus.SUCCESS:
        return OrganizationTransportResult(operation, OrganizationTransportStatus.SUCCESS)
    if status is WriteStatus.FAILED:
        return OrganizationTransportResult(
            operation, OrganizationTransportStatus.FAILED, "remote_failed"
        )
    return OrganizationTransportResult(
        operation, OrganizationTransportStatus.UNCERTAIN, "outcome_unknown"
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
    "LiveP115OrganizationTransport",
    "OfflineP115OrganizationTransport",
    "OrganizationObjectIntent",
    "OrganizationTransportCall",
    "P115OrganizationMethod",
    "P115OrganizationTransportError",
    "create_live_p115_organization_transport",
    "create_p115_organization_transport",
]
