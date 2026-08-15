"""通知分发器:把业务事件推送到外部通知渠道(如飞书)。

与 NotifyChannelService 不同,notify_dispatcher 是主动出网的一段:
- 自己 select 出全部 enabled==True 的 NotifyChannel 行;
- 逐行用 SecretCrypto.decrypt 解出明文 webhook_url(绝不回显);
- 为每个渠道构造对应适配器(默认 FeishuChannel)并 send。
单渠道失败只记 notify.delivery_failed 事件,不抛错、不中断其它渠道
(fail-open)。notify_resource_found / notify_task_available 是两条高层
入口,分别反查订阅/任务信息后拼出中文标题、正文与跳转链接;任何异常
都吞掉并记日志,绝不携带到上层主流程。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import (
    MediaType,
    NotifyChannel,
    Subscription,
    Task,
    Workflow,
)
from watch_assistant.schemas import LoggingLevel
from watch_assistant.services.notify_channels.cli import (
    DEFAULT_CLI_TIMEOUT_SECONDS,
    CliNotifyChannel,
    decode_channel_config,
)
from watch_assistant.services.notify_channels.feishu import FeishuChannel
from watch_assistant.services.observability import EventLogger, emit_event

logger = logging.getLogger(__name__)

# 缺省渠道工厂:真实产出 FeishuChannel。测试可注入替身以规避真实出网。
DefaultChannel = FeishuChannel


class ChannelFactory(Protocol):
    def __call__(self, webhook_url: str) -> Any: ...


class NotifyDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: SecretCrypto,
        event_logger: EventLogger | None = None,
        base_url: str = "http://192.168.6.236:8115",
        *,
        channel_factory: ChannelFactory | None = None,
        cli_channel_factory: Any | None = None,
        feishu_cli_command: str = "feishu-cli",
        clawbot_command: str = "openclaw",
        cli_timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._event_logger = event_logger
        self._base_url = base_url.rstrip("/")
        self._channel_factory = channel_factory or DefaultChannel
        self._cli_channel_factory = cli_channel_factory or self._build_cli_channel
        self._feishu_cli_command = feishu_cli_command
        self._clawbot_command = clawbot_command
        self._cli_timeout_seconds = cli_timeout_seconds

    async def dispatch_notify(
        self,
        *,
        title: str,
        text: str,
        link_path: str | None = None,
    ) -> int:
        """向所有启用渠道投递一条通知,返回成功投递的渠道数。"""
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(NotifyChannel).where(NotifyChannel.enabled.is_(True))
                    )
                ).all()
            )
        if not rows:
            return 0
        link_url = f"{self._base_url}{link_path}" if link_path else None
        delivered = 0
        for row in rows:
            try:
                webhook_url = self._crypto.decrypt(row.webhook_url_encrypted)
            except Exception as exc:  # noqa: BLE001 - a bad row must not block others
                await self._log_failure(row.id, exc)
                continue
            channel = self._build_channel(row, webhook_url)
            if channel is None:
                await self._log_failure(
                    row.id, RuntimeError("invalid_notify_cli_config")
                )
                continue
            try:
                try:
                    ok = await channel.send(title=title, text=text, link_url=link_url)
                except Exception as exc:  # noqa: BLE001 - fail-open per channel
                    await self._log_failure(row.id, exc)
                    continue
                if ok:
                    delivered += 1
                else:
                    await self._log_failure(
                        row.id, RuntimeError("channel_send_failed")
                    )
            finally:
                await self._close_channel(channel)
        if delivered:
            await emit_event(
                self._event_logger,
                "notify.delivered",
                level=LoggingLevel.INFO,
                fields={"count": delivered},
            )
        return delivered

    async def notify_resource_found(self, subscription_id: str) -> None:
        """订阅发现新资源:推送「追更命中」通知(fail-open)。"""
        try:
            async with self._session_factory() as session:
                sub = await session.get(Subscription, subscription_id)
                if sub is None:
                    return
                media_type = sub.media_type
                tmdb_id = sub.tmdb_id
            title = "追更命中"
            text = f"订阅 {media_type.value}:{tmdb_id} 发现新资源"
            link_path = (
                f"/movie/{tmdb_id}"
                if media_type == MediaType.MOVIE
                else f"/tv/{tmdb_id}"
            )
            await self.dispatch_notify(
                title=title, text=text, link_path=link_path
            )
        except Exception as exc:  # noqa: BLE001 - must never affect subscription flow
            logger.warning("notify_resource_found failed: %s", exc)
            try:
                await self._log_failure("subscription:" + subscription_id, exc)
            except Exception:  # noqa: BLE001, S110 - logging must not throw
                pass

    async def notify_task_available(self, task_id: str) -> None:
        """任务已入库:推送「资源已入库」通知(fail-open)。"""
        try:
            async with self._session_factory() as session:
                task = await session.get(Task, task_id)
                if task is None:
                    return
                workflow_id = task.workflow_id
            title = "资源已入库"
            text = f"任务 {task_id} 已完成入库"
            link_path: str | None = None
            if workflow_id:
                async with self._session_factory() as session:
                    workflow = await session.get(Workflow, workflow_id)
                if workflow is not None and workflow.tmdb_id is not None:
                    link_path = (
                        f"/movie/{workflow.tmdb_id}"
                        if workflow.media_type == MediaType.MOVIE
                        else f"/tv/{workflow.tmdb_id}"
                    )
            await self.dispatch_notify(
                title=title, text=text, link_path=link_path
            )
        except Exception as exc:  # noqa: BLE001 - never affects worker availability
            logger.warning("notify_task_available failed: %s", exc)
            try:
                await self._log_failure("task:" + task_id, exc)
            except Exception:  # noqa: BLE001, S110
                pass

    def _build_channel(self, row: NotifyChannel, secret: str) -> Any:
        """Build a webhook or CLI channel from a decrypted channel secret."""
        if row.kind == "feishu":
            return self._channel_factory(secret)
        config = decode_channel_config(row.kind, secret)
        target = config.get("target")
        if not target:
            return None
        if row.kind == "feishu_cli":
            return self._cli_channel_factory(
                row.kind,
                target=target,
                cli_channel=None,
                command=self._feishu_cli_command,
                timeout_seconds=self._cli_timeout_seconds,
            )
        if row.kind == "clawbot":
            cli_channel = config.get("channel")
            if not cli_channel:
                return None
            return self._cli_channel_factory(
                row.kind,
                target=target,
                cli_channel=cli_channel,
                command=self._clawbot_command,
                timeout_seconds=self._cli_timeout_seconds,
            )
        return None

    @staticmethod
    def _build_cli_channel(
        kind: str,
        *,
        target: str,
        cli_channel: str | None,
        command: str,
        timeout_seconds: float,
    ) -> Any:
        return CliNotifyChannel(
            kind=kind,
            target=target,
            command=command,
            channel=cli_channel or "openclaw-weixin",
            timeout_seconds=timeout_seconds,
        )

    async def aclose(self) -> None:
        # 每次 send 后已立即关闭渠道;保留 aclose 以兼容调用方与测试注入。
        return

    async def _close_channel(self, channel: Any) -> None:
        """Best-effort close after every send to avoid fd/client accumulation."""
        closer = getattr(channel, "aclose", None)
        if not callable(closer):
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001, S110 - close must never mask delivery result
            pass

    async def _log_failure(self, channel_id: str, exc: Exception) -> None:
        await emit_event(
            self._event_logger,
            "notify.delivery_failed",
            level=LoggingLevel.WARNING,
            fields={"error_code": getattr(exc, "code", "channel_send_failed")},
            resource_type="notify_channel",
            resource_id=channel_id,
        )


__all__ = ["NotifyDispatcher"]
