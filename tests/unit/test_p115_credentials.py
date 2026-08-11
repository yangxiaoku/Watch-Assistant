import os
import stat

import pytest

from watch_assistant.services.p115_credentials import (
    MAX_COOKIE_BYTES,
    CompositeCookieProvider,
    CookieProvider,
)

COOKIE = "UID=123_A1_456; CID=cid; KID=kid; SEID=seid"


def _write_cookie(path, value=COOKIE):
    path.write_text(value, encoding="ascii", newline="")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_cookie_provider_validates_and_reloads_changed_file(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    provider = CookieProvider(path)

    assert provider.load() == COOKIE
    assert COOKIE not in repr(provider)
    _write_cookie(path, "UID=999_A1_456; CID=new; KID=new; SEID=new")
    assert provider.read() == "UID=999_A1_456; CID=new; KID=new; SEID=new"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_cookie_provider_accepts_terminal_line_ending(tmp_path, line_ending):
    path = tmp_path / "p115-cookie"
    _write_cookie(path, COOKIE + line_ending)

    assert CookieProvider(path).load() == COOKIE


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
    path = tmp_path / "p115-cookie"
    _write_cookie(path, content)
    assert CookieProvider(path).load() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_cookie_provider_rejects_symlink_and_group_permissions(tmp_path):
    source = tmp_path / "p115-cookie"
    _write_cookie(source)
    link = tmp_path / "cookie-link"
    link.symlink_to(source)
    assert CookieProvider(link).load() is None

    source.chmod(0o640)
    assert CookieProvider(source).load() is None


def test_composite_provider_prefers_managed_cookie(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    provider = CompositeCookieProvider(CookieProvider(path))

    assert provider.source == "file"
    assert provider.load() == COOKIE
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")

    assert provider.source == "managed"
    assert provider.load() == "UID=managed; CID=cid; KID=kid; SEID=seid"


def test_composite_provider_returns_to_file_after_managed_reset(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    provider = CompositeCookieProvider(CookieProvider(path))
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")

    provider.set_managed(None)

    assert provider.source == "file"
    assert provider.load() == COOKIE



class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_composite_provider_degrades_to_fallback_after_repeated_failures(
    tmp_path,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    clock = _FakeClock()
    provider = CompositeCookieProvider(
        CookieProvider(path), failure_threshold=3, degrade_seconds=60.0, clock=clock
    )
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")
    assert provider.source == "managed"

    provider.notify_failure()
    provider.notify_failure()
    assert provider.source == "managed"
    assert provider.load() == "UID=managed; CID=cid; KID=kid; SEID=seid"

    # 达到阈值 → 降级窗口内 load 返回 fallback 文件 cookie。
    provider.notify_failure()
    assert provider.source == "file(degraded)"
    assert provider.load() == COOKIE


def test_composite_provider_recovers_managed_after_degrade_window(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    clock = _FakeClock()
    provider = CompositeCookieProvider(
        CookieProvider(path), failure_threshold=1, degrade_seconds=60.0, clock=clock
    )
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")
    provider.notify_failure()
    assert provider.source == "file(degraded)"
    assert provider.load() == COOKIE

    # 降级窗口结束 → 自动重置并重新优先 managed。
    clock.now = 61.0
    assert provider.source == "managed"
    assert provider.load() == "UID=managed; CID=cid; KID=kid; SEID=seid"


def test_composite_provider_failures_accumulate_across_loads(tmp_path):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    clock = _FakeClock()
    provider = CompositeCookieProvider(
        CookieProvider(path), failure_threshold=3, degrade_seconds=60.0, clock=clock
    )
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")
    # 每次请求:load 拿到 managed → 远端认证失败 → notify_failure。
    for _ in range(2):
        assert provider.load() == "UID=managed; CID=cid; KID=kid; SEID=seid"
        provider.notify_failure()
    assert provider.source == "managed"

    provider.load()
    provider.notify_failure()
    assert provider.source == "file(degraded)"
    assert provider.load() == COOKIE


def test_composite_provider_set_managed_and_retry_managed_reset_degradation(
    tmp_path,
):
    path = tmp_path / "p115-cookie"
    _write_cookie(path)
    clock = _FakeClock()
    provider = CompositeCookieProvider(
        CookieProvider(path), failure_threshold=1, degrade_seconds=60.0, clock=clock
    )
    provider.set_managed("UID=managed; CID=cid; KID=kid; SEID=seid")
    provider.notify_failure()
    assert provider.load() == COOKIE

    provider.retry_managed()
    assert provider.source == "managed"
    assert provider.load() == "UID=managed; CID=cid; KID=kid; SEID=seid"

    provider.notify_failure()
    provider.set_managed("UID=fresh; CID=cid; KID=kid; SEID=seid")
    assert provider.source == "managed"
    assert provider.load() == "UID=fresh; CID=cid; KID=kid; SEID=seid"
