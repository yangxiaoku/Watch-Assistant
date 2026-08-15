"""飞书开放平台 Bot API 渠道测试。"""

import httpx
import pytest
import respx

from watch_assistant.services.notify_channels.feishu_bot import FeishuBotChannel


@pytest.mark.asyncio
@respx.mock
async def test_feishu_bot_obtains_tenant_token_and_sends_text():
    token_route = respx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "tenant_access_token": "t-123", "expire": 7200},
        )
    )
    send_route = respx.post(
        "https://open.feishu.cn/open-apis/im/v1/messages"
    ).mock(return_value=httpx.Response(200, json={"code": 0, "msg": "success"}))

    channel = FeishuBotChannel(
        target="ou_user",
        app_id="cli_app",
        app_secret="secret",
    )
    try:
        ok = await channel.send(
            title="标题", text="正文", link_url="https://watch.local/tv/1"
        )
    finally:
        await channel.aclose()

    assert ok is True
    assert token_route.called
    assert send_route.called
    request = send_route.calls.last.request
    assert request.url.params["receive_id_type"] == "open_id"
    payload = httpx.Response(200, content=request.content).json()
    assert payload["receive_id"] == "ou_user"
    assert "https://watch.local/tv/1" in payload["content"]


@pytest.mark.asyncio
@respx.mock
async def test_feishu_bot_reports_failure_when_api_rejects():
    respx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ).mock(return_value=httpx.Response(200, json={"code": 0, "tenant_access_token": "t-123"}))
    respx.post("https://open.feishu.cn/open-apis/im/v1/messages").mock(
        return_value=httpx.Response(200, json={"code": 99991672, "msg": "denied"})
    )

    channel = FeishuBotChannel(
        target="oc_chat",
        app_id="cli_app",
        app_secret="secret",
    )
    try:
        ok = await channel.send(title="标题", text="正文")
    finally:
        await channel.aclose()
    assert ok is False


def test_feishu_bot_rejects_unsupported_target():
    with pytest.raises(ValueError):
        FeishuBotChannel(
            target="bad-target", app_id="cli_app", app_secret="secret"
        )
