"""Offline C03 write contracts; deliberately no client or network execution.

The DTOs freeze the fixed p115client payload candidates without expanding the
read-only gateway.  A fake can exercise state classification, but there is no
production transport in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol


class WriteOperation(StrEnum):
    MKDIR = "mkdir"
    MOVE = "move"
    RENAME = "rename"
    QUARANTINE = "quarantine"
    RESTORE = "restore"
    RECYCLE = "recycle"
    DELETE = "delete"


class OrganizationWriteCapability(StrEnum):
    """Capabilities frozen by the independently verified organization contract."""

    READ_SCOPE = "read_scope"
    MKDIR = "mkdir"
    MOVE = "move"
    RENAME = "rename"
    RECYCLE = "recycle"
    POSTCONDITION = "postcondition"


ORGANIZATION_CONTRACT_VERSION = "c03-organization-v1"
ORGANIZATION_CONTRACT_EVIDENCE_ENV = "P115_ORGANIZATION_CONTRACT_EVIDENCE_PATH"
_MAX_CONTRACT_EVIDENCE_BYTES = 16 * 1024


class WriteStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class PostconditionStatus(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True, repr=False)
class WriteResult:
    operation: WriteOperation
    status: WriteStatus
    error_code: str | None = None
    postcondition_required: bool = False

    def __repr__(self) -> str:
        return (
            f"WriteResult(operation={self.operation.value!r}, "
            f"status={self.status.value!r}, error_code={self.error_code!r}, "
            f"postcondition_required={self.postcondition_required!r})"
        )


@dataclass(frozen=True, slots=True)
class WriteGate:
    """Independent C03 gates; every gate is closed by default."""

    write_enabled: bool = False
    user_approved: bool = False
    disposable_fixture: bool = False
    cleanup_plan: bool = False
    permanent_delete_enabled: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class WriteGateDecision:
    allowed: bool
    error_code: str | None = None

    def __repr__(self) -> str:
        return f"WriteGateDecision(allowed={self.allowed!r}, error_code={self.error_code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationContractEvidence:
    """Redacted evidence produced by the independently verified C03 probe."""

    evidence_id: str
    capabilities: frozenset[OrganizationWriteCapability]
    timeout_enforced: bool
    version: str = ORGANIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_id, str)
            or not self.evidence_id
            or len(self.evidence_id) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                for character in self.evidence_id
            )
        ):
            raise ValueError("invalid_organization_contract_evidence")
        if self.version != ORGANIZATION_CONTRACT_VERSION:
            raise ValueError("organization_contract_unverified")
        if not isinstance(self.timeout_enforced, bool) or not self.timeout_enforced:
            raise ValueError("invalid_organization_contract_evidence")
        try:
            capabilities = frozenset(
                capability
                if isinstance(capability, OrganizationWriteCapability)
                else OrganizationWriteCapability(capability)
                for capability in self.capabilities
            )
        except (TypeError, ValueError):
            raise ValueError("invalid_organization_contract_evidence") from None
        if not capabilities:
            raise ValueError("invalid_organization_contract_evidence")
        object.__setattr__(self, "capabilities", capabilities)

    def __repr__(self) -> str:
        return (
            "OrganizationContractEvidence("
            f"capability_count={len(self.capabilities)}, "
            f"timeout_enforced={self.timeout_enforced!r})"
        )


class OrganizationContractEvidenceError(ValueError):
    """Safe, non-sensitive error raised while loading runtime evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _evidence_error(code: str) -> OrganizationContractEvidenceError:
    return OrganizationContractEvidenceError(code)


def _read_contract_evidence(path: Path) -> Mapping[str, Any]:
    """Read one bounded, regular, non-writable evidence file without following links."""

    try:
        initial = path.lstat()
    except FileNotFoundError:
        raise _evidence_error("organization_contract_evidence_missing") from None
    except PermissionError:
        raise _evidence_error("organization_contract_evidence_unreadable") from None
    except OSError:
        raise _evidence_error("organization_contract_evidence_unreadable") from None
    if stat.S_ISLNK(initial.st_mode):
        raise _evidence_error("organization_contract_evidence_symlink")
    if not stat.S_ISREG(initial.st_mode):
        raise _evidence_error("organization_contract_evidence_regular_file_required")
    if stat.S_IMODE(initial.st_mode) & 0o022:
        raise _evidence_error("organization_contract_evidence_permissions")
    if initial.st_size > _MAX_CONTRACT_EVIDENCE_BYTES:
        raise _evidence_error("organization_contract_evidence_too_large")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise _evidence_error("organization_contract_evidence_missing") from None
    except PermissionError:
        raise _evidence_error("organization_contract_evidence_unreadable") from None
    except OSError:
        raise _evidence_error("organization_contract_evidence_unreadable") from None

    try:
        current = os.fstat(descriptor)
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise _evidence_error("organization_contract_evidence_regular_file_required")
        if stat.S_IMODE(current.st_mode) & 0o022:
            raise _evidence_error("organization_contract_evidence_permissions")
        if (
            getattr(initial, "st_dev", None) != getattr(current, "st_dev", None)
            or getattr(initial, "st_ino", None) != getattr(current, "st_ino", None)
        ):
            raise _evidence_error("organization_contract_evidence_changed")
        if current.st_size > _MAX_CONTRACT_EVIDENCE_BYTES:
            raise _evidence_error("organization_contract_evidence_too_large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            raw = stream.read(_MAX_CONTRACT_EVIDENCE_BYTES + 1)
    except UnicodeDecodeError:
        raise _evidence_error("organization_contract_evidence_encoding") from None
    except OSError:
        raise _evidence_error("organization_contract_evidence_unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw.encode("utf-8")) > _MAX_CONTRACT_EVIDENCE_BYTES:
        raise _evidence_error("organization_contract_evidence_too_large")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise _evidence_error("organization_contract_evidence_json") from None
    if not isinstance(payload, Mapping):
        raise _evidence_error("organization_contract_evidence_schema")
    return payload


def load_organization_contract_evidence(
    path: Path | str,
) -> OrganizationContractEvidence:
    """Load and strictly validate the redacted C03 evidence document."""

    payload = _read_contract_evidence(Path(path))
    expected_fields = {"version", "evidence_id", "capabilities", "timeout_enforced"}
    if set(payload) != expected_fields:
        raise _evidence_error("organization_contract_evidence_schema")
    capabilities = payload["capabilities"]
    if isinstance(capabilities, (str, bytes)) or not isinstance(capabilities, list):
        raise _evidence_error("organization_contract_evidence_schema")
    if not isinstance(payload["version"], str):
        raise _evidence_error("organization_contract_evidence_schema")
    if not isinstance(payload["evidence_id"], str):
        raise _evidence_error("organization_contract_evidence_schema")
    if not isinstance(payload["timeout_enforced"], bool):
        raise _evidence_error("organization_contract_evidence_schema")
    try:
        return OrganizationContractEvidence(
            evidence_id=payload["evidence_id"],
            capabilities=frozenset(capabilities),
            timeout_enforced=payload["timeout_enforced"],
            version=payload["version"],
        )
    except (TypeError, ValueError):
        raise _evidence_error("organization_contract_evidence_invalid") from None


def load_p115_organization_contract(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[P115OrganizationContract, str]:
    """Load a verified contract, or return a closed contract and safe status."""

    configured = str(path).strip() if path is not None else ""
    if not configured:
        environment = os.environ if environ is None else environ
        configured = environment.get(
            ORGANIZATION_CONTRACT_EVIDENCE_ENV, ""
        ).strip()
    if not configured:
        return P115OrganizationContract(), "not_configured"
    try:
        evidence = load_organization_contract_evidence(configured)
        contract = P115OrganizationContract(
            verified=True,
            capabilities=evidence.capabilities,
            timeout_enforced=evidence.timeout_enforced,
            version=evidence.version,
            evidence=evidence,
        )
    except OrganizationContractEvidenceError as exc:
        return P115OrganizationContract(), exc.code
    return contract, "loaded"


@dataclass(frozen=True, slots=True, repr=False)
class P115OrganizationContract:
    """The small, explicit capability contract required by the live gateway.

    A boolean environment flag is not sufficient evidence for a write.  The
    contract records the versioned verification result and the exact methods
    that were verified.  It intentionally has no credential or remote data.
    """

    verified: bool = False
    capabilities: frozenset[OrganizationWriteCapability] = frozenset()
    timeout_enforced: bool = False
    version: str = ORGANIZATION_CONTRACT_VERSION
    evidence: OrganizationContractEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool) or not isinstance(
            self.timeout_enforced, bool
        ):
            raise TypeError("invalid_organization_contract")
        if self.version != ORGANIZATION_CONTRACT_VERSION:
            raise ValueError("organization_contract_unverified")
        if self.verified and self.evidence is None:
            raise ValueError("organization_contract_evidence_required")
        if self.evidence is not None and not isinstance(
            self.evidence, OrganizationContractEvidence
        ):
            raise TypeError("invalid_organization_contract_evidence")
        if self.evidence is not None:
            if not self.verified:
                raise ValueError("organization_contract_unverified")
            if (
                self.evidence.version != self.version
                or self.evidence.capabilities != frozenset(self.capabilities)
                or self.evidence.timeout_enforced != self.timeout_enforced
            ):
                raise ValueError("organization_contract_evidence_mismatch")
        try:
            capabilities = frozenset(
                capability
                if isinstance(capability, OrganizationWriteCapability)
                else OrganizationWriteCapability(capability)
                for capability in self.capabilities
            )
        except (TypeError, ValueError):
            raise ValueError("invalid_organization_contract") from None
        object.__setattr__(self, "capabilities", capabilities)

    def supports(self, operation: WriteOperation) -> bool:
        required = {
            WriteOperation.MKDIR: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MKDIR,
                OrganizationWriteCapability.POSTCONDITION,
            },
            WriteOperation.MOVE: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MOVE,
                OrganizationWriteCapability.POSTCONDITION,
            },
            WriteOperation.RENAME: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.RENAME,
                OrganizationWriteCapability.POSTCONDITION,
            },
            WriteOperation.RECYCLE: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.RECYCLE,
                OrganizationWriteCapability.POSTCONDITION,
            },
            WriteOperation.QUARANTINE: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MOVE,
                OrganizationWriteCapability.RENAME,
                OrganizationWriteCapability.POSTCONDITION,
            },
            WriteOperation.RESTORE: {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MOVE,
                OrganizationWriteCapability.RENAME,
                OrganizationWriteCapability.POSTCONDITION,
            },
        }.get(operation)
        return (
            self.verified
            and self.evidence is not None
            and self.timeout_enforced
            and required is not None
            and required <= self.capabilities
        )

    def supports_read_scope(self) -> bool:
        """Return whether bounded read-only observations are independently verified."""

        return (
            self.verified
            and self.evidence is not None
            and self.timeout_enforced
            and OrganizationWriteCapability.READ_SCOPE in self.capabilities
        )

    def __repr__(self) -> str:
        return (
            "P115OrganizationContract("
            f"verified={self.verified!r}, "
            f"capability_count={len(self.capabilities)}, "
            f"timeout_enforced={self.timeout_enforced!r}, "
            f"evidence_present={self.evidence is not None!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationWriteGate:
    """Runtime gates for one confirmed organization operation."""

    write_enabled: bool = False
    plan_confirmed: bool = False
    scope_confirmed: bool = False
    contract: P115OrganizationContract = P115OrganizationContract()
    permanent_delete_enabled: bool = False

    def __repr__(self) -> str:
        return (
            "OrganizationWriteGate("
            f"write_enabled={self.write_enabled!r}, "
            f"plan_confirmed={self.plan_confirmed!r}, "
            f"scope_confirmed={self.scope_confirmed!r}, "
            f"permanent_delete_enabled={self.permanent_delete_enabled!r})"
        )


def evaluate_organization_write_gate(
    gate: OrganizationWriteGate, operation: WriteOperation
) -> WriteGateDecision:
    """Fail closed before a live organization transport can be constructed."""

    if not gate.write_enabled:
        return WriteGateDecision(False, "write_disabled")
    if not gate.contract.verified or not gate.contract.timeout_enforced:
        return WriteGateDecision(False, "contract_unverified")
    if not gate.scope_confirmed:
        return WriteGateDecision(False, "scope_unverified")
    if not gate.plan_confirmed:
        return WriteGateDecision(False, "approval_required")
    if operation is WriteOperation.DELETE and not gate.permanent_delete_enabled:
        return WriteGateDecision(False, "permanent_delete_disabled")
    if not gate.contract.supports(operation):
        return WriteGateDecision(False, "capability_unverified")
    return WriteGateDecision(True)


def evaluate_organization_read_gate(gate: OrganizationWriteGate) -> WriteGateDecision:
    """Gate remote reconciliation without opening any write capability."""

    if not gate.contract.verified or not gate.contract.timeout_enforced:
        return WriteGateDecision(False, "contract_unverified")
    if not gate.scope_confirmed:
        return WriteGateDecision(False, "scope_unverified")
    if not gate.plan_confirmed:
        return WriteGateDecision(False, "approval_required")
    if not gate.contract.supports_read_scope():
        return WriteGateDecision(False, "capability_unverified")
    return WriteGateDecision(True)


def evaluate_write_gate(
    gate: WriteGate, operation: WriteOperation
) -> WriteGateDecision:
    """Apply all C03 gates without invoking a remote method."""

    if not gate.write_enabled:
        return WriteGateDecision(False, "write_disabled")
    if not gate.user_approved:
        return WriteGateDecision(False, "approval_required")
    if not gate.disposable_fixture:
        return WriteGateDecision(False, "fixture_scope_required")
    if not gate.cleanup_plan:
        return WriteGateDecision(False, "cleanup_plan_required")
    if operation is WriteOperation.DELETE and not gate.permanent_delete_enabled:
        return WriteGateDecision(False, "permanent_delete_disabled")
    return WriteGateDecision(True)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedWrite:
    operation: WriteOperation
    payload: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("invalid_payload")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.payload.items()
        ):
            raise ValueError("invalid_payload")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def __repr__(self) -> str:
        return f"PreparedWrite(operation={self.operation.value!r}, payload_fields={tuple(sorted(self.payload))!r})"


@dataclass(frozen=True, slots=True, repr=False)
class WritePlan:
    operation: WriteOperation
    steps: tuple[PreparedWrite, ...]

    def __repr__(self) -> str:
        return f"WritePlan(operation={self.operation.value!r}, step_count={len(self.steps)})"


@dataclass(frozen=True, slots=True, repr=False)
class WritePostcondition:
    operation: WriteOperation
    file_id: str
    parent_id: str | None = None
    name: str | None = None
    present: bool = True

    def __repr__(self) -> str:
        return (
            f"WritePostcondition(operation={self.operation.value!r}, "
            f"parent_present={self.parent_id is not None}, "
            f"name_present={self.name is not None}, present={self.present!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PostconditionResult:
    status: PostconditionStatus
    error_code: str | None = None

    def __repr__(self) -> str:
        return f"PostconditionResult(status={self.status.value!r}, error_code={self.error_code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class WriteCall:
    operation: WriteOperation
    payload_fields: tuple[str, ...]

    def __repr__(self) -> str:
        return f"WriteCall(operation={self.operation.value!r}, payload_fields={self.payload_fields!r})"


class P115LibraryWriteGateway(Protocol):
    """Future write boundary; no implementation is provided in C03 offline prep."""

    async def execute(
        self, request: PreparedWrite, *, gate: WriteGate
    ) -> WriteResult: ...

    async def verify_postcondition(
        self, check: WritePostcondition
    ) -> PostconditionResult: ...


def _id(value: Any) -> str:
    if isinstance(value, bool):
        raise TypeError("invalid_id")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("invalid_id")
        return str(value)
    if isinstance(value, str) and value and all(char in "0123456789" for char in value):
        return value
    raise TypeError("invalid_id")


def _name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("invalid_name")
    return value


def prepare_mkdir(parent_id: Any, name: Any) -> PreparedWrite:
    return PreparedWrite(
        WriteOperation.MKDIR,
        {"pid": _id(parent_id), "file_name": _name(name)},
    )


def prepare_move(file_id: Any, target_parent_id: Any) -> PreparedWrite:
    return PreparedWrite(
        WriteOperation.MOVE,
        {"file_ids": _id(file_id), "to_cid": _id(target_parent_id)},
    )


def prepare_rename(file_id: Any, name: Any) -> PreparedWrite:
    return PreparedWrite(
        WriteOperation.RENAME,
        {"file_id": _id(file_id), "file_name": _name(name)},
    )


def prepare_recycle(file_id: Any) -> PreparedWrite:
    return PreparedWrite(WriteOperation.RECYCLE, {"file_id": _id(file_id)})


def prepare_delete(file_id: Any) -> PreparedWrite:
    return PreparedWrite(WriteOperation.DELETE, {"file_id": _id(file_id)})


def prepare_quarantine(
    file_id: Any, quarantine_parent_id: Any, quarantine_name: Any
) -> WritePlan:
    normalized_id = _id(file_id)
    return WritePlan(
        WriteOperation.QUARANTINE,
        (
            prepare_move(normalized_id, quarantine_parent_id),
            prepare_rename(normalized_id, quarantine_name),
        ),
    )


def prepare_restore(
    file_id: Any, original_parent_id: Any, original_name: Any
) -> WritePlan:
    normalized_id = _id(file_id)
    return WritePlan(
        WriteOperation.RESTORE,
        (
            prepare_move(normalized_id, original_parent_id),
            prepare_rename(normalized_id, original_name),
        ),
    )


def classify_write_exception(
    operation: WriteOperation, error: BaseException
) -> WriteResult:
    """Map transport outcomes without retaining exception details."""

    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, TimeoutError):
        return WriteResult(operation, WriteStatus.UNCERTAIN, "timeout", True)
    return WriteResult(operation, WriteStatus.FAILED, "remote_failed")


class FakeP115LibraryWriteGateway:
    """Offline fake; it records only operation names and payload field names."""

    def __init__(
        self,
        outcomes: Mapping[WriteOperation, WriteResult | BaseException] | None = None,
        postconditions: Mapping[WriteOperation, PostconditionResult] | None = None,
    ) -> None:
        self._outcomes = dict(outcomes or {})
        self._postconditions = dict(postconditions or {})
        self.calls: list[WriteCall] = []
        self.postcondition_calls: list[WriteOperation] = []

    def __repr__(self) -> str:
        return f"FakeP115LibraryWriteGateway(call_count={len(self.calls)})"

    async def execute(self, request: PreparedWrite, *, gate: WriteGate) -> WriteResult:
        decision = evaluate_write_gate(gate, request.operation)
        if not decision.allowed:
            return WriteResult(
                request.operation, WriteStatus.FAILED, decision.error_code
            )
        self.calls.append(WriteCall(request.operation, tuple(sorted(request.payload))))
        outcome = self._outcomes.get(request.operation)
        if isinstance(outcome, BaseException):
            return classify_write_exception(request.operation, outcome)
        if outcome is not None:
            return outcome
        return WriteResult(request.operation, WriteStatus.SUCCESS, None, True)

    async def verify_postcondition(
        self, check: WritePostcondition
    ) -> PostconditionResult:
        self.postcondition_calls.append(check.operation)
        return self._postconditions.get(
            check.operation, PostconditionResult(PostconditionStatus.UNVERIFIED)
        )


__all__ = [
    "ORGANIZATION_CONTRACT_EVIDENCE_ENV",
    "ORGANIZATION_CONTRACT_VERSION",
    "FakeP115LibraryWriteGateway",
    "OrganizationContractEvidence",
    "OrganizationContractEvidenceError",
    "OrganizationWriteCapability",
    "OrganizationWriteGate",
    "P115LibraryWriteGateway",
    "P115OrganizationContract",
    "PostconditionResult",
    "PostconditionStatus",
    "PreparedWrite",
    "WriteCall",
    "WriteGate",
    "WriteGateDecision",
    "WriteOperation",
    "WritePlan",
    "WritePostcondition",
    "WriteResult",
    "WriteStatus",
    "classify_write_exception",
    "evaluate_organization_read_gate",
    "evaluate_organization_write_gate",
    "evaluate_write_gate",
    "load_organization_contract_evidence",
    "load_p115_organization_contract",
    "prepare_delete",
    "prepare_mkdir",
    "prepare_move",
    "prepare_quarantine",
    "prepare_recycle",
    "prepare_rename",
    "prepare_restore",
]
