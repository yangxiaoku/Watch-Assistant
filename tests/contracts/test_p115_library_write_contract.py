import asyncio
import inspect

import pytest
from p115client import P115Client

from watch_assistant.adapters.p115_library_write_contract import (
    FakeP115LibraryWriteGateway,
    OrganizationWriteCapability,
    OrganizationWriteGate,
    P115OrganizationContract,
    PostconditionResult,
    PostconditionStatus,
    WriteGate,
    WriteOperation,
    WritePostcondition,
    WriteStatus,
    classify_write_exception,
    evaluate_write_gate,
    evaluate_organization_write_gate,
    prepare_delete,
    prepare_mkdir,
    prepare_move,
    prepare_quarantine,
    prepare_recycle,
    prepare_rename,
    prepare_restore,
)


def _open_gate() -> WriteGate:
    return WriteGate(
        write_enabled=True,
        user_approved=True,
        disposable_fixture=True,
        cleanup_plan=True,
    )


def test_fixed_client_write_signatures_are_recorded_without_client_creation():
    assert tuple(inspect.signature(P115Client.fs_mkdir).parameters)[:2] == (
        "self",
        "payload",
    )
    assert tuple(inspect.signature(P115Client.fs_move).parameters)[:3] == (
        "self",
        "payload",
        "pid",
    )
    assert tuple(inspect.signature(P115Client.fs_rename).parameters)[:2] == (
        "self",
        "payload",
    )
    assert tuple(inspect.signature(P115Client.fs_delete).parameters)[:2] == (
        "self",
        "payload",
    )


def test_payload_candidates_match_fixed_client_and_reject_unsafe_values():
    assert prepare_mkdir("0", "ProbeRoot").payload == {
        "pid": "0",
        "file_name": "ProbeRoot",
    }
    assert prepare_move(101, "7000").payload == {
        "file_ids": "101",
        "to_cid": "7000",
    }
    assert prepare_rename("101", "renamed.mkv").payload == {
        "file_id": "101",
        "file_name": "renamed.mkv",
    }
    assert prepare_recycle("101").payload == {"file_id": "101"}
    assert prepare_delete("101").payload == {"file_id": "101"}
    quarantine = prepare_quarantine("101", "7001", "quarantine.mkv")
    restore = prepare_restore("101", "7000", "original.mkv")
    assert [step.operation for step in quarantine.steps] == [
        WriteOperation.MOVE,
        WriteOperation.RENAME,
    ]
    assert [step.operation for step in restore.steps] == [
        WriteOperation.MOVE,
        WriteOperation.RENAME,
    ]
    with pytest.raises((TypeError, ValueError), match="invalid_id"):
        prepare_move(True, 7000)
    with pytest.raises((TypeError, ValueError), match="invalid_id"):
        prepare_move(-1, 7000)
    with pytest.raises(ValueError, match="invalid_name"):
        prepare_rename(101, "../outside")


def test_all_write_gates_are_closed_by_default_and_delete_stays_disabled():
    for operation in WriteOperation:
        decision = evaluate_write_gate(WriteGate(), operation)
        assert decision.allowed is False
        assert decision.error_code == "write_disabled"
    decision = evaluate_write_gate(
        WriteGate(
            write_enabled=True,
            user_approved=True,
            disposable_fixture=True,
            cleanup_plan=True,
        ),
        WriteOperation.DELETE,
    )
    assert decision.allowed is False
    assert decision.error_code == "permanent_delete_disabled"


def test_organization_gate_requires_verified_capabilities_and_scope():
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
    base = OrganizationWriteGate(
        write_enabled=True,
        plan_confirmed=True,
        scope_confirmed=True,
        contract=contract,
    )

    assert evaluate_organization_write_gate(base, WriteOperation.MOVE).allowed is True
    assert evaluate_organization_write_gate(base, WriteOperation.RENAME).allowed is True
    assert evaluate_organization_write_gate(
        OrganizationWriteGate(
            write_enabled=True,
            plan_confirmed=True,
            scope_confirmed=True,
        ),
        WriteOperation.MOVE,
    ).error_code == "contract_unverified"
    assert evaluate_organization_write_gate(
        OrganizationWriteGate(
            write_enabled=True,
            plan_confirmed=True,
            scope_confirmed=False,
            contract=contract,
        ),
        WriteOperation.MOVE,
    ).error_code == "scope_unverified"
    assert evaluate_organization_write_gate(
        OrganizationWriteGate(
            write_enabled=True,
            plan_confirmed=False,
            scope_confirmed=True,
            contract=contract,
        ),
        WriteOperation.MOVE,
    ).error_code == "approval_required"
    assert evaluate_organization_write_gate(base, WriteOperation.DELETE).error_code == (
        "permanent_delete_disabled"
    )
    assert evaluate_organization_write_gate(base, WriteOperation.RECYCLE).error_code == (
        "capability_unverified"
    )


@pytest.mark.asyncio
async def test_fake_gateway_records_only_payload_shape_and_requires_gate():
    gateway = FakeP115LibraryWriteGateway()
    request = prepare_move("90000000000000000001", "80000000000000000002")
    denied = await gateway.execute(request, gate=WriteGate())
    assert denied.status is WriteStatus.FAILED
    assert denied.error_code == "write_disabled"
    assert gateway.calls == []

    result = await gateway.execute(request, gate=_open_gate())
    assert result.status is WriteStatus.SUCCESS
    assert result.postcondition_required is True
    assert gateway.calls[0].payload_fields == ("file_ids", "to_cid")
    rendered = repr(gateway) + repr(gateway.calls)
    assert "90000000000000000001" not in rendered
    assert "80000000000000000002" not in rendered


@pytest.mark.asyncio
async def test_timeout_is_uncertain_and_requires_postcondition_without_retry():
    gateway = FakeP115LibraryWriteGateway(
        {WriteOperation.MOVE: TimeoutError("sensitive response")}
    )
    result = await gateway.execute(prepare_move(101, 7000), gate=_open_gate())
    assert result.status is WriteStatus.UNCERTAIN
    assert result.error_code == "timeout"
    assert result.postcondition_required is True
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_error_propagates_and_is_not_reclassified():
    gateway = FakeP115LibraryWriteGateway(
        {WriteOperation.RENAME: asyncio.CancelledError()}
    )
    with pytest.raises(asyncio.CancelledError):
        await gateway.execute(prepare_rename(101, "new.mkv"), gate=_open_gate())
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_remote_failure_is_stable_and_postcondition_shape_is_read_only():
    gateway = FakeP115LibraryWriteGateway(
        {WriteOperation.MKDIR: OSError("cookie and path must not leak")},
        {WriteOperation.MKDIR: PostconditionResult(PostconditionStatus.SATISFIED)},
    )
    failed = await gateway.execute(prepare_mkdir(7000, "new-dir"), gate=_open_gate())
    assert failed.status is WriteStatus.FAILED
    assert failed.error_code == "remote_failed"
    postcondition = await gateway.verify_postcondition(
        WritePostcondition(WriteOperation.MKDIR, "101", "7000", "new-dir")
    )
    assert postcondition.status is PostconditionStatus.SATISFIED
    assert gateway.postcondition_calls == [WriteOperation.MKDIR]
    assert "cookie and path" not in repr(failed)


def test_exception_classifier_never_exposes_exception_text():
    result = classify_write_exception(
        WriteOperation.MOVE, RuntimeError("COOKIE=secret /private/path")
    )
    assert result.status is WriteStatus.FAILED
    assert result.error_code == "remote_failed"
    assert "COOKIE" not in repr(result)
    assert "/private/path" not in repr(result)
