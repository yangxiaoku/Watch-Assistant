import os
import stat

import pytest

from scripts.sync_tgto_cookie import CookieSyncError, sync_cookie
from watch_assistant.services.p115_credentials import (
    MAX_COOKIE_BYTES,
    CookieProvider,
)

COOKIE = "UID=123_A1_456; CID=cid; KID=kid; SEID=seid"


def _write_cookie(path, value=COOKIE):
    path.write_text(value, encoding="ascii", newline="")
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
    source = tmp_path / "tgto-cookie"
    destination = tmp_path / "p115-cookie"
    _write_cookie(source)

    sync_cookie(source, destination)

    assert destination.read_text(encoding="ascii") == COOKIE
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    _write_cookie(source, "UID=999_A1_456; CID=new; KID=new; SEID=new")
    assert destination.read_text(encoding="ascii") == COOKIE


def test_sync_cookie_does_not_modify_destination_on_invalid_source(tmp_path):
    source = tmp_path / "bad-cookie"
    destination = tmp_path / "p115-cookie"
    source.write_text("not a cookie", encoding="ascii")
    destination.write_text("keep", encoding="ascii")

    with pytest.raises(CookieSyncError, match="unavailable"):
        sync_cookie(source, destination)
    assert destination.read_text(encoding="ascii") == "keep"
