"""Narrow live transport for the already-audited C03 probe.

The caller owns client construction and credentials.  This adapter only calls
the fixed ``p115client`` methods required by C03 and immediately reduces each
response to the probe DTOs.  It never retains or renders a third-party
response.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from watch_assistant.adapters.p115_c03_fixture_probe import (
    MAX_RECOVERY_LIST_PAGE_CALLS,
    C03DirectoryListing,
    C03RemoteEntry,
    C03WriteReceipt,
    P115C03Transport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    PreparedWrite,
    WriteOperation,
    WriteStatus,
)

EXPECTED_P115CLIENT_VERSION = "0.0.9.6.5.1"
# Directory listings are read one entry per page (VERIFIED_FS_FILES_PAGE_SIZE)
# so a real source directory with several release files plus a subtitle
# folder stays readable.  Each page is one verified round-trip; 8 pages is a
# bounded ceiling that still fails closed on unbounded listings.
MAX_FS_FILES_PAGE_CALLS = 8
MAX_RECOVERY_FS_FILES_PAGE_CALLS = MAX_RECOVERY_LIST_PAGE_CALLS
VERIFIED_FS_FILES_PAGE_SIZE = 1
# 生产执行路径（组织整理、空目录清理等）的完整分页配置。C03 live runner 验证
# 路径必须保持类级默认（VERIFIED_FS_FILES_PAGE_SIZE=1、MAX_FS_FILES_PAGE_CALLS=8，
# 见 AGENTS.md），生产路径不受该验证约束：任何真实媒体目录（>8 条）若仍按
# 8 页 × 1 条/页读取，都会在组织写入前得到 complete=False → observation_unverified
# → 首次写入前即永久 UNCERTAIN。生产路径因此显式放大分页：
#  - 页上限 256，与 fixture probe 恢复路径 MAX_RECOVERY_LIST_PAGE_CALLS 同量级；
#  - 页大小 50，与 p115_library_gateway.VERIFIED_BATCH_PAGE_SIZE 相同——该值已在
#    真实 115 上实证 fs_files 分页结构与 limit=1 一致（见 gateway 模块注释）。
PRODUCTION_FS_FILES_PAGE_CALLS = 256
PRODUCTION_FS_FILES_PAGE_SIZE = 50


class P115ClientLike(Protocol):
    def fs_mkdir(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_move(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_rename(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_delete(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_info(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_files(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...

    def fs_info_app(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_files_app(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...


P115C03CallExecutor = Callable[..., Awaitable[Any] | Any]
P115_BUSY_OPERATION_ERRNO = 990009
P115_BUSY_OPERATION_RETRY_DELAY_SECONDS = 3.0


class P115C03CallTimeoutUnavailable(RuntimeError):
    """The injected client seam cannot enforce a call-level timeout."""


class P115C03LiveTransport(P115C03Transport):
    """Translate only fixed C03 calls through a caller-created client."""

    _max_page_calls = MAX_FS_FILES_PAGE_CALLS
    _page_size = VERIFIED_FS_FILES_PAGE_SIZE

    def __init__(
        self,
        client: P115ClientLike,
        *,
        call_executor: P115C03CallExecutor | None = None,
        max_page_calls: int | None = None,
        page_size: int | None = None,
    ) -> None:
        # 分页参数可逐实例覆盖：C03 live runner 验证路径保持类级默认
        # （8 页 × 1 条/页，AGENTS.md 约束）；生产执行路径通过
        # P115C03ProductionTransport 放大为完整分页，避免 >8 条的真实
        # 媒体目录在首次写入前即 observation_unverified。
        self._client = client
        self._call_executor = call_executor
        self._max_page_calls = _validated_page_count(
            max_page_calls, self._max_page_calls
        )
        self._page_size = _validated_page_size(page_size, self._page_size)

    def __repr__(self) -> str:
        return "P115C03LiveTransport(mode='c03', client='injected')"

    async def execute(
        self, request: PreparedWrite, *, timeout_seconds: float
    ) -> C03WriteReceipt:
        if self._call_executor is None:
            raise P115C03CallTimeoutUnavailable
        method_name = {
            WriteOperation.MKDIR: "fs_mkdir",
            WriteOperation.MOVE: "fs_move",
            WriteOperation.RENAME: "fs_rename",
            WriteOperation.RECYCLE: "fs_delete",
            WriteOperation.DELETE: "fs_delete",
        }.get(request.operation)
        payload = _client_payload(request)
        if method_name is None or payload is None:
            return C03WriteReceipt(WriteStatus.UNCERTAIN)
        try:
            response = await _call(
                self._call_executor,
                getattr(self._client, method_name),
                payload,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The provider retired the web write endpoints (HTTP 405); retry
            # through the app endpoint with its payload shape when available.
            app_name = _APP_WRITE_METHODS.get(request.operation)
            app_method = (
                getattr(self._client, app_name, None) if app_name else None
            )
            app_payload = _client_payload_app(request)
            if not _is_method_not_allowed(error) or not callable(app_method) or app_payload is None:
                raise
            response = await _call(
                self._call_executor,
                app_method,
                app_payload,
                timeout_seconds=timeout_seconds,
            )
        return _normalize_write_response(request.operation, response)

    async def read(
        self, file_id: str, *, timeout_seconds: float
    ) -> C03RemoteEntry | None:
        if self._call_executor is None:
            raise P115C03CallTimeoutUnavailable
        response = await _call_read_with_405_fallback(
            self._call_executor,
            self._client.fs_info,
            getattr(self._client, "fs_info_app", None),
            {"cid": file_id},
            timeout_seconds=timeout_seconds,
        )
        return _normalize_info_response(file_id, response)

    async def list_children(
        self, parent_id: str, *, timeout_seconds: float
    ) -> C03DirectoryListing:
        if self._call_executor is None:
            raise P115C03CallTimeoutUnavailable
        entries: list[C03RemoteEntry] = []
        offset = 0
        deadline = time.monotonic() + timeout_seconds
        expected_total: int | None = None
        for page_calls in range(1, self._max_page_calls + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls - 1
                )
            try:
                response = await _call_read_with_405_fallback(
                    self._call_executor,
                    self._client.fs_files,
                    getattr(self._client, "fs_files_app", None),
                    {
                        "cid": parent_id,
                        "limit": self._page_size,
                        "offset": offset,
                        "record_open_time": 0,
                        "show_dir": 1,
                    },
                    timeout_seconds=remaining,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - response details stay opaque
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            page = _normalize_files_page(response, parent_id, offset, self._page_size)
            if page is None:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            page_entries, total = page
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            known_ids = {entry.file_id for entry in entries}
            if any(entry.file_id in known_ids for entry in page_entries):
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            entries.extend(page_entries)
            if offset + len(page_entries) == expected_total:
                return C03DirectoryListing(
                    tuple(entries), complete=True, page_calls=page_calls
                )
            if not page_entries:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            offset += len(page_entries)
        return C03DirectoryListing(
            tuple(entries), complete=False, page_calls=self._max_page_calls
        )


class P115C03RecoveryTransport(P115C03LiveTransport):
    """Use the separately bounded read cap required by fixture recovery."""

    _max_page_calls = MAX_RECOVERY_FS_FILES_PAGE_CALLS


class P115C03ProductionTransport(P115C03LiveTransport):
    """生产执行路径专用：完整分页读取，不受 C03 live 验证的 8 页上限约束。

    组织整理、空目录清理等生产路径在写入前必须完整读取源/目标目录；真实媒体
    目录通常远超 8 条，按验证路径的 8 页 × 1 条/页读取必然 complete=False →
    observation_unverified → 首次写入前即永久 UNCERTAIN。本类把分页放大到
    256 页 × 50 条/页（50 条/页已在真实 115 上实证，见 PRODUCTION_FS_FILES_PAGE_SIZE
    注释），同时保留逐页严格校验与 deadline，验证强度不降。
    """

    _max_page_calls = PRODUCTION_FS_FILES_PAGE_CALLS
    _page_size = PRODUCTION_FS_FILES_PAGE_SIZE


async def _call(
    call_executor: P115C03CallExecutor | None,
    method: Callable[..., Any],
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Run one call only through a seam that owns call-level timeout.

    同步 executor(生产 ``p115_c03_timeout_executor``,含 busy 重试的
    ``time.sleep(3)``)统一在 ``asyncio.to_thread`` 线程池执行,避免阻塞
    事件循环;async executor(测试/runner 注入)保持直接 await。
    """

    if call_executor is None:
        raise P115C03CallTimeoutUnavailable
    if inspect.iscoroutinefunction(call_executor):
        result = call_executor(
            method,
            dict(payload),
            timeout_seconds=timeout_seconds,
        )
    else:
        result = await asyncio.to_thread(
            call_executor,
            method,
            dict(payload),
            timeout_seconds=timeout_seconds,
        )
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_read_with_405_fallback(
    call_executor: P115C03CallExecutor | None,
    primary_method: Callable[..., Any],
    fallback_method: Callable[..., Any] | None,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Use the verified app read endpoint only for a provider HTTP 405."""

    try:
        response = await _call(
            call_executor,
            fallback_method if callable(fallback_method) else primary_method,
            payload,
            timeout_seconds=timeout_seconds,
        )
        return response
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # 优先使用 proapi(app) 接口:旧接口在迁移后索引过期且已 405;
        # app 接口异常时回退旧接口兜底。
        if not _is_method_not_allowed(error):
            raise
        return await _call(
            call_executor,
            primary_method,
            payload,
            timeout_seconds=timeout_seconds,
        )


def _is_method_not_allowed(error: BaseException) -> bool:
    for name in ("status", "status_code", "code"):
        value = getattr(error, name, None)
        if value == 405 or value == "405":
            return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) in {405, "405"}


def _is_structured_method_not_allowed(response: object) -> bool:
    if not isinstance(response, Mapping):
        return False
    return any(response.get(name) in {405, "405"} for name in ("status_code", "http_status"))


def p115_c03_timeout_executor(
    method: Callable[..., Any],
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Execute one fixed-client call with timeout and the known busy retry."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise RuntimeError("invalid_timeout")
    try:
        from urllib3_future_request import request as urllib3_request
    except ImportError:
        raise RuntimeError("timeout_transport_unavailable") from None

    def request_with_timeout(*, async_: bool = False, **request_kwargs: Any) -> Any:
        if async_:
            raise RuntimeError("async_transport_unsupported")
        request_kwargs["timeout"] = float(timeout_seconds)
        request_kwargs["retries"] = False
        return urllib3_request(async_=False, **request_kwargs)

    for attempt in range(2):
        try:
            return method(payload, async_=False, request=request_with_timeout)
        except Exception as error:
            if attempt or not _has_busy_errno(error):
                raise
            time.sleep(P115_BUSY_OPERATION_RETRY_DELAY_SECONDS)
    raise RuntimeError("call_unreachable")


def _has_busy_errno(error: BaseException) -> bool:
    if getattr(error, "errno", None) == P115_BUSY_OPERATION_ERRNO:
        return True
    return any(
        isinstance(argument, Mapping)
        and argument.get("errno") == P115_BUSY_OPERATION_ERRNO
        for argument in getattr(error, "args", ())
    )


def _client_payload(request: PreparedWrite) -> dict[str, str] | None:
    """Map the generic write DTO to the fixed p115client payload shape."""

    payload = request.payload
    if request.operation is WriteOperation.MKDIR:
        if set(payload) != {"pid", "file_name"}:
            return None
        return {"pid": payload["pid"], "cname": payload["file_name"]}
    if request.operation is WriteOperation.MOVE:
        if set(payload) != {"file_ids", "to_cid"}:
            return None
        return {"fid": payload["file_ids"], "pid": payload["to_cid"]}
    if request.operation is WriteOperation.RENAME:
        if set(payload) != {"file_id", "file_name"}:
            return None
        return {f"files_new_name[{payload['file_id']}]": payload["file_name"]}
    if request.operation in {WriteOperation.RECYCLE, WriteOperation.DELETE}:
        if set(payload) != {"file_id"}:
            return None
        return {"fid": payload["file_id"]}
    return None


_APP_WRITE_METHODS = {
    WriteOperation.MKDIR: "fs_mkdir_app",
    WriteOperation.MOVE: "fs_move_app",
    WriteOperation.RENAME: "fs_rename_app",
    WriteOperation.RECYCLE: "fs_delete_app",
    WriteOperation.DELETE: "fs_delete_app",
}


def _client_payload_app(request: PreparedWrite) -> dict[str, str] | None:
    """Map the generic write DTO to the new provider app-endpoint payloads.

    The web endpoints (fs_mkdir / fs_move / fs_delete) return HTTP 405 since
    the provider migrated to the proapi endpoints; the app endpoints use a
    different payload shape (name/ids/to_cid/file_ids) while rename keeps
    the same files_new_name form.
    """

    payload = request.payload
    if request.operation is WriteOperation.MKDIR:
        if set(payload) != {"pid", "file_name"}:
            return None
        return {"pid": payload["pid"], "name": payload["file_name"]}
    if request.operation is WriteOperation.MOVE:
        if set(payload) != {"file_ids", "to_cid"}:
            return None
        return {"ids": payload["file_ids"], "to_cid": payload["to_cid"]}
    if request.operation is WriteOperation.RENAME:
        if set(payload) != {"file_id", "file_name"}:
            return None
        return {f"files_new_name[{payload['file_id']}]": payload["file_name"]}
    if request.operation in {WriteOperation.RECYCLE, WriteOperation.DELETE}:
        if set(payload) != {"file_id"}:
            return None
        return {"file_ids": payload["file_id"]}
    return None


def _normalize_write_response(
    operation: WriteOperation, response: Any
) -> C03WriteReceipt:
    if not isinstance(response, Mapping) or not _response_success(response):
        return C03WriteReceipt(WriteStatus.UNCERTAIN)
    if operation is not WriteOperation.MKDIR:
        return C03WriteReceipt(WriteStatus.SUCCESS)
    detail = response.get("data")
    if not isinstance(detail, Mapping):
        detail = response
    file_id = _single_id(
        detail, ("cid", "directory_id", "fid", "file_id", "category_id")
    )
    if file_id is None:
        return C03WriteReceipt(WriteStatus.UNCERTAIN)
    return C03WriteReceipt(WriteStatus.SUCCESS, file_id)


def _normalize_info_response(requested_id: str, response: Any) -> C03RemoteEntry | None:
    if not isinstance(response, Mapping) or not _response_success(response):
        return None
    detail = response.get("data")
    if not isinstance(detail, Mapping):
        detail = response
    if _directory_marker(detail) is not True:
        return None
    name = _single_text(detail, ("name", "n", "fn", "file_name", "category_name"))
    parent_id = _single_id(detail, ("parent_id", "pid"))
    response_id = _single_id(
        detail, ("file_id", "fid", "directory_id", "category_id", "cid")
    )
    if name is None or parent_id is None or response_id is None:
        return None
    if response_id != requested_id:
        return None
    return C03RemoteEntry(requested_id, parent_id, name, True)


def _normalize_files_page(
    response: Any, parent_id: str, expected_offset: int, page_size: int
) -> tuple[tuple[C03RemoteEntry, ...], int] | None:
    if not isinstance(response, Mapping) or not _response_success(response):
        return None
    offset = _integer(response.get("offset"))
    limit = _integer(response.get("limit"))
    total = _integer(response.get("count"))
    records = _records(response)
    if (
        offset != expected_offset
        or limit != page_size
        or total is None
        or total < 0
        or records is None
        or len(records) > page_size
        or expected_offset > total
        or expected_offset + len(records) > total
        or len(records) != min(page_size, total - expected_offset)
        or (not records and total > expected_offset)
    ):
        return None
    entries: list[C03RemoteEntry] = []
    for record in records:
        entry = _normalize_list_entry(record, parent_id)
        if entry is None:
            return None
        entries.append(entry)
    return tuple(entries), total


def _normalize_list_entry(
    record: Mapping[str, Any], parent_id: str
) -> C03RemoteEntry | None:
    is_directory = _directory_marker(record)
    if is_directory is None:
        return None
    identity_names = (
        ("file_id", "fid")
        if not is_directory
        else ("file_id", "fid", "directory_id", "category_id", "cid")
    )
    file_id = _single_id(record, identity_names)
    parent_names = (
        ("parent_id", "pid", "cid") if not is_directory else ("parent_id", "pid")
    )
    record_parent_id = _single_id(record, parent_names)
    name = _single_text(record, ("name", "n", "fn", "file_name", "category_name"))
    if file_id is None or record_parent_id != parent_id or name is None:
        return None
    return C03RemoteEntry(file_id, parent_id, name, is_directory)


def _records(response: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    data = response.get("data")
    if isinstance(data, Mapping):
        data = data.get("list", data.get("items"))
    if not isinstance(data, list) or any(
        not isinstance(item, Mapping) for item in data
    ):
        return None
    return data


def _response_success(response: Mapping[str, Any]) -> bool:
    if response.get("state") is not True:
        return False
    if "success" in response and response["success"] is not True:
        return False
    if "code" in response and not _zero_code(response["code"]):
        return False
    return "errno" not in response or _errno_success(response["errno"])


def _zero_code(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, int):
        return value in {0, 200}
    return isinstance(value, str) and value.strip() in {"0", "200"}


def _errno_success(value: Any) -> bool:
    return _zero_code(value) or (isinstance(value, str) and not value.strip())


def _single_id(record: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for name in names:
        if name not in record:
            continue
        value = record[name]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        normalized = str(value)
        if not normalized.isdigit() or normalized.startswith("0"):
            return None
        values.append(normalized)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _single_text(record: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for name in names:
        if name not in record:
            continue
        value = record[name]
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        values.append(value)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _directory_marker(record: Mapping[str, Any]) -> bool | None:
    values: list[bool] = []
    for name in ("is_dir", "is_directory"):
        if name in record:
            if not isinstance(record[name], bool):
                return None
            values.append(record[name])
    if "fc" in record:
        value = record["fc"]
        if isinstance(value, bool) or value not in (0, 1, "0", "1"):
            return None
        values.append(value in (0, "0"))
    if "file_category" in record:
        value = record["file_category"]
        if isinstance(value, bool) or value not in (0, 1, "0", "1"):
            return None
        values.append(value in (0, "0"))
    if not values or len(set(values)) != 1:
        return None
    return values[0]


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _validated_page_count(value: int | None, default: int) -> int:
    """逐实例分页上限必须为正整数，否则保持类级默认。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid_max_page_calls")
    return value


def _validated_page_size(value: int | None, default: int) -> int:
    """逐实例页大小必须为正整数，否则保持类级默认。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid_page_size")
    return value


__all__ = [
    "EXPECTED_P115CLIENT_VERSION",
    "MAX_FS_FILES_PAGE_CALLS",
    "MAX_RECOVERY_FS_FILES_PAGE_CALLS",
    "PRODUCTION_FS_FILES_PAGE_CALLS",
    "PRODUCTION_FS_FILES_PAGE_SIZE",
    "VERIFIED_FS_FILES_PAGE_SIZE",
    "P115C03CallExecutor",
    "P115C03CallTimeoutUnavailable",
    "P115C03LiveTransport",
    "P115C03ProductionTransport",
    "P115C03RecoveryTransport",
    "P115ClientLike",
    "p115_c03_timeout_executor",
]
