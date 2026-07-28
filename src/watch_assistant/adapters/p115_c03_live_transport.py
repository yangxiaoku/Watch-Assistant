"""Narrow live transport for the already-audited C03 probe.

The caller owns client construction and credentials.  This adapter only calls
the fixed ``p115client`` methods required by C03 and immediately reduces each
response to the probe DTOs.  It never retains or renders a third-party
response.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from watch_assistant.adapters.p115_c03_fixture_probe import (
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
MAX_FS_FILES_PAGE_CALLS = 4
VERIFIED_FS_FILES_PAGE_SIZE = 1


class P115ClientLike(Protocol):
    def fs_mkdir(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_move(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_rename(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_delete(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_info(self, payload: Mapping[str, str], **kwargs: Any) -> Any: ...

    def fs_files(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...


P115C03CallExecutor = Callable[..., Awaitable[Any] | Any]


class P115C03CallTimeoutUnavailable(RuntimeError):
    """The injected client seam cannot enforce a call-level timeout."""


class P115C03LiveTransport(P115C03Transport):
    """Translate only fixed C03 calls through a caller-created client."""

    def __init__(
        self,
        client: P115ClientLike,
        *,
        call_executor: P115C03CallExecutor | None = None,
    ) -> None:
        self._client = client
        self._call_executor = call_executor

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
        }.get(request.operation)
        payload = _client_payload(request)
        if method_name is None or payload is None:
            return C03WriteReceipt(WriteStatus.UNCERTAIN)
        response = await _call(
            self._call_executor,
            getattr(self._client, method_name),
            payload,
            timeout_seconds=timeout_seconds,
        )
        return _normalize_write_response(request.operation, response)

    async def read(
        self, file_id: str, *, timeout_seconds: float
    ) -> C03RemoteEntry | None:
        if self._call_executor is None:
            raise P115C03CallTimeoutUnavailable
        response = await _call(
            self._call_executor,
            self._client.fs_info,
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
        for page_calls in range(1, MAX_FS_FILES_PAGE_CALLS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls - 1
                )
            try:
                response = await _call(
                    self._call_executor,
                    self._client.fs_files,
                    {
                        "cid": parent_id,
                        "limit": VERIFIED_FS_FILES_PAGE_SIZE,
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
            page = _normalize_files_page(response, parent_id, offset)
            if page is None:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            page_entries, total = page
            known_ids = {entry.file_id for entry in entries}
            if any(entry.file_id in known_ids for entry in page_entries):
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            entries.extend(page_entries)
            if offset + len(page_entries) == total:
                return C03DirectoryListing(
                    tuple(entries), complete=True, page_calls=page_calls
                )
            if not page_entries:
                return C03DirectoryListing(
                    tuple(entries), complete=False, page_calls=page_calls
                )
            offset += len(page_entries)
        return C03DirectoryListing(
            tuple(entries), complete=False, page_calls=MAX_FS_FILES_PAGE_CALLS
        )


async def _call(
    call_executor: P115C03CallExecutor | None,
    method: Callable[..., Any],
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Run one call only through a seam that owns call-level timeout."""

    if call_executor is None:
        raise P115C03CallTimeoutUnavailable
    result = call_executor(
        method,
        dict(payload),
        timeout_seconds=timeout_seconds,
    )
    if inspect.isawaitable(result):
        return await result
    return result


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
    if request.operation is WriteOperation.RECYCLE:
        if set(payload) != {"file_id"}:
            return None
        return {"fid": payload["file_id"]}
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
    file_id = _single_id(detail, ("cid", "directory_id", "fid", "file_id"))
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
    response: Any, parent_id: str, expected_offset: int
) -> tuple[tuple[C03RemoteEntry, ...], int] | None:
    if not isinstance(response, Mapping) or not _response_success(response):
        return None
    offset = _integer(response.get("offset"))
    limit = _integer(response.get("limit"))
    total = _integer(response.get("count"))
    records = _records(response)
    if (
        offset != expected_offset
        or limit != VERIFIED_FS_FILES_PAGE_SIZE
        or total is None
        or total < 0
        or records is None
        or len(records) > VERIFIED_FS_FILES_PAGE_SIZE
        or expected_offset > total
        or expected_offset + len(records) > total
        or len(records) != min(VERIFIED_FS_FILES_PAGE_SIZE, total - expected_offset)
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
    record_parent_id = _single_id(record, ("parent_id", "pid"))
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


__all__ = [
    "EXPECTED_P115CLIENT_VERSION",
    "MAX_FS_FILES_PAGE_CALLS",
    "VERIFIED_FS_FILES_PAGE_SIZE",
    "P115C03CallExecutor",
    "P115C03CallTimeoutUnavailable",
    "P115C03LiveTransport",
    "P115ClientLike",
]
