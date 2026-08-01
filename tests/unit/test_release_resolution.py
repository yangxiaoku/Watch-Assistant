import importlib
import os


def test_release_accepts_only_a_safe_commit_hash(
    tmp_path, monkeypatch
):
    module = importlib.import_module("watch_assistant.app")
    sha = "0123456789abcdef0123456789abcdef01234567"
    release_path = tmp_path / sha
    release_path.mkdir()
    monkeypatch.delenv("WATCH_ASSISTANT_RELEASE", raising=False)

    assert module._resolve_release(release_path) == sha

    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "ABCDEF1")
    assert module._resolve_release(tmp_path / "missing") == "abcdef1"

    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "configured-release")
    assert module._resolve_release(tmp_path / "missing") == "unknown"

    monkeypatch.delenv("WATCH_ASSISTANT_RELEASE", raising=False)
    for name in (
        "0123456789abcdef0123456789abcdef0123456",
        f"{sha}0",
        f"x{sha}",
        f"{sha}x",
    ):
        candidate = tmp_path / name
        candidate.mkdir()
        assert module._resolve_release(candidate) == "unknown"

    missing = tmp_path / "does-not-exist"
    assert module._resolve_release(missing) == "unknown"

    target = tmp_path / sha
    link = tmp_path / "current"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        assert module._resolve_release(link) == sha


def test_release_version_file_is_the_authoritative_source(tmp_path, monkeypatch):
    module = importlib.import_module("watch_assistant.app")
    release_root = tmp_path / "release"
    release_root.mkdir()
    release_root.joinpath("VERSION").write_text(
        "commit=abcdef1\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")

    assert module._resolve_release(release_root) == "abcdef1"


def test_explicit_release_root_isolated_from_host_release_environment(
    tmp_path, monkeypatch
):
    module = importlib.import_module("watch_assistant.app")
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    staging_root.joinpath("VERSION").write_text(
        "commit=abcdef1\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")

    assert module._resolve_release(staging_root) == "abcdef1"


def test_production_release_resolution_still_prefers_trusted_current_version(
    tmp_path, monkeypatch
):
    module = importlib.import_module("watch_assistant.app")
    current_root = tmp_path / "current"
    current_root.mkdir()
    current_root.joinpath("VERSION").write_text(
        "commit=abcdef1\nbuild_time=2026-08-02T00:00:00Z\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "_TRUSTED_RELEASE_PATH", current_root)
    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "0123456")

    assert module._resolve_release() == "abcdef1"
