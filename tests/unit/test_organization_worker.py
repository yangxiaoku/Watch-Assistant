from types import SimpleNamespace

import pytest

from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
)
from watch_assistant.models import OrganizationOperationStatus
from watch_assistant.services.organization_executor import (
    OrganizationExecutionResult,
    OrganizationExecutionStatus,
)
from watch_assistant.services.organization_worker import OrganizationWorker


class _Lease:
    operation_id = "operation-one"
    revision = 1
    lease_token = "lease-token"


class _Operations:
    def __init__(self, *, scope, steps):
        self.scope = scope
        self.steps = steps
        self.finished = []
        self.finish_after_lease_loss_calls = 0

    async def claim_next(self):
        return _Lease()

    async def get(self, _operation_id):
        return SimpleNamespace(
            status=OrganizationOperationStatus.UNCERTAIN,
            revision=1,
        )

    async def plan_execution_scope(self, _operation_id):
        return self.scope

    async def load_execution_steps(self, _operation_id):
        return self.steps

    async def finish(self, _operation_id, **kwargs):
        self.finished.append(kwargs)

    async def finish_after_lease_loss(self, *_args, **_kwargs):
        self.finish_after_lease_loss_calls += 1


class _CookieProvider:
    def load(self):
        return "credential-marker"


class _Client:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _contract() -> P115OrganizationContract:
    capabilities = frozenset(
        {
            OrganizationWriteCapability.READ_SCOPE,
            OrganizationWriteCapability.MOVE,
            OrganizationWriteCapability.RENAME,
            OrganizationWriteCapability.RECYCLE,
            OrganizationWriteCapability.POSTCONDITION,
        }
    )
    return P115OrganizationContract(
        verified=True,
        capabilities=capabilities,
        timeout_enforced=True,
        evidence=OrganizationContractEvidence(
            evidence_id="worker-gate-fixture",
            capabilities=capabilities,
            timeout_enforced=True,
        ),
    )


def _steps():
    member = SimpleNamespace(
        object_id="100",
        source_parent_id="7000",
        source_name="source.mkv",
        target_parent_id="9000",
        target_name="target.mkv",
    )
    return (SimpleNamespace(members=(member,), replacement_object_id=None),)


def _worker(operations, *, write_enabled, client_factory=None):
    return OrganizationWorker(
        session_factory=object(),
        operation_service=operations,
        cookie_provider=_CookieProvider(),
        production_root_id="9000",
        live_enabled=True,
        write_enabled=write_enabled,
        organization_contract=_contract(),
        client_factory=client_factory,
    )


@pytest.mark.asyncio
async def test_disabled_runtime_fails_before_constructing_live_transport(monkeypatch):
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    transport_calls = []
    client_calls = []
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        lambda **kwargs: transport_calls.append(kwargs),
    )

    worker = _worker(
        operations,
        write_enabled=False,
        client_factory=lambda _credential: client_calls.append(True),
    )

    assert await worker.run_once() is True
    assert transport_calls == []
    assert client_calls == []
    assert operations.finished[0]["status"] is OrganizationOperationStatus.FAILED
    assert operations.finished[0]["error_code"] == "write_disabled"


@pytest.mark.asyncio
async def test_unconfirmed_plan_fails_before_constructing_live_transport(monkeypatch):
    operations = _Operations(scope=None, steps=None)
    transport_calls = []
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        lambda **kwargs: transport_calls.append(kwargs),
    )
    worker = _worker(operations, write_enabled=True)

    assert await worker.run_once() is True
    assert transport_calls == []
    assert operations.finished[0]["error_code"] == "plan_prerequisites_changed"


@pytest.mark.asyncio
async def test_confirmed_runtime_forwards_all_live_gate_values(monkeypatch):
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    client = _Client()
    transport_calls = []
    executor_calls = []

    class _Executor:
        def __init__(self, _operations, _session_factory, transport, **_kwargs):
            executor_calls.append(transport)

        async def execute(self, *_args, **_kwargs):
            return None

    def create_transport(**kwargs):
        transport_calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        create_transport,
    )
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.OrganizationExecutor",
        _Executor,
    )
    worker = _worker(
        operations,
        write_enabled=True,
        client_factory=lambda _credential: client,
    )

    assert await worker.run_once() is True
    assert len(transport_calls) == 1
    assert transport_calls[0]["live_enabled"] is True
    assert transport_calls[0]["write_enabled"] is True
    assert transport_calls[0]["plan_confirmed"] is True
    assert transport_calls[0]["scope_confirmed"] is True
    assert transport_calls[0]["organization_contract"] == _contract()
    assert len(executor_calls) == 1
    assert client.closed is True


@pytest.mark.asyncio
async def test_reconcile_uses_read_only_transport_when_writes_are_disabled(monkeypatch):
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    client = _Client()
    transport_calls = []

    class _Executor:
        def __init__(self, _operations, _session_factory, transport, **_kwargs):
            self.transport = transport

        async def reconcile_uncertain(self, *_args, **_kwargs):
            return OrganizationExecutionResult(
                "operation-one",
                OrganizationExecutionStatus.ORGANIZED,
                None,
                1,
                1,
            )

    def create_transport(**kwargs):
        transport_calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        create_transport,
    )
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.OrganizationExecutor",
        _Executor,
    )
    worker = _worker(
        operations,
        write_enabled=False,
        client_factory=lambda _credential: client,
    )

    result = await worker.reconcile_once("operation-one", expected_revision=1)

    assert result.status is OrganizationExecutionStatus.ORGANIZED
    assert len(transport_calls) == 1
    assert transport_calls[0]["read_only"] is True
    assert transport_calls[0]["write_enabled"] is False
    assert transport_calls[0]["plan_confirmed"] is True
    assert transport_calls[0]["scope_confirmed"] is True
    assert client.closed is True
