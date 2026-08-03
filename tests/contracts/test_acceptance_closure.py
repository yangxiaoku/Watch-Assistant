from argparse import Namespace
from pathlib import Path

import pytest

from scripts.acceptance_closure import (
    ClosureInputError,
    _dry_run,
    _runtime_environment,
    _stage_status,
    _stage_success,
    _validate_execute_args,
)


def test_inventory_and_plan_success_require_complete_read_only_evidence():
    assert _stage_success(
        "inventory", {"complete": True, "root_identity_verified": True}
    )
    assert _stage_success(
        "organization_plan",
        {"status": "success", "write_started": False, "remote_write_calls": 0},
    )
    assert not _stage_success(
        "organization_plan",
        {"status": "success", "write_started": True, "remote_write_calls": 1},
    )


def test_write_stage_timeout_is_uncertain_and_never_normal_retry():
    assert (
        _stage_status(
            "fixture",
            {"status": "uncertain", "write_started": True},
            1,
            write_stage=True,
        )
        == "uncertain"
    )
    assert (
        _stage_status("inventory", None, 1, write_stage=False) == "blocked"
    )
    assert _stage_status("fixture", None, 1, write_stage=True) == "uncertain"


def test_runtime_environment_bridges_native_library_path(monkeypatch):
    monkeypatch.setenv("WATCH_ASSISTANT_NATIVE_LIBRARY_PATH", "/tmp/native")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/tmp/old")

    environment = _runtime_environment()

    assert environment["DYLD_LIBRARY_PATH"] == "/tmp/native:/tmp/old"


def test_dry_run_lists_fixture_and_strm_only_for_execute(tmp_path: Path):
    preview = _dry_run(Namespace(mode="preview"), tmp_path / "preview")
    execute = _dry_run(Namespace(mode="execute"), tmp_path / "execute")

    assert preview["status"] == "dry_run"
    assert [item["stage"] for item in preview["stages"]] == [
        "inventory",
        "organization_plan",
    ]
    assert [item["stage"] for item in execute["stages"]] == [
        "inventory",
        "organization_plan",
        "fixture",
        "strm",
    ]


def test_execute_requires_confirmation_before_reading_scope(tmp_path: Path):
    args = Namespace(
        root_id="1000",
        source_id="1001",
        target_id="1002",
        cookie_path=tmp_path / "cookie",
        managed_scope_path=tmp_path / "scope",
        live_read=True,
        confirm_fixture=False,
        fixture_parent_id="2000",
        fixture_authorization_path=tmp_path / "fixture-auth",
        strm_root_id="2000",
        strm_file_id="3000",
        strm_rename_authorization=tmp_path / "rename-auth",
        strm_restore_authorization=tmp_path / "restore-auth",
    )
    with pytest.raises(ClosureInputError, match="fixture_confirmation_required"):
        _validate_execute_args(args)
