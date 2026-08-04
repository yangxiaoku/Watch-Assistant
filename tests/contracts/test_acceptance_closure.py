import sys
from argparse import Namespace
from pathlib import Path

import pytest

import scripts.acceptance_closure as closure
from scripts.acceptance_closure import (
    ClosureInputError,
    PRODUCTION_ACCEPTANCE_ENV,
    PRODUCTION_PYTHON_ENV,
    _dry_run,
    _runtime_environment,
    _select_runtime,
    _stage_status,
    _stage_success,
    _validate_production_python,
    _validate_execute_args,
)
from scripts.p115_organization_application_live_runner import _configure_preview_scope


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
    monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", "/tmp/old")

    environment = _runtime_environment()

    assert environment["DYLD_FALLBACK_LIBRARY_PATH"] == "/tmp/native:/tmp/old"


def test_runtime_environment_discovers_bundled_macos_libraries(monkeypatch, tmp_path: Path):
    dependencies = tmp_path / "dependencies"
    native = dependencies / "native/poppler/poppler/lib"
    native.mkdir(parents=True)
    (native / "libssl.3.dylib").touch()
    (native / "libcrypto.3.dylib").touch()
    base_executable = dependencies / "python/bin/python3.12"
    base_executable.parent.mkdir(parents=True)
    base_executable.touch()

    monkeypatch.delenv("WATCH_ASSISTANT_NATIVE_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "_base_executable", str(base_executable), raising=False)

    environment = _runtime_environment()

    assert environment["WATCH_ASSISTANT_NATIVE_LIBRARY_PATH"] == str(native)
    assert environment["DYLD_FALLBACK_LIBRARY_PATH"] == str(native)


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="ascii")
    path.chmod(0o755)
    return path


def test_external_runtime_is_rejected_without_explicit_acceptance(
    monkeypatch, tmp_path: Path
):
    external_python = _make_executable(tmp_path / "venv/bin/python")
    root = tmp_path / "release"
    root.mkdir()
    (root / "src/watch_assistant").mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(external_python))

    with pytest.raises(ClosureInputError, match="worktree_venv_required"):
        _select_runtime(
            Namespace(production_acceptance=False, production_python=None),
            root=root,
            base_environment={},
        )


def test_cli_production_acceptance_selects_external_python_and_release_pythonpath(
    tmp_path: Path,
):
    external_python = _make_executable(tmp_path / "venv/bin/python")
    root = tmp_path / "release"
    root.mkdir()
    (root / "src/watch_assistant").mkdir(parents=True)

    runtime = _select_runtime(
        Namespace(production_acceptance=True, production_python=external_python),
        root=root,
        base_environment={"PYTHONPATH": "/unknown/install", "SECRET_VALUE": "hidden"},
    )

    assert runtime.python_executable == str(external_python)
    assert runtime.environment["PYTHONPATH"] == str((root / "src").resolve())
    assert runtime.audit == {
        "mode": "production_external",
        "authorization_source": "cli",
        "python_source": "cli",
        "pythonpath_scope": "release_src",
    }


def test_environment_production_acceptance_selects_declared_python(
    tmp_path: Path,
):
    external_python = _make_executable(tmp_path / "venv/bin/python")
    root = tmp_path / "release"
    root.mkdir()
    (root / "src/watch_assistant").mkdir(parents=True)

    runtime = _select_runtime(
        Namespace(production_acceptance=False, production_python=None),
        root=root,
        base_environment={
            PRODUCTION_ACCEPTANCE_ENV: "1",
            PRODUCTION_PYTHON_ENV: str(external_python),
            "PYTHONPATH": "/unknown/install",
        },
    )

    assert runtime.python_executable == str(external_python)
    assert runtime.audit["authorization_source"] == "environment"
    assert runtime.audit["python_source"] == "environment"
    assert runtime.environment["PYTHONPATH"] == str((root / "src").resolve())


def test_production_python_cannot_point_inside_current_release(tmp_path: Path):
    root = tmp_path / "release"
    root.mkdir()

    with pytest.raises(ClosureInputError, match="production_python_must_be_external"):
        _validate_production_python(root / ".venv/bin/python", root)

    with pytest.raises(ClosureInputError, match="production_python_must_be_absolute"):
        _validate_production_python(Path("venv/bin/python"), root)


def test_preview_uses_selected_runtime_for_every_child_stage(monkeypatch, tmp_path: Path):
    cookie = tmp_path / "cookie"
    managed_scope = tmp_path / "scope"
    cookie.touch()
    managed_scope.touch()
    args = Namespace(
        root_id="1000",
        source_id="1001",
        target_id="1002",
        cookie_path=cookie,
        managed_scope_path=managed_scope,
        live_read=True,
    )
    commands: list[tuple[str, ...]] = []

    def fake_run_stage(**kwargs):
        commands.append(tuple(kwargs["command"]))
        return {"stage": kwargs["stage"], "status": "success"}

    monkeypatch.setattr(closure, "_run_stage", fake_run_stage)

    reports = closure._preview(
        args,
        root=tmp_path / "release",
        output_dir=tmp_path / "evidence",
        environment={},
        python_executable="/opt/watch-assistant/venv/bin/python",
    )

    assert [report["status"] for report in reports] == ["success", "success"]
    assert [command[0] for command in commands] == [
        "/opt/watch-assistant/venv/bin/python",
        "/opt/watch-assistant/venv/bin/python",
    ]


def test_rejected_runtime_does_not_echo_secret_environment_values(
    monkeypatch, tmp_path: Path, capsys
):
    secret = "cookie-token-must-not-appear"
    monkeypatch.setattr(sys, "executable", str(tmp_path / "external/python"))
    monkeypatch.delenv(PRODUCTION_ACCEPTANCE_ENV, raising=False)
    monkeypatch.setenv(PRODUCTION_PYTHON_ENV, f"/opt/{secret}/python")

    exit_code = closure.main(
        ["offline", "--confirm-fixture", "--output-dir", str(tmp_path / "evidence")]
    )

    output = capsys.readouterr().out
    summary = (tmp_path / "evidence" / "SUMMARY.json").read_text(encoding="utf-8")
    assert exit_code == 2
    assert "production_acceptance_required" in output
    assert secret not in output
    assert secret not in summary


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


def test_live_plan_preview_registers_both_managed_directory_ids():
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace())

    _configure_preview_scope(
        app,
        source_id="3482085898508567892",
        target_id="3482969620225197691",
    )

    assert app.state.organization_target_root_id == "3482969620225197691"
    assert app.state.p115_browsed_directory_ids == {
        "3482085898508567892",
        "3482969620225197691",
    }
