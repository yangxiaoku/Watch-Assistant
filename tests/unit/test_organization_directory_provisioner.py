import pytest

from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03DirectoryListing,
    C03RemoteEntry,
    C03WriteReceipt,
)
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


class _OwnershipService:
    def __init__(self):
        self.calls = []

    async def register_created(self, **kwargs):
        self.calls.append(kwargs)


async def _noop_sleep(_delay: float) -> None:
    return None


def _provisioner(**kwargs) -> OrganizationDirectoryProvisioner:
    # 默认注入 no-op sleep:目录间隔由专门的注入测试覆盖,避免测试变慢。
    kwargs.setdefault("sleep", _noop_sleep)
    return OrganizationDirectoryProvisioner(
        object(),
        call_executor=lambda *_args, **_kwargs: None,
        organization_contract=_contract(),
        ownership_service=_OwnershipService(),
        **kwargs,
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
            library_id="library-1",
            operation_id="op-directory-test",
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
    list_calls = []

    async def execute(request, *, timeout_seconds):
        remote_calls.append((request.operation, timeout_seconds))
        return C03WriteReceipt(WriteStatus.SUCCESS, "8000")

    async def list_children(parent_id, *, timeout_seconds):
        list_calls.append((parent_id, timeout_seconds))
        return C03DirectoryListing((), complete=True, page_calls=1)

    provisioner._transport.execute = execute
    provisioner._transport.list_children = list_children

    await provisioner.ensure(
        target_root_id="9000",
        library_id="library-1",
        operation_id="op-directory-test",
        existing_directories={"": "9000"},
        paths=("Movies",),
        write_enabled=True,
        plan_confirmed=True,
        scope_confirmed=True,
        lease_active=True,
    )

    assert remote_calls == [(WriteOperation.MKDIR, 30.0)]
    assert list_calls == [("8000", 30.0)]


@pytest.mark.asyncio
async def test_directory_provisioner_accepts_already_existing_directory():
    """A provider mkdir rejection for an existing name is idempotent."""
    provisioner = _provisioner()
    remote_calls = []
    list_calls = []

    async def execute(request, *, timeout_seconds):
        remote_calls.append((request.operation, timeout_seconds))
        return C03WriteReceipt(WriteStatus.UNCERTAIN, None)

    async def list_children(parent_id, *, timeout_seconds):
        list_calls.append(parent_id)
        return C03DirectoryListing(
            (C03RemoteEntry("8100", parent_id, "Movies", True),),
            complete=True,
            page_calls=1,
        )

    provisioner._transport.execute = execute
    provisioner._transport.list_children = list_children

    await provisioner.ensure(
        target_root_id="9000",
        library_id="library-1",
        operation_id="op-directory-test",
        existing_directories={"": "9000"},
        paths=("Movies",),
        write_enabled=True,
        plan_confirmed=True,
        scope_confirmed=True,
        lease_active=True,
    )

    assert remote_calls == [(WriteOperation.MKDIR, 30.0)]
    assert list_calls == ["9000", "8100"]


@pytest.mark.asyncio
async def test_directory_provisioner_sleeps_between_consecutive_mkdirs():
    sleeps = []

    async def recording_sleep(delay: float) -> None:
        sleeps.append(delay)

    provisioner = _provisioner(interval_seconds=2.0, sleep=recording_sleep)
    remote_calls = []
    list_calls = []

    async def execute(request, *, timeout_seconds):
        remote_calls.append((request.operation, timeout_seconds))
        return C03WriteReceipt(WriteStatus.SUCCESS, "8000")

    async def list_children(parent_id, *, timeout_seconds):
        list_calls.append(parent_id)
        return C03DirectoryListing((), complete=True, page_calls=1)

    provisioner._transport.execute = execute
    provisioner._transport.list_children = list_children

    await provisioner.ensure(
        target_root_id="9000",
        library_id="library-1",
        operation_id="op-directory-test",
        existing_directories={"": "9000"},
        paths=("Movies/Anime", "TV"),
        write_enabled=True,
        plan_confirmed=True,
        scope_confirmed=True,
        lease_active=True,
    )

    # TV, Movies, Movies/Anime 共 3 次 mkdir,期间 2 次间隔等待。
    assert len(remote_calls) == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_uncertain_mkdir_receipt_is_not_retryable():
    provisioner = _provisioner()

    async def execute(request, *, timeout_seconds):
        return C03WriteReceipt(WriteStatus.UNCERTAIN)

    provisioner._transport.execute = execute

    with pytest.raises(OrganizationDirectoryProvisionError) as error:
        await provisioner.ensure(
            target_root_id="9000",
            library_id="library-1",
            operation_id="op-directory-test",
            existing_directories={"": "9000"},
            paths=("Movies",),
            write_enabled=True,
            plan_confirmed=True,
            scope_confirmed=True,
            lease_active=True,
        )

    assert error.value.code == "target_directory_create_failed"
    assert error.value.uncertain is True


@pytest.mark.asyncio
async def test_successful_mkdir_requires_exact_read_after_write_postcondition():
    provisioner = _provisioner()

    async def execute(request, *, timeout_seconds):
        return C03WriteReceipt(WriteStatus.SUCCESS, "8000")

    async def read(file_id, *, timeout_seconds):
        return C03RemoteEntry(file_id, "9000", "Different")

    provisioner._transport.execute = execute
    provisioner._transport.read = read

    with pytest.raises(OrganizationDirectoryProvisionError) as error:
        await provisioner.ensure(
            target_root_id="9000",
            library_id="library-1",
            operation_id="op-directory-test",
            existing_directories={"": "9000"},
            paths=("Movies",),
            write_enabled=True,
            plan_confirmed=True,
            scope_confirmed=True,
            lease_active=True,
        )

    assert error.value.code == "target_directory_create_failed"
    assert error.value.uncertain is True
