import importlib


def test_release_prefers_environment_and_falls_back_only_to_full_sha(
    tmp_path, monkeypatch
):
    module = importlib.import_module("watch_assistant.app")
    release_path = tmp_path / "RELEASE"
    release_path.write_text(
        "release 0123456789abcdef0123456789abcdef01234567\n", encoding="ascii"
    )
    monkeypatch.setattr(module, "_TRUSTED_RELEASE_PATHS", (release_path,))
    monkeypatch.delenv("WATCH_ASSISTANT_RELEASE", raising=False)

    assert module._resolve_release() == "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setenv("WATCH_ASSISTANT_RELEASE", "configured-release")
    assert module._resolve_release() == "configured-release"

    monkeypatch.delenv("WATCH_ASSISTANT_RELEASE", raising=False)
    release_path.write_text("not-a-complete-sha", encoding="ascii")
    assert module._resolve_release() == "unknown"
