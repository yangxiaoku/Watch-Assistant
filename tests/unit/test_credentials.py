"""CredentialService 运行时回调 fail-closed 与可观测性。"""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from watch_assistant.services.credentials import CredentialService
from watch_assistant.services.p115_credentials import CookieProvider


@pytest.mark.asyncio
async def test_p115_runtime_callback_failure_logs_and_fails_closed(tmp_path, crypto, caplog):
    """p115 运行时回调(如启动 dirty worker)抛异常时必须 fail-closed 且有日志。

    历史 bug:此异常被 try/except 静默吞掉,导致 STRM 自动增量队列积压数天而
    worker 从未启动、日志里却没有任何痕迹。现在记录堆栈供诊断。
    """
    session_factory = async_sessionmaker()
    fallback = CookieProvider(tmp_path / "p115-cookie")
    runtime_state = SimpleNamespace(
        p115_ready=True, push_capabilities={"magnet": True, "share": False}
    )

    async def failing_callback(ready: bool) -> None:
        del ready
        raise RuntimeError("apply_dirty_runtime exploded")

    service = CredentialService(
        session_factory,
        crypto,
        environment_tmdb_key="",
        fallback_cookie_provider=fallback,
        runtime_state=runtime_state,
        p115_runtime_callback=failing_callback,
    )

    with caplog.at_level("ERROR", logger="watch_assistant.services.credentials"):
        await service._set_p115_runtime(True)

    # fail-closed:回调失败 → p115 标记不可用,推送能力关闭。
    assert runtime_state.p115_ready is False
    assert runtime_state.push_capabilities == {"magnet": False, "share": False}
    # 可观测:异常必须有日志,不再静默。
    assert any(
        "p115 runtime callback failed" in record.message for record in caplog.records
    )
