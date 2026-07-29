"""Bounded, read-only recursive P115 inventory baseline.

This scanner has no write-capable dependency. It requires the production root
CID explicitly and records only counts and pagination/identity status.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyDirectoryGateway

PAGE_SIZE = 1
MAX_PAGES = 100_000


class FileCredential:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str:
        value = self._path.read_text(encoding="ascii").strip()
        if not value or "\n" in value or "\r" in value:
            raise RuntimeError("credential_missing")
        return value


async def scan(root_id: str, cookie_path: Path) -> dict[str, object]:
    gateway = P115ReadOnlyDirectoryGateway(
        FileCredential(cookie_path),
        authorized_directory_ids=(root_id,),
        request_timeout_seconds=30,
    )
    queue = deque([root_id])
    visited: set[str] = set()
    object_ids: set[str] = set()
    directories = 0
    files = 0
    pages = 0
    while queue:
        directory_id = queue.popleft()
        if directory_id in visited:
            continue
        visited.add(directory_id)
        page_number = 1
        while True:
            if pages >= MAX_PAGES:
                raise RuntimeError("pagination_limit")
            page = await gateway.list_directory(
                directory_id, page=page_number, page_size=PAGE_SIZE
            )
            pages += 1
            if page.page != page_number or page.state.value != "complete" or page.scan_complete is False:
                raise RuntimeError("pagination_unverified")
            if page.page_count is not None and page_number > page.page_count:
                raise RuntimeError("pagination_unverified")
            for item in page.items:
                object_id = item.directory_id if item.is_directory else item.file_id
                if object_id is None or object_id in object_ids:
                    raise RuntimeError("identity_unverified")
                object_ids.add(object_id)
                if item.is_directory:
                    directories += 1
                    queue.append(object_id)
                else:
                    files += 1
            terminal = page.terminal is True or page.has_more is False or (
                page.page_count is not None and page_number == page.page_count
            )
            if terminal:
                break
            if page.terminal is False or page.has_more is True:
                page_number += 1
                continue
            raise RuntimeError("pagination_unverified")
    return {
        "scope": "production_root_configured",
        "root_identity_verified": True,
        "recursive": True,
        "complete": True,
        "directories": directories,
        "files": files,
        "unique_object_ids": len(object_ids),
        "directories_scanned": len(visited),
        "pages_read": pages,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded P115 read-only inventory scan")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.root_id.isdigit() or args.root_id.startswith("0"):
        parser.error("root-id must be a stable numeric identity")
    result = asyncio.run(scan(args.root_id, args.cookie_path))
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
