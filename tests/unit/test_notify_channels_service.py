"""任务级单测:通知渠道 CRUD 服务(加密落库 + 脱敏回显)。

覆盖 Task 3 的关键契约:
- webhook_url 用 crypto.encrypt 加密后落库,DB 里查不到明文;解密可还原。
- 所有响应只回显 webhook_url_prefix(前 48 字符 + … + 尾 4 位),
  绝不包含完整 webhook_url 或 webhook_url_encrypted。
- create 校验 kind 支持("feishu"/否则 unsupported_notify_kind)、name 去空白、
  webhook_url 去空白且非空。
- delete 静默成功(找不到不抛错,与幂等删除惯例对齐)。
"""

import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.models import NotifyChannel
from watch_assistant.services.notify_channels_service import (
    NotifyChannelResponse,
    NotifyChannelService,
)


@pytest.fixture
async def service(tmp_path, crypto):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'notify.db'}")
    await initialize_database(database.engine)
    svc = NotifyChannelService(database.session_factory, crypto)
    try:
        yield svc, database
    finally:
        await database.engine.dispose()


def _mask_prefix(url: str) -> str:
    """按同一规则计算期望的脱敏前缀:前 48 字符 + … + 尾 4 位。"""
    return url[:48] + "…" + url[-4:]


@pytest.mark.asyncio
async def test_create_encrypts_and_masks_url(service, crypto):
    svc, database = service
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/0123-4567-89ab-cdef"
    created = await svc.create(" 测试机器人 ", url)

    # 响应只回显脱敏前缀,绝不回显完整 URL。
    assert created.name == "测试机器人"  # 去空白
    assert created.kind == "feishu"
    assert created.webhook_url_prefix == _mask_prefix(url)
    assert url not in created.webhook_url_prefix
    # 尾 4 位出现在前缀中。
    assert created.webhook_url_prefix.endswith(url[-4:])
    assert hasattr(created, "id")
    assert created.enabled is True
    assert created.revision == 1

    # DB 里存的密文不能包含明文 URL,且可解密还原。
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert row is not None
    assert url not in row.webhook_url_encrypted
    assert row.webhook_url_encrypted != url
    assert crypto.decrypt(row.webhook_url_encrypted) == url


@pytest.mark.asyncio
async def test_list_response_never_contains_full_url(service, crypto):
    svc, _database = service
    url_1 = "https://open.feishu.cn/open-apis/bot/v2/hook/aaaa-bbbb-cccc-dddd"
    url_2 = "https://open.feishu.cn/open-apis/bot/v2/hook/1111-2222-3333-4444"
    await svc.create("渠道A", url_1)
    await svc.create("渠道B", url_2)

    import dataclasses

    items = await svc.list()
    assert len(items) == 2
    # list 按 created_at 倒序,后创建的 url_2 在前。
    for item, url in zip(items, (url_2, url_1), strict=False):
        assert url not in item.webhook_url_prefix
        assert item.webhook_url_prefix == _mask_prefix(url)
    # 整个序列化结果里都不允许出现明文/完整 URL 或其加密字段。
    for item in items:
        serialized = dataclasses.asdict(item)
        assert "webhook_url_encrypted" not in serialized
        assert "webhook_url" not in serialized
        for field_value in serialized.values():
            if isinstance(field_value, str):
                assert url_1 not in field_value and url_2 not in field_value


@pytest.mark.asyncio
async def test_list_orders_channels_by_created_desc(service):
    svc, _database = service
    await svc.create("先", "https://open.feishu.cn/open-apis/bot/v2/hook/xxx-1")
    await svc.create("后", "https://open.feishu.cn/open-apis/bot/v2/hook/xxx-2")
    items = await svc.list()
    assert [item.name for item in items] == ["后", "先"]


@pytest.mark.asyncio
async def test_create_rejects_unsupported_kind(service):
    svc, _database = service
    with pytest.raises(ValueError) as error:
        await svc.create("x", "https://open.feishu.cn/x", kind="slack")
    assert str(error.value) == "unsupported_notify_kind"


@pytest.mark.asyncio
async def test_create_rejects_empty_or_blank_webhook_url(service):
    svc, _database = service
    for bad in ("", "   "):
        with pytest.raises(ValueError) as error:
            await svc.create("x", bad)
        assert str(error.value) == "webhook_url_required"


@pytest.mark.asyncio
async def test_create_strips_webhook_url_whitespace(service, crypto):
    svc, database = service
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/strip-1234"
    created = await svc.create("x", f"  {url}  ")
    assert created.webhook_url_prefix == _mask_prefix(url)
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert crypto.decrypt(row.webhook_url_encrypted) == url
    assert len(url) > 48  # 确保掩码规则覆盖截断场景


@pytest.mark.asyncio
async def test_set_enabled_roundtrip_and_bumps_revision(service):
    svc, _database = service
    created = await svc.create("x", "https://open.feishu.cn/open-apis/bot/v2/hook/en-1")
    assert created.enabled is True
    disabled = await svc.set_enabled(created.id, False)
    assert disabled.enabled is False
    assert disabled.revision == created.revision + 1
    reenabled = await svc.set_enabled(created.id, True)
    assert reenabled.enabled is True
    assert reenabled.revision == disabled.revision + 1
    # 响应类型一致。
    assert isinstance(reenabled, NotifyChannelResponse)


@pytest.mark.asyncio
async def test_set_enabled_unknown_channel_raises(service):
    svc, _database = service
    with pytest.raises(ValueError) as error:
        await svc.set_enabled("nope", True)
    assert str(error.value) == "notify_channel_not_found"


@pytest.mark.asyncio
async def test_delete_removes_channel_and_is_idempotent(service):
    svc, database = service
    created = await svc.create("x", "https://open.feishu.cn/open-apis/bot/v2/hook/del-1")
    await svc.delete(created.id)
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert row is None
    # delete 找不到时静默成功(幂等删除,不抛错)。
    await svc.delete(created.id)


@pytest.mark.asyncio
async def test_create_feishu_cli_stores_json_config(service, crypto):
    svc, database = service
    created = await svc.create(
        "飞书 CLI", None, kind="feishu_cli", target="ou_abc"
    )
    assert created.kind == "feishu_cli"
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert crypto.decrypt(row.webhook_url_encrypted) == '{"target":"ou_abc"}'


@pytest.mark.asyncio
async def test_create_feishu_bot_stores_target_only(service, crypto):
    svc, database = service
    created = await svc.create(
        "飞书 Bot", None, kind="feishu_bot", target="oc_group"
    )
    assert created.kind == "feishu_bot"
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert crypto.decrypt(row.webhook_url_encrypted) == '{"target":"oc_group"}'


@pytest.mark.asyncio
async def test_create_clawbot_normalizes_wechat_channel(service, crypto):
    svc, database = service
    created = await svc.create(
        "微信 ClawBot",
        None,
        kind="clawbot",
        target="wxid@im.wechat",
        cli_channel="wechat",
        cli_account="wx-account-1",
    )
    assert created.kind == "clawbot"
    async with database.session_factory() as session:
        row = await session.get(NotifyChannel, created.id)
    assert crypto.decrypt(row.webhook_url_encrypted) == (
        '{"target":"wxid@im.wechat","channel":"openclaw-weixin",'
        '"account":"wx-account-1"}'
    )


def test_response_contract_fields():
    """NotifyChannelResponse 必须暴露约定字段,且不含加密/完整 URL 字段。"""
    import dataclasses

    fields = {field.name for field in dataclasses.fields(NotifyChannelResponse)}
    required = {
        "id", "name", "kind", "webhook_url_prefix",
        "enabled", "revision", "created_at", "updated_at",
    }
    assert required.issubset(fields)
    forbidden = {"webhook_url_encrypted", "webhook_url"}
    assert forbidden.isdisjoint(fields)
