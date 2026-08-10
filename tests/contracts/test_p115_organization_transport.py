import asyncio

import pytest

from scripts.p115_c03_live_runner import _c03_organization_contract
from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03DirectoryListing,
    C03RemoteEntry,
)
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
)
from watch_assistant.adapters.p115_organization_transport import (
    LiveP115OrganizationTransport,
    OfflineP115OrganizationTransport,
    OrganizationObjectIntent,
    P115OrganizationMethod,
    P115OrganizationTransportError,
    create_live_p115_organization_transport,
    create_p115_organization_transport,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import (
    OrganizationTransportOperation,
    OrganizationTransportResult,
    OrganizationTransportStatus,
)


class _LiveFakeP115Client:
    def __init__(self, responses):
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls = []

    def _response(self, name, payload, **kwargs):
        self.calls.append((name, dict(payload), dict(kwargs)))
        value = self.responses[name].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def fs_move(self, payload, **kwargs):
        return self._response("fs_move", payload, **kwargs)

    def fs_rename(self, payload, **kwargs):
        return self._response("fs_rename", payload, **kwargs)

    def fs_delete(self, payload, **kwargs):
        return self._response("fs_delete", payload, **kwargs)

    def fs_info(self, payload, **kwargs):
        return self._response("fs_info", payload, **kwargs)

    def fs_files(self, payload, **kwargs):
        return self._response("fs_files", payload, **kwargs)

    def fs_mkdir_app(self, payload, **kwargs):
        return self._response("fs_mkdir_app", payload, **kwargs)

    def fs_move_app(self, payload, **kwargs):
        return self._response("fs_move_app", payload, **kwargs)

    def fs_delete_app(self, payload, **kwargs):
        return self._response("fs_delete_app", payload, **kwargs)


async def _live_call_executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


def _live_file(file_id, parent_id, name):
    return {"state": True, "data": {"fc": 1, "fid": file_id, "cid": parent_id, "n": name}}


# 生产组织路径默认使用 P115C03ProductionTransport（50 条/页），
# 页响应必须回显 limit=50 才能通过逐页严格校验。
def _empty_page():
    return {"state": True, "data": [], "offset": 0, "limit": 50, "count": 0}


def _file_page(file_id, parent_id, name):
    return {
        "state": True,
        "data": [{"fc": 1, "fid": file_id, "cid": parent_id, "n": name}],
        "offset": 0,
        "limit": 50,
        "count": 1,
    }


def _directory_page(file_id, parent_id, name):
    return {
        "state": True,
        "data": [{"fc": 0, "fid": file_id, "pid": parent_id, "n": name}],
        "offset": 0,
        "limit": 50,
        "count": 1,
    }


def _intent(
    *,
    object_id: str = "100",
    source_parent_id: str = "7000",
    source_name: str = "before.mkv",
    target_parent_id: str = "8000",
    target_name: str = "after.mkv",
    target_directory_path: str | None = None,
) -> OrganizationObjectIntent:
    return OrganizationObjectIntent(
        object_id,
        source_parent_id,
        source_name,
        target_parent_id,
        target_name,
        target_directory_path=target_directory_path,
    )


def _organization_contract() -> P115OrganizationContract:
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
        timeout_enforced=True,
        capabilities=capabilities,
        evidence=OrganizationContractEvidence(
            evidence_id="c03-fixture-organization-v1",
            capabilities=capabilities,
            timeout_enforced=True,
        ),
    )


def test_c03_live_contract_covers_the_bounded_fixture_operations():
    contract = _c03_organization_contract()

    assert contract.verified is True
    assert contract.timeout_enforced is True
    assert contract.evidence is not None
    assert contract.evidence.evidence_id == "c03-fixture-organization-v1"
    assert contract.supports_read_scope() is True


def _transport(**kwargs) -> OfflineP115OrganizationTransport:
    values = {
        "intents": (_intent(),),
        "managed_directory_ids": ("7000", "8000"),
        "scope_confirmed": True,
        "states": {"100": RemoteObjectState("100", "7000", "before.mkv")},
    }
    values.update(kwargs)
    return create_p115_organization_transport(**values)


def test_factory_is_offline_only_and_has_no_destructive_operations():
    transport = _transport()

    assert isinstance(transport, OfflineP115OrganizationTransport)
    assert not hasattr(transport, "delete")
    assert not hasattr(transport, "quarantine")
    with pytest.raises(P115OrganizationTransportError) as error:
        _transport(live=True)
    assert error.value.code == "live_transport_disabled"
    assert transport.calls == ()


@pytest.mark.parametrize(
    "intent",
    (
        OrganizationObjectIntent("100", "7000", "before", "8000", "after"),
        OrganizationObjectIntent("101", "7000", "before-2", "8000", "after-2"),
    ),
)
def test_intent_repr_is_redacted(intent):
    rendered = repr(intent)
    assert "100" not in rendered
    assert "101" not in rendered
    assert "before" not in rendered
    assert "after" not in rendered


@pytest.mark.parametrize(
    "intent",
    (
        ("0", "7000", "before", "8000", "after"),
        ("100", "outside", "before", "8000", "after"),
        ("100", "7000", "../before", "8000", "after"),
        ("100", "7000", "before", "8000", "bad/name"),
    ),
)
def test_invalid_identity_or_name_is_rejected(intent):
    with pytest.raises(ValueError, match="invalid_organization_intent"):
        OrganizationObjectIntent(*intent)


def test_scope_must_cover_every_intent_and_targets_must_be_unique():
    with pytest.raises(ValueError, match="invalid_organization_scope"):
        _transport(managed_directory_ids=("7000",))

    with pytest.raises(ValueError, match="invalid_organization_scope"):
        _transport(
            intents=(
                _intent(),
                _intent(object_id="101"),
            )
        )


@pytest.mark.asyncio
async def test_exact_intent_executes_move_then_rename_and_preserves_identity():
    transport = _transport()

    source = await transport.read_object("100")
    target = await transport.read_target("8000", "after.mkv")
    moved = await transport.move("100", "8000")
    renamed = await transport.rename("100", "after.mkv")
    observed = await transport.read_object("100")

    assert source == RemoteObjectState("100", "7000", "before.mkv")
    assert target is None
    assert moved == OrganizationTransportResult(
        OrganizationTransportOperation.MOVE,
        OrganizationTransportStatus.SUCCESS,
    )
    assert renamed == OrganizationTransportResult(
        OrganizationTransportOperation.RENAME,
        OrganizationTransportStatus.SUCCESS,
    )
    assert observed == RemoteObjectState("100", "8000", "after.mkv")
    assert [call.operation for call in transport.calls] == [
        "read_object",
        "read_target",
        "move",
        "rename",
        "read_object",
    ]


@pytest.mark.asyncio
async def test_live_transport_uses_scope_fixed_payloads_and_receipt_before_verify():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                _file_page("100", "7000", "before.mkv"),
                _empty_page(),
                _empty_page(),
                _empty_page(),
                _file_page("100", "8000", "after.mkv"),
            ],
            "fs_move": [{"state": True}],
            "fs_rename": [{"state": True}],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
    )

    assert isinstance(transport, LiveP115OrganizationTransport)
    assert await transport.read_object("100") == RemoteObjectState(
        "100", "7000", "before.mkv"
    )
    assert await transport.read_target("8000", "after.mkv") is None
    move = await transport.move("100", "8000")
    rename = await transport.rename("100", "after.mkv")
    assert await transport.read_object("100") == RemoteObjectState(
        "100", "8000", "after.mkv"
    )
    assert move.status is OrganizationTransportStatus.SUCCESS
    assert rename.status is OrganizationTransportStatus.SUCCESS
    assert transport.receipts == (move, rename)
    assert [call[0] for call in client.calls] == [
        "fs_files",
        "fs_files",
        "fs_files",
        "fs_move",
        "fs_rename",
        "fs_files",
        "fs_files",
    ]
    assert client.calls[3][1] == {"fid": "100", "pid": "8000"}
    assert client.calls[4][1] == {"files_new_name[100]": "after.mkv"}


@pytest.mark.asyncio
async def test_live_transport_reads_replacement_across_the_frozen_scope():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                _file_page("100", "7000", "before.mkv"),
                _empty_page(),
            ],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    observed = await transport.read_object_in_scope("100", ("7000", "8000"))

    assert observed == RemoteObjectState("100", "7000", "before.mkv")
    assert [call[0] for call in client.calls] == ["fs_files", "fs_files"]
    assert [call[1]["cid"] for call in client.calls] == ["7000", "8000"]


@pytest.mark.asyncio
async def test_live_transport_returns_missing_for_absent_object_in_complete_scope():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [_empty_page(), _empty_page()],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    assert await transport.read_object_in_scope("100", ("7000", "8000")) is None


@pytest.mark.asyncio
async def test_live_transport_reads_object_in_directory_with_more_than_eight_entries():
    # 回归：生产组织路径默认使用完整分页（50 条/页），真实媒体目录（>8 条）
    # 必须在首次写入前可完整读取；旧实现 8 页 × 1 条/页会让本场景在写入前即
    # observation_unverified → 永久 UNCERTAIN。
    others = [
        {"fc": 1, "fid": f"9{index}", "cid": "7000", "n": f"filler-{index}.mkv"}
        for index in range(9)
    ]
    page = {
        "state": True,
        "data": [
            {"fc": 1, "fid": "100", "cid": "7000", "n": "before.mkv"},
            *others,
        ],
        "offset": 0,
        "limit": 50,
        "count": 10,
    }
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [page, _empty_page()],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
    )

    assert await transport.read_object("100") == RemoteObjectState(
        "100", "7000", "before.mkv"
    )


@pytest.mark.asyncio
async def test_live_transport_rejects_directory_with_planned_file_identity():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [_directory_page("100", "7000", "before.mkv")],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    with pytest.raises(P115OrganizationTransportError, match="observation_unverified"):
        await transport.read_object("100")

    # 目录页现在能正常解析:观察在目录/文件身份校验处被拒绝(仍是 fail-closed)。
    assert [call[0] for call in client.calls] == ["fs_files", "fs_files"]


@pytest.mark.asyncio
async def test_live_transport_rejects_scope_outside_managed_directories():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.read_object_in_scope("100", ("7000", "9000"))

    assert error.value.code == "scope_unverified"
    assert client.calls == []


@pytest.mark.asyncio
async def test_live_transport_rejects_non_boolean_complete_listing():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    async def malformed_listing(parent_id, *, timeout_seconds):
        return C03DirectoryListing(
            (C03RemoteEntry("100", parent_id, "before.mkv", False),),
            complete=1,
        )

    transport._c03.list_children = malformed_listing

    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.read_target("8000", "after.mkv")

    assert error.value.code == "observation_unverified"


@pytest.mark.asyncio
async def test_live_transport_read_only_mode_observes_with_writes_closed():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                _file_page("100", "7000", "before.mkv"),
                _empty_page(),
            ],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=False,
        plan_confirmed=True,
        read_only=True,
        organization_contract=_organization_contract(),
    )

    assert await transport.read_object("100") == RemoteObjectState(
        "100", "7000", "before.mkv"
    )
    for write in (
        lambda: transport.move("100", "8000"),
        lambda: transport.rename("100", "after.mkv"),
        lambda: transport.recycle("100", "8000", "after.mkv"),
    ):
        with pytest.raises(P115OrganizationTransportError) as error:
            await write()
        assert error.value.code == "write_disabled"

    assert [call[0] for call in client.calls] == ["fs_files", "fs_files"]


@pytest.mark.asyncio
async def test_live_transport_timeout_is_uncertain_and_is_not_retried():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [],
            "fs_move": [TimeoutError("private")],
            "fs_rename": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
    )

    with pytest.raises(TimeoutError):
        await transport.move("100", "8000")
    assert len(client.calls) == 1
    assert transport.receipts == ()


@pytest.mark.asyncio
async def test_live_transport_recycle_uses_one_receipt():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                _file_page("200", "8000", "movie.mkv"),
                _empty_page(),
                _empty_page(),
            ],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [{"state": True}],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(object_id="200", target_name="movie.mkv"),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
    )

    result = await transport.recycle("200", "8000", "movie.mkv")

    assert result == OrganizationTransportResult(
        OrganizationTransportOperation.RECYCLE,
        OrganizationTransportStatus.SUCCESS,
    )
    assert transport.receipts == (result,)
    assert client.calls == [("fs_delete", {"fid": "200"}, {"async_": False})]


@pytest.mark.asyncio
async def test_live_transport_recycle_rejects_unknown_result_without_retry():
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [_file_page("200", "8000", "movie.mkv")],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [{"state": False}],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(_intent(object_id="200", source_parent_id="8000", source_name="movie.mkv", target_name="movie.mkv"),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
    )

    result = await transport.recycle("200", "8000", "movie.mkv")

    assert result.status is OrganizationTransportStatus.UNCERTAIN
    assert result.error_code == "outcome_unknown"
    assert len(client.calls) == 1


def test_live_transport_requires_explicit_gate_and_confirmed_scope():
    with pytest.raises(P115OrganizationTransportError, match="live_transport_disabled"):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=True,
        )
    with pytest.raises(ValueError, match="invalid_organization_scope"):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=False,
            live_enabled=True,
        )
    with pytest.raises(
        P115OrganizationTransportError, match="organization_contract_required"
    ):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=True,
            live_enabled=True,
        )


@pytest.mark.parametrize(
    ("gate", "error_code"),
    (
        ({}, "write_disabled"),
        ({"write_enabled": True}, "approval_required"),
        ({"write_enabled": False, "plan_confirmed": True}, "write_disabled"),
    ),
)
def test_live_transport_requires_explicit_runtime_write_gate(gate, error_code):
    with pytest.raises(P115OrganizationTransportError, match=error_code):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=True,
            live_enabled=True,
            organization_contract=_organization_contract(),
            **gate,
        )


def test_live_transport_rejects_unverified_organization_contract():
    with pytest.raises(P115OrganizationTransportError, match="contract_unverified"):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=True,
            live_enabled=True,
            write_enabled=True,
            plan_confirmed=True,
            organization_contract=P115OrganizationContract(),
        )

    contract = _organization_contract()
    transport = create_live_p115_organization_transport(
        client=object(),
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=contract,
    )
    assert isinstance(transport, LiveP115OrganizationTransport)


@pytest.mark.asyncio
async def test_target_conflict_is_observable_without_authorizing_its_identity():
    transport = _transport(
        states={
            "100": RemoteObjectState("100", "7000", "before.mkv"),
            "999": RemoteObjectState("999", "8000", "after.mkv"),
        }
    )

    conflict = await transport.read_target("8000", "after.mkv")

    assert conflict == RemoteObjectState("999", "8000", "after.mkv")
    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.move("999", "8000")
    assert error.value.code == "scope_unverified"
    assert [call.operation for call in transport.calls] == ["read_target"]


@pytest.mark.asyncio
async def test_unconfirmed_or_out_of_scope_calls_stop_before_recording():
    unconfirmed = _transport(scope_confirmed=False)

    for call in (
        lambda: unconfirmed.read_object("100"),
        lambda: unconfirmed.read_target("8000", "after.mkv"),
        lambda: unconfirmed.move("100", "8000"),
        lambda: unconfirmed.rename("100", "after.mkv"),
    ):
        with pytest.raises(P115OrganizationTransportError) as error:
            await call()
        assert error.value.code == "scope_unverified"
    assert unconfirmed.calls == ()

    transport = _transport()
    for call in (
        lambda: transport.read_object("101"),
        lambda: transport.read_target("7000", "before.mkv"),
        lambda: transport.move("100", "7000"),
        lambda: transport.rename("100", "other.mkv"),
    ):
        with pytest.raises(P115OrganizationTransportError) as error:
            await call()
        assert error.value.code == "scope_unverified"
    assert transport.calls == ()


@pytest.mark.asyncio
async def test_changed_remote_observation_fails_closed_without_write():
    transport = _transport(
        states={"100": RemoteObjectState("100", "9000", "private-name.mkv")}
    )

    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.read_object("100")
    result = await transport.move("100", "8000")

    assert error.value.code == "observation_unverified"
    assert result.status is OrganizationTransportStatus.UNCERTAIN
    assert result.error_code == "outcome_unknown"
    assert [call.operation for call in transport.calls] == ["read_object", "move"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", (TimeoutError("secret"), asyncio.CancelledError()))
async def test_timeout_and_cancellation_propagate_without_retry(error):
    transport = _transport(outcomes={(P115OrganizationMethod.MOVE, "100"): error})

    with pytest.raises(type(error)):
        await transport.move("100", "8000")

    assert [call.operation for call in transport.calls] == ["move"]


@pytest.mark.asyncio
async def test_unknown_result_and_exception_are_redacted_and_not_retried():
    malformed = _transport(outcomes={(P115OrganizationMethod.MOVE, "100"): object()})
    result = await malformed.move("100", "8000")

    assert result.status is OrganizationTransportStatus.UNCERTAIN
    assert result.error_code == "outcome_unknown"
    assert [call.operation for call in malformed.calls] == ["move"]

    failed = _transport(
        outcomes={
            (P115OrganizationMethod.MOVE, "100"): RuntimeError(
                "COOKIE=secret https://private/path"
            )
        }
    )
    with pytest.raises(P115OrganizationTransportError) as error:
        await failed.move("100", "8000")
    rendered = repr(failed) + repr(failed.calls) + repr(error.value)
    assert error.value.code == "outcome_unknown"
    assert "COOKIE" not in rendered
    assert "private" not in rendered
    assert "100" not in rendered
    assert "7000" not in rendered
    assert "8000" not in rendered


@pytest.mark.asyncio
async def test_failed_or_uncertain_receipt_is_returned_once_without_mutation():
    for status in (
        OrganizationTransportStatus.FAILED,
        OrganizationTransportStatus.UNCERTAIN,
    ):
        expected = OrganizationTransportResult(
            OrganizationTransportOperation.MOVE, status, "provider secret"
        )
        transport = _transport(
            outcomes={(P115OrganizationMethod.MOVE, "100"): expected}
        )

        result = await transport.move("100", "8000")
        assert result.status is status
        assert result.error_code in {"remote_failed", "outcome_unknown"}
        assert "provider secret" not in repr(result)
        assert await transport.read_object("100") == RemoteObjectState(
            "100", "7000", "before.mkv"
        )
        assert [call.operation for call in transport.calls].count("move") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "identity", "call"),
    (
        (
            P115OrganizationMethod.READ_OBJECT,
            "100",
            lambda transport: transport.read_object("100"),
        ),
        (
            P115OrganizationMethod.READ_TARGET,
            "8000",
            lambda transport: transport.read_target("8000", "after.mkv"),
        ),
    ),
)
@pytest.mark.parametrize("error", (TimeoutError("secret"), asyncio.CancelledError()))
async def test_read_timeout_and_cancellation_propagate_once(
    method, identity, call, error
):
    transport = _transport(outcomes={(method, identity): error})

    with pytest.raises(type(error)):
        await call(transport)

    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unknown_read_result_fails_closed_without_provider_detail():
    transport = _transport(
        outcomes={(P115OrganizationMethod.READ_OBJECT, "100"): object()}
    )

    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.read_object("100")

    assert error.value.code == "observation_unverified"


@pytest.mark.parametrize(
    "path",
    ("", "bad\\path", "bad//path", "bad/../path", "../evil", "/leading/slash"),
)
def test_intent_rejects_invalid_target_directory_path(path):
    with pytest.raises(ValueError, match="invalid_organization_intent"):
        _intent(target_parent_id="9000", target_directory_path=path)


def test_live_transport_path_target_requires_target_root():
    with pytest.raises(P115OrganizationTransportError) as error:
        create_live_p115_organization_transport(
            client=_LiveFakeP115Client({}),
            call_executor=_live_call_executor,
            intents=(
                _intent(
                    target_parent_id="9000",
                    target_directory_path="library/movie",
                ),
            ),
            managed_directory_ids=("7000", "9000"),
            scope_confirmed=True,
            live_enabled=True,
            read_only=True,
            organization_contract=_organization_contract(),
        )
    assert error.value.code == "target_root_missing"


@pytest.mark.asyncio
async def test_live_transport_resolves_target_directory_path_and_moves_into_it():
    """多级目标:read_target/move 按路径解析真实目录,观察按计划空间根目录返回。"""
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                # read_object(执行前):先解析路径再列目录
                _directory_page("8100", "9000", "library"),  # resolve library
                _directory_page("8200", "8100", "movie"),  # resolve movie
                _directory_page("8300", "8200", "Season 02"),  # resolve season
                _file_page("100", "7000", "before.mkv"),  # source
                _empty_page(),  # plan target root
                _empty_page(),  # resolved dir
                # read_target:解析路径
                _directory_page("8100", "9000", "library"),
                _directory_page("8200", "8100", "movie"),
                _directory_page("8300", "8200", "Season 02"),
                _empty_page(),  # resolved dir -> target absent
                # move:解析路径
                _directory_page("8100", "9000", "library"),
                _directory_page("8200", "8100", "movie"),
                _directory_page("8300", "8200", "Season 02"),
                # read_object(执行后):先解析路径再列目录
                _directory_page("8100", "9000", "library"),
                _directory_page("8200", "8100", "movie"),
                _directory_page("8300", "8200", "Season 02"),
                _empty_page(),  # source
                _empty_page(),  # plan target root
                _file_page("100", "8300", "after.mkv"),  # resolved dir
            ],
            "fs_move": [{"state": True}],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(
            _intent(
                target_parent_id="9000",
                target_directory_path="library/movie/Season 02",
            ),
        ),
        managed_directory_ids=("7000", "9000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
        target_root_id="9000",
    )

    assert await transport.read_object("100") == RemoteObjectState(
        "100", "7000", "before.mkv"
    )
    assert await transport.read_target("9000", "after.mkv") is None
    move = await transport.move("100", "9000")
    assert move.status is OrganizationTransportStatus.SUCCESS
    # 观察按计划空间语义返回 target_parent_id(根),而不是解析出的 Season 02。
    assert await transport.read_object("100") == RemoteObjectState(
        "100", "9000", "after.mkv"
    )
    assert client.calls[13][1] == {"fid": "100", "pid": "8300"}
    assert [call[0] for call in client.calls] == (
        ["fs_files"] * 13 + ["fs_move"] + ["fs_files"] * 6
    )


@pytest.mark.asyncio
async def test_live_transport_path_resolution_failure_fails_closed():
    """路径任一目录缺失:read_target/move 解析失败按 scope_unverified 关闭。"""
    client = _LiveFakeP115Client(
        {
            "fs_info": [],
            "fs_files": [
                _directory_page("8100", "9000", "library"),  # read_target: resolve
                _empty_page(),  # movie 缺失 -> None
                _directory_page("8100", "9000", "library"),  # move: resolve
                _empty_page(),  # movie 缺失 -> None
            ],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
        }
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_live_call_executor,
        intents=(
            _intent(
                target_parent_id="9000",
                target_directory_path="library/movie",
            ),
        ),
        managed_directory_ids=("7000", "9000"),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_organization_contract(),
        target_root_id="9000",
    )

    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.read_target("9000", "after.mkv")
    assert error.value.code == "scope_unverified"
    with pytest.raises(P115OrganizationTransportError) as error:
        await transport.move("100", "9000")
    assert error.value.code == "scope_unverified"
    # 解析失败发生在写调用之前:不得发出 fs_move。
    assert [call[0] for call in client.calls] == ["fs_files", "fs_files", "fs_files", "fs_files"]


@pytest.mark.asyncio
async def test_offline_transport_path_target_reads_and_moves_in_plan_space():
    transport = _transport(
        intents=(
            _intent(
                target_parent_id="9000",
                target_directory_path="library/movie",
            ),
        ),
        managed_directory_ids=("7000", "9000"),
        states={"100": RemoteObjectState("100", "7000", "before.mkv")},
    )

    assert await transport.read_target("9000", "after.mkv") is None
    move = await transport.move("100", "9000")
    rename = await transport.rename("100", "after.mkv")
    assert move.status is OrganizationTransportStatus.SUCCESS
    assert rename.status is OrganizationTransportStatus.SUCCESS
    assert await transport.read_object("100") == RemoteObjectState(
        "100", "9000", "after.mkv"
    )
    assert [call.operation for call in transport.calls] == [
        "read_target",
        "move",
        "rename",
        "read_object",
    ]


@pytest.mark.asyncio
async def test_write_execute_falls_back_to_app_endpoint_on_provider_405():
    """The retired web write endpoints (HTTP 405) fall back to the app endpoints."""
    from urllib.error import HTTPError

    from watch_assistant.adapters.p115_c03_live_transport import (
        P115C03LiveTransport,
    )
    from watch_assistant.adapters.p115_library_write_contract import (
        WriteStatus,
        prepare_mkdir,
        prepare_move,
        prepare_recycle,
    )

    class _FallingBackClient(_LiveFakeP115Client):
        def __init__(self, responses):
            super().__init__(responses)
            self.app_calls = []

        def _app_response(self, name, payload, **kwargs):
            self.app_calls.append((name, dict(payload), dict(kwargs)))
            value = self.responses[name].pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        def fs_mkdir(self, payload, **kwargs):
            raise HTTPError(
                "https://webapi.115.com/files/add", 405, "Method Not Allowed", None, None
            )

        def fs_move(self, payload, **kwargs):
            raise HTTPError(
                "https://webapi.115.com/files/move", 405, "Method Not Allowed", None, None
            )

        def fs_delete(self, payload, **kwargs):
            raise HTTPError(
                "https://webapi.115.com/rb/delete", 405, "Method Not Allowed", None, None
            )

        def fs_mkdir_app(self, payload, **kwargs):
            return self._app_response("fs_mkdir_app", payload, **kwargs)

        def fs_move_app(self, payload, **kwargs):
            return self._app_response("fs_move_app", payload, **kwargs)

        def fs_delete_app(self, payload, **kwargs):
            return self._app_response("fs_delete_app", payload, **kwargs)

    def make_transport(client):
        return P115C03LiveTransport(client, call_executor=_live_call_executor)

    # mkdir: name param on the app endpoint
    client = _FallingBackClient(
        {"fs_mkdir_app": [{"state": True, "data": {"category_id": "42"}}]}
    )
    transport = make_transport(client)
    receipt = await transport.execute(
        prepare_mkdir("7000", "Season 02"), timeout_seconds=30.0
    )
    assert receipt.status is WriteStatus.SUCCESS
    assert client.app_calls[0][0] == "fs_mkdir_app"
    assert client.app_calls[0][1] == {"pid": "7000", "name": "Season 02"}

    # move: ids/to_cid on the app endpoint
    client = _FallingBackClient({"fs_move_app": [{"state": True}]})
    transport = make_transport(client)
    receipt = await transport.execute(
        prepare_move("100", "9000"), timeout_seconds=30.0
    )
    assert receipt.status is WriteStatus.SUCCESS
    assert client.app_calls[0][0] == "fs_move_app"
    assert client.app_calls[0][1] == {"ids": "100", "to_cid": "9000"}

    # recycle: file_ids on the app endpoint
    client = _FallingBackClient({"fs_delete_app": [{"state": True}]})
    transport = make_transport(client)
    receipt = await transport.execute(
        prepare_recycle("100"), timeout_seconds=30.0
    )
    assert receipt.status is WriteStatus.SUCCESS
    assert client.app_calls[0][0] == "fs_delete_app"
    assert client.app_calls[0][1] == {"file_ids": "100"}
