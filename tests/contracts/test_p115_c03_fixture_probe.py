import pytest

from scripts.p115_c03_fixture_probe import main as probe_cli_main
from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_WRITE_ENABLED_ENV,
    MAX_BATCH_OBSERVATION_CALLS,
    MAX_CONFLICT_OBSERVATION_CALLS,
    MAX_READ_CALLS,
    MAX_TOTAL_CALLS,
    MAX_WRITE_CALLS,
    C03CallBudget,
    C03ProbeStatus,
    FakeP115C03Transport,
    WriteOperation,
    normalize_parent_id,
    run_p115_c03_fixture_probe,
)


def _enabled_env() -> dict[str, str]:
    return {
        C03_WRITE_ENABLED_ENV: "1",
        C03_MANAGED_FIXTURE_ENV: "1",
        C03_CLEANUP_PLAN_ENV: "1",
    }


@pytest.mark.asyncio
async def test_c03_lifecycle_is_bounded_and_reclaims_only_its_exact_root():
    transport = FakeP115C03Transport()

    report = await run_p115_c03_fixture_probe(
        transport=transport, parent_id="7000", env=_enabled_env()
    )

    assert report.status is C03ProbeStatus.SUCCESS
    assert report.cleanup == "complete"
    assert report.error_code is None
    assert report.write_calls == MAX_WRITE_CALLS == 10
    assert report.read_calls == MAX_READ_CALLS == 15
    assert report.list_calls == 4
    assert transport.list_count == report.list_calls
    assert report.write_calls + report.read_calls <= MAX_TOTAL_CALLS
    assert transport.write_operations == [
        WriteOperation.MKDIR,
        WriteOperation.MKDIR,
        WriteOperation.MKDIR,
        WriteOperation.MKDIR,
        WriteOperation.RENAME,
        WriteOperation.MOVE,
        WriteOperation.RENAME,
        WriteOperation.MOVE,
        WriteOperation.RENAME,
        WriteOperation.RECYCLE,
    ]
    assert transport._entries == {}

    rendered = repr(report) + repr(report.to_public_dict()) + repr(transport)
    for value in ("7000", "source", "quarantine", "artifact", "wa-c03"):
        assert value not in rendered
    assert len(report.fixture_fingerprint or "") == 16


@pytest.mark.asyncio
async def test_all_three_gates_are_required_before_transport_calls():
    for missing in (
        C03_WRITE_ENABLED_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_CLEANUP_PLAN_ENV,
    ):
        env = _enabled_env()
        env.pop(missing)
        transport = FakeP115C03Transport()
        report = await run_p115_c03_fixture_probe(
            transport=transport, parent_id=7000, env=env
        )
        assert report.status is C03ProbeStatus.BLOCKED
        assert transport.write_operations == []
        assert transport.read_count == 0

    live_transport = FakeP115C03Transport()
    live_report = await run_p115_c03_fixture_probe(
        transport=live_transport, parent_id=7000, env=_enabled_env(), live=True
    )
    assert live_report.status is C03ProbeStatus.BLOCKED
    assert live_report.error_code == "live_disabled"
    assert live_transport.write_operations == []

    live_env = _enabled_env()
    live_env[C03_LIVE_ENV] = "1"
    live_report = await run_p115_c03_fixture_probe(
        transport=FakeP115C03Transport(), parent_id=7000, env=live_env, live=True
    )
    assert live_report.status is C03ProbeStatus.SUCCESS


@pytest.mark.asyncio
async def test_parent_id_accepts_only_nonzero_decimal_values():
    assert normalize_parent_id(7000) == "7000"
    assert normalize_parent_id("7000") == "7000"
    for value in (0, "0", "", "7000/child", "https://115.com/7000", True, 1.5):
        report = await run_p115_c03_fixture_probe(
            transport=FakeP115C03Transport(), parent_id=value, env=_enabled_env()
        )
        assert report.status is C03ProbeStatus.BLOCKED
        assert report.error_code == "invalid_parent_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_code"),
    (
        ("move", "remote_write_failed"),
        ("timeout:move", "timeout"),
        ("cancel:move", "cancelled"),
    ),
)
async def test_failure_timeout_and_cancel_are_uncertain_without_write_retry(
    failure, error_code
):
    transport = FakeP115C03Transport(failure)
    report = await run_p115_c03_fixture_probe(
        transport=transport, parent_id="7000", env=_enabled_env()
    )

    assert report.status is C03ProbeStatus.UNCERTAIN
    assert report.error_code == error_code
    assert report.cleanup == "not_attempted_uncertain"
    assert transport.write_operations.count(WriteOperation.MOVE) == 1
    assert WriteOperation.RECYCLE not in transport.write_operations


@pytest.mark.asyncio
async def test_unconfirmed_root_is_never_recycled():
    transport = FakeP115C03Transport("mkdir")
    report = await run_p115_c03_fixture_probe(
        transport=transport, parent_id="7000", env=_enabled_env()
    )

    assert report.status is C03ProbeStatus.UNCERTAIN
    assert report.error_code == "remote_write_failed"
    assert report.cleanup == "not_attempted_uncertain"
    assert WriteOperation.RECYCLE not in transport.write_operations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_fault", ("foreign", "missing", "incomplete", "list-failed")
)
async def test_cleanup_requires_complete_exact_managed_scope(scope_fault):
    transport = FakeP115C03Transport(scope_fault=scope_fault)
    report = await run_p115_c03_fixture_probe(
        transport=transport, parent_id="7000", env=_enabled_env()
    )

    assert report.status is C03ProbeStatus.UNCERTAIN
    assert report.cleanup == "not_attempted_uncertain"
    assert WriteOperation.RECYCLE not in transport.write_operations
    assert transport.list_count >= 1


@pytest.mark.asyncio
async def test_invalid_budget_and_observation_paths_are_closed():
    for budget in (C03CallBudget(max_write_calls=0), C03CallBudget(max_write_calls=11)):
        report = await run_p115_c03_fixture_probe(
            transport=FakeP115C03Transport(),
            parent_id="7000",
            env=_enabled_env(),
            budget=budget,
        )
        assert report.status is C03ProbeStatus.BLOCKED
        assert report.error_code == "invalid_call_budget"
    assert MAX_CONFLICT_OBSERVATION_CALLS == 0
    assert MAX_BATCH_OBSERVATION_CALLS == 0


def test_offline_cli_is_gated_and_does_not_print_fixture_values(monkeypatch, capsys):
    for name in (C03_WRITE_ENABLED_ENV, C03_MANAGED_FIXTURE_ENV, C03_CLEANUP_PLAN_ENV):
        monkeypatch.delenv(name, raising=False)
    assert probe_cli_main(["--parent-id", "7000", "--offline-fixture", "success"]) == 1
    blocked = capsys.readouterr().out
    assert '"status": "blocked"' in blocked
    assert "7000" not in blocked
    assert "source" not in blocked

    for name in (C03_WRITE_ENABLED_ENV, C03_MANAGED_FIXTURE_ENV, C03_CLEANUP_PLAN_ENV):
        monkeypatch.setenv(name, "1")
    assert probe_cli_main(["--parent-id", "7000", "--offline-fixture", "success"]) == 0
    public = capsys.readouterr().out
    assert '"status": "success"' in public
    assert "7000" not in public
    assert "wa-c03" not in public


@pytest.mark.asyncio
async def test_cancelled_error_does_not_escape_as_third_party_text():
    transport = FakeP115C03Transport("cancel:move")
    report = await run_p115_c03_fixture_probe(
        transport=transport, parent_id="7000", env=_enabled_env()
    )
    assert "opaque" not in repr(report)
    assert "opaque" not in repr(transport)
    assert not isinstance(report.error_code, BaseException)
