"""Offline-first C03 fixture lifecycle boundary.

The transport is intentionally injected.  This module does not load
credentials, construct a P115 client, or perform network I/O.  A caller that
owns an authorized transport must translate the fixed payloads from
``p115_library_write_contract`` into the transport's client calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from watch_assistant.adapters.p115_library_write_contract import (
    PreparedWrite,
    WriteGate,
    WriteOperation,
    WriteStatus,
    evaluate_write_gate,
    prepare_mkdir,
    prepare_move,
    prepare_recycle,
    prepare_rename,
)

C03_WRITE_ENABLED_ENV = "WATCH_ASSISTANT_P115_C03_WRITE"
C03_MANAGED_FIXTURE_ENV = "WATCH_ASSISTANT_P115_C03_MANAGED_FIXTURE"
C03_CLEANUP_PLAN_ENV = "WATCH_ASSISTANT_P115_C03_CLEANUP_PLAN"
C03_LIVE_ENV = "WATCH_ASSISTANT_P115_C03_LIVE"
PROBE_ENABLED_VALUE = "1"

MAX_WRITE_CALLS = 10
MAX_READ_CALLS = 11
MAX_TOTAL_CALLS = MAX_WRITE_CALLS + MAX_READ_CALLS
MAX_CONFLICT_OBSERVATION_CALLS = 0
MAX_BATCH_OBSERVATION_CALLS = 0

_NONZERO_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_MAX_ID_LENGTH = 64


class C03ProbeStatus(StrEnum):
    BLOCKED = "blocked"
    SUCCESS = "success"
    UNCERTAIN = "uncertain"


class C03StepStatus(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True, repr=False)
class C03WriteReceipt:
    """A transport-normalized write result; dynamic values stay private."""

    status: WriteStatus
    file_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"C03WriteReceipt(status={self.status.value!r}, "
            f"has_file_id={self.file_id is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class C03RemoteEntry:
    """A transport-normalized read result used only for local comparison."""

    file_id: str
    parent_id: str
    name: str
    is_directory: bool = True

    def __repr__(self) -> str:
        return f"C03RemoteEntry(is_directory={self.is_directory!r})"


class P115C03Transport(Protocol):
    """Caller-owned async transport seam for C03 writes and read checks."""

    async def execute(self, request: PreparedWrite) -> C03WriteReceipt: ...

    async def read(self, file_id: str) -> C03RemoteEntry | None: ...


@dataclass(frozen=True, slots=True)
class C03CallBudget:
    max_write_calls: int = MAX_WRITE_CALLS
    max_read_calls: int = MAX_READ_CALLS
    max_total_calls: int = MAX_TOTAL_CALLS
    max_conflict_observation_calls: int = MAX_CONFLICT_OBSERVATION_CALLS
    max_batch_observation_calls: int = MAX_BATCH_OBSERVATION_CALLS

    def valid(self) -> bool:
        return (
            self.max_write_calls > 0
            and self.max_write_calls <= MAX_WRITE_CALLS
            and self.max_read_calls > 0
            and self.max_read_calls <= MAX_READ_CALLS
            and self.max_total_calls > 0
            and self.max_total_calls <= MAX_TOTAL_CALLS
            and self.max_conflict_observation_calls == 0
            and self.max_batch_observation_calls == 0
        )


@dataclass(frozen=True, slots=True, repr=False)
class C03StepReport:
    operation: str
    status: C03StepStatus

    def __repr__(self) -> str:
        return (
            f"C03StepReport(operation={self.operation!r}, status={self.status.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class C03ProbeReport:
    status: C03ProbeStatus
    fixture_fingerprint: str | None
    steps: tuple[C03StepReport, ...]
    write_calls: int
    read_calls: int
    cleanup: str
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            f"C03ProbeReport(status={self.status.value!r}, "
            f"fixture_fingerprint_present={self.fixture_fingerprint is not None}, "
            f"step_count={len(self.steps)}, write_calls={self.write_calls}, "
            f"read_calls={self.read_calls}, cleanup={self.cleanup!r}, "
            f"error_code={self.error_code!r})"
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "fixture_fingerprint": self.fixture_fingerprint,
            "steps": [
                {"operation": step.operation, "status": step.status.value}
                for step in self.steps
            ],
            "write_calls": self.write_calls,
            "read_calls": self.read_calls,
            "cleanup": self.cleanup,
            "error_code": self.error_code,
        }


@dataclass
class _ProbeState:
    write_calls: int = 0
    read_calls: int = 0
    steps: list[C03StepReport] = field(default_factory=list)
    root_id: str | None = None
    root_name: str | None = None
    root_confirmed: bool = False


class _ProbeHalt(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_parent_id(value: object) -> str:
    """Accept only a non-zero decimal directory ID, never a path or URL."""

    if isinstance(value, bool):
        raise TypeError("invalid_parent_id")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("invalid_parent_id")
        return str(value)
    if (
        not isinstance(value, str)
        or len(value) > _MAX_ID_LENGTH
        or not _NONZERO_DECIMAL.fullmatch(value)
    ):
        raise ValueError("invalid_parent_id")
    return value


def _normalize_file_id(value: object) -> str:
    if isinstance(value, bool):
        raise TypeError("invalid_remote_id")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("invalid_remote_id")
        return str(value)
    if (
        not isinstance(value, str)
        or len(value) > _MAX_ID_LENGTH
        or not _NONZERO_DECIMAL.fullmatch(value)
    ):
        raise ValueError("invalid_remote_id")
    return value


def _env_enabled(env: Mapping[str, str], name: str) -> bool:
    return env.get(name) == PROBE_ENABLED_VALUE


def _gate_error(
    env: Mapping[str, str], *, live: bool, budget: C03CallBudget
) -> str | None:
    gate = WriteGate(
        write_enabled=_env_enabled(env, C03_WRITE_ENABLED_ENV),
        user_approved=_env_enabled(env, C03_MANAGED_FIXTURE_ENV),
        disposable_fixture=_env_enabled(env, C03_MANAGED_FIXTURE_ENV),
        cleanup_plan=_env_enabled(env, C03_CLEANUP_PLAN_ENV),
    )
    decision = evaluate_write_gate(gate, WriteOperation.MKDIR)
    if not decision.allowed:
        return decision.error_code
    if live and not _env_enabled(env, C03_LIVE_ENV):
        return "live_disabled"
    if not budget.valid():
        return "invalid_call_budget"
    return None


def _fixture_name(label: str) -> str:
    return f"wa-c03-{label}-{secrets.token_hex(8)}"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:16]


def _report(
    state: _ProbeState,
    status: C03ProbeStatus,
    *,
    fixture_fingerprint: str | None,
    cleanup: str,
    error_code: str | None = None,
) -> C03ProbeReport:
    return C03ProbeReport(
        status=status,
        fixture_fingerprint=fixture_fingerprint,
        steps=tuple(state.steps),
        write_calls=state.write_calls,
        read_calls=state.read_calls,
        cleanup=cleanup,
        error_code=error_code,
    )


async def run_p115_c03_fixture_probe(
    *,
    transport: P115C03Transport,
    parent_id: object,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
    budget: C03CallBudget | None = None,
    live: bool = False,
) -> C03ProbeReport:
    """Run the bounded source/quarantine/restore lifecycle.

    ``live`` only changes the required environment gate.  It never creates a
    transport; a live caller must inject one explicitly.
    """

    state = _ProbeState()
    selected_budget = budget or C03CallBudget()
    environment = os.environ if env is None else env
    try:
        normalized_parent_id = normalize_parent_id(parent_id)
    except (TypeError, ValueError):
        return _report(
            state,
            C03ProbeStatus.BLOCKED,
            fixture_fingerprint=None,
            cleanup="not_started",
            error_code="invalid_parent_id",
        )
    gate_error = _gate_error(environment, live=live, budget=selected_budget)
    if gate_error is not None:
        return _report(
            state,
            C03ProbeStatus.BLOCKED,
            fixture_fingerprint=None,
            cleanup="not_started",
            error_code=gate_error,
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        return _report(
            state,
            C03ProbeStatus.BLOCKED,
            fixture_fingerprint=None,
            cleanup="not_started",
            error_code="invalid_timeout",
        )

    root_name = _fixture_name("root")
    fingerprint = _fingerprint(root_name)
    outcome = C03ProbeStatus.SUCCESS
    error_code: str | None = None
    cleanup = "not_attempted"
    try:
        root_receipt = await _write(
            transport,
            prepare_mkdir(normalized_parent_id, root_name),
            state,
            selected_budget,
            timeout_seconds,
        )
        root_id = _receipt_file_id(root_receipt)
        if root_id is None:
            raise _ProbeHalt("root_creation_unconfirmed")
        state.root_id = root_id
        state.root_name = root_name
        await _verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            operation=WriteOperation.MKDIR,
            file_id=root_id,
            parent_id=normalized_parent_id,
            name=root_name,
        )
        state.root_confirmed = True
        if root_receipt.status is not WriteStatus.SUCCESS:
            raise _ProbeHalt("root_creation_unconfirmed")

        source_id = await _mkdir(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            parent_id=root_id,
            name="source",
        )
        quarantine_id = await _mkdir(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            parent_id=root_id,
            name="quarantine",
        )
        await _mkdir(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            parent_id=root_id,
            name="artifact",
        )

        await _rename_and_verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            file_id=source_id,
            parent_id=root_id,
            name="source-isolated",
        )
        await _move_and_verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            file_id=source_id,
            parent_id=quarantine_id,
            name="source-isolated",
        )
        await _rename_and_verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            file_id=source_id,
            parent_id=quarantine_id,
            name="source-quarantined",
        )
        await _move_and_verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            file_id=source_id,
            parent_id=root_id,
            name="source-quarantined",
        )
        await _rename_and_verify(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            file_id=source_id,
            parent_id=root_id,
            name="source",
        )
    except asyncio.CancelledError:
        outcome = C03ProbeStatus.UNCERTAIN
        error_code = "cancelled"
    except _ProbeHalt as error:
        outcome = C03ProbeStatus.UNCERTAIN
        error_code = error.code

    if state.root_confirmed:
        cleanup, cleanup_error = await _cleanup(
            transport,
            state,
            selected_budget,
            timeout_seconds,
            parent_id=normalized_parent_id,
        )
        if cleanup_error is not None:
            outcome = C03ProbeStatus.UNCERTAIN
            error_code = cleanup_error
    elif state.root_id is not None:
        cleanup = "not_attempted_unconfirmed"

    return _report(
        state,
        outcome,
        fixture_fingerprint=fingerprint,
        cleanup=cleanup,
        error_code=error_code,
    )


async def _mkdir(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    *,
    parent_id: str,
    name: str,
) -> str:
    receipt = await _write(
        transport,
        prepare_mkdir(parent_id, name),
        state,
        budget,
        timeout_seconds,
    )
    file_id = _receipt_file_id(receipt)
    if file_id is None:
        raise _ProbeHalt("mkdir_unconfirmed")
    await _verify(
        transport,
        state,
        budget,
        timeout_seconds,
        operation=WriteOperation.MKDIR,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
    )
    if receipt.status is not WriteStatus.SUCCESS:
        raise _ProbeHalt("mkdir_unconfirmed")
    return file_id


async def _rename_and_verify(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    *,
    file_id: str,
    parent_id: str,
    name: str,
) -> None:
    receipt = await _write(
        transport,
        prepare_rename(file_id, name),
        state,
        budget,
        timeout_seconds,
        verify_identity=(file_id, parent_id, name),
    )
    await _verify(
        transport,
        state,
        budget,
        timeout_seconds,
        operation=WriteOperation.RENAME,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
    )
    if receipt.status is not WriteStatus.SUCCESS:
        raise _ProbeHalt("rename_unconfirmed")


async def _move_and_verify(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    *,
    file_id: str,
    parent_id: str,
    name: str,
) -> None:
    receipt = await _write(
        transport,
        prepare_move(file_id, parent_id),
        state,
        budget,
        timeout_seconds,
        verify_identity=(file_id, parent_id, name),
    )
    await _verify(
        transport,
        state,
        budget,
        timeout_seconds,
        operation=WriteOperation.MOVE,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
    )
    if receipt.status is not WriteStatus.SUCCESS:
        raise _ProbeHalt("move_unconfirmed")


async def _write(
    transport: P115C03Transport,
    request: PreparedWrite,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    verify_identity: tuple[str, str, str] | None = None,
) -> C03WriteReceipt:
    if state.write_calls >= budget.max_write_calls:
        raise _ProbeHalt("write_call_limit_reached")
    if state.write_calls + state.read_calls >= budget.max_total_calls:
        raise _ProbeHalt("total_call_limit_reached")
    state.write_calls += 1
    try:
        receipt = await asyncio.wait_for(transport.execute(request), timeout_seconds)
    except asyncio.CancelledError:
        if verify_identity is not None:
            await _best_effort_verify(
                transport,
                state,
                budget,
                timeout_seconds,
                request.operation,
                verify_identity,
            )
        raise
    except TimeoutError:
        if verify_identity is None:
            state.steps.append(
                C03StepReport(request.operation.value, C03StepStatus.UNCONFIRMED)
            )
        else:
            await _best_effort_verify(
                transport,
                state,
                budget,
                timeout_seconds,
                request.operation,
                verify_identity,
            )
        raise _ProbeHalt("timeout") from None
    except Exception:  # noqa: BLE001 - transport details never cross the boundary
        if verify_identity is None:
            state.steps.append(
                C03StepReport(request.operation.value, C03StepStatus.UNCONFIRMED)
            )
        else:
            await _best_effort_verify(
                transport,
                state,
                budget,
                timeout_seconds,
                request.operation,
                verify_identity,
            )
        raise _ProbeHalt("remote_write_failed") from None
    if not isinstance(receipt, C03WriteReceipt):
        state.steps.append(
            C03StepReport(request.operation.value, C03StepStatus.UNCONFIRMED)
        )
        raise _ProbeHalt("write_result_unconfirmed")
    return receipt


async def _best_effort_verify(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    operation: WriteOperation,
    identity: tuple[str, str, str],
) -> None:
    try:
        await _verify(
            transport,
            state,
            budget,
            timeout_seconds,
            operation=operation,
            file_id=identity[0],
            parent_id=identity[1],
            name=identity[2],
        )
    except (asyncio.CancelledError, _ProbeHalt):
        return


async def _verify(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    *,
    operation: WriteOperation,
    file_id: str,
    parent_id: str,
    name: str,
) -> None:
    if state.read_calls >= budget.max_read_calls:
        raise _ProbeHalt("read_call_limit_reached")
    if state.write_calls + state.read_calls >= budget.max_total_calls:
        raise _ProbeHalt("total_call_limit_reached")
    state.read_calls += 1
    try:
        entry = await asyncio.wait_for(transport.read(file_id), timeout_seconds)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        state.steps.append(C03StepReport(operation.value, C03StepStatus.UNCONFIRMED))
        raise _ProbeHalt("timeout") from None
    except Exception:  # noqa: BLE001 - read details never cross the boundary
        state.steps.append(C03StepReport(operation.value, C03StepStatus.UNCONFIRMED))
        raise _ProbeHalt("read_failed") from None
    if not isinstance(entry, C03RemoteEntry):
        state.steps.append(C03StepReport(operation.value, C03StepStatus.UNCONFIRMED))
        raise _ProbeHalt("read_result_unconfirmed")
    try:
        matched = (
            _normalize_file_id(entry.file_id) == file_id
            and _normalize_file_id(entry.parent_id) == parent_id
            and entry.name == name
            and entry.is_directory is True
        )
    except (TypeError, ValueError):
        matched = False
    state.steps.append(
        C03StepReport(
            operation.value,
            C03StepStatus.SATISFIED if matched else C03StepStatus.NOT_SATISFIED,
        )
    )
    if not matched:
        raise _ProbeHalt("postcondition_not_satisfied")


def _receipt_file_id(receipt: C03WriteReceipt) -> str | None:
    if receipt.file_id is None:
        return None
    try:
        return _normalize_file_id(receipt.file_id)
    except (TypeError, ValueError):
        return None


async def _cleanup(
    transport: P115C03Transport,
    state: _ProbeState,
    budget: C03CallBudget,
    timeout_seconds: float,
    *,
    parent_id: str,
) -> tuple[str, str | None]:
    assert state.root_id is not None
    assert state.root_name is not None
    try:
        await _verify(
            transport,
            state,
            budget,
            timeout_seconds,
            operation=WriteOperation.RECYCLE,
            file_id=state.root_id,
            parent_id=parent_id,
            name=state.root_name,
        )
        receipt = await _write(
            transport,
            prepare_recycle(state.root_id),
            state,
            budget,
            timeout_seconds,
        )
        if receipt.status is not WriteStatus.SUCCESS:
            return "uncertain", "cleanup_unconfirmed"
        if state.read_calls >= budget.max_read_calls:
            return "uncertain", "cleanup_read_call_limit_reached"
        if state.write_calls + state.read_calls >= budget.max_total_calls:
            return "uncertain", "cleanup_total_call_limit_reached"
        state.read_calls += 1
        try:
            remaining = await asyncio.wait_for(
                transport.read(state.root_id), timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return "uncertain", "cleanup_timeout"
        except Exception:  # noqa: BLE001 - read details never cross the boundary
            return "uncertain", "cleanup_read_failed"
        if remaining is not None:
            return "uncertain", "cleanup_not_confirmed"
        return "complete", None
    except asyncio.CancelledError:
        return "not_attempted_cancelled", "cancelled"
    except _ProbeHalt as error:
        return "not_completed", f"cleanup_{error.code}"


class FakeP115C03Transport:
    """In-memory fixture; public state exposes counts and operation shapes only."""

    def __init__(self, failure: str | None = None) -> None:
        self._failure = failure
        self._next_id = 90000000000000000001
        self._entries: dict[str, C03RemoteEntry] = {}
        self.write_operations: list[WriteOperation] = []
        self.read_count = 0

    def __repr__(self) -> str:
        return (
            f"FakeP115C03Transport(write_count={len(self.write_operations)}, "
            f"read_count={self.read_count}, entry_count={len(self._entries)})"
        )

    async def execute(self, request: PreparedWrite) -> C03WriteReceipt:
        self.write_operations.append(request.operation)
        if self._failure == request.operation.value:
            raise OSError("opaque transport failure")
        if self._failure == f"timeout:{request.operation.value}":
            raise TimeoutError("opaque transport timeout")
        if self._failure == f"cancel:{request.operation.value}":
            raise asyncio.CancelledError()
        if request.operation is WriteOperation.MKDIR:
            parent_id = request.payload["pid"]
            name = request.payload["cname"]
            file_id = str(self._next_id)
            self._next_id += 1
            self._entries[file_id] = C03RemoteEntry(file_id, parent_id, name)
            return C03WriteReceipt(WriteStatus.SUCCESS, file_id)
        if request.operation is WriteOperation.MOVE:
            file_id = request.payload["fid"]
            entry = self._entries[file_id]
            self._entries[file_id] = C03RemoteEntry(
                entry.file_id, request.payload["pid"], entry.name
            )
            return C03WriteReceipt(WriteStatus.SUCCESS)
        if request.operation is WriteOperation.RENAME:
            key = next(
                key for key in request.payload if key.startswith("files_new_name[")
            )
            file_id = key.removeprefix("files_new_name[").removesuffix("]")
            entry = self._entries[file_id]
            self._entries[file_id] = C03RemoteEntry(
                entry.file_id, entry.parent_id, request.payload[key]
            )
            return C03WriteReceipt(WriteStatus.SUCCESS)
        if request.operation is WriteOperation.RECYCLE:
            root_id = request.payload["fid"]
            descendants = {
                file_id
                for file_id in self._entries
                if self._is_descendant(file_id, root_id)
            }
            self._entries.pop(root_id, None)
            for file_id in descendants:
                self._entries.pop(file_id, None)
            return C03WriteReceipt(WriteStatus.SUCCESS)
        raise RuntimeError("unsupported offline operation")

    async def read(self, file_id: str) -> C03RemoteEntry | None:
        self.read_count += 1
        if self._failure == "read":
            raise OSError("opaque read failure")
        if self._failure == "timeout:read":
            raise TimeoutError("opaque read timeout")
        return self._entries.get(file_id)

    def _is_descendant(self, file_id: str, root_id: str) -> bool:
        current = self._entries[file_id]
        while current.parent_id in self._entries:
            if current.parent_id == root_id:
                return True
            current = self._entries[current.parent_id]
        return False


__all__ = [
    "C03_CLEANUP_PLAN_ENV",
    "C03_LIVE_ENV",
    "C03_MANAGED_FIXTURE_ENV",
    "C03_WRITE_ENABLED_ENV",
    "MAX_BATCH_OBSERVATION_CALLS",
    "MAX_CONFLICT_OBSERVATION_CALLS",
    "MAX_READ_CALLS",
    "MAX_TOTAL_CALLS",
    "MAX_WRITE_CALLS",
    "C03CallBudget",
    "C03ProbeReport",
    "C03ProbeStatus",
    "C03RemoteEntry",
    "C03StepReport",
    "C03StepStatus",
    "C03WriteReceipt",
    "FakeP115C03Transport",
    "P115C03Transport",
    "normalize_parent_id",
    "run_p115_c03_fixture_probe",
]
