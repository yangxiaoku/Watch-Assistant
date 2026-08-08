import json
import os
from pathlib import Path

import pytest

from watch_assistant.adapters.p115_library_write_contract import (
    ORGANIZATION_CONTRACT_VERSION,
    OrganizationContractEvidenceError,
    OrganizationWriteCapability,
    WriteOperation,
    load_organization_contract_evidence,
    load_p115_organization_contract,
)
from watch_assistant.app import create_app

CAPABILITIES = [
    OrganizationWriteCapability.READ_SCOPE.value,
    OrganizationWriteCapability.MKDIR.value,
    OrganizationWriteCapability.MOVE.value,
    OrganizationWriteCapability.RENAME.value,
    OrganizationWriteCapability.RECYCLE.value,
    OrganizationWriteCapability.POSTCONDITION.value,
]


def _write_evidence(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "version": ORGANIZATION_CONTRACT_VERSION,
        "evidence_id": "c03-fixture-organization-v1",
        "capabilities": CAPABILITIES,
        "timeout_enforced": True,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


@pytest.mark.skipif(
    os.name == "nt",
    reason="evidence permission bits are not representable on Windows",
)
def test_runtime_loader_accepts_only_redacted_versioned_evidence(tmp_path: Path):
    evidence_path = tmp_path / "organization-contract.json"
    _write_evidence(evidence_path)

    contract, status = load_p115_organization_contract(evidence_path)

    assert status == "loaded"
    assert contract.verified is True
    assert contract.timeout_enforced is True
    assert contract.evidence is not None
    assert contract.supports(WriteOperation.MOVE) is True
    assert contract.capabilities == frozenset(
        OrganizationWriteCapability(value) for value in CAPABILITIES
    )


def test_runtime_loader_stays_closed_when_path_is_missing_or_unconfigured(tmp_path: Path):
    missing, missing_status = load_p115_organization_contract(tmp_path / "missing.json")
    empty, empty_status = load_p115_organization_contract(None, environ={})

    assert missing.verified is False
    assert missing_status == "organization_contract_evidence_missing"
    assert empty.verified is False
    assert empty_status == "not_configured"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "old-contract"),
        ("capabilities", ["read_scope", "unknown-capability"]),
        ("timeout_enforced", False),
        ("unexpected", "must be rejected"),
    ],
)
def test_runtime_loader_rejects_invalid_evidence_without_opening_write(
    tmp_path: Path, field: str, value: object
):
    evidence_path = tmp_path / "invalid.json"
    _write_evidence(evidence_path, **{field: value})

    contract, status = load_p115_organization_contract(evidence_path)

    assert contract.verified is False
    assert status.startswith("organization_contract_evidence_")


@pytest.mark.skipif(os.name == "nt", reason="chmod and symlink semantics differ on Windows")
def test_runtime_loader_rejects_group_or_world_writable_evidence(tmp_path: Path):
    evidence_path = tmp_path / "writable.json"
    _write_evidence(evidence_path)
    evidence_path.chmod(0o664)

    with pytest.raises(
        OrganizationContractEvidenceError,
        match="organization_contract_evidence_permissions",
    ):
        load_organization_contract_evidence(evidence_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink support is not guaranteed on Windows")
def test_runtime_loader_rejects_symlink_evidence(tmp_path: Path):
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    _write_evidence(target)
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    contract, status = load_p115_organization_contract(link)

    assert contract.verified is False
    assert status == "organization_contract_evidence_symlink"


@pytest.mark.skipif(
    os.name == "nt",
    reason="evidence permission bits are not representable on Windows",
)
def test_create_app_loads_runtime_evidence_but_legacy_flag_cannot_do_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence_path = tmp_path / "organization-contract.json"
    _write_evidence(evidence_path)
    monkeypatch.setenv("ORGANIZATION_WRITE_CONTRACT_VERIFIED", "true")
    monkeypatch.setenv(
        "P115_ORGANIZATION_CONTRACT_EVIDENCE_PATH", str(evidence_path)
    )

    application = create_app(
        frontend_dir=tmp_path / "missing-frontend",
        organization_write_enabled=True,
        organization_write_contract_verified=True,
    )

    assert application.state.organization_contract.verified is True
    assert application.state.organization_contract_load_status == "loaded"
    assert application.state.organization_write_contract_verified is True

    monkeypatch.delenv("P115_ORGANIZATION_CONTRACT_EVIDENCE_PATH")
    closed_application = create_app(
        frontend_dir=tmp_path / "missing-frontend",
        organization_write_enabled=True,
        organization_write_contract_verified=True,
    )
    assert closed_application.state.organization_contract.verified is False
    assert closed_application.state.organization_write_contract_verified is False
