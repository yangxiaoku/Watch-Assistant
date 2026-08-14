"""Bounded read-only P115 directory gateway.

This adapter maps the small ``fs_files`` contract verified in Phase 0 plus
bounded ``fs_info`` detail reads for allowlisted IDs. Credentials and transport
construction are injected, and callers must provide any durable child scope.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError

from watch_assistant.adapters.p115_library import (
    DirectoryDetail,
    DirectoryPage,
    FileDetail,
    LibraryContractError,
    LibraryEntry,
    ScanState,
    parse_library_entry,
)
from watch_assistant.adapters.p115_library_transport import (
    P115ReadOnlyTransportProtocol,
    P115ReadOnlyTransportUnavailable,
    create_p115_readonly_transport,
)

VERIFIED_PAGE_SIZE = 1
# 登录失效类 errno（与 p115 适配器 _AUTH_ERRNOS 保持一致）：
# 命中时通知 credential provider 记录一次认证失败。
_AUTH_FAILURE_ERRNOS = frozenset((99, 911, 990001, 40101004, 40101017, 40101032))
# 目录选择器批量读取的已验证页大小：2026-08-09 在真实 115 上对非虚拟根目录
# 实证 fs_files limit=50 返回结构/分页与 limit=1 一致（见 probe 证据），
# 作为与 VERIFIED_PAGE_SIZE 并列的另一条已验证路径。
VERIFIED_BATCH_PAGE_SIZE = 50
VIRTUAL_ROOT_PAGE_SIZE = 50
VIRTUAL_ROOT_RESPONSE_LIMIT_DELTA = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_SCOPE_VERIFICATION_PAGES = 10_000
# 115 风控/瞬时错误(429/5xx)的有限重试:退避后重试,预算耗尽仍失败关闭。
# 配合进程级节流(throttle_read)降低触发账号级风控的概率。
_GATEWAY_CALL_MAX_ATTEMPTS = 3
_GATEWAY_CALL_RETRY_DELAY_SECONDS = 2.0
_GATEWAY_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class P115CredentialSource(Protocol):
    """Inject a credential only at the point where a client is built."""

    def load(self) -> str | None: ...


P115TransportFactory = Callable[[str], P115ReadOnlyTransportProtocol]


class P115ReadOnlyGatewayError(LibraryContractError):
    """Stable, redacted boundary error with no third-party detail text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class P115ReadOnlyDirectoryGateway:
    """Map bounded ``fs_files`` and ``fs_info`` results into local DTOs."""

    def __init__(
        self,
        credential_source: P115CredentialSource,
        transport_factory: P115TransportFactory = create_p115_readonly_transport,
        *,
        authorized_directory_ids: Collection[str],
        authorized_file_ids: Collection[str] = (),
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        allow_virtual_root: bool = False,
    ) -> None:
        authorized_directories = frozenset(authorized_directory_ids)
        authorized_files = frozenset(authorized_file_ids)
        if not authorized_directories or any(
            _directory_id(value, allow_zero=allow_virtual_root) != value
            for value in authorized_directories
        ):
            raise ValueError("invalid_authorized_directories")
        if any(_stable_id(value) != value for value in authorized_files):
            raise ValueError("invalid_authorized_files")
        if _positive_timeout(request_timeout_seconds) is None:
            raise ValueError("invalid_request_timeout")
        self._credential_source = credential_source
        self._transport_factory = transport_factory
        self._authorized_directory_ids = authorized_directories
        self._authorized_file_ids = authorized_files
        self._observed_directories: dict[str, LibraryEntry] = {}
        self._observed_files: dict[str, LibraryEntry] = {}
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._clock = clock
        self._allow_virtual_root = allow_virtual_root
        self._transport: P115ReadOnlyTransportProtocol | None = None

    def __repr__(self) -> str:
        return "P115ReadOnlyDirectoryGateway(mode='bounded_read_only')"

    async def list_directory(
        self,
        directory_id: str,
        *,
        page: int = 1,
        page_size: int = VERIFIED_PAGE_SIZE,
    ) -> DirectoryPage:
        """Read one proven-safe page or fail closed before exposing entries.

        真实 115 实测(2026-08-14):show_dir=1 列表即目录的直接子项(目录 + 文件,
        含新推送尚未进入文件索引的文件),记录对文件与目录同形(fc 恒 0 或 "0"、
        无 fid/is_dir),只能以 ``fs_info`` 判型:响应的 ``count``(字符串,目录的
        子树文件数)>0 或 ``folder_count``>0 即目录,否则为文件(folder_count 对
        纯文件目录为 0,不可单独作判据;play_long 对文件为时长、对目录也可能非零)。
        父级只认记录的 pid(app/legacy 目录列表均为真实父级);文件 id 取 fid,
        缺失时取 webapi id(cid)。
        """

        normalized_directory_id = _directory_id(
            directory_id, allow_zero=self._allow_virtual_root
        )
        if normalized_directory_id is None:
            raise P115ReadOnlyGatewayError("directory_id_unverified")
        if (
            normalized_directory_id
            not in self._authorized_directory_ids | self._observed_directories.keys()
        ):
            raise P115ReadOnlyGatewayError("scope_unverified")
        if page < 1 or isinstance(page, bool):
            raise P115ReadOnlyGatewayError("page_invalid")
        if (
            page_size not in (VERIFIED_PAGE_SIZE, VERIFIED_BATCH_PAGE_SIZE)
            or isinstance(page_size, bool)
        ):
            raise P115ReadOnlyGatewayError("page_size_unverified")

        is_virtual_root = (
            self._allow_virtual_root and normalized_directory_id == "0"
        )
        request_page_size = VIRTUAL_ROOT_PAGE_SIZE if is_virtual_root else page_size
        # page_size=1 时 offset = page-1（每页 1 条，向后兼容）；批量页
        # page_size=50 时 offset = (page-1)*50（每页 50 条）。
        offset = (page - 1) * request_page_size
        deadline = self._deadline()
        response = await self._call(
            "fs_files",
            {
                "cid": normalized_directory_id,
                "limit": request_page_size,
                "offset": offset,
                "record_open_time": 0,
                "show_dir": 1,
            },
            deadline=deadline,
        )
        # 虚拟根目录的文件夹索引实测会把 limit=50 回显为 48(既有契约)。
        records, terminal, total = _verify_page(
            response,
            offset=offset,
            expected_limit=request_page_size,
            expected_response_limit=request_page_size
            - (VIRTUAL_ROOT_RESPONSE_LIMIT_DELTA if is_virtual_root else 0),
        )
        entries = [
            _parse_entry(
                record,
                is_directory=await self._resolve_record_type(record, deadline),
                allow_zero_parent=is_virtual_root,
                # 目录列表=被列目录的直接子项;file-style 记录无 pid 时父级即本目录。
                fallback_parent_id=normalized_directory_id,
            )
            for record in records
        ]
        if any(entry.parent_id != normalized_directory_id for entry in entries):
            raise P115ReadOnlyGatewayError("entry_scope_unverified")
        for entry in entries:
            if entry.is_directory and entry.directory_id is not None:
                self._observed_directories[entry.directory_id] = entry
            elif entry.file_id is not None:
                self._observed_files[entry.file_id] = entry
        return DirectoryPage(
            items=tuple(entries),
            page=page,
            page_count=None,
            total=total,
            scan_complete=True if terminal else None,
            state=ScanState.COMPLETE,
            has_more=not terminal,
            next_page=None if terminal else page + 1,
            terminal=terminal,
        )

    async def _resolve_record_type(
        self, record: Mapping[str, Any], deadline: float
    ) -> bool:
        """返回记录是否为目录;列表记录无法凭字段判型,统一用 fs_info 判型。"""
        marker = _directory_marker(record)
        if marker is not None:
            return marker
        return await self._classify_via_detail(record, deadline)

    async def _classify_via_detail(
        self, record: Mapping[str, Any], deadline: float
    ) -> bool:
        """用 fs_info 判型:count>0 或 folder_count>0 即目录,否则为文件。

        注意 file-style 记录(带 fid)的 cid 是直接父级 id,必须优先用 fid 判型,
        否则目录内的文件会被误判为目录(父级目录 count>0),树扫描产生目录环。"""
        fid = _single_id(record, ("file_id", "fid"))
        if fid is not None:
            payload: Mapping[str, str] = {"fid": fid}
        else:
            cid = _single_id(record, ("directory_id", "category_id", "cid"))
            if cid is None:
                raise P115ReadOnlyGatewayError("entry_unverified")
            payload = {"cid": cid}
        response = await self._call("fs_info", payload, deadline=deadline)
        detail = response.get("data")
        if not isinstance(detail, Mapping):
            detail = response
        count = _nonnegative_int(detail.get("count"))
        if count is not None and count > 0:
            return True
        folder_count = _nonnegative_int(detail.get("folder_count"))
        if folder_count is None:
            raise P115ReadOnlyGatewayError("entry_unverified")
        return folder_count > 0

    async def get_file_detail(self, file_id: str) -> FileDetail:
        normalized_file_id = _stable_id(file_id)
        if normalized_file_id is None:
            raise P115ReadOnlyGatewayError("file_id_unverified")
        if (
            normalized_file_id
            not in self._authorized_file_ids | self._observed_files.keys()
        ):
            raise P115ReadOnlyGatewayError("scope_unverified")
        response = await self._call(
            "fs_info", {"fid": normalized_file_id}, deadline=self._deadline()
        )
        return _parse_detail(
            response,
            normalized_file_id,
            expect_directory=False,
            observed_entry=self._observed_files.get(normalized_file_id),
            authorized_directory_ids=self._authorized_directory_ids,
            observed_directories=self._observed_directories,
        )

    async def get_directory_detail(self, directory_id: str) -> DirectoryDetail:
        normalized_directory_id = _stable_id(directory_id)
        if normalized_directory_id is None:
            raise P115ReadOnlyGatewayError("directory_id_unverified")
        if (
            normalized_directory_id
            not in self._authorized_directory_ids | self._observed_directories.keys()
        ):
            raise P115ReadOnlyGatewayError("scope_unverified")
        response = await self._call(
            "fs_info",
            {"cid": normalized_directory_id},
            deadline=self._deadline(),
        )
        return _parse_detail(
            response,
            normalized_directory_id,
            expect_directory=True,
            observed_entry=self._observed_directories.get(normalized_directory_id),
            authorized_directory_ids=self._authorized_directory_ids,
            observed_directories=self._observed_directories,
        )

    async def _call(
        self,
        method_name: str,
        payload: Mapping[str, int | str],
        *,
        deadline: float,
    ) -> Mapping[str, Any]:
        transport = await self._get_transport(deadline)
        last_error: BaseException | None = None
        for attempt in range(_GATEWAY_CALL_MAX_ATTEMPTS):
            try:
                response = await self._transport_read(
                    transport, method_name, payload, deadline
                )
            except asyncio.CancelledError:
                raise
            except P115ReadOnlyGatewayError:
                raise
            except P115ReadOnlyTransportUnavailable:
                raise P115ReadOnlyGatewayError("blocked_environment") from None
            except TimeoutError:
                raise P115ReadOnlyGatewayError(f"{method_name}_timeout") from None
            except HTTPError as error:
                last_error = error
                if (
                    error.code not in _GATEWAY_RETRYABLE_HTTP_STATUSES
                    or attempt + 1 >= _GATEWAY_CALL_MAX_ATTEMPTS
                ):
                    # 非可重试状态(如风控 405)或重试预算耗尽:失败关闭。
                    raise P115ReadOnlyGatewayError(f"{method_name}_failed") from None
                await asyncio.sleep(
                    _GATEWAY_CALL_RETRY_DELAY_SECONDS * (attempt + 1)
                )
                continue
            except Exception:  # noqa: BLE001 - do not expose provider error details
                raise P115ReadOnlyGatewayError(f"{method_name}_failed") from None
            if not isinstance(response, Mapping):
                raise P115ReadOnlyGatewayError("malformed_response")
            if not _response_success(response):
                if _response_auth_failure(response):
                    # 登录失效:通知 credential provider 降级回退,
                    # 避免失效的 managed cookie 持续阻塞读接口。
                    notify = getattr(self._credential_source, "notify_failure", None)
                    if callable(notify):
                        notify()
                raise P115ReadOnlyGatewayError("remote_failed")
            return response
        raise P115ReadOnlyGatewayError(f"{method_name}_failed") from last_error

    async def _transport_read(
        self,
        transport: P115ReadOnlyTransportProtocol,
        method_name: str,
        payload: Mapping[str, int | str],
        deadline: float,
    ) -> Mapping[str, Any]:
        """一次 115 读尝试:app 端点优先,仅对 provider HTTP 405 或非 Mapping
        响应回退旧接口;其余异常(网络/502/超时)由上层重试或失败关闭,避免
        静默使用过期索引数据。"""
        remaining = self._remaining(deadline)
        if method_name == "fs_files":
            try:
                response = await transport.fs_files_app(
                    payload, timeout_seconds=remaining
                )
                if not isinstance(response, Mapping) or _is_structured_method_not_allowed(response):
                    # app 端点返回非 Mapping(如空列表)或结构化 405 时回退旧接口;
                    # 否则非 Mapping 会被 malformed_response 拒绝,结构化 405 会被
                    # _response_success 误判为成功,再退化为含义不明的 pagination_unverified。
                    response = await transport.fs_files(
                        payload, timeout_seconds=remaining
                    )
            except Exception as error:
                if not _is_method_not_allowed(error):
                    raise
                response = await transport.fs_files(
                    payload, timeout_seconds=remaining
                )
            return response
        if method_name == "fs_info":
            try:
                response = await transport.fs_info_app(
                    payload, timeout_seconds=remaining
                )
                if not isinstance(response, Mapping) or _is_structured_method_not_allowed(response):
                    # fs_info_app 对文件(fid)请求返回空列表(实测),必须回退旧接口;
                    # 否则文件详情永远 malformed_response,推送任务的可用性观察
                    # 全部失败为 availability_observer_unavailable。
                    response = await transport.fs_info(
                        payload, timeout_seconds=remaining
                    )
            except Exception as error:
                if not _is_method_not_allowed(error):
                    raise
                response = await transport.fs_info(
                    payload, timeout_seconds=remaining
                )
            return response
        raise P115ReadOnlyTransportUnavailable("blocked_environment")

    async def _get_transport(self, deadline: float) -> P115ReadOnlyTransportProtocol:
        if self._transport is not None:
            return self._transport
        try:
            credential = await asyncio.wait_for(
                asyncio.to_thread(self._credential_source.load),
                timeout=self._remaining(deadline),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise P115ReadOnlyGatewayError("blocked_environment") from None
        except Exception:  # noqa: BLE001 - credential details are secret
            raise P115ReadOnlyGatewayError("credentials_unavailable") from None
        if not credential:
            raise P115ReadOnlyGatewayError("credentials_missing")
        try:
            transport = await asyncio.wait_for(
                asyncio.to_thread(self._transport_factory, credential),
                timeout=self._remaining(deadline),
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, P115ReadOnlyTransportUnavailable):
            raise P115ReadOnlyGatewayError("blocked_environment") from None
        except Exception:  # noqa: BLE001 - client details stay opaque
            raise P115ReadOnlyGatewayError("client_unavailable") from None
        self._transport = transport
        return transport

    def _deadline(self) -> float:
        return self._clock() + self._request_timeout_seconds

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if _positive_timeout(remaining) is None:
            raise P115ReadOnlyGatewayError("request_timeout")
        return remaining


def _verify_page(
    response: Mapping[str, Any],
    *,
    offset: int,
    expected_limit: int = VERIFIED_PAGE_SIZE,
    expected_response_limit: int | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], bool, int]:
    """只做分页验证,返回 (records, terminal, total);条目解析由调用方完成。"""
    records = _records(response)
    response_offset = _integer(response.get("offset"))
    response_limit = _integer(response.get("limit"))
    total = _integer(response.get("count"))
    if expected_response_limit is None:
        expected_response_limit = expected_limit
    if (
        records is None
        or response_offset != offset
        or response_limit != expected_response_limit
        or total is None
        or total < offset + len(records)
        or len(records) > expected_limit
    ):
        raise P115ReadOnlyGatewayError("pagination_unverified")
    if not records and total > offset:
        raise P115ReadOnlyGatewayError("pagination_unverified")
    terminal = offset + len(records) == total
    return records, terminal, total


def _records(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
    data = response.get("data")
    if isinstance(data, list):
        records = data
    elif isinstance(data, Mapping):
        records = data.get("list", data.get("items"))
    else:
        return None
    if not isinstance(records, list) or any(
        not isinstance(item, Mapping) for item in records
    ):
        return None
    return tuple(records)


def _parse_entry(
    record: Mapping[str, Any],
    *,
    allow_zero_parent: bool = False,
    is_directory: bool | None = None,
    fallback_parent_id: str | None = None,
    parent_from_pid: bool = True,
) -> LibraryEntry:
    if is_directory is None:
        is_directory = _directory_marker(record)
    if is_directory is None:
        raise P115ReadOnlyGatewayError("entry_unverified")
    name = _single_text(record, ("name", "n", "fn", "file_name", "category_name"))
    if name is None:
        raise P115ReadOnlyGatewayError("entry_unverified")
    file_id = _single_id(record, ("file_id", "fid"))
    directory_id = _single_id(record, ("directory_id", "category_id", "cid"))
    if is_directory:
        directory_id = directory_id or file_id
        if directory_id is None:
            raise P115ReadOnlyGatewayError("entry_unverified")
    else:
        if file_id is None:
            # 文件夹索引中的文件条目没有 fid,只有 webapi id(cid)。
            file_id = directory_id
        if file_id is None:
            raise P115ReadOnlyGatewayError("entry_unverified")
    # 父级只认 pid(列表记录的 cid 是条目自身 id,不是父级);文件索引记录无可靠
    # pid(app 文件索引的 pid 是文件自身 webapi id),父级由调用方注入本次列出的目录。
    parent_id = (
        _single_id(record, ("parent_id", "pid"), allow_zero=allow_zero_parent)
        if parent_from_pid
        else None
    )
    if parent_id is None and fallback_parent_id is not None:
        parent_id = fallback_parent_id
    size = _single_nonnegative_int(
        record, ("size_bytes", "size", "s", "fs", "file_size")
    )
    pickcode = _optional_pickcode(record, error_code="entry_unverified")
    normalized = {
        "is_directory": is_directory,
        "name": name,
        "directory_id": directory_id,
        "file_id": file_id,
        "parent_id": parent_id,
        "size": size,
        "pickcode": pickcode,
    }
    return parse_library_entry(normalized)


def _parse_detail(
    response: Mapping[str, Any],
    requested_id: str,
    *,
    expect_directory: bool,
    observed_entry: LibraryEntry | None,
    authorized_directory_ids: Collection[str] = (),
    observed_directories: dict[str, LibraryEntry] | None = None,
) -> LibraryEntry:
    detail = response.get("data")
    if not isinstance(detail, Mapping):
        detail = response
    # 详情响应携带类型信号(folder_count>0 目录 / play_long>0 文件)时,与调用期望
    # 冲突即失败关闭(真实 115 的 fs_info 对文件与目录都返回 file_category="0",
    # 该字段不能作为判据;marker 无法判定时放行,由身份/父链校验兜底)。
    marker = _directory_marker(detail)
    if marker is not None and marker != expect_directory:
        raise P115ReadOnlyGatewayError("detail_unverified")
    is_directory = expect_directory
    name = _single_text(detail, ("name", "n", "fn", "file_name", "category_name"))
    if name is None:
        raise P115ReadOnlyGatewayError("detail_unverified")
    identity_names = (
        ("directory_id", "category_id", "cid", "fid", "file_id")
        if expect_directory
        else ("file_id", "fid")
    )
    response_identity = _single_id(detail, identity_names)
    if _contains_any(detail, identity_names) and response_identity is None:
        raise P115ReadOnlyGatewayError("detail_unverified")
    observed_id = (
        observed_entry.directory_id if expect_directory and observed_entry else None
    ) or (observed_entry.file_id if observed_entry else None)
    paths_parent = _paths_parent(detail)
    identity = response_identity or observed_id
    if (
        identity is None
        and paths_parent is not None
        and (
            paths_parent[0] in authorized_directory_ids
            or (
                observed_directories is not None
                and paths_parent[0] in observed_directories
            )
        )
    ):
        # legacy/proapi 详情都不回显对象自身 ID;以 paths 父链(文件/目录的
        # paths 末端都是其直接父级)落在授权目录内为锚,把请求 ID 作为对象身份
        # (请求本身是 fid/cid 回显,父链是真实校验)。
        identity = requested_id
    if identity != requested_id:
        raise P115ReadOnlyGatewayError("detail_unverified")
    directory_id = identity if expect_directory else None
    file_id = None if expect_directory else identity
    parent_names = (
        ("parent_id", "pid")
        if expect_directory
        else (
            "parent_id",
            "pid",
            "cid",
        )
    )
    response_parent_id = _single_id(detail, parent_names)
    if _contains_any(detail, parent_names) and response_parent_id is None:
        raise P115ReadOnlyGatewayError("detail_unverified")
    observed_parent_id = observed_entry.parent_id if observed_entry else None
    if (
        response_parent_id is not None
        and observed_parent_id is not None
        and response_parent_id != observed_parent_id
    ):
        raise P115ReadOnlyGatewayError("detail_unverified")
    paths_parent_id: str | None = None
    if paths_parent is not None:
        paths_parent_id, paths_parent_name = paths_parent
        if observed_parent_id is not None and paths_parent_id != observed_parent_id:
            raise P115ReadOnlyGatewayError("detail_unverified")
        if (
            observed_directories is not None
            and paths_parent_id in authorized_directory_ids
        ):
            # 把父链末端目录登记为已观察目录:legacy 详情不回显目录身份,
            # 随后的 get_directory_detail(parent) 才能验证身份。
            try:
                observed_directories[paths_parent_id] = parse_library_entry(
                    {
                        "is_directory": True,
                        "name": paths_parent_name,
                        "directory_id": paths_parent_id,
                        "parent_id": None,
                        "pickcode": None,
                        "path": None,
                    }
                )
            except LibraryContractError:
                raise P115ReadOnlyGatewayError("detail_unverified") from None
    if expect_directory and observed_entry is not None:
        if observed_entry.name != name:
            raise P115ReadOnlyGatewayError("detail_unverified")
    pickcode = _optional_pickcode(detail, error_code="detail_unverified")
    if pickcode is None and observed_entry is not None:
        pickcode = observed_entry.pickcode
    normalized = {
        "is_directory": is_directory,
        "name": name,
        "directory_id": directory_id,
        "file_id": file_id,
        "parent_id": response_parent_id or paths_parent_id or observed_parent_id,
        "size": _single_nonnegative_int(
            detail, ("size_bytes", "size", "s", "fs", "file_size")
        ),
        "modified_at": _optional_timestamp(
            detail, ("modified_at", "user_utime", "ptime", "t", "te")
        ),
        "pickcode": pickcode,
        "path": None,
    }
    try:
        return parse_library_entry(normalized)
    except LibraryContractError:
        raise P115ReadOnlyGatewayError("detail_unverified") from None


def _directory_marker(record: Mapping[str, Any]) -> bool | None:
    values: list[bool] = []
    for name in ("is_dir", "is_directory"):
        if name in record:
            if not isinstance(record[name], bool):
                return None
            values.append(record[name])
    # 真实 115 实测 fc 语义随接口漂移(legacy 文件夹索引文件/目录都 fc=0,
    # legacy 文件索引文件 fc=1,proapi 列表文件 fc="0"),不能作为判据。
    # fs_info 详情中 count(目录的子树文件数)>0 或 folder_count>0 表示目录,
    # play_long>0 表示文件(纯文件目录的 folder_count=0、目录也可能有 play_long,
    # 两者单独都不可靠,count 是最稳定的目录信号)。
    if "count" in record:
        value = _nonnegative_int(record["count"])
        if value is None:
            return None
        if value > 0:
            values.append(True)
    if "folder_count" in record:
        value = _nonnegative_int(record["folder_count"])
        if value is None:
            return None
        if value > 0:
            values.append(True)
    if "play_long" in record:
        value = _nonnegative_int(record["play_long"])
        if value is None:
            return None
        if value > 0:
            values.append(False)
    if not values or len(set(values)) != 1:
        return None
    return values[0]


def _paths_parent(detail: Mapping[str, Any]) -> tuple[str, str] | None:
    """fs_info 详情的 paths 父链末端 (id, name),即该对象的直接父目录。"""
    paths = detail.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    last = paths[-1]
    if not isinstance(last, Mapping):
        return None
    file_id = _directory_id(last.get("file_id"), allow_zero=True)
    name = last.get("file_name")
    if file_id is None or not isinstance(name, str) or not name.strip():
        return None
    return file_id, name.strip()


def _single_text(record: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    values = []
    for name in names:
        if name not in record:
            continue
        value = record[name]
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value.strip())
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _optional_pickcode(
    record: Mapping[str, Any], *, error_code: str
) -> str | None:
    # p115's verified app listing uses the compact ``pc`` field.
    names = ("pickcode", "pick_code", "pc")
    value = _single_text(record, names)
    if _contains_any(record, names) and value is None and any(
        record[name] is not None for name in names if name in record
    ):
        raise P115ReadOnlyGatewayError(error_code)
    return value


def _single_id(
    record: Mapping[str, Any], names: tuple[str, ...], *, allow_zero: bool = False
) -> str | None:
    values = []
    for name in names:
        if name not in record:
            continue
        value = _directory_id(record[name], allow_zero=allow_zero)
        if value is None:
            return None
        values.append(value)
    if not values:
        return None
    return values[0] if all(value == values[0] for value in values[1:]) else None


def _single_nonnegative_int(
    record: Mapping[str, Any], names: tuple[str, ...]
) -> int | None:
    values = []
    for name in names:
        if name not in record:
            continue
        value = _nonnegative_int(record[name])
        if value is None:
            return None
        values.append(value)
    if not values:
        return None
    return values[0] if all(value == values[0] for value in values[1:]) else None


def _optional_timestamp(
    record: Mapping[str, Any], names: tuple[str, ...]
) -> int | None:
    """Keep only unambiguous epoch timestamps from optional provider metadata."""

    values: list[int] = []
    for name in names:
        if name not in record:
            continue
        value = _nonnegative_int(record[name])
        if value is None:
            return None
        values.append(value)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _contains_any(record: Mapping[str, Any], names: tuple[str, ...]) -> bool:
    return any(name in record for name in names)


def _stable_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    return (
        normalized if normalized.isdigit() and not normalized.startswith("0") else None
    )


def _directory_id(value: Any, *, allow_zero: bool = False) -> str | None:
    if allow_zero and value in (0, "0"):
        return "0"
    return _stable_id(value)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_auth_failure(response: Mapping[str, Any]) -> bool:
    for name in ("errno", "errNo", "errcode", "errCode", "code", "msg_code"):
        value = response.get(name)
        if value is None:
            continue
        if value in _AUTH_FAILURE_ERRNOS or str(value) in {
            str(item) for item in _AUTH_FAILURE_ERRNOS
        }:
            return True
    return False


def _response_success(response: Mapping[str, Any]) -> bool:
    for name in ("state", "success"):
        if name in response and not _is_success_marker(response[name]):
            return False
    for name in ("errno", "errNo"):
        if name in response and not _is_zero_errno(response[name]):
            return False
    for name in ("errcode", "errCode", "code", "msg_code"):
        if name in response and not _is_success_code(response[name]):
            return False
    return True


def _is_success_marker(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    return isinstance(value, str) and value.strip().casefold() in {"1", "true"}


def _is_success_code(value: Any) -> bool:
    if value is None or value is True:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value in {0, 200}
    return isinstance(value, str) and value.strip() in {"", "0", "200"}


def _is_zero_errno(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return isinstance(value, str) and value.strip() in {"", "0"}


def _positive_timeout(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        return None
    return float(value)


def _is_method_not_allowed(error: BaseException) -> bool:
    """Use the old interface only for a provider-level HTTP 405.

    Mirrors ``p115_c03_live_transport._is_method_not_allowed``: every other
    failure of the proapi app endpoint must fail closed instead of silently
    serving the potentially stale legacy index.
    """

    for name in ("status", "status_code", "code"):
        value = getattr(error, name, None)
        if value == 405 or value == "405":
            return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) in {405, "405"}


def _is_structured_method_not_allowed(response: object) -> bool:
    """Detect a provider-level 405 returned as a structured mapping.

    Mirrors ``p115_library_transport._is_structured_method_not_allowed``: the
    app endpoint can signal ``Method Not Allowed`` inside the JSON body rather
    than raising. Treating it as success would let ``_response_success`` pass
    and degrade into an opaque ``pagination_unverified`` failure, so the legacy
    interface fallback must also cover this shape.
    """

    if not isinstance(response, Mapping):
        return False
    return any(
        response.get(name) in {405, "405"}
        for name in ("status_code", "http_status")
    )


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "MAX_SCOPE_VERIFICATION_PAGES",
    "VERIFIED_PAGE_SIZE",
    "VIRTUAL_ROOT_PAGE_SIZE",
    "P115CredentialSource",
    "P115ReadOnlyDirectoryGateway",
    "P115ReadOnlyGatewayError",
    "P115TransportFactory",
]
