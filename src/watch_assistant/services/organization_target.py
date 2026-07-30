"""Read-only resolution of a managed 115 target directory tree."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)


class OrganizationTargetError(ValueError):
    """Stable target-scope error without remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationTargetCatalog:
    """A complete, relative-path-to-CID view of one target root."""

    root_directory_id: str
    directories: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        return (
            "OrganizationTargetCatalog(root_directory_id=<redacted>, "
            f"directory_count={len(self.directories)})"
        )

    @property
    def by_path(self) -> dict[str, str]:
        return dict(self.directories)

    def directory_id_for(self, path: str) -> str | None:
        normalized = _relative_path(path)
        if normalized is None:
            return None
        return self.by_path.get(normalized)


async def read_target_catalog(
    gateway: P115ReadOnlyDirectoryGateway,
    root_directory_id: str,
    *,
    max_directories: int = 2048,
) -> OrganizationTargetCatalog:
    """Read every directory below ``root_directory_id`` with hard bounds.

    Directory names are used only to construct an in-memory relative path. The
    result is accepted only when every paginated listing is complete and every
    path maps to one stable directory ID. No write or delete method is exposed.
    """

    if not _stable_id(root_directory_id):
        raise OrganizationTargetError("target_directory_id_invalid")
    if not isinstance(max_directories, int) or isinstance(max_directories, bool):
        raise OrganizationTargetError("target_directory_limit_invalid")
    if max_directories < 1 or max_directories > 100_000:
        raise OrganizationTargetError("target_directory_limit_invalid")

    pending: list[tuple[str, str]] = [(root_directory_id, "")]
    by_path: dict[str, str] = {"": root_directory_id}
    seen_ids: set[str] = {root_directory_id}
    while pending:
        directory_id, parent_path = pending.pop(0)
        page = 1
        while True:
            try:
                result = await gateway.list_directory(
                    directory_id, page=page, page_size=1
                )
            except asyncio.CancelledError:
                raise
            except P115ReadOnlyGatewayError:
                raise OrganizationTargetError("target_directory_read_failed") from None
            except Exception:  # noqa: BLE001 - remote details stay opaque
                raise OrganizationTargetError("target_directory_read_failed") from None
            if result.state.value != "complete" or not isinstance(
                result.terminal, bool
            ):
                raise OrganizationTargetError("target_directory_incomplete")
            for entry in result.items:
                if not entry.is_directory or entry.directory_id is None:
                    continue
                child_id = entry.directory_id
                child_path = _join(parent_path, entry.name)
                if child_path is None:
                    raise OrganizationTargetError("target_directory_name_invalid")
                existing = by_path.get(child_path)
                if existing is not None and existing != child_id:
                    raise OrganizationTargetError("target_directory_path_conflict")
                if child_id in seen_ids and existing != child_id:
                    raise OrganizationTargetError("target_directory_identity_conflict")
                if existing is None:
                    if len(by_path) >= max_directories:
                        raise OrganizationTargetError("target_directory_limit_exceeded")
                    by_path[child_path] = child_id
                    seen_ids.add(child_id)
                    pending.append((child_id, child_path))
            if result.terminal:
                if result.next_page is not None:
                    raise OrganizationTargetError(
                        "target_directory_pagination_unverified"
                    )
                break
            if result.next_page != page + 1:
                raise OrganizationTargetError("target_directory_pagination_unverified")
            page = result.next_page
    return OrganizationTargetCatalog(
        root_directory_id=root_directory_id,
        directories=tuple(sorted(by_path.items())),
    )


def _stable_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isdigit()
        and not value.startswith("0")
    )


def _relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return "" if value == "" else None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = "/".join(path.parts)
    return normalized if normalized and len(normalized) <= 4096 else None


def _join(parent: str, name: object) -> str | None:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    return _relative_path("/".join(part for part in (parent, name) if part))


__all__ = ["OrganizationTargetCatalog", "OrganizationTargetError", "read_target_catalog"]
