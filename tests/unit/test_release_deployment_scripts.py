import importlib.util
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
systemd_prepare = _load_script("systemd_release_prepare")


def _write_version(path: Path, commit: str = "abcdef1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )


def _write_release_tree(root: Path, commit: str = "abcdef1") -> None:
    for relative_name in systemd_prepare.REQUIRED_RELEASE_FILES:
        path = root / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative_name == "VERSION":
            _write_version(path, commit)
        else:
            path.write_text("release fixture\n", encoding="utf-8")


def _prepare_release(
    release_root: Path, releases_root: Path, **kwargs
) -> str:
    original_allowed_root = systemd_prepare.DEFAULT_RELEASES_ROOT
    systemd_prepare.DEFAULT_RELEASES_ROOT = releases_root
    try:
        return systemd_prepare.prepare_release(
            release_root=release_root,
            **kwargs,
        )
    finally:
        systemd_prepare.DEFAULT_RELEASES_ROOT = original_allowed_root


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


def test_systemd_release_prepare_normalizes_only_top_level_and_checks_service_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    releases_root = tmp_path / "releases"
    release_root = releases_root / "watch-assistant-abcdef1"
    _write_release_tree(release_root)
    release_root.chmod(0o700)
    ordinary_file = release_root / "src/watch_assistant/app.py"
    ordinary_file.chmod(0o640)
    ordinary_mode = ordinary_file.stat().st_mode & 0o777
    data_directory = release_root / "data"
    data_directory.mkdir()
    data_directory.chmod(0o700)
    data_mode = data_directory.stat().st_mode & 0o777
    release_env = release_root / "release.env"
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    release_env.chmod(0o600)
    release_env_mode = release_env.stat().st_mode & 0o777
    chmod_calls: list[tuple[Path, int]] = []
    original_chmod = Path.chmod

    def record_chmod(path: Path, mode: int) -> None:
        chmod_calls.append((path, mode))
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", record_chmod)

    service_uid = getattr(os, "getuid", lambda: release_root.stat().st_uid)()
    prepared = _prepare_release(
        release_root,
        releases_root,
        expected_release="abcdef1",
        service_user=None,
    )

    assert prepared == "abcdef1"
    if os.name == "nt":
        assert (release_root, 0o755) in chmod_calls
    else:
        assert (release_root.stat().st_mode & 0o777) == 0o755
    assert (ordinary_file.stat().st_mode & 0o777) == ordinary_mode
    assert (data_directory.stat().st_mode & 0o777) == data_mode
    assert (release_env.stat().st_mode & 0o777) == release_env_mode
    assert service_uid == release_root.stat().st_uid


def test_systemd_release_prepare_rejects_scope_version_and_missing_files_without_root_mutation(
    tmp_path: Path,
):
    releases_root = tmp_path / "releases"
    release_root = releases_root / "watch-assistant-abcdef1"
    _write_release_tree(release_root)
    release_root.chmod(0o700)
    initial_mode = release_root.stat().st_mode & 0o777

    with pytest.raises(systemd_prepare.ReleasePrepareError) as out_of_scope:
        _prepare_release(
            tmp_path,
            releases_root,
            expected_release="abcdef1",
            service_user=None,
        )
    assert out_of_scope.value.code == "release_path_out_of_scope"
    assert (release_root.stat().st_mode & 0o777) == initial_mode

    with pytest.raises(systemd_prepare.ReleasePrepareError) as version_mismatch:
        _prepare_release(
            release_root,
            releases_root,
            expected_release="0123456",
            service_user=None,
        )
    assert version_mismatch.value.code == "release_version_mismatch"
    assert (release_root.stat().st_mode & 0o777) == initial_mode

    (release_root / "frontend/dist/index.html").unlink()
    with pytest.raises(systemd_prepare.ReleasePrepareError) as missing_file:
        _prepare_release(
            release_root,
            releases_root,
            expected_release="abcdef1",
            service_user=None,
        )
    assert missing_file.value.code == "required_file_missing"
    assert (release_root.stat().st_mode & 0o777) == initial_mode


def test_systemd_release_prepare_rejects_escape_symlink(tmp_path: Path):
    releases_root = tmp_path / "releases"
    release_root = releases_root / "watch-assistant-abcdef1"
    _write_release_tree(release_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (release_root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(systemd_prepare.ReleasePrepareError) as escaped:
        _prepare_release(
            release_root,
            releases_root,
            expected_release="abcdef1",
            service_user=None,
        )
    assert escaped.value.code == "release_symlink_escape"


def test_systemd_release_prepare_rejects_key_file_without_service_read_access(
    tmp_path: Path,
):
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX mode semantics")
    releases_root = tmp_path / "releases"
    release_root = releases_root / "watch-assistant-abcdef1"
    _write_release_tree(release_root)
    key_file = release_root / "src/watch_assistant/app.py"
    key_file.chmod(0o600)
    file_stat = key_file.stat()
    identity = systemd_prepare.ServiceIdentity(
        uid=file_stat.st_uid + 1,
        gids=(file_stat.st_gid + 1,),
    )

    with pytest.raises(systemd_prepare.ReleasePrepareError) as denied:
        _prepare_release(
            release_root,
            releases_root,
            expected_release="abcdef1",
            service_user=None,
            service_identity=identity,
        )
    assert denied.value.code == "release_access_denied"


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
