"""Verified, reversible cleanup of directories emptied by organization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from enum import StrEnum

from watch_assistant.adapters.p115_c03_live_transport import (
    P115C03CallExecutor,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_recycle,
)


class EmptyDirectoryCleanupStatus(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class EmptyDirectoryCleanupError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = True) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


LeaseCheck = Callable[[], Awaitable[bool]]


class LiveP115EmptyDirectoryCleaner:
    """Recycle one directory only after two complete observations prove empty."""

    def __init__(
        self,
        *,
        client,
        call_executor: P115C03CallExecutor,
        managed_directory_ids: Collection[str],
        system_created_directory_ids: Collection[str],
        scope_confirmed: bool,
        timeout_seconds: float = 30.0,
    ) -> None:
        if scope_confirmed is not True:
            raise ValueError("invalid_cleanup_scope")
        scope = frozenset(managed_directory_ids)
        if not scope or any(not _stable_id(item) for item in scope):
            raise ValueError("invalid_cleanup_scope")
        system_created = frozenset(system_created_directory_ids)
        if any(not _stable_id(item) for item in system_created):
            raise ValueError("invalid_cleanup_scope")
        if timeout_seconds <= 0:
            raise ValueError("invalid_timeout")
        self._scope = scope
        self._system_created = system_created
        self._timeout_seconds = float(timeout_seconds)
        self._c03 = P115C03LiveTransport(client, call_executor=call_executor)

    async def cleanup(
        self,
        directory_id: str,
        parent_id: str,
        name: str,
        *,
        lease_check: LeaseCheck | None = None,
    ) -> EmptyDirectoryCleanupStatus:
        if (
            not _stable_id(directory_id)
            or not _stable_id(parent_id)
            or parent_id not in self._scope
            or directory_id not in self._scope
            or directory_id not in self._system_created
            or not _safe_name(name)
        ):
            raise EmptyDirectoryCleanupError("cleanup_scope_unverified")
        await _raise_if_lease_lost(lease_check)
        parent_listing = await self._c03.list_children(
            parent_id, timeout_seconds=self._timeout_seconds
        )
        await _raise_if_lease_lost(lease_check)
        if not parent_listing.complete:
            raise EmptyDirectoryCleanupError("cleanup_observation_unverified")
        matches = [
            entry
            for entry in parent_listing.entries
            if entry.file_id == directory_id
            and entry.parent_id == parent_id
            and entry.name == name
            and entry.is_directory
        ]
        if not matches:
            return EmptyDirectoryCleanupStatus.SKIPPED
        if len(matches) != 1:
            raise EmptyDirectoryCleanupError("cleanup_observation_unverified")
        await _raise_if_lease_lost(lease_check)
        child_listing = await self._c03.list_children(
            directory_id, timeout_seconds=self._timeout_seconds
        )
        await _raise_if_lease_lost(lease_check)
        if not child_listing.complete:
            raise EmptyDirectoryCleanupError("cleanup_observation_unverified")
        if child_listing.entries:
            return EmptyDirectoryCleanupStatus.SKIPPED
        await _raise_if_lease_lost(lease_check)
        receipt = await self._c03.execute(
            prepare_recycle(directory_id), timeout_seconds=self._timeout_seconds
        )
        await _raise_if_lease_lost(lease_check)
        if receipt.status is WriteStatus.UNCERTAIN:
            raise EmptyDirectoryCleanupError("cleanup_outcome_unknown")
        if receipt.status is not WriteStatus.SUCCESS:
            raise EmptyDirectoryCleanupError("cleanup_remote_failed", uncertain=False)
        await _raise_if_lease_lost(lease_check)
        after_listing = await self._c03.list_children(
            parent_id, timeout_seconds=self._timeout_seconds
        )
        await _raise_if_lease_lost(lease_check)
        if not after_listing.complete:
            raise EmptyDirectoryCleanupError("cleanup_postcondition_unverified")
        if any(entry.file_id == directory_id for entry in after_listing.entries):
            raise EmptyDirectoryCleanupError("cleanup_postcondition_mismatch")
        return EmptyDirectoryCleanupStatus.SUCCESS


async def _raise_if_lease_lost(lease_check: LeaseCheck | None) -> None:
    if lease_check is not None and not await lease_check():
        raise EmptyDirectoryCleanupError("cleanup_lease_lost")


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit() and not value.startswith("0")


def _safe_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 255
        and value not in {".", ".."}
        and not any(char in value for char in ("/", "\\", "\x00"))
    )


__all__ = [
    "EmptyDirectoryCleanupError",
    "EmptyDirectoryCleanupStatus",
    "LiveP115EmptyDirectoryCleaner",
]
