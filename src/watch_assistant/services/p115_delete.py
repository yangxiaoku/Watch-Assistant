"""Explicitly gated, postcondition-checked permanent deletion boundary."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import version
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    p115_c03_timeout_executor,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_delete,
)
from watch_assistant.adapters.p115_permanent_delete_transport import (
    P115PermanentDeleteTransport,
)
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)


class DeleteStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DeleteResult:
    status: DeleteStatus
    error_code: str | None = None


class P115DeleteService:
    """Delete only a file in the latest complete, verified library snapshot."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cookie_provider,
        *,
        client_factory: Callable[[str], Any] | None = None,
        call_executor=None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._cookie_provider = cookie_provider
        self._client_factory = client_factory or _default_client_factory
        self._call_executor = call_executor or p115_c03_timeout_executor
        self._timeout_seconds = timeout_seconds

    async def delete(
        self,
        library_id: str,
        object_id: str,
        *,
        expected_name: str,
        confirmed: bool,
        gate: Callable[[], None] | None = None,
    ) -> DeleteResult:
        if not confirmed:
            return DeleteResult(DeleteStatus.FAILED, "confirmation_required")
        if gate is not None:
            try:
                gate()
            except Exception:  # noqa: BLE001 - gate details stay private
                return DeleteResult(DeleteStatus.FAILED, "delete_gate_denied")
        async with self._session_factory() as session:
            library = await session.get(MediaLibrary, library_id)
            run = await session.scalar(
                select(LibraryScanRun)
                .where(
                    LibraryScanRun.library_id == library_id,
                    LibraryScanRun.complete.is_(True),
                    LibraryScanRun.state == "completed",
                )
                .order_by(LibraryScanRun.snapshot_revision.desc())
            )
            entry = None
            if run is not None:
                entry = await session.scalar(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id,
                        LibraryScanEntry.object_type == "file",
                        LibraryScanEntry.object_id == object_id,
                        LibraryScanEntry.is_directory.is_(False),
                    )
                )
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or run is None
            or entry is None
            or entry.parent_id is None
            or entry.name != expected_name
        ):
            return DeleteResult(DeleteStatus.FAILED, "delete_precondition_changed")
        cookie = self._cookie_provider.load()
        if not cookie:
            return DeleteResult(DeleteStatus.FAILED, "credential_unavailable")
        try:
            client = await asyncio.to_thread(self._client_factory, cookie)
        except Exception:  # noqa: BLE001 - client details stay private
            return DeleteResult(DeleteStatus.FAILED, "client_unavailable")
        try:
            transport = P115PermanentDeleteTransport(
                client, call_executor=self._call_executor
            )
            listing = await transport.list_children(
                entry.parent_id, timeout_seconds=self._timeout_seconds
            )
            if not listing.complete:
                return DeleteResult(DeleteStatus.FAILED, "observation_unverified")
            matches = [item for item in listing.entries if item.file_id == object_id]
            if len(matches) != 1 or matches[0].name != expected_name:
                return DeleteResult(DeleteStatus.FAILED, "delete_precondition_changed")
            before_recycle = await transport.list_entries(
                timeout_seconds=self._timeout_seconds
            )
            if before_recycle is None:
                return DeleteResult(DeleteStatus.UNCERTAIN, "recycle_listing_unverified")
            before_recycle_ids = {item.recycle_id for item in before_recycle}
            reversible = await transport.move_to_recycle(
                prepare_delete(object_id), timeout_seconds=self._timeout_seconds
            )
            if reversible.status is not WriteStatus.SUCCESS:
                return DeleteResult(DeleteStatus.UNCERTAIN, "recycle_outcome_unknown")
            recycle_matches = await transport.find_new_entries(
                before_recycle_ids,
                parent_id=entry.parent_id,
                name=expected_name,
                size_bytes=entry.size_bytes,
                timeout_seconds=self._timeout_seconds,
            )
            if recycle_matches is None:
                return DeleteResult(DeleteStatus.UNCERTAIN, "recycle_listing_unverified")
            if len(recycle_matches) != 1:
                return DeleteResult(DeleteStatus.UNCERTAIN, "recycle_entry_unverified")
            permanent = await transport.permanently_clean(
                recycle_matches[0].recycle_id,
                timeout_seconds=self._timeout_seconds,
            )
            if permanent.status is not WriteStatus.SUCCESS:
                return DeleteResult(DeleteStatus.UNCERTAIN, "permanent_delete_unconfirmed")
            after = await transport.list_children(
                entry.parent_id, timeout_seconds=self._timeout_seconds
            )
            if not after.complete:
                return DeleteResult(DeleteStatus.UNCERTAIN, "postcondition_unverified")
            if any(item.file_id == object_id for item in after.entries):
                return DeleteResult(DeleteStatus.UNCERTAIN, "postcondition_mismatch")
            recycle_absent = await transport.wait_until_absent(
                recycle_matches[0].recycle_id,
                timeout_seconds=self._timeout_seconds,
            )
            if recycle_absent is not True:
                return DeleteResult(
                    DeleteStatus.UNCERTAIN,
                    "permanent_delete_postcondition_unverified",
                )
            return DeleteResult(DeleteStatus.SUCCESS)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return DeleteResult(DeleteStatus.UNCERTAIN, "timeout")
        except Exception:  # noqa: BLE001 - remote details stay private
            return DeleteResult(DeleteStatus.UNCERTAIN, "outcome_unknown")
        finally:
            await _close_client(client)


def _default_client_factory(cookie: str) -> Any:
    if version("p115client") != EXPECTED_P115CLIENT_VERSION:
        raise RuntimeError("unsupported_p115client_version")
    from p115client import P115Client

    return P115Client(cookie)


async def _close_client(client: Any) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = ["DeleteResult", "DeleteStatus", "P115DeleteService"]
