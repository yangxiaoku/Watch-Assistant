"""通知渠道 CRUD 服务。

webhook_url 属敏感凭据,用 SecretCrypto 加密后落库(webhook_url_encrypted);
对外只回显脱敏前缀 webhook_url_prefix(前 48 字符 + … + 尾 4 位),
任何响应都不含完整 URL 或密文,防止凭据从 API/UI 侧泄露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import NotifyChannel

SUPPORTED_KINDS = frozenset({"feishu"})
# 只读回显的掩码规则:URL 前部保留长度。
_MASK_HEAD_CHARS = 48
_MASK_TAIL_CHARS = 4


class NotifyChannelError(ValueError):
    """通知渠道领域错误。code 为稳定机器码(英文 snake_case),供上层映射中文提示。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NotifyChannelResponse:
    """对外只读回显对象。绝不包含 webhook_url_encrypted 或完整 webhook_url。"""

    id: str
    name: str
    kind: str
    webhook_url_prefix: str
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


class NotifyChannelService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto

    async def create(
        self,
        name: str,
        webhook_url: str,
        *,
        kind: str = "feishu",
    ) -> NotifyChannelResponse:
        if kind not in SUPPORTED_KINDS:
            raise NotifyChannelError("unsupported_notify_kind")
        clean_name = name.strip()
        url = webhook_url.strip()
        if not url:
            raise NotifyChannelError("unsupported_notify_kind")
        now = datetime.now(UTC)
        item = NotifyChannel(
            id="notify_" + uuid4().hex,
            name=clean_name,
            kind=kind,
            webhook_url_encrypted=self._crypto.encrypt(url),
            webhook_url_prefix=_mask_webhook_url(url),
            enabled=True,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(item)
            await session.commit()
        return _response_from(item)

    async def list(self) -> list[NotifyChannelResponse]:
        async with self._session_factory() as session:
            items = list(
                await session.scalars(
                    select(NotifyChannel).order_by(NotifyChannel.created_at.desc())
                )
            )
        return [_response_from(item) for item in items]

    async def set_enabled(
        self, channel_id: str, enabled: bool
    ) -> NotifyChannelResponse:
        async with self._session_factory() as session:
            item = await session.get(NotifyChannel, channel_id)
            if item is None:
                raise NotifyChannelError("notify_channel_not_found")
            item.enabled = enabled
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
        return _response_from(item)

    async def delete(self, channel_id: str) -> None:
        """删除渠道。找不到时静默成功(幂等删除,不抛错)。"""
        async with self._session_factory() as session:
            item = await session.get(NotifyChannel, channel_id)
            if item is None:
                return
            await session.delete(item)
            await session.commit()


def _mask_webhook_url(url: str) -> str:
    """计算脱敏前缀:前 _MASK_HEAD_CHARS 字符 + … + 尾 _MASK_TAIL_CHARS 字符。"""
    return url[:_MASK_HEAD_CHARS] + "…" + url[-_MASK_TAIL_CHARS:]


def _response_from(item: NotifyChannel) -> NotifyChannelResponse:
    return NotifyChannelResponse(
        id=item.id,
        name=item.name,
        kind=item.kind,
        webhook_url_prefix=item.webhook_url_prefix,
        enabled=item.enabled,
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


__all__ = [
    "NotifyChannelError",
    "NotifyChannelResponse",
    "NotifyChannelService",
]
