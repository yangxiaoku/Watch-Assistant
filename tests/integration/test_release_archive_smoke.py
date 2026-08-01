import importlib
import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_smoke_script():
    path = ROOT / "scripts" / "release_startup_smoke.py"
    spec = importlib.util.spec_from_file_location("release_startup_smoke", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_smoke = _load_smoke_script()


def _write_version(path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )


def test_release_archive_smoke_uses_staged_version_with_old_current_present(
    tmp_path: Path, monkeypatch, capsys
):
    release_root = tmp_path / "watch-assistant-abcdef1"
    shutil.copytree(ROOT / "src", release_root / "src")
    shutil.copytree(ROOT / "scripts", release_root / "scripts")
    shutil.copytree(ROOT / "config", release_root / "config")
    (release_root / "frontend" / "dist").mkdir(parents=True)
    (release_root / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html><title>release smoke</title>", encoding="utf-8"
    )
    _write_version(release_root / "VERSION", "abcdef1")

    old_current = tmp_path / "current"
    _write_version(old_current / "VERSION", "0123456")
    app_module = importlib.import_module("watch_assistant.app")
    monkeypatch.setattr(app_module, "_TRUSTED_RELEASE_PATH", old_current)
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")
    monkeypatch.setattr(
        sys,
        "argv",
        ["release_startup_smoke.py", "--release-root", str(release_root)],
    )

    assert release_smoke.main() == 0
    output = capsys.readouterr().out
    assert "RELEASE_SMOKE_RESULT=ok" in output
    assert "RELEASE_SMOKE_HEALTH=ok" in output
    assert "0123456" not in output
