import asyncio
from types import SimpleNamespace

import pytest

from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
)
from watch_assistant.models import OrganizationOperationStatus
from watch_assistant.services.organization_directory_provisioner import (
    OrganizationDirectoryProvisionError,
)
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
        self.scope_revisions = []
        self.step_revisions = []

    async def claim_next(self):
        return _Lease()

    async def get(self, _operation_id):
        return SimpleNamespace(
            status=OrganizationOperationStatus.UNCERTAIN,
            revision=1,
        )

    async def plan_execution_scope(
        self, _operation_id, *, expected_operation_revision=None
    ):
        self.scope_revisions.append(expected_operation_revision)
        return self.scope

    async def load_execution_steps(
        self, _operation_id, *, expected_operation_revision=None
    ):
        self.step_revisions.append(expected_operation_revision)
        return self.steps

    async def renew_lease(self, operation_id, *, expected_revision, lease_token):
        return SimpleNamespace(
            operation_id=operation_id,
            revision=expected_revision + 1,
            lease_token=lease_token,
        )

    async def finish(self, _operation_id, **kwargs):
        self.finished.append(kwargs)

    async def finish_after_lease_loss(self, *_args, **_kwargs):
        self.finish_after_lease_loss_calls += 1

    async def fail_stale_uncertain(self, operation_id, *, expected_revision):
        self.finished.append(
            {
                "operation_id": operation_id,
                "expected_revision": expected_revision,
                "error_code": "plan_prerequisites_changed",
            }
        )


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
        target_directory_path="library/movie",
    )
    return (SimpleNamespace(members=(member,), replacement_object_id=None),)


def _worker(
    operations,
    *,
    write_enabled,
    client_factory=None,
    directory_provisioner=None,
    settings_service=None,
    target_root_provider=None,
):
    return OrganizationWorker(
        session_factory=object(),
        operation_service=operations,
        cookie_provider=_CookieProvider(),
        production_root_id="9000",
        live_enabled=True,
        write_enabled=write_enabled,
        organization_contract=_contract(),
        client_factory=client_factory,
        directory_provisioner=directory_provisioner,
        settings_service=settings_service,
        target_root_provider=target_root_provider,
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
    assert operations.scope_revisions == [1]
    assert operations.step_revisions == [1]


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
async def test_mismatched_target_root_finishes_with_target_root_changed(monkeypatch):
    # 目标根不在计划范围内是配置变更:操作 failed(不 invalidate 计划),
    # 错误码必须区分于 plan_prerequisites_changed。
    operations = _Operations(scope=frozenset({"7000"}), steps=_steps())
    transport_calls = []
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        lambda **kwargs: transport_calls.append(kwargs),
    )
    worker = _worker(operations, write_enabled=True)

    assert await worker.run_once() is True
    assert transport_calls == []
    assert operations.finished[0]["status"] is OrganizationOperationStatus.FAILED
    assert operations.finished[0]["error_code"] == "target_root_changed"


@pytest.mark.asyncio
async def test_confirmed_runtime_forwards_all_live_gate_values(monkeypatch):
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    client = _Client()
    transport_calls = []
    executor_calls = []

    class _Executor:
        def __init__(self, _operations, _session_factory, transport, **_kwargs):
            executor_calls.append((transport, _kwargs))

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
    assert transport_calls[0]["target_root_id"] == "9000"
    assert transport_calls[0]["intents"][0].target_directory_path == "library/movie"
    assert len(executor_calls) == 1
    # 无 settings_service 时回退 operation_delay=0.25,
    # 写间隔 = max(3.0, 0.25 * 2) = 3.0。
    assert executor_calls[0][1]["min_call_interval"] == 0.25
    assert executor_calls[0][1]["write_interval_seconds"] == 3.0
    assert client.closed is True
    assert operations.scope_revisions == [1]
    assert operations.step_revisions == [1]


@pytest.mark.asyncio
async def test_target_root_provider_is_reconsulted_each_run(monkeypatch):
    """settings PATCH 后 target root 变化,下一次执行必须使用新值。"""
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    client = _Client()
    transport_calls = []
    executor_calls = []

    class _Executor:
        def __init__(self, _operations, _session_factory, transport, **_kwargs):
            executor_calls.append((transport, _kwargs))

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
    holder = {"root": "9000"}
    worker = _worker(
        operations,
        write_enabled=True,
        client_factory=lambda _credential: client,
        target_root_provider=lambda: holder["root"],
    )

    assert await worker.run_once() is True
    assert len(transport_calls) == 1
    assert transport_calls[0]["target_root_id"] == "9000"
    assert operations.finished == []

    # 运行中 target 变更(settings PATCH 同步 state 后) → 下次执行用新值,
    # 而不是沿用启动快照。
    holder["root"] = "9001"
    operations.scope = frozenset({"7000", "9001"})
    assert await worker.run_once() is True
    assert len(transport_calls) == 2
    assert transport_calls[1]["target_root_id"] == "9001"
    assert operations.finished == []


@pytest.mark.asyncio
async def test_target_root_provider_unset_target_fails_before_transport(monkeypatch):
    """provider 返回 None(target 未配置)时按范围不确认失败,不构建 transport。"""
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    transport_calls = []
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        lambda **kwargs: transport_calls.append(kwargs),
    )
    worker = _worker(
        operations,
        write_enabled=True,
        client_factory=lambda _credential: _Client(),
        target_root_provider=lambda: None,
    )

    assert await worker.run_once() is True
    assert transport_calls == []
    assert operations.finished[0]["error_code"] == "plan_prerequisites_changed"


@pytest.mark.asyncio
async def test_member_without_target_directory_path_defaults_to_none(monkeypatch):
    """旧计划成员没有 target_directory_path 字段时,intent 保持 None。"""
    member = SimpleNamespace(
        object_id="100",
        source_parent_id="7000",
        source_name="source.mkv",
        target_parent_id="9000",
        target_name="target.mkv",
    )
    operations = _Operations(
        scope=frozenset({"7000", "9000"}),
        steps=(SimpleNamespace(members=(member,), replacement_object_id=None),),
    )
    client = _Client()
    transport_calls = []

    class _Executor:
        def __init__(self, *_args, **_kwargs):
            pass

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
    assert transport_calls[0]["target_root_id"] == "9000"
    assert transport_calls[0]["intents"][0].target_directory_path is None


class _SettingsService:
    def __init__(self, operation_delay_seconds):
        self._operation_delay_seconds = operation_delay_seconds

    async def get_organization(self):
        return SimpleNamespace(operation_delay_seconds=self._operation_delay_seconds)


@pytest.mark.asyncio
async def test_write_interval_is_derived_from_operation_delay_settings(monkeypatch):
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    client = _Client()
    executor_kwargs = []

    class _Executor:
        def __init__(self, _operations, _session_factory, transport, **kwargs):
            executor_kwargs.append(kwargs)

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.create_live_p115_organization_transport",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "watch_assistant.services.organization_worker.OrganizationExecutor",
        _Executor,
    )
    worker = _worker(
        operations,
        write_enabled=True,
        client_factory=lambda _credential: client,
        settings_service=_SettingsService(2.0),
    )

    assert await worker.run_once() is True
    assert executor_kwargs[0]["min_call_interval"] == 2.0
    assert executor_kwargs[0]["write_interval_seconds"] == 4.0


@pytest.mark.asyncio
async def test_uncertain_directory_provision_is_not_marked_retryable():
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())

    async def provision(_operation_id):
        raise OrganizationDirectoryProvisionError(
            "target_directory_create_failed", uncertain=True
        )

    worker = _worker(
        operations,
        write_enabled=True,
        directory_provisioner=provision,
    )

    assert await worker.run_once() is True
    assert operations.finished[0]["status"] is OrganizationOperationStatus.UNCERTAIN
    assert operations.finished[0]["error_code"] == "target_directory_create_failed"


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
    assert transport_calls[0]["target_root_id"] == "9000"
    assert transport_calls[0]["intents"][0].target_directory_path == "library/movie"
    assert client.closed is True
    assert operations.scope_revisions == [1]
    assert operations.step_revisions == [1]


@pytest.mark.asyncio
async def test_run_forever_backs_off_when_run_once_keeps_failing(monkeypatch):
    # 回归(L9):run_once 持续抛异常时必须指数退避,不得热循环。
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    worker = _worker(operations, write_enabled=False)
    calls = 0

    async def exploding_run_once():
        nonlocal calls
        calls += 1
        raise RuntimeError("transient database lock")

    monkeypatch.setattr(worker, "run_once", exploding_run_once)
    stop = asyncio.Event()
    loop = asyncio.create_task(worker.run_forever(stop))
    try:
        # 首次异常后进入退避窗口(≥1s):窗口内不得再调用 run_once。
        await asyncio.sleep(0.25)
        assert calls == 1
        # 退避窗口结束才允许下一次调用。
        await asyncio.sleep(2.1)
        assert calls >= 2
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=2)


@pytest.mark.asyncio
async def test_run_forever_success_resets_backoff(monkeypatch):
    # 回归(L9):成功执行后退避复位,恢复轮询节奏而不是继续拉长间隔。
    operations = _Operations(scope=frozenset({"7000", "9000"}), steps=_steps())
    worker = OrganizationWorker(
        session_factory=object(),
        operation_service=operations,
        cookie_provider=_CookieProvider(),
        production_root_id="9000",
        live_enabled=True,
        write_enabled=False,
        organization_contract=_contract(),
        poll_interval_seconds=0.05,
    )
    calls = 0
    state = {"fail": True}

    async def flaky_run_once():
        nonlocal calls
        calls += 1
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("transient database lock")
        return False

    monkeypatch.setattr(worker, "run_once", flaky_run_once)
    stop = asyncio.Event()
    loop = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.sleep(0.25)
        # 首次异常:进入退避,尚未复位。
        assert calls == 1
        # 退避窗口过后:失败→成功复位→恢复 0.05s 轮询节奏,
        # 若退避未复位,此时最多只有 2 次调用。
        await asyncio.sleep(2.15)
        assert calls >= 5
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=2)


@pytest.mark.asyncio
async def test_reconcile_terminalizes_operation_when_plan_revision_changed():
    """计划被新扫描失效(plan_execution_scope/load_execution_steps 因版本
    失配返回空)时,reconcile 必须终态化 UNCERTAIN 操作并 invalidate 计划,
    而不是永久停在 scope_unverified、阻塞计划后续操作。"""
    operations = _Operations(scope=frozenset(), steps=())
    worker = _worker(
        operations,
        write_enabled=False,
        client_factory=lambda _credential: _Client(),
    )

    result = await worker.reconcile_once("operation-one", expected_revision=1)

    assert result.status is OrganizationExecutionStatus.UNCERTAIN
    assert result.error_code == "plan_prerequisites_changed"
    # 必须调 finish 终态化(而非停在 scope_unverified)
    assert operations.finished, "计划失配时必须终态化操作"
    assert operations.finished[0]["error_code"] == "plan_prerequisites_changed"
