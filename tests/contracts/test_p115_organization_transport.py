import asyncio

import pytest

from watch_assistant.adapters.p115_organization_transport import (
    LiveP115OrganizationTransport,
    OfflineP115OrganizationTransport,
    OrganizationObjectIntent,
    P115OrganizationMethod,
    P115OrganizationTransportError,
    create_live_p115_organization_transport,
    create_p115_organization_transport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationWriteCapability,
    P115OrganizationContract,
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


async def _live_call_executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


def _live_file(file_id, parent_id, name):
    return {"state": True, "data": {"fc": 1, "fid": file_id, "cid": parent_id, "n": name}}


def _empty_page():
    return {"state": True, "data": [], "offset": 0, "limit": 1, "count": 0}


def _file_page(file_id, parent_id, name):
    return {
        "state": True,
        "data": [{"fc": 1, "fid": file_id, "cid": parent_id, "n": name}],
        "offset": 0,
        "limit": 1,
        "count": 1,
    }


def _intent(
    *,
    object_id: str = "100",
    source_parent_id: str = "7000",
    source_name: str = "before.mkv",
    target_parent_id: str = "8000",
    target_name: str = "after.mkv",
) -> OrganizationObjectIntent:
    return OrganizationObjectIntent(
        object_id,
        source_parent_id,
        source_name,
        target_parent_id,
        target_name,
    )


def _organization_contract() -> P115OrganizationContract:
    return P115OrganizationContract(
        verified=True,
        timeout_enforced=True,
        capabilities=frozenset(
            {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MOVE,
                OrganizationWriteCapability.RENAME,
                OrganizationWriteCapability.RECYCLE,
                OrganizationWriteCapability.POSTCONDITION,
            }
        ),
    )


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


def test_live_transport_rejects_unverified_organization_contract():
    with pytest.raises(P115OrganizationTransportError, match="contract_unverified"):
        create_live_p115_organization_transport(
            client=object(),
            call_executor=_live_call_executor,
            intents=(_intent(),),
            managed_directory_ids=("7000", "8000"),
            scope_confirmed=True,
            live_enabled=True,
            organization_contract=P115OrganizationContract(),
        )

    contract = P115OrganizationContract(
        verified=True,
        timeout_enforced=True,
        capabilities=frozenset(
            {
                OrganizationWriteCapability.READ_SCOPE,
                OrganizationWriteCapability.MOVE,
                OrganizationWriteCapability.RENAME,
                OrganizationWriteCapability.POSTCONDITION,
            }
        ),
    )
    transport = create_live_p115_organization_transport(
        client=object(),
        call_executor=_live_call_executor,
        intents=(_intent(),),
        managed_directory_ids=("7000", "8000"),
        scope_confirmed=True,
        live_enabled=True,
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
    assert [call.operation for call in transport.calls] == ["read_object"]
