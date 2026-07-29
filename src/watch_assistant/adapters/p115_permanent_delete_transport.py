"""Bounded two-phase P115 permanent-delete transport.

``fs_delete`` only moves an object to the recycle bin.  This adapter keeps
that reversible step separate from ``recyclebin_clean`` and exposes only
redacted, stable DTOs to callers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from watch_assistant.adapters.p115_c03_fixture_probe import C03WriteReceipt
from watch_assistant.adapters.p115_c03_live_transport import (
    P115C03CallExecutor,
    P115C03LiveTransport,
    _call,
    _response_success,
)
from watch_assistant.adapters.p115_library_write_contract import (
    PreparedWrite,
    WriteOperation,
    WriteStatus,
)


class P115PermanentDeleteClient(Protocol):
    def recyclebin_list(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...

    def recyclebin_clean(self, payload: Mapping[str, int | str], **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True, repr=False)
class RecycleBinEntry:
    recycle_id: str
    parent_id: str
    name: str
    size_bytes: int | None = None

    def __repr__(self) -> str:
        return "RecycleBinEntry(<redacted>)"


class P115PermanentDeleteTransport:
    """Use a fixed client and timeout executor for one scoped deletion."""

    def __init__(
        self,
        client: P115PermanentDeleteClient,
        *,
        call_executor: P115C03CallExecutor,
    ) -> None:
        self._client = client
        self._call_executor = call_executor
        self._reversible = P115C03LiveTransport(
            client, call_executor=call_executor  # type: ignore[arg-type]
        )

    async def move_to_recycle(
        self, request: PreparedWrite, *, timeout_seconds: float
    ) -> C03WriteReceipt:
        if request.operation is not WriteOperation.DELETE:
            return C03WriteReceipt(WriteStatus.UNCERTAIN)
        return await self._reversible.execute(request, timeout_seconds=timeout_seconds)

    async def list_children(self, parent_id: str, *, timeout_seconds: float):
        return await self._reversible.list_children(
            parent_id, timeout_seconds=timeout_seconds
        )

    async def list_entries(self, *, timeout_seconds: float) -> tuple[RecycleBinEntry, ...] | None:
        response = await _call(
            self._call_executor,
            self._client.recyclebin_list,
            {"aid": 7, "cid": 0, "limit": 100, "offset": 0},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response, Mapping) or not _response_success(response):
            return None
        data = response.get("data")
        if data is None and response.get("count") in {0, "0"}:
            return ()
        if not isinstance(data, list):
            return None
        entries: list[RecycleBinEntry] = []
        for record in data:
            entry = _normalize_entry(record)
            if entry is None:
                # The recycle-bin root can contain a synthetic ``cid=0``
                # record. It cannot identify a deleted file and must not
                # make an otherwise complete listing unusable.
                if isinstance(record, Mapping) and record.get("cid") in {0, "0"}:
                    continue
                return None
            entries.append(entry)
        return tuple(entries)

    async def find_new_entries(
        self,
        before_recycle_ids: set[str],
        *,
        parent_id: str,
        name: str,
        size_bytes: int | None,
        timeout_seconds: float,
    ) -> tuple[RecycleBinEntry, ...] | None:
        """Poll briefly for the provider's eventually-consistent new record."""

        deadline = time.monotonic() + min(timeout_seconds, 8.0)
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            entries = await self.list_entries(timeout_seconds=remaining)
            if entries is None:
                return None
            matches = tuple(
                item
                for item in entries
                if item.recycle_id not in before_recycle_ids
                and item.parent_id == parent_id
                and item.name == name
                and (
                    size_bytes is None
                    or item.size_bytes is None
                    or item.size_bytes == size_bytes
                )
            )
            if matches or time.monotonic() >= deadline:
                return matches
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    async def wait_until_absent(
        self, recycle_id: str, *, timeout_seconds: float
    ) -> bool | None:
        """Wait briefly for a cleaned record to leave the eventual-consistent list."""

        deadline = time.monotonic() + min(timeout_seconds, 8.0)
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            entries = await self.list_entries(timeout_seconds=remaining)
            if entries is None:
                return None
            if all(item.recycle_id != recycle_id for item in entries):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    async def permanently_clean(
        self, recycle_id: str, *, timeout_seconds: float
    ) -> C03WriteReceipt:
        if not _stable_id(recycle_id):
            return C03WriteReceipt(WriteStatus.UNCERTAIN)
        response = await _call(
            self._call_executor,
            self._client.recyclebin_clean,
            {"tid": recycle_id},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response, Mapping) or not _response_success(response):
            return C03WriteReceipt(WriteStatus.UNCERTAIN)
        return C03WriteReceipt(WriteStatus.SUCCESS)


def _normalize_entry(record: object) -> RecycleBinEntry | None:
    if not isinstance(record, Mapping):
        return None
    recycle_id = _single_id(record, ("id", "rid"))
    # The recycle-bin ``cid`` is the former parent directory, not the deleted
    # file ID.  The provider does not expose a stable original file ID here.
    parent_id = _single_id(record, ("cid", "parent_id", "pid"))
    name = record.get("file_name", record.get("name"))
    if recycle_id is None or parent_id is None or not _safe_name(name):
        return None
    size = record.get("file_size", record.get("size"))
    size_bytes = _nonnegative_int(size) if size is not None else None
    if size is not None and size_bytes is None:
        return None
    return RecycleBinEntry(recycle_id, parent_id, name, size_bytes)


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


def _safe_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 255 and "\x00" not in value and "/" not in value and "\\" not in value


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _stable_id(value: object) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool) and str(value).isdigit() and not str(value).startswith("0")


__all__ = ["P115PermanentDeleteTransport", "RecycleBinEntry"]
