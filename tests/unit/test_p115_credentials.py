import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sync_tgto_cookie import (
    MAX_DOTENV_BYTES,
    CookieSyncError,
    sync_cookie,
)
from watch_assistant.services.p115_credentials import (
    MAX_COOKIE_BYTES,
    CookieProvider,
)

COOKIE = "UID=123_A1_456; CID=cid; KID=kid; SEID=seid"


def _write_cookie(path, value=COOKIE):
    path.write_text(value, encoding="ascii", newline="")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _write_env(path, value=COOKIE):
    path.write_text(
        "UNRELATED_TOKEN=do-not-copy\n"
        "ENV_115_COOKIES=" + value + "\nOTHER_SECRET=also-do-not-copy\n",
        encoding="utf-8",
        newline="",
    )
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_cookie_provider_validates_and_reloads_changed_file(tmp_path):
    path = tmp_path / "tgto-cookie.txt"
    _write_cookie(path)
    provider = CookieProvider(path)

    assert provider.load() == COOKIE
    assert "123_A1_456" not in repr(provider)

    _write_cookie(path, "UID=999_A1_456; CID=new; KID=new; SEID=new")
    assert provider.read() == "UID=999_A1_456; CID=new; KID=new; SEID=new"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "UID=123_A1_456; CID=cid; KID=kid",
        "UID=123_A1_456; CID=cid; KID=kid; SEID=seid\nextra",
        "UID=123_A1_456; CID=cid; KID=kid; SEID=se\x00id",
        "UID=123_A1_456; CID=cid; KID=kid; SEID=" + ("x" * MAX_COOKIE_BYTES),
    ],
)
def test_cookie_provider_rejects_invalid_content(tmp_path, content):
    path = tmp_path / "cookie"
    _write_cookie(path, content)
    assert CookieProvider(path).load() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_cookie_provider_rejects_symlink_and_group_permissions(tmp_path):
    source = tmp_path / "cookie"
    _write_cookie(source)
    link = tmp_path / "link"
    link.symlink_to(source)
    assert CookieProvider(link).load() is None

    source.chmod(0o640)
    assert CookieProvider(source).load() is None


def test_sync_cookie_is_one_way_and_writes_secure_destination(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    _write_env(source)

    sync_cookie(source, destination)

    assert destination.read_text(encoding="ascii") == COOKIE
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    _write_env(source, "UID=999_A1_456; CID=new; KID=new; SEID=new")
    sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == (
        "UID=999_A1_456; CID=new; KID=new; SEID=new"
    )


def test_sync_cookie_does_not_modify_destination_on_invalid_source(tmp_path):
    source = tmp_path / "bad-user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text("OTHER_SECRET=not a cookie\n", encoding="ascii")
    destination.write_text("keep", encoding="ascii")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "keep"


def test_sync_cookie_ignores_unrelated_sensitive_assignments(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    _write_env(source)

    sync_cookie(source, destination)

    assert destination.read_text(encoding="ascii") == COOKIE
    assert "UNRELATED" not in destination.read_text(encoding="ascii")
    assert "OTHER_SECRET" not in destination.read_text(encoding="ascii")


def test_sync_cookie_accepts_quoted_dotenv_assignment(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text(
        'OTHER_SECRET="hidden"\nENV_115_COOKIES="' + COOKIE + '" # comment\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        source.chmod(stat.S_IRUSR | stat.S_IWUSR)

    sync_cookie(source, destination)

    assert destination.read_text(encoding="ascii") == COOKIE


@pytest.mark.parametrize(
    "content",
    [
        "OTHER_SECRET=only-this-variable\n",
        "ENV_115_COOKIES=" + COOKIE + "\nENV_115_COOKIES=" + COOKIE,
        "ENV_115_COOKIES=" + COOKIE + "\x00\n",
        'ENV_115_COOKIES="' + COOKIE + "\nnext\n",
    ],
)
def test_sync_cookie_rejects_missing_or_duplicate_target(tmp_path, content):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text(content, encoding="utf-8", newline="")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)


def test_sync_cookie_accepts_large_dotenv_with_cookie_at_end(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text(
        "OTHER_SECRET=" + ("x" * (MAX_COOKIE_BYTES + 100)) + "\n"
        "ENV_115_COOKIES=" + COOKIE + "\n",
        encoding="utf-8",
    )

    sync_cookie(source, destination)

    assert destination.read_text(encoding="ascii") == COOKIE
    assert "OTHER_SECRET" not in destination.read_text(encoding="ascii")


def test_sync_cookie_rejects_cookie_definitions_far_apart(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text(
        "ENV_115_COOKIES="
        + COOKIE
        + "\n"
        + ("OTHER=" + ("x" * 100) + "\n") * 200
        + "ENV_115_COOKIES="
        + COOKIE
        + "\n",
        encoding="utf-8",
    )
    destination.write_text("keep", encoding="ascii")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "keep"


def test_sync_cookie_rejects_dotenv_over_limit_without_replacing(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_text(
        "OTHER_SECRET=" + ("x" * MAX_DOTENV_BYTES) + "\n"
        "ENV_115_COOKIES=" + COOKIE + "\n",
        encoding="utf-8",
    )
    destination.write_text("keep", encoding="ascii")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "keep"


def test_sync_cookie_rejects_oversized_cookie_without_replacing(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    oversized = "UID=uid; CID=cid; KID=kid; SEID=" + ("x" * MAX_COOKIE_BYTES)
    source.write_text("ENV_115_COOKIES=" + oversized + "\n", encoding="ascii")
    destination.write_text("keep", encoding="ascii")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "keep"


def test_sync_cookie_does_not_replace_unchanged_content(tmp_path, monkeypatch):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    _write_env(source)
    sync_cookie(source, destination)
    original_replace = __import__("os").replace

    def fail_replace(*args):
        raise AssertionError("unchanged content should not replace")

    monkeypatch.setattr("scripts.sync_tgto_cookie.os.replace", fail_replace)
    sync_cookie(source, destination)
    monkeypatch.setattr("scripts.sync_tgto_cookie.os.replace", original_replace)


@pytest.mark.skipif(os.name == "nt", reason="symlink privilege contract")
def test_sync_cookie_rejects_destination_symlink(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    target = tmp_path / "actual"
    _write_env(source)
    target.write_text("keep", encoding="ascii")
    destination.symlink_to(target)

    with pytest.raises(CookieSyncError, match="symlink"):
        sync_cookie(source, destination)


def test_sync_cookie_rejects_invalid_utf8_without_secret_in_error(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    source.write_bytes(b"ENV_115_COOKIES=\xff\n")

    with pytest.raises(CookieSyncError) as error:
        sync_cookie(source, destination)
    assert "ff" not in repr(error.value).casefold()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership contract")
def test_sync_cookie_applies_explicit_owner_and_group(tmp_path):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    _write_env(source)

    sync_cookie(source, destination, owner=os.getuid(), group=os.getgid())

    assert destination.stat().st_uid == os.getuid()
    assert destination.stat().st_gid == os.getgid()


def test_sync_cookie_replace_failure_keeps_previous_file(tmp_path, monkeypatch):
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    _write_env(source)
    destination.write_text("old", encoding="ascii")

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr("scripts.sync_tgto_cookie.os.replace", fail_replace)
    with pytest.raises(CookieSyncError, match="failed"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "old"
    assert not list(tmp_path.glob(".p115-cookie-*"))


def test_systemd_cookie_sync_examples_have_import_path_and_safe_arguments():
    root = Path(__file__).parents[2]
    service = (root / "deploy/p115-cookie-sync.service.example").read_text()
    timer = (root / "deploy/p115-cookie-sync.timer.example").read_text()

    assert "WorkingDirectory=/opt/watch-assistant/current" in service
    assert "Environment=PYTHONPATH=/opt/watch-assistant/current/src" in service
    assert "Type=oneshot" in service
    assert "User=root" in service
    assert "Group=root" in service
    assert "/opt/watch-assistant/venv/bin/python" in service
    assert "--source /root/TgtoDrive-deploy/db/user.env" in service
    assert "--destination /etc/watch-assistant/p115-cookie" in service
    assert "--owner watch-assistant" in service
    assert "--group watch-assistant" in service
    assert "UMask=0077" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "ReadOnlyPaths=/root/TgtoDrive-deploy/db/user.env" in service
    assert "ReadWritePaths=/etc/watch-assistant" in service
    assert "NoNewPrivileges=true" in service
    assert "Unit=p115-cookie-sync.service" in timer


def test_cookie_sync_runs_from_external_directory_with_explicit_pythonpath(tmp_path):
    root = Path(__file__).parents[2]
    source = tmp_path / "user.env"
    destination = tmp_path / "p115-cookie"
    secret = "UID=sync_A1_456; CID=cid; KID=kid; SEID=seid"
    source.write_text(
        "OTHER_SECRET=must-not-print\nENV_115_COOKIES=" + secret + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/sync_tgto_cookie.py"),
            "--source",
            str(source),
            "--destination",
            str(destination),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert secret not in result.stdout + result.stderr
    assert "must-not-print" not in result.stdout + result.stderr
    assert destination.read_text(encoding="ascii") == secret
