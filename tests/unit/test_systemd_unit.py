import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unit = _load("systemd_unit")
release_update = _load("systemd_release_update")
postdeploy = _load("postdeploy_release_check")


def _owner() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def _write_package(root: Path, commit: str, marker: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "deploy").mkdir()
    service = root / "deploy" / "watch-assistant.service"
    content = (ROOT / "deploy" / "watch-assistant.service").read_text(encoding="utf-8")
    if marker:
        content += f"\n# {marker}\n"
    service.write_text(content, encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "commit": commit,
        "short_commit": commit[:7],
        "source_sha256": "a" * 64,
        "frontend_sha256": "b" * 64,
        "unit_sha256": hashlib.sha256(service.read_bytes()).hexdigest(),
        "build_time": "2026-08-02T00:00:00Z",
        "branch": "codex/publish-main",
    }
    (root / "release-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (root / "VERSION").write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n"
        "branch=codex/publish-main\n",
        encoding="utf-8",
    )
    return service


def test_unit_drift_is_rejected_without_explicit_install(tmp_path: Path):
    package = tmp_path / "release"
    source = _write_package(package, "a" * 40)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    destination = systemd_dir / unit.UNIT_NAME
    destination.write_text("administrator unit\n", encoding="utf-8")
    uid, gid = _owner()

    with pytest.raises(unit.SystemdUnitError, match="unit_drift"):
        unit.plan_unit_change(
            package,
            destination=destination,
            state_directory=tmp_path / "state",
            install=False,
            expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            unit_uid=uid,
            unit_gid=gid,
        )


def test_opt_in_unit_install_is_atomic_and_restorable(tmp_path: Path):
    package = tmp_path / "release"
    source = _write_package(package, "a" * 40)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    destination = systemd_dir / unit.UNIT_NAME
    original = b"[Unit]\nDescription=administrator unit\n"
    destination.write_bytes(original)
    uid, gid = _owner()
    change = unit.plan_unit_change(
        package,
        destination=destination,
        state_directory=tmp_path / "state",
        install=True,
        expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        unit_uid=uid,
        unit_gid=gid,
    )

    unit.apply_unit_change(change, unit_uid=uid, unit_gid=gid)
    assert destination.read_bytes() == source.read_bytes()
    assert change.backup_path is not None
    assert change.backup_path.read_bytes() == original
    assert change.backup_path.stat().st_mode & 0o777 == unit.BACKUP_MODE

    unit.restore_unit_change(change, unit_uid=uid, unit_gid=gid)
    assert destination.read_bytes() == original


def test_unknown_drop_in_blocks_install_and_is_not_removed(tmp_path: Path):
    package = tmp_path / "release"
    source = _write_package(package, "a" * 40)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    destination = systemd_dir / unit.UNIT_NAME
    destination.write_bytes((ROOT / "deploy" / "watch-assistant.service").read_bytes())
    drop_in_dir = systemd_dir / f"{unit.UNIT_NAME}.d"
    drop_in_dir.mkdir()
    unknown = drop_in_dir / "inspection.conf"
    unknown.write_text("[Service]\nEnvironment=INSPECTION_ENABLED=false\n", encoding="utf-8")
    uid, gid = _owner()

    with pytest.raises(unit.SystemdUnitError, match="unit_drop_in_present"):
        unit.plan_unit_change(
            package,
            destination=destination,
            state_directory=tmp_path / "state",
            install=True,
            expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            unit_uid=uid,
            unit_gid=gid,
        )
    assert unknown.exists()


def test_postdeploy_verifies_unit_digest_and_drop_ins(tmp_path: Path, monkeypatch):
    commit = "a" * 40
    current_root = tmp_path / "current"
    _write_package(current_root, commit)
    release_env = tmp_path / "release.env"
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={commit}\n", encoding="utf-8")
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    destination = systemd_dir / unit.UNIT_NAME
    shutil.copy2(current_root / "deploy" / unit.UNIT_NAME, destination)
    drop_in_dir = systemd_dir / f"{unit.UNIT_NAME}.d"

    monkeypatch.setattr(
        postdeploy.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f"EnvironmentFiles={release_env}\n"
                "MainPID=1234\n"
                f"WorkingDirectory={current_root}\n"
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(postdeploy, "_read_health", lambda *_args: commit)

    assert postdeploy.check_release_consistency(
        version_file=current_root / "VERSION",
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        unit_path=destination,
        drop_in_dir=drop_in_dir,
        unit_uid=os.getuid(),
        unit_gid=os.getgid(),
        health_timeout=0.01,
        health_poll_interval=0.005,
    ) == (True, "ok")

    drop_in_dir.mkdir()
    (drop_in_dir / "inspection.conf").write_text("[Service]\n", encoding="utf-8")
    assert postdeploy.check_release_consistency(
        version_file=current_root / "VERSION",
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        unit_path=destination,
        drop_in_dir=drop_in_dir,
        unit_uid=os.getuid(),
        unit_gid=os.getgid(),
        health_timeout=0.01,
        health_poll_interval=0.005,
    ) == (False, "unit_drop_in_present")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_release_rollback_restores_backed_up_unit(tmp_path: Path):
    old_commit = "a" * 40
    new_commit = "b" * 40
    releases = tmp_path / "releases"
    old_root = releases / "watch-assistant-old"
    new_root = releases / "watch-assistant-new"
    old_unit = _write_package(old_root, old_commit, "old")
    new_unit = _write_package(new_root, new_commit, "new")
    current = tmp_path / "current"
    current.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    installed = systemd_dir / unit.UNIT_NAME
    shutil.copy2(old_unit, installed)
    uid, gid = _owner()

    with pytest.raises(ValueError, match="unit_drift"):
        release_update.switch_release(
            release_root=new_root,
            expected_release=new_commit,
            current_root=current,
            release_env=release_env,
            state_file=state_file,
            allowed_releases_root=releases,
            unit_path=installed,
            unit_uid=uid,
            unit_gid=gid,
        )

    state = release_update.switch_release(
        release_root=new_root,
        expected_release=new_commit,
        current_root=current,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases,
        unit_path=installed,
        install_unit=True,
        unit_uid=uid,
        unit_gid=gid,
    )
    assert state["unit_install"] is True
    assert installed.read_bytes() == new_unit.read_bytes()

    release_update.rollback_release(
        current_root=current,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases,
        unit_path=installed,
        unit_uid=uid,
        unit_gid=gid,
    )
    assert current.resolve() == old_root.resolve()
    assert installed.read_bytes() == old_unit.read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_release_switch_failure_restores_unit_before_return(
    tmp_path: Path, monkeypatch
):
    old_commit = "a" * 40
    new_commit = "b" * 40
    releases = tmp_path / "releases"
    old_root = releases / "watch-assistant-old"
    new_root = releases / "watch-assistant-new"
    old_unit = _write_package(old_root, old_commit, "old")
    _write_package(new_root, new_commit, "new")
    current = tmp_path / "current"
    current.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    installed = systemd_dir / unit.UNIT_NAME
    shutil.copy2(old_unit, installed)
    uid, gid = _owner()

    def fail_switch(*_args, **_kwargs):
        raise OSError("fixture switch failure")

    monkeypatch.setattr(release_update, "_atomic_switch", fail_switch)
    with pytest.raises(OSError, match="fixture switch failure"):
        release_update.switch_release(
            release_root=new_root,
            expected_release=new_commit,
            current_root=current,
            release_env=release_env,
            state_file=state_file,
            allowed_releases_root=releases,
            unit_path=installed,
            install_unit=True,
            unit_uid=uid,
            unit_gid=gid,
        )

    assert current.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )
    assert installed.read_bytes() == old_unit.read_bytes()
