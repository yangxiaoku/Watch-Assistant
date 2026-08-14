"""飞书通知渠道适配器单元测试。"""
from __future__ import annotations

import respx
from httpx import Response

from watch_assistant.services.notify_channels.feishu import (
    FeishuChannel,
    build_feishu_payload,
)

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token-123"


class TestBuildFeishuPayload:
    def test_payload_is_interactive_with_title(self) -> None:
        payload = build_feishu_payload(title="任务完成", text="下载已完成")
        assert payload["msg_type"] == "interactive"
        header = payload["card"]["header"]["title"]
        assert header["tag"] == "plain_text"
        assert "任务完成" in header["content"]
        # 无链接时不渲染按钮
        elements = payload["card"]["elements"]
        assert len(elements) == 1
        assert elements[0]["tag"] == "div"

    def test_payload_with_link_url_has_button(self) -> None:
        payload = build_feishu_payload(
            title="任务完成",
            text="点击前往查看详情",
            link_url="https://example.com/task/1",
        )
        elements = payload["card"]["elements"]
        assert len(elements) == 2
        actions = elements[1]
        assert actions["tag"] == "action"
        button = actions["actions"][0]
        assert button["tag"] == "button"
        assert button["type"] == "primary"
        assert button["text"]["text"] == "前往查看"
        assert button["url"] == "https://example.com/task/1"


@respx.mock
class TestFeishuChannelSend:
    @staticmethod
    async def test_send_returns_true_on_success() -> None:
        route = respx.post(FEISHU_WEBHOOK).mock(return_value=Response(200, json={"ok": True}))
        channel = FeishuChannel(FEISHU_WEBHOOK)
        try:
            result = await channel.send(title="标题", text="正文")
        finally:
            await channel.aclose()
        assert result is True
        assert route.called

    @staticmethod
    async def test_send_returns_false_on_http_500() -> None:
        respx.post(FEISHU_WEBHOOK).mock(return_value=Response(500, text="boom"))
        channel = FeishuChannel(FEISHU_WEBHOOK)
        try:
            result = await channel.send(title="标题", text="正文")
        finally:
            await channel.aclose()
        assert result is False

    @staticmethod
    async def test_send_returns_false_on_network_error() -> None:
        import httpx

        respx.post(FEISHU_WEBHOOK).mock(side_effect=httpx.ConnectError("network down"))
        channel = FeishuChannel(FEISHU_WEBHOOK)
        try:
            result = await channel.send(title="标题", text="正文")
        finally:
            await channel.aclose()
        assert result is False
