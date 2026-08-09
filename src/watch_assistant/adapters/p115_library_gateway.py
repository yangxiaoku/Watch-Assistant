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
# 目录选择器批量读取的已验证页大小：2026-08-09 在真实 115 上对非虚拟根目录
# 实证 fs_files limit=50 返回结构/分页与 limit=1 一致（见 probe 证据），
# 作为与 VERIFIED_PAGE_SIZE 并列的另一条已验证路径。
VERIFIED_BATCH_PAGE_SIZE = 50
VIRTUAL_ROOT_PAGE_SIZE = 50
VIRTUAL_ROOT_RESPONSE_LIMIT_DELTA = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_SCOPE_VERIFICATION_PAGES = 10_000


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
        """Read one proven-safe page or fail closed before exposing entries."""

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
        result = _parse_page(
            response,
            page=page,
            offset=offset,
            allow_zero_parent=(
                is_virtual_root
            ),
            expected_limit=request_page_size,
            virtual_root=is_virtual_root,
        )
        if any(entry.parent_id != normalized_directory_id for entry in result.items):
            raise P115ReadOnlyGatewayError("entry_scope_unverified")
        for entry in result.items:
            if entry.is_directory and entry.directory_id is not None:
                self._observed_directories[entry.directory_id] = entry
            elif entry.file_id is not None:
                self._observed_files[entry.file_id] = entry
        return result

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
        )

    async def _call(
        self,
        method_name: str,
        payload: Mapping[str, int | str],
        *,
        deadline: float,
    ) -> Mapping[str, Any]:
        transport = await self._get_transport(deadline)
        remaining = self._remaining(deadline)
        try:
            if method_name == "fs_files":
                response = await transport.fs_files(payload, timeout_seconds=remaining)
            elif method_name == "fs_info":
                response = await transport.fs_info(payload, timeout_seconds=remaining)
            else:
                raise P115ReadOnlyTransportUnavailable("blocked_environment")
        except asyncio.CancelledError:
            raise
        except P115ReadOnlyTransportUnavailable:
            raise P115ReadOnlyGatewayError("blocked_environment") from None
        except TimeoutError:
            raise P115ReadOnlyGatewayError(f"{method_name}_timeout") from None
        except Exception:  # noqa: BLE001 - do not expose provider error details
            raise P115ReadOnlyGatewayError(f"{method_name}_failed") from None
        if not isinstance(response, Mapping):
            raise P115ReadOnlyGatewayError("malformed_response")
        if not _response_success(response):
            raise P115ReadOnlyGatewayError("remote_failed")
        return response

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


def _parse_page(
    response: Mapping[str, Any],
    *,
    page: int,
    offset: int,
    allow_zero_parent: bool = False,
    expected_limit: int = VERIFIED_PAGE_SIZE,
    virtual_root: bool = False,
) -> DirectoryPage:
    records = _records(response)
    response_offset = _integer(response.get("offset"))
    response_limit = _integer(response.get("limit"))
    total = _integer(response.get("count"))
    expected_response_limit = (
        expected_limit - VIRTUAL_ROOT_RESPONSE_LIMIT_DELTA
        if virtual_root
        else expected_limit
    )
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

    try:
        entries = tuple(
            _parse_entry(record, allow_zero_parent=allow_zero_parent)
            for record in records
        )
    except P115ReadOnlyGatewayError:
        raise
    except LibraryContractError:
        raise P115ReadOnlyGatewayError("entry_unverified") from None

    terminal = offset + len(entries) == total
    return DirectoryPage(
        items=entries,
        page=page,
        page_count=None,
        total=total,
        scan_complete=True if terminal else None,
        state=ScanState.COMPLETE,
        has_more=not terminal,
        next_page=None if terminal else page + 1,
        terminal=terminal,
    )


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
    record: Mapping[str, Any], *, allow_zero_parent: bool = False
) -> LibraryEntry:
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
    elif file_id is None:
        raise P115ReadOnlyGatewayError("entry_unverified")
    parent_names = ("parent_id", "pid") if is_directory else ("parent_id", "pid", "cid")
    parent_id = _single_id(record, parent_names, allow_zero=allow_zero_parent)
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
) -> LibraryEntry:
    detail = response.get("data")
    if not isinstance(detail, Mapping):
        detail = response
    is_directory = _directory_marker(detail)
    if is_directory is not expect_directory:
        raise P115ReadOnlyGatewayError("detail_unverified")
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
    identity = response_identity or observed_id
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
    pickcode = _optional_pickcode(detail, error_code="detail_unverified")
    if pickcode is None and observed_entry is not None:
        pickcode = observed_entry.pickcode
    normalized = {
        "is_directory": is_directory,
        "name": name,
        "directory_id": directory_id,
        "file_id": file_id,
        "parent_id": response_parent_id or observed_parent_id,
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
