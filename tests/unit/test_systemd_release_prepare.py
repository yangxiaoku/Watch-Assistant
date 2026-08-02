import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FULL_COMMIT = "abcdef1" + "0" * 33
PREPARE_PATHS = (
    "VERSION",
    "release-manifest.json",
    "frontend/dist/index.html",
    "src/watch_assistant",
    "src/watch_assistant/app.py",
    "src/watch_assistant/release_metadata.py",
    "deploy/watch-assistant.service",
    "scripts/deploy_systemd_release.sh",
    "scripts/systemd_release_prepare.py",
    "scripts/systemd_release_update.py",
    "scripts/postdeploy_release_check.py",
    "scripts/release_startup_smoke.py",
)


def _load_prepare_script():
    path = ROOT / "scripts" / "systemd_release_prepare.py"
    spec = importlib.util.spec_from_file_location("systemd_release_prepare", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = _load_prepare_script()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_release(
    allowed_root: Path, commit: str = FULL_COMMIT
) -> tuple[Path, dict[str, Path]]:
    release_root = allowed_root / f"watch-assistant-{commit[:7]}"
    (release_root / "frontend" / "dist").mkdir(parents=True)
    (release_root / "src" / "watch_assistant").mkdir(parents=True)
    (release_root / "scripts").mkdir()
    (release_root / "config").mkdir()
    (release_root / "deploy").mkdir()
    (release_root / "VERSION").write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )
    (release_root / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html>", encoding="utf-8"
    )
    (release_root / "src" / "watch_assistant" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    for relative in PREPARE_PATHS[1:]:
        path = release_root / relative
        if relative in {"frontend/dist/index.html", "src/watch_assistant"}:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
    protected = {
        "ordinary": release_root / "src" / "ordinary.py",
        "executable": release_root / "scripts" / "executable.sh",
        "data": release_root / "data" / "state.db",
        "backup": release_root / "backup" / "snapshot.db",
        "release_env": release_root / "release.env",
    }
    protected["ordinary"].write_text("ordinary", encoding="utf-8")
    protected["executable"].write_text("#!/bin/sh\n", encoding="utf-8")
    protected["data"].parent.mkdir()
    protected["data"].write_text("data", encoding="utf-8")
    protected["backup"].parent.mkdir()
    protected["backup"].write_text("backup", encoding="utf-8")
    protected["release_env"].write_text(
        f"WATCH_ASSISTANT_RELEASE={commit}\n", encoding="utf-8"
    )
    return release_root, protected


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics are unavailable")
def test_prepare_fixes_root_mode_without_recursing_into_release(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    release_root, protected = _write_release(allowed_root)
    os.chmod(release_root, 0o700)
    os.chmod(protected["ordinary"], 0o640)
    os.chmod(protected["executable"], 0o751)
    os.chmod(protected["data"].parent, 0o700)
    os.chmod(protected["data"], 0o600)
    os.chmod(protected["backup"].parent, 0o700)
    os.chmod(protected["backup"], 0o600)
    os.chmod(protected["release_env"], 0o600)

    before = {name: _mode(path) for name, path in protected.items()}

    assert (
        prepare.prepare_release(
            release_root,
            FULL_COMMIT,
            allowed_root,
            required_files=PREPARE_PATHS,
        )
        == FULL_COMMIT
    )

    assert _mode(release_root) == 0o755
    assert {name: _mode(path) for name, path in protected.items()} == before


def test_prepare_rejects_missing_and_version_mismatch(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    release_root, _ = _write_release(allowed_root)
    (release_root / "frontend" / "dist" / "index.html").unlink()

    with pytest.raises(prepare.ReleasePrepareError) as missing:
        prepare.prepare_release(
            release_root,
            FULL_COMMIT,
            allowed_root,
            required_files=PREPARE_PATHS,
        )
    assert missing.value.code == "required_path_missing"

    release_root, _ = _write_release(allowed_root / "second")
    with pytest.raises(prepare.ReleasePrepareError) as mismatch:
        prepare.prepare_release(
            release_root,
            "0123456" + "0" * 33,
            allowed_root / "second",
            required_files=PREPARE_PATHS,
        )
    assert mismatch.value.code == "version_mismatch"


def test_prepare_rejects_root_scope_and_allowed_root_itself(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()

    with pytest.raises(prepare.ReleasePrepareError) as out_of_scope:
        prepare.prepare_release(
            outside_root,
            FULL_COMMIT,
            allowed_root,
            required_files=("VERSION",),
        )
    assert out_of_scope.value.code == "release_root_out_of_scope"

    with pytest.raises(prepare.ReleasePrepareError) as same_root:
        prepare.prepare_release(
            allowed_root,
            FULL_COMMIT,
            allowed_root,
            required_files=("VERSION",),
        )
    assert same_root.value.code == "release_root_is_allowed_root"


def test_prepare_cli_accepts_configured_root_and_service_user_on_offline_hosts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    allowed_root = tmp_path / "releases"
    release_root = allowed_root / f"watch-assistant-{FULL_COMMIT[:7]}"
    release_root.mkdir(parents=True)

    assert (
        prepare.main(
            [
                "--release-root",
                str(release_root),
                "--expected-release",
                FULL_COMMIT,
                "--allowed-releases-root",
                str(allowed_root),
                "--service-user",
                "offline-watch-assistant",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "SYSTEMD_RELEASE_PREPARE_CODE=required_path_missing" in output
    assert str(release_root) not in output


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires POSIX test support")
def test_prepare_rejects_release_root_symlink(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    target, _ = _write_release(allowed_root)
    link = allowed_root / "release-link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(prepare.ReleasePrepareError) as error:
        prepare.prepare_release(
            link,
            FULL_COMMIT,
            allowed_root,
            required_files=PREPARE_PATHS,
        )
    assert error.value.code == "release_root_symlink"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires POSIX test support")
def test_prepare_rejects_required_file_symlink_escape(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    release_root, _ = _write_release(allowed_root)
    outside = tmp_path / "outside-index.html"
    outside.write_text("<!doctype html>", encoding="utf-8")
    index = release_root / "frontend" / "dist" / "index.html"
    index.unlink()
    index.symlink_to(outside)

    with pytest.raises(prepare.ReleasePrepareError) as error:
        prepare.prepare_release(
            release_root,
            FULL_COMMIT,
            allowed_root,
            required_files=PREPARE_PATHS,
        )
    assert error.value.code == "required_path_escape"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics are unavailable")
def test_prepare_requires_parent_traversal_bit(tmp_path: Path):
    allowed_root = tmp_path / "releases"
    allowed_root.mkdir()
    release_root, _ = _write_release(allowed_root)
    os.chmod(allowed_root, 0o700)
    service_identity = prepare.ServiceIdentity(uid=10**9, gids=(10**9,))

    with pytest.raises(prepare.ReleasePrepareError) as error:
        prepare.prepare_release(
            release_root,
            FULL_COMMIT,
            allowed_root,
            required_files=PREPARE_PATHS,
            service_identity=service_identity,
        )
    assert error.value.code == "release_parent_not_traversable"
