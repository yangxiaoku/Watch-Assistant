import importlib
import os


def test_release_prefers_environment_and_falls_back_only_to_full_sha(
    tmp_path, monkeypatch
):
    module = importlib.import_module("watch_assistant.app")
    sha = "0123456789abcdef0123456789abcdef01234567"
    release_path = tmp_path / sha
    release_path.mkdir()
    monkeypatch.delenv("WATCH_ASSISTANT_RELEASE", raising=False)

    assert module._resolve_release(release_path) == sha

    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "configured-release")
    assert module._resolve_release(tmp_path / "missing") == "configured-release"

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
