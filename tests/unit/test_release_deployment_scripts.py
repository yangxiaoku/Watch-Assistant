import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


systemd_release = _load_script("systemd_release_update")
postdeploy_release = _load_script("postdeploy_release_check")


def _write_version(path: Path, commit: str = "abcdef1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )


def _systemd_identity(current_root: Path, release_env: Path) -> str:
    return (
        f"EnvironmentFiles=/etc/watch-assistant.env (ignore_errors=no) "
        f"-{release_env} (ignore_errors=yes)\n"
        "MainPID=1234\n"
        f"WorkingDirectory={current_root}\n"
    )


def _mock_systemctl(
    monkeypatch: pytest.MonkeyPatch, current_root: Path, release_env: Path
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_systemd_identity(current_root, release_env),
            stderr="",
        )

    monkeypatch.setattr(postdeploy_release.subprocess, "run", run)
    return calls


def test_systemd_release_update_is_atomic_and_removes_stale_drop_in(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    release_env = tmp_path / "state" / "release.env"
    stale_drop_in = tmp_path / "systemd" / "release.conf"
    _write_version(version_file)
    release_env.parent.mkdir()
    release_env.write_text("WATCH_ASSISTANT_RELEASE=0123456\n", encoding="utf-8")
    stale_drop_in.parent.mkdir()
    stale_drop_in.write_text("[Service]\nEnvironment=WATCH_ASSISTANT_RELEASE=0123456\n")

    release = systemd_release.update_release_metadata(
        version_file=version_file,
        release_env=release_env,
        stale_drop_in=stale_drop_in,
    )

    assert release == "abcdef1"
    assert release_env.read_text(encoding="utf-8") == "WATCH_ASSISTANT_RELEASE=abcdef1\n"
    assert not stale_drop_in.exists()
    assert list(release_env.parent.glob(".release.env.*.tmp")) == []


def test_release_build_and_verify_use_provenance_and_project_venv():
    build = (ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_release_artifact.sh").read_text(
        encoding="utf-8"
    )
    verify_gate = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")

    assert 'git archive --format=tar "$EXPECTED_COMMIT"' in build
    assert "npm ci" in build
    assert "npm run build" in build
    assert "release-manifest.json" in build
    assert "source_sha256" in build and "frontend_sha256" in build
    assert "release_startup_smoke.py" in verify
    assert "git archive --format=tar \"$EXPECTED_COMMIT\"" in verify
    assert "frontend_sha256" in verify
    assert "${PYTHON_BIN:-python}" not in build
    assert "${PYTHON_BIN:-python}" not in verify_gate
    assert "worktree .venv Python is required" in verify_gate


def test_compose_and_docker_reject_unknown_release():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "WATCH_ASSISTANT_RELEASE:?" in compose
    assert "WATCH_ASSISTANT_RELEASE=unknown" not in compose
    assert "ARG WATCH_ASSISTANT_RELEASE" in dockerfile
    assert "^[0-9a-fA-F]{40}$" in dockerfile


def test_systemd_release_update_rejects_invalid_version_without_mutation(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    release_env = tmp_path / "release.env"
    stale_drop_in = tmp_path / "release.conf"
    version_file.write_text("commit=not-a-release\n", encoding="utf-8")
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    stale_drop_in.write_text("stale\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid_release_version"):
        systemd_release.update_release_metadata(
            version_file=version_file,
            release_env=release_env,
            stale_drop_in=stale_drop_in,
        )

    assert release_env.read_text(encoding="utf-8") == "WATCH_ASSISTANT_RELEASE=abcdef1\n"
    assert stale_drop_in.exists()


def test_postdeploy_check_requires_matching_version_systemd_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "state" / "release.env"
    stale_drop_in = tmp_path / "release.conf"
    _write_version(version_file)
    release_env.parent.mkdir()
    release_env.write_text(
        "WATCH_ASSISTANT_RELEASE=abcdef1\nOTHER_SECRET=must-not-be-read-as-release\n",
        encoding="utf-8",
    )

    calls = _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(
        postdeploy_release,
        "_read_health",
        lambda _url, _timeout: "abcdef1",
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        stale_drop_in=stale_drop_in,
        release_env=release_env,
        current_root=current_root,
    ) == (True, "ok")
    assert "--property=Environment" not in calls[0]
    assert "--property=EnvironmentFiles" in calls[0]
    assert "must-not-be-read-as-release" not in capsys.readouterr().out


def test_postdeploy_check_fails_closed_when_health_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(postdeploy_release, "_read_health", lambda *_args: "0123456")

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "release_mismatch")


def test_postdeploy_check_fails_when_release_env_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="EnvironmentFiles=/etc/watch-assistant.env\n", stderr=""
        ),
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "release_env_not_loaded")


def test_postdeploy_check_requires_ok_health_status_and_safe_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    commit = "a" * 40
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file, commit)
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={commit}\n", encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(postdeploy_release, "_read_health", lambda *_args: commit)
    monkeypatch.setattr(
        postdeploy_release,
        "_read_diagnostics",
        lambda *_args: "database_integrity_not_supported",
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        expected_release=commit,
        diagnostics_url="http://fixture.invalid/diagnostics",
        diagnostics_token="fixture-token",
    ) == (False, "database_integrity_not_supported")

    monkeypatch.setattr(postdeploy_release, "_read_diagnostics", lambda *_args: "ok")
    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        expected_release=commit,
        diagnostics_url="http://fixture.invalid/diagnostics",
        diagnostics_token="fixture-token",
    ) == (True, "ok")


def test_read_health_rejects_non_ok_status_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "degraded", "release": "a" * 40}).encode()

        def getcode(self):
            return 200

    monkeypatch.setattr(postdeploy_release, "urlopen", lambda *_args, **_kwargs: _Response())
    assert postdeploy_release._read_health("http://fixture.invalid/health", 1.0) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_systemd_release_switch_records_previous_metadata_and_rolls_back(
    tmp_path: Path,
):
    old_commit = "a" * 40
    new_commit = "b" * 40
    releases_root = tmp_path / "releases"
    old_root = releases_root / "watch-assistant-old"
    new_root = releases_root / "watch-assistant-new"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    _write_version(old_root / "VERSION", old_commit)
    _write_version(new_root / "VERSION", new_commit)
    (new_root / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    current_root = tmp_path / "current"
    current_root.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"

    state = systemd_release.switch_release(
        release_root=new_root,
        expected_release=new_commit,
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    assert state["previous_release"] == old_commit
    assert current_root.resolve() == new_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={new_commit}\n"
    )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["previous_current_target"] == str(old_root.resolve())
    assert saved["target_release"] == new_commit

    systemd_release.rollback_release(
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    assert current_root.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_systemd_release_rollback_restores_legacy_short_release_metadata(
    tmp_path: Path,
):
    old_commit = "abcdef1"
    new_commit = "b" * 40
    releases_root = tmp_path / "releases"
    old_root = releases_root / "watch-assistant-old"
    new_root = releases_root / "watch-assistant-new"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    _write_version(old_root / "VERSION", old_commit)
    _write_version(new_root / "VERSION", new_commit)
    current_root = tmp_path / "current"
    current_root.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"

    systemd_release.switch_release(
        release_root=new_root,
        expected_release=new_commit,
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    systemd_release.rollback_release(
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )

    assert current_root.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (None, "release_env_missing"),
        ("OTHER_SECRET=do-not-report\n", "release_env_invalid"),
        ("WATCH_ASSISTANT_RELEASE=not-a-release\n", "release_env_invalid"),
    ],
)
def test_postdeploy_check_fails_closed_on_missing_or_invalid_release_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    expected_code: str,
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    if content is not None:
        release_env.write_text(content, encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, expected_code)


def test_postdeploy_check_fails_closed_on_current_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = tmp_path / "staged" / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file, "abcdef1")
    _write_version(current_root / "VERSION", "0123456")
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "current_release_mismatch")


def test_postdeploy_check_fails_closed_on_service_path_or_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f"EnvironmentFiles={release_env}\n"
                "MainPID=0\n"
                f"WorkingDirectory={tmp_path / 'old-current'}\n"
            ),
            stderr="",
        ),
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "service_not_running")
