"""通知渠道适配器（飞书等）。"""
from __future__ import annotations

from watch_assistant.services.notify_channels.feishu import (
    FeishuChannel,
    build_feishu_payload,
)

__all__ = ["FeishuChannel", "build_feishu_payload"]
