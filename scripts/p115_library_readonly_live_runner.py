"""Explicit, bounded C02 live-read acceptance runner.

This runner is not wired into the application. It is disabled by default and
only permits a small, allowlisted sequence of ``fs_files`` and ``fs_info``
calls against one non-root directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from watch_assistant.adapters.p115_library import DirectoryPage
from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
    P115TransportFactory,
)
from watch_assistant.adapters.p115_library_transport import (
    create_p115_readonly_transport,
)
from watch_assistant.services.p115_credentials import normalize_cookie_text

LIVE_ENV = "WATCH_ASSISTANT_P115_LIBRARY_READONLY_LIVE"
MAX_COOKIE_BYTES = 16 * 1024
MAX_DIRECTORY_PAGES = 2
MAX_DETAIL_CALLS = 2
MAX_RUN_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class C02ReadOnlyReport:
    status: str
    error_code: str | None
    pages_read: int
    entries_seen: int
    file_details: int
    directory_details: int
    complete: bool

    def __repr__(self) -> str:
        return (
            "C02ReadOnlyReport("
            f"status={self.status!r}, error_code={self.error_code!r}, "
            f"pages_read={self.pages_read}, entries_seen={self.entries_seen}, "
            f"file_details={self.file_details}, "
            f"directory_details={self.directory_details}, complete={self.complete})"
        )

    def to_public_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "status": self.status,
            "error_code": self.error_code,
            "pages_read": self.pages_read,
            "entries_seen": self.entries_seen,
            "file_details": self.file_details,
            "directory_details": self.directory_details,
            "complete": self.complete,
        }


class _CookiePathSource:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self) -> str | None:
        try:
            raw = self._path.read_bytes()
        except OSError:
            return None
        if not raw or len(raw) > MAX_COOKIE_BYTES or b"\x00" in raw:
            return None
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return normalize_cookie_text(text)


def run_live_acceptance(
    *,
    directory_id: object,
    cookie_path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None = None,
    credential_source: object | None = None,
    transport_factory: P115TransportFactory = create_p115_readonly_transport,
    timeout_seconds: float = MAX_RUN_TIMEOUT_SECONDS,
) -> C02ReadOnlyReport:
    """Run one bounded read-only acceptance sequence with no automatic retry."""

    environment = os.environ if env is None else env
    normalized_directory_id = _directory_id(directory_id)
    if (
        environment.get(LIVE_ENV) != "1"
        or normalized_directory_id is None
        or cookie_path is None
        and credential_source is None
        or not _positive_timeout(timeout_seconds)
    ):
        return _blocked("blocked_environment")

    source = credential_source or _CookiePathSource(cookie_path)
    return asyncio.run(
        _run_acceptance(
            normalized_directory_id,
            source=source,
            transport_factory=transport_factory,
            timeout_seconds=float(timeout_seconds),
        )
    )


async def _run_acceptance(
    directory_id: str,
    *,
    source: object,
    transport_factory: P115TransportFactory,
    timeout_seconds: float,
) -> C02ReadOnlyReport:
    try:
        return await asyncio.wait_for(
            _read_bounded_directory(
                directory_id,
                source=source,
                transport_factory=transport_factory,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return _partial("run_timeout", pages_read=0, entries_seen=0)
    except Exception:  # noqa: BLE001 - runner details must not cross the boundary
        return _partial("blocked_environment", pages_read=0, entries_seen=0)


async def _read_bounded_directory(
    directory_id: str,
    *,
    source: object,
    transport_factory: P115TransportFactory,
) -> C02ReadOnlyReport:
    gateway = P115ReadOnlyDirectoryGateway(
        source,  # type: ignore[arg-type]
        transport_factory,
        authorized_directory_ids=(directory_id,),
    )
    pages: list[DirectoryPage] = []
    try:
        for page_number in range(1, MAX_DIRECTORY_PAGES + 1):
            page = await gateway.list_directory(directory_id, page=page_number)
            pages.append(page)
            if page.terminal:
                break
            if page.next_page != page_number + 1:
                return _partial(
                    "pagination_unverified",
                    pages_read=len(pages),
                    entries_seen=_entry_count(pages),
                )
        else:
            return _partial(
                "page_budget_exhausted",
                pages_read=len(pages),
                entries_seen=_entry_count(pages),
            )
    except P115ReadOnlyGatewayError as error:
        return _partial(
            error.code, pages_read=len(pages), entries_seen=_entry_count(pages)
        )

    entries = tuple(entry for page in pages for entry in page.items)
    file_ids = tuple(entry.file_id for entry in entries if not entry.is_directory)
    directory_ids = tuple(entry.directory_id for entry in entries if entry.is_directory)
    file_details = 0
    directory_details = 0
    try:
        if file_ids:
            await gateway.get_file_detail(file_ids[0])
            file_details = 1
        if directory_ids and file_details + directory_details < MAX_DETAIL_CALLS:
            await gateway.get_directory_detail(directory_ids[0])
            directory_details = 1
    except P115ReadOnlyGatewayError as error:
        return _partial(
            error.code,
            pages_read=len(pages),
            entries_seen=len(entries),
            file_details=file_details,
            directory_details=directory_details,
        )

    if not file_ids and not directory_ids:
        return _partial(
            "detail_candidate_missing", pages_read=len(pages), entries_seen=0
        )
    return C02ReadOnlyReport(
        status="success",
        error_code=None,
        pages_read=len(pages),
        entries_seen=len(entries),
        file_details=file_details,
        directory_details=directory_details,
        complete=pages[-1].terminal,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the explicitly gated C02 read probe"
    )
    parser.add_argument("--directory-id", required=True)
    parser.add_argument("--cookie-path")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    if not args.live:
        environment.pop(LIVE_ENV, None)
    report = run_live_acceptance(
        directory_id=args.directory_id,
        cookie_path=args.cookie_path,
        env=environment,
    )
    print(json.dumps(report.to_public_dict(), ensure_ascii=True, sort_keys=True))
    return 0 if report.status == "success" else 1


def _directory_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    return (
        normalized if normalized.isdigit() and not normalized.startswith("0") else None
    )


def _entry_count(pages: Sequence[DirectoryPage]) -> int:
    return sum(len(page.items) for page in pages)


def _positive_timeout(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _blocked(error_code: str) -> C02ReadOnlyReport:
    return C02ReadOnlyReport("blocked", error_code, 0, 0, 0, 0, False)


def _partial(
    error_code: str,
    *,
    pages_read: int,
    entries_seen: int,
    file_details: int = 0,
    directory_details: int = 0,
) -> C02ReadOnlyReport:
    return C02ReadOnlyReport(
        "partial",
        error_code,
        pages_read,
        entries_seen,
        file_details,
        directory_details,
        False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
