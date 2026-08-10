"""L11:get_status 分页按响应 total/count 字段推断总页数并及早截断。

修复前 get_status 逐页串行请求硬顶到 101 页;现在只要响应带 count/total
就能按 ceil(count / page_size) 截断,不再每次都打满页数上限。
"""

from watch_assistant.adapters.p115 import (
    P115Adapter,
    _task_page_count,
    _task_total_count,
)
from watch_assistant.schemas import RemoteStatus


class _FakeCookieProvider:
    """只提供固定 cookie 字符串,不涉及真实文件。"""

    def load(self) -> str | None:
        return "fixture-cookie"


class _FakeTaskClient:
    """按页返回 115 云下载任务列表响应,并记录实际请求过的页码。"""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.requested_pages: list[int] = []

    def clouddownload_task_list(self, payload: dict) -> dict:
        page = int(payload["page"])
        self.requested_pages.append(page)
        return self.pages[page - 1]


def _task_page_response(
    *,
    page: int,
    count: int,
    page_size: int = 30,
    tasks: list[dict] | None = None,
) -> dict:
    if tasks is None:
        tasks = [
            {"info_hash": f"{page * page_size + index:040x}", "status": 1}
            for index in range(1, page_size + 1)
        ]
    return {"state": True, "data": {"count": count, "page": page, "tasks": tasks}}


def test_task_page_count_prefers_explicit_page_fields():
    """显式 page_count/totalPages 字段优先,不被 count 推断覆盖。"""
    response = {"state": True, "data": {"count": 500, "totalPages": 3, "tasks": []}}
    assert _task_page_count(response) == 3


def test_task_page_count_infers_from_count_when_page_fields_absent():
    """缺页数字段时按 ceil(count / page_size) 推断总页数。"""
    # 默认每页 30 条:60 → 2 页
    assert _task_page_count({"state": True, "data": {"count": 60}}) == 2
    # 61 → ceil(61 / 30) = 3 页
    assert _task_page_count({"state": True, "data": {"count": 61}}) == 3
    # 响应自带 page_size 时按其计算
    response = {"state": True, "data": {"count": 61, "page_size": 25}}
    assert _task_page_count(response) == 3


def test_task_page_count_falls_back_to_safety_cap_without_paging_hint():
    """既无页数字段也无 count/total 时保持 101 安全上限(不放大请求)。"""
    response = {"state": True, "data": {"tasks": []}}
    assert _task_page_count(response) == 101


def test_task_total_count_accepts_common_total_keys():
    """total/count/total_count/totalCount 都视为任务总数来源。"""
    for key in ("total", "count", "total_count", "totalCount"):
        response = {"state": True, "data": {key: 45}}
        assert _task_total_count(response) == 45, key
    # 布尔值不算总数
    assert _task_total_count({"state": True, "data": {"count": True}}) is None


async def test_get_status_truncates_pagination_by_inferred_page_count():
    """count=60、每页 30 条:只请求 2 页就截断,而不是打到 101 页。"""
    pages = [
        _task_page_response(page=1, count=60),
        _task_page_response(page=2, count=60),
    ]
    client = _FakeTaskClient(pages)
    adapter = P115Adapter(
        cookie_provider=_FakeCookieProvider(),
        target_cid="0",
        client_factory=lambda _cookie: client,
    )

    result = await adapter.get_status(f"infohash:{'ab' * 20}")

    assert result is None
    assert client.requested_pages == [1, 2]


async def test_get_status_still_finds_task_on_later_page_before_truncation():
    """截断前仍逐页扫描:目标任务在最后一页也能命中。"""
    target_hash = "ab" * 20
    second_tasks = [
        {"info_hash": f"{60 + index:040x}", "status": 1}
        for index in range(1, 30)
    ] + [{"info_hash": target_hash, "status": -1}]
    pages = [
        _task_page_response(page=1, count=60),
        _task_page_response(page=2, count=60, tasks=second_tasks),
    ]
    client = _FakeTaskClient(pages)
    adapter = P115Adapter(
        cookie_provider=_FakeCookieProvider(),
        target_cid="0",
        client_factory=lambda _cookie: client,
    )

    result = await adapter.get_status(f"infohash:{target_hash}")

    assert result == RemoteStatus.FAILED
    assert client.requested_pages == [1, 2]
