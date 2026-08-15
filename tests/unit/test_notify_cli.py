"""CLI 通知渠道的纯函数与 runner 测试。"""

import pytest
from pydantic import ValidationError

from watch_assistant.schemas import NotifyChannelCreateRequest
from watch_assistant.services.notify_channels.cli import (
    CliNotifyChannel,
    clawbot_argv,
    decode_channel_config,
    feishu_cli_argv,
    feishu_receive_id_type,
    validate_cli_channel,
    validate_cli_target,
)


def test_feishu_target_type_mapping():
    assert feishu_receive_id_type("user@example.com") == "email"
    assert feishu_receive_id_type("oc_chat") == "chat_id"
    assert feishu_receive_id_type("ou_user") == "open_id"
    assert feishu_receive_id_type("on_union") == "union_id"
    assert feishu_receive_id_type("u_user") == "user_id"
    assert feishu_receive_id_type("bad-target") is None


def test_argv_builders_never_use_shell_concat():
    assert feishu_cli_argv("/usr/local/bin/feishu-cli", "oc_chat", "hello") == (
        "/usr/local/bin/feishu-cli",
        "msg",
        "send",
        "--receive-id-type",
        "chat_id",
        "--receive-id",
        "oc_chat",
        "--text",
        "hello",
    )
    assert clawbot_argv("openclaw", "openclaw-weixin", "peer-1", "hello") == (
        "openclaw",
        "message",
        "send",
        "--channel",
        "openclaw-weixin",
        "--target",
        "peer-1",
        "--message",
        "hello",
        "--json",
    )


def test_target_and_channel_validation():
    assert validate_cli_target("ou_abc") == "ou_abc"
    assert validate_cli_channel("openclaw-weixin") == "openclaw-weixin"
    with pytest.raises(ValueError):
        validate_cli_target("bad target; rm -rf /")
    with pytest.raises(ValueError):
        validate_cli_channel("-bad")


def test_decode_channel_config_accepts_legacy_webhook_url_and_json():
    assert decode_channel_config("feishu", "https://open.feishu.cn/hook/x") == {
        "url": "https://open.feishu.cn/hook/x"
    }
    assert decode_channel_config(
        "clawbot", '{"target":"ou_1","channel":"feishu"}'
    ) == {"target": "ou_1", "channel": "feishu"}


@pytest.mark.asyncio
async def test_cli_channel_send_builds_args_and_reports_failure():
    seen = []

    async def runner(argv, timeout_seconds):
        seen.append((argv, timeout_seconds))
        return False

    channel = CliNotifyChannel(
        kind="clawbot",
        target="ou_user",
        command="openclaw",
        channel="feishu",
        runner=runner,
    )
    ok = await channel.send(title="标题", text="正文", link_url="http://x/1")
    assert ok is False
    assert seen and seen[0][0][-2] == "标题\n正文\nhttp://x/1"


def test_notify_channel_create_request_kind_validation():
    with pytest.raises(ValidationError):
        NotifyChannelCreateRequest(name="x", kind="feishu")
    with pytest.raises(ValidationError):
        NotifyChannelCreateRequest(name="x", kind="clawbot", target="ou_1")
    request = NotifyChannelCreateRequest(
        name="x", kind="clawbot", target="ou_1", cli_channel="feishu"
    )
    assert request.target == "ou_1"
    feishu_request = NotifyChannelCreateRequest(
        name="x", kind="feishu_cli", target="ou_1"
    )
    assert feishu_request.kind == "feishu_cli"


@pytest.mark.asyncio
async def test_default_cli_runner_handles_missing_executable():
    from watch_assistant.services.notify_channels.cli import default_cli_runner

    ok = await default_cli_runner(("/definitely/missing/cli", "x"), 5.0)
    assert ok is False
