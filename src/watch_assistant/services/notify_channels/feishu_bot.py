"""Feishu Open Platform bot channel (tenant token + im/v1/messages).

This is the bot-API equivalent of the old custom-group webhook: no public
webhook URL is stored, and only ``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET`` +
the destination id are required.
"""

from __future__ import annotations

import json
import time

import httpx

from watch_assistant.services.notify_channels.cli import (
    build_notification_body,
    feishu_receive_id_type,
)

DEFAULT_FEISHU_API_BASE_URL = "https://open.feishu.cn"
DEFAULT_TIMEOUT_SECONDS = 15.0


class FeishuBotChannel:
    def __init__(
        self,
        *,
        target: str,
        app_id: str,
        app_secret: str,
        api_base_url: str = DEFAULT_FEISHU_API_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if feishu_receive_id_type(target) is None:
            raise ValueError("unsupported_feishu_target")
        self._target = target
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=self._timeout_seconds,
            trust_env=False,
        )
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def send(
        self,
        *,
        title: str,
        text: str,
        link_url: str | None = None,
    ) -> bool:
        if not self._app_id or not self._app_secret:
            return False
        token = await self._access_token()
        if token is None:
            return False
        body = build_notification_body(title=title, text=text, link_url=link_url)
        receive_id_type = feishu_receive_id_type(self._target)
        if receive_id_type is None:  # pragma: no cover - validated in __init__
            return False
        try:
            response = await self._client.post(
                "/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                json={
                    "receive_id": self._target,
                    "msg_type": "text",
                    "content": json.dumps(
                        {"text": body}, ensure_ascii=False
                    ),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("code") == 0

    async def _access_token(self) -> str | None:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        try:
            response = await self._client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        token = payload.get("tenant_access_token") if isinstance(payload, dict) else None
        expire = payload.get("expire") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            return None
        try:
            self._token_expires_at = now + max(60.0, float(expire) - 60)
        except (TypeError, ValueError):
            self._token_expires_at = now + 3600.0
        self._token = token
        return token

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = [
    "DEFAULT_FEISHU_API_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FeishuBotChannel",
]
