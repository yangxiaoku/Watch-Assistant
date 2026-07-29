"""Offline-first contracts for the read-only 115 library surface.

This module deliberately has no p115client import and no network client.  A
future adapter may translate the fixed client's responses into these DTOs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class LibraryContractError(ValueError):
    """A third-party response did not satisfy the offline contract."""


class ScanState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class LibraryEntry:
    """A sanitized directory entry or detail record.

    ``path`` and ``pickcode`` are intentionally presentation/streaming
    attributes.  They are not identity fields and are hidden from repr.
    """

    directory_id: str | None
    file_id: str | None
    parent_id: str | None
    name: str
    is_directory: bool
    size_bytes: int | None
    modified_at: datetime | None
    pickcode: str | None
    path: str | None = None

    def __repr__(self) -> str:
        return (
            "LibraryEntry(directory_id=<redacted>, file_id=<redacted>, "
            "parent_id=<redacted>, name=<redacted>, is_directory="
            f"{self.is_directory!r}, size_bytes={self.size_bytes!r}, "
            f"modified_at={self.modified_at!r})"
        )


FileDetail = LibraryEntry
DirectoryDetail = LibraryEntry


@dataclass(frozen=True, slots=True, repr=False)
class DirectoryPage:
    items: tuple[LibraryEntry, ...]
    page: int
    page_count: int | None
    total: int | None
    scan_complete: bool | None
    state: ScanState = ScanState.COMPLETE
    error_code: str | None = None
    has_more: bool | None = None
    next_page: int | None = None
    terminal: bool | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise LibraryContractError("invalid_page")
        if self.page_count is not None and self.page_count < 1:
            raise LibraryContractError("invalid_page_count")
        if self.total is not None and self.total < 0:
            raise LibraryContractError("invalid_total")
        if self.state == ScanState.COMPLETE and self.scan_complete is False:
            raise LibraryContractError("invalid_scan_state")
        if self.state != ScanState.COMPLETE and self.scan_complete is True:
            raise LibraryContractError("invalid_scan_state")
        if self.has_more is not None and not isinstance(self.has_more, bool):
            raise LibraryContractError("invalid_pagination")
        if self.next_page is not None and self.next_page < 1:
            raise LibraryContractError("invalid_next_page")
        if self.terminal is not None and not isinstance(self.terminal, bool):
            raise LibraryContractError("invalid_pagination")

    def __repr__(self) -> str:
        return (
            f"DirectoryPage(item_count={len(self.items)}, page={self.page}, "
            f"page_count={self.page_count!r}, total={self.total!r}, "
            f"scan_complete={self.scan_complete!r}, state={self.state.value!r})"
        )


ScanPage = DirectoryPage


@dataclass(frozen=True, slots=True, repr=False)
class ScanResult:
    items: tuple[LibraryEntry, ...]
    pages_read: int
    page_count: int | None
    total: int | None
    scan_complete: bool
    state: ScanState
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            f"ScanResult(item_count={len(self.items)}, pages_read={self.pages_read}, "
            f"page_count={self.page_count!r}, total={self.total!r}, "
            f"scan_complete={self.scan_complete!r}, state={self.state.value!r})"
        )


class P115LibraryGateway(Protocol):
    """Read-only gateway boundary; no write methods are part of this protocol."""

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage: ...

    async def get_file_detail(self, file_id: str) -> FileDetail: ...

    async def get_directory_detail(self, directory_id: str) -> DirectoryDetail: ...


@dataclass(frozen=True, slots=True)
class GatewayCall:
    method: str
    page: int | None = None
    page_size: int | None = None


class FakeP115LibraryGateway:
    """Deterministic fake used by contract tests and offline probes."""

    def __init__(
        self,
        pages: Mapping[int, DirectoryPage],
        *,
        file_details: Mapping[str, FileDetail] | None = None,
        directory_details: Mapping[str, DirectoryDetail] | None = None,
        fail_page: int | None = None,
    ) -> None:
        self._pages = dict(pages)
        self._file_details = dict(file_details or {})
        self._directory_details = dict(directory_details or {})
        self._fail_page = fail_page
        self.calls: list[GatewayCall] = []

    def __repr__(self) -> str:
        return f"FakeP115LibraryGateway(page_count={len(self._pages)})"

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        del directory_id
        self.calls.append(GatewayCall("list_directory", page, page_size))
        if self._fail_page == page:
            raise OSError("gateway request failed")
        if page not in self._pages:
            raise LibraryContractError("page_not_found")
        return self._pages[page]

    async def get_file_detail(self, file_id: str) -> FileDetail:
        self.calls.append(GatewayCall("get_file_detail"))
        detail = self._file_details.get(file_id)
        if detail is None:
            raise LibraryContractError("file_not_found")
        return detail

    async def get_directory_detail(self, directory_id: str) -> DirectoryDetail:
        self.calls.append(GatewayCall("get_directory_detail"))
        detail = self._directory_details.get(directory_id)
        if detail is None:
            raise LibraryContractError("directory_not_found")
        return detail


def _value(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _id_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LibraryContractError("invalid_id")
    normalized = str(value).strip()
    if not normalized or (normalized.startswith("-") and normalized[1:].isdigit()):
        raise LibraryContractError("invalid_id")
    return normalized


def _size_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LibraryContractError("invalid_size")
    return value


def _timestamp_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise LibraryContractError("invalid_timestamp") from exc
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise LibraryContractError("invalid_timestamp") from exc
    else:
        raise LibraryContractError("invalid_timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LibraryContractError("invalid_timestamp")
    return parsed.astimezone(UTC)


def parse_library_entry(raw: Mapping[str, Any]) -> LibraryEntry:
    """Map one fixed-client record without retaining the raw mapping."""

    if not isinstance(raw, Mapping):
        raise LibraryContractError("invalid_entry")
    is_directory = _value(raw, "is_directory", "is_dir")
    if not isinstance(is_directory, bool):
        raise LibraryContractError("invalid_entry")
    name = _value(raw, "name", "file_name")
    if not isinstance(name, str) or not name.strip():
        raise LibraryContractError("invalid_name")
    directory_id = _id_value(_value(raw, "directory_id", "cid"))
    file_id = _id_value(_value(raw, "file_id", "fid"))
    if is_directory and directory_id is None:
        directory_id = file_id
    if not is_directory and file_id is None:
        raise LibraryContractError("missing_file_id")
    parent_id = _id_value(_value(raw, "parent_id", "pid"))
    pickcode = _value(raw, "pickcode", "pick_code")
    if pickcode is not None and (not isinstance(pickcode, str) or not pickcode.strip()):
        raise LibraryContractError("invalid_pickcode")
    path = _value(raw, "path")
    if path is not None and not isinstance(path, str):
        raise LibraryContractError("invalid_path")
    return LibraryEntry(
        directory_id=directory_id,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        size_bytes=_size_value(_value(raw, "size_bytes", "size")),
        modified_at=_timestamp_value(_value(raw, "modified_at", "user_utime")),
        pickcode=pickcode,
        path=path,
    )


def parse_directory_page(raw: Mapping[str, Any]) -> DirectoryPage:
    """Map a fixed-client list response into a page DTO."""

    if not isinstance(raw, Mapping):
        raise LibraryContractError("invalid_page")
    body = raw.get("data", raw)
    if not isinstance(body, Mapping):
        raise LibraryContractError("invalid_page")
    if "list" in body:
        raw_items = body["list"]
    elif "items" in body:
        raw_items = body["items"]
    else:
        raise LibraryContractError("missing_items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise LibraryContractError("invalid_items")
    entries = tuple(parse_library_entry(item) for item in raw_items)
    page = _value(body, "page", "page_no")
    page_count = _value(body, "page_count", "pagecount", "pages")
    total = _value(body, "total", "count")
    if not isinstance(page, int) or isinstance(page, bool):
        raise LibraryContractError("invalid_page")
    if page_count is not None and (
        not isinstance(page_count, int) or isinstance(page_count, bool)
    ):
        raise LibraryContractError("invalid_page_count")
    if total is not None and (not isinstance(total, int) or isinstance(total, bool)):
        raise LibraryContractError("invalid_total")
    scan_complete = body.get("scan_complete")
    if scan_complete is not None and not isinstance(scan_complete, bool):
        raise LibraryContractError("invalid_scan_state")
    state_value = _value(body, "state", "scan_state")
    try:
        state = ScanState(
            state_value
            or (ScanState.PARTIAL if scan_complete is False else ScanState.COMPLETE)
        )
    except ValueError as exc:
        raise LibraryContractError("invalid_scan_state") from exc
    has_more = body.get("has_more")
    if has_more is not None and not isinstance(has_more, bool):
        raise LibraryContractError("invalid_pagination")
    next_page = body.get("next_page")
    if next_page is not None and (
        not isinstance(next_page, int) or isinstance(next_page, bool) or next_page < 1
    ):
        raise LibraryContractError("invalid_next_page")
    terminal = body.get("terminal")
    if terminal is not None and not isinstance(terminal, bool):
        raise LibraryContractError("invalid_pagination")
    return DirectoryPage(
        items=entries,
        page=page,
        page_count=page_count,
        total=total,
        scan_complete=scan_complete,
        state=state,
        error_code="partial_page" if state == ScanState.PARTIAL else None,
        has_more=has_more,
        next_page=next_page,
        terminal=terminal,
    )


def parse_file_detail(raw: Mapping[str, Any]) -> FileDetail:
    """Normalize one file detail response into the same safe entry DTO."""

    entry = parse_library_entry(raw)
    if entry.is_directory or entry.file_id is None:
        raise LibraryContractError("invalid_file_detail")
    return entry


def parse_directory_detail(raw: Mapping[str, Any]) -> DirectoryDetail:
    """Normalize one directory detail response into the same safe entry DTO."""

    entry = parse_library_entry(raw)
    if not entry.is_directory or entry.directory_id is None:
        raise LibraryContractError("invalid_directory_detail")
    return entry


async def scan_directory(
    gateway: P115LibraryGateway,
    directory_id: str,
    *,
    page_size: int = 100,
) -> ScanResult:
    """Read pages until complete, conservatively marking anomalies partial."""

    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError("invalid_page_size")
    items: list[LibraryEntry] = []
    seen_pages: set[int] = set()
    seen_entries: set[tuple[str | None, str | None]] = set()
    expected_page_count: int | None = None
    page_count_seen = False
    total: int | None = None
    total_seen = False
    pages_read = 0
    page_number = 1
    while True:
        try:
            page = await gateway.list_directory(
                directory_id, page=page_number, page_size=page_size
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sanitize all gateway failures
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "gateway_error",
            )
        if page.page != page_number or page.page in seen_pages:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "repeated_page",
            )
        seen_pages.add(page.page)
        pages_read += 1
        if not page_count_seen:
            expected_page_count = page.page_count
            page_count_seen = True
        elif page.page_count != expected_page_count:
            return ScanResult(
                tuple(items),
                pages_read,
                page.page_count,
                page.total,
                False,
                ScanState.PARTIAL,
                "page_count_changed",
            )
        if total_seen and page.total != total:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                page.total,
                False,
                ScanState.PARTIAL,
                "total_changed",
            )
        if not total_seen:
            total = page.total
            total_seen = True
        if page.state != ScanState.COMPLETE or page.scan_complete is False:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                page.state,
                page.error_code or "partial_page",
            )
        page_count_terminal = (
            expected_page_count is not None and page_number == expected_page_count
        )
        page_count_continues = (
            expected_page_count is not None and page_number < expected_page_count
        )
        explicit_terminal = page.terminal is True or page.has_more is False
        pagination_signal_conflict = (
            (page.terminal is True and page.has_more is True)
            or (page.terminal is True and page.next_page is not None)
            or (page.has_more is False and page.next_page is not None)
            or (page.terminal is False and page.has_more is False)
            or (page.next_page is not None and page.next_page != page_number + 1)
            or (
                page_count_terminal
                and (
                    page.terminal is False
                    or page.has_more is True
                    or page.next_page is not None
                )
            )
            or (
                page_count_continues
                and (page.terminal is True or page.has_more is False)
            )
            or (expected_page_count is not None and page_number > expected_page_count)
        )
        if pagination_signal_conflict:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "pagination_unverified",
            )
        if (
            not page.items
            and (
                expected_page_count is None
                or page_number < expected_page_count
                or (page.total not in (None, 0))
            )
            and page.terminal is not True
            and page.has_more is not False
        ):
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "pagination_unverified"
                if expected_page_count is None
                and page.has_more is None
                and page.next_page is None
                and page.terminal is None
                else "empty_page",
            )
        for item in page.items:
            identity = (item.directory_id, item.file_id)
            if identity in seen_entries:
                return ScanResult(
                    tuple(items),
                    pages_read,
                    expected_page_count,
                    total,
                    False,
                    ScanState.PARTIAL,
                    "repeated_entry",
                )
            seen_entries.add(identity)
            items.append(item)
        pagination_continues = (
            page_count_continues or page.has_more is True or page.next_page is not None
        )
        if page_count_terminal or explicit_terminal:
            if total is not None and len(items) != total:
                return ScanResult(
                    tuple(items),
                    pages_read,
                    expected_page_count,
                    total,
                    False,
                    ScanState.PARTIAL,
                    "total_mismatch",
                )
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                True,
                ScanState.COMPLETE,
            )
        if not pagination_continues:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "pagination_unverified",
            )
        next_page = page.next_page if page.next_page is not None else page_number + 1
        if next_page <= page_number or next_page in seen_pages:
            return ScanResult(
                tuple(items),
                pages_read,
                expected_page_count,
                total,
                False,
                ScanState.PARTIAL,
                "pagination_unverified",
            )
        page_number = next_page


@dataclass(frozen=True, slots=True)
class ProbeReport:
    mode: str
    scan: ScanResult


async def run_read_only_probe(
    gateway: P115LibraryGateway,
    directory_id: str,
    *,
    page_size: int = 100,
) -> ProbeReport:
    """Offline probe seam; callers must inject a gateway and no client is built."""

    return ProbeReport(
        mode="offline_only",
        scan=await scan_directory(gateway, directory_id, page_size=page_size),
    )
