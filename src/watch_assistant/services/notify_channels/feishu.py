"""飞书(Feishu/Lark)群机器人通知渠道适配器。

使用飞书「自定义机器人」的 Webhook 发送交互卡片(interactive)消息。
所有用户可见文案使用中文，机器码恒为英文。
"""
from __future__ import annotations

from typing import Any

import httpx

_BUTTON_TEXT = "前往查看"


def build_feishu_payload(
    title: str,
    text: str,
    link_url: str | None = None,
) -> dict[str, Any]:
    """构建飞书交互卡片消息体。

    返回的 payload 供飞书自定义机器人 webhook 使用，msg_type 固定为
    "interactive"。卡片 header 显示 title；正文以 lark_md 呈现 text；
    当提供 link_url 时，追加一个 primary 类型的「前往查看」跳转按钮。
    """
    card: dict[str, Any] = {
        "header": {
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
        ],
    }
    if link_url:
        card["elements"].append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "text": _BUTTON_TEXT},
                        "url": link_url,
                    }
                ],
            }
        )
    return {"msg_type": "interactive", "card": card}


class FeishuChannel:
    """飞书机器人通知渠道。

    通过 httpx.AsyncClient(trust_env=False) POST 到 webhook。send 内部
    调用 raise_for_status，任何 httpx.HTTPError 均返回 False。
    """

    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._client = httpx.AsyncClient(trust_env=False, timeout=self._timeout)

    async def send(
        self,
        *,
        title: str,
        text: str,
        link_url: str | None = None,
    ) -> bool:
        payload = build_feishu_payload(title=title, text=text, link_url=link_url)
        try:
            response = await self._client.post(self._webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
