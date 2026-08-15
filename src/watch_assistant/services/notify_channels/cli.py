"""CLI-backed notification channels (Feishu CLI and OpenClaw/ClawBot).

Unlike the webhook Feishu channel, these channels do not keep an HTTP client:
each send starts one short-lived subprocess.  Command strings come from
deployment settings, target/channel come from the encrypted per-channel
config, and arguments are always passed via ``exec`` (never a shell).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable

DEFAULT_CLI_TIMEOUT_SECONDS = 30.0
DEFAULT_CLAWBOT_CHANNEL = "openclaw-weixin"

_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_.@:+~-]{1,128}$")
_CHANNEL_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

CliRunner = Callable[[tuple[str, ...], float], Awaitable[bool]]


def decode_channel_config(kind: str, value: str) -> dict[str, str]:
    """Decrypt output is either a legacy webhook URL or a CLI config JSON."""
    if kind == "feishu":
        return {"url": value}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: item
        for key, item in payload.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def validate_cli_target(value: object) -> str:
    if not isinstance(value, str) or _TARGET_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_notify_cli_target")
    return value


def validate_cli_channel(value: object) -> str:
    if not isinstance(value, str) or _CHANNEL_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_notify_cli_channel")
    return value


def feishu_receive_id_type(target: str) -> str | None:
    """Map common Feishu IDs to ``--receive-id-type`` values."""
    if "@" in target:
        return "email"
    if target.startswith("oc_"):
        return "chat_id"
    if target.startswith("ou_"):
        return "open_id"
    if target.startswith("on_"):
        return "union_id"
    if target.startswith("u_"):
        return "user_id"
    return None


def build_notification_body(*, title: str, text: str, link_url: str | None) -> str:
    parts = [title, text]
    if link_url:
        parts.append(link_url)
    return "\n".join(parts)


def feishu_cli_argv(
    command: str,
    target: str,
    body: str,
) -> tuple[str, ...]:
    receive_id_type = feishu_receive_id_type(target)
    if receive_id_type is None:
        raise ValueError("unsupported_feishu_target")
    return (
        command,
        "msg",
        "send",
        "--receive-id-type",
        receive_id_type,
        "--receive-id",
        target,
        "--text",
        body,
    )


def normalize_clawbot_target(channel: str, target: str) -> str:
    """Normalize OpenClaw/ClawBot targets per channel docs.

    Feishu accepts ``chat:<chatId>`` / ``user:<openId>`` (bare ``oc_``/
    ``ou_`` are also understood by the runtime, but the CLI dry-run path
    asks for an explicit prefix). WeChat/Weixin peers are direct
    ``<userId>@im.wechat`` ids and are passed through unchanged.
    """
    if channel != "feishu":
        return target
    lowered = target.casefold()
    if lowered.startswith(
        ("chat:", "group:", "channel:", "user:", "dm:", "open_id:", "feishu:", "lark:")
    ):
        return target
    if lowered.startswith("oc_"):
        return f"chat:{target}"
    if lowered.startswith("ou_"):
        return f"user:{target}"
    return target


def clawbot_argv(
    command: str,
    channel: str,
    target: str,
    body: str,
    *,
    account: str | None = None,
) -> tuple[str, ...]:
    normalized_target = normalize_clawbot_target(channel, target)
    argv = [
        command,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        normalized_target,
        "--message",
        body,
        "--json",
    ]
    if account:
        argv[1:1] = ["--account", account]
    return tuple(argv)


async def default_cli_runner(
    argv: tuple[str, ...],
    timeout_seconds: float,
) -> bool:
    """Run one CLI send with a hard timeout; never let a hung CLI block the loop."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        try:
            await process.wait()
        except OSError:
            pass
        return False
    return process.returncode == 0


class CliNotifyChannel:
    """One configured CLI destination.  ``aclose`` is a no-op by design."""

    def __init__(
        self,
        *,
        kind: str,
        target: str,
        command: str,
        channel: str = DEFAULT_CLAWBOT_CHANNEL,
        runner: CliRunner = default_cli_runner,
        timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
        account: str | None = None,
    ) -> None:
        if kind not in {"feishu_cli", "clawbot"}:
            raise ValueError(f"unsupported_notify_kind: {kind}")
        self._kind = kind
        self._target = target
        self._command = command
        self._channel = channel
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._account = account

    async def send(
        self,
        *,
        title: str,
        text: str,
        link_url: str | None = None,
    ) -> bool:
        body = build_notification_body(title=title, text=text, link_url=link_url)
        try:
            if self._kind == "feishu_cli":
                argv = feishu_cli_argv(self._command, self._target, body)
            else:
                argv = clawbot_argv(
                    self._command,
                    self._channel,
                    self._target,
                    body,
                    account=self._account,
                )
        except ValueError:
            return False
        return await self._runner(argv, self._timeout_seconds)

    async def aclose(self) -> None:
        return


__all__ = [
    "DEFAULT_CLAWBOT_CHANNEL",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
    "CliNotifyChannel",
    "clawbot_argv",
    "decode_channel_config",
    "default_cli_runner",
    "feishu_cli_argv",
    "feishu_receive_id_type",
    "normalize_clawbot_target",
    "validate_cli_channel",
    "validate_cli_target",
]
