import pytest

from watch_assistant.adapters.p115_c03_fixture_probe import C03WriteReceipt
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
    WriteOperation,
    WriteStatus,
)
from watch_assistant.services.organization_directory_provisioner import (
    OrganizationDirectoryProvisioner,
    OrganizationDirectoryProvisionError,
)


def _contract() -> P115OrganizationContract:
    capabilities = frozenset(
        {
            OrganizationWriteCapability.READ_SCOPE,
            OrganizationWriteCapability.MKDIR,
            OrganizationWriteCapability.POSTCONDITION,
        }
    )
    return P115OrganizationContract(
        verified=True,
        capabilities=capabilities,
        timeout_enforced=True,
        evidence=OrganizationContractEvidence(
            evidence_id="directory-gate-fixture",
            capabilities=capabilities,
            timeout_enforced=True,
        ),
    )


def _provisioner() -> OrganizationDirectoryProvisioner:
    return OrganizationDirectoryProvisioner(
        object(),
        call_executor=lambda *_args, **_kwargs: None,
        organization_contract=_contract(),
    )


@pytest.mark.asyncio
async def test_directory_provisioner_defaults_write_gate_closed():
    provisioner = _provisioner()
    remote_calls = []

    async def execute(request, *, timeout_seconds):
        remote_calls.append((request.operation, timeout_seconds))
        return C03WriteReceipt(WriteStatus.SUCCESS, "8000")

    provisioner._transport.execute = execute

    with pytest.raises(OrganizationDirectoryProvisionError, match="write_disabled"):
        await provisioner.ensure(
            target_root_id="9000",
            existing_directories={"": "9000"},
            paths=("Movies",),
            plan_confirmed=True,
            scope_confirmed=True,
            lease_active=True,
        )

    assert remote_calls == []


@pytest.mark.asyncio
async def test_directory_provisioner_forwards_confirmed_write_gate():
    provisioner = _provisioner()
    remote_calls = []

    async def execute(request, *, timeout_seconds):
        remote_calls.append((request.operation, timeout_seconds))
        return C03WriteReceipt(WriteStatus.SUCCESS, "8000")

    provisioner._transport.execute = execute

    await provisioner.ensure(
        target_root_id="9000",
        existing_directories={"": "9000"},
        paths=("Movies",),
        write_enabled=True,
        plan_confirmed=True,
        scope_confirmed=True,
        lease_active=True,
    )

    assert remote_calls == [(WriteOperation.MKDIR, 30.0)]
