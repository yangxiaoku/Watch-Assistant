import importlib.util
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
    path.write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )


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


def test_postdeploy_check_requires_matching_version_systemd_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    version_file = tmp_path / "VERSION"
    stale_drop_in = tmp_path / "release.conf"
    _write_version(version_file)

    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="WATCH_ASSISTANT_RELEASE=abcdef1 OTHER=redacted\n",
            stderr="",
        ),
    )
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
    ) == (True, "ok")


def test_postdeploy_check_fails_closed_on_release_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    version_file = tmp_path / "VERSION"
    _write_version(version_file)
    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="WATCH_ASSISTANT_RELEASE=0123456\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(postdeploy_release, "_read_health", lambda *_args: "abcdef1")

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
    ) == (False, "release_mismatch")
