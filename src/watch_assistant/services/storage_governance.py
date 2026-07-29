"""Read-only storage governance calculations.

The module consumes an immutable inventory snapshot and never touches the
filesystem or a 115 write API.  It deliberately refuses cleanup planning for
incomplete snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


class StorageGovernanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StorageEntry:
    object_id: str
    name: str
    size_bytes: int | None
    is_directory: bool = False
    content_hash: str | None = None
    media_identity: str | None = None
    category: str | None = None
    protected: bool = False
    managed: bool = True
    modified_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.object_id or len(self.object_id) > 128:
            raise StorageGovernanceError("invalid_object_id")
        if not self.name or len(self.name) > 1024:
            raise StorageGovernanceError("invalid_name")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise StorageGovernanceError("invalid_size")
        if self.content_hash is not None and len(self.content_hash) > 128:
            raise StorageGovernanceError("invalid_content_hash")


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    snapshot_revision: int | None
    complete: bool
    entries: tuple[StorageEntry, ...]


@dataclass(frozen=True, slots=True)
class StorageCandidate:
    candidate_id: str
    kind: Literal["exact_duplicate", "large_file"]
    object_ids: tuple[str, ...]
    estimated_release_bytes: int
    protected: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class StorageReport:
    snapshot_revision: int | None
    complete: bool
    total_size_bytes: int
    file_count: int
    directory_count: int
    category_sizes: dict[str, int]
    candidates: tuple[StorageCandidate, ...]
    cleanup_allowed: bool


def build_storage_report(
    snapshot: StorageSnapshot,
    *,
    large_file_threshold_bytes: int = 50 * 1024**3,
) -> StorageReport:
    if large_file_threshold_bytes < 1:
        raise StorageGovernanceError("invalid_large_file_threshold")
    seen: set[str] = set()
    files: list[StorageEntry] = []
    directories = 0
    category_sizes: dict[str, int] = {}
    for entry in snapshot.entries:
        if entry.object_id in seen:
            raise StorageGovernanceError("duplicate_object_id")
        seen.add(entry.object_id)
        if entry.is_directory:
            directories += 1
            continue
        files.append(entry)
        size = entry.size_bytes or 0
        category = entry.category or "unknown"
        category_sizes[category] = category_sizes.get(category, 0) + size

    candidates: list[StorageCandidate] = []
    by_hash: dict[str, list[StorageEntry]] = {}
    for entry in files:
        if entry.content_hash:
            by_hash.setdefault(entry.content_hash, []).append(entry)
    for content_hash, group in sorted(by_hash.items()):
        if len(group) < 2:
            continue
        ordered = tuple(sorted(group, key=lambda item: item.object_id))
        keep = ordered[0]
        release = sum(item.size_bytes or 0 for item in ordered[1:])
        candidates.append(
            StorageCandidate(
                candidate_id=f"duplicate:{content_hash[:32]}",
                kind="exact_duplicate",
                object_ids=tuple(item.object_id for item in ordered),
                estimated_release_bytes=release,
                # The candidate includes the retained object as well; a protected
                # retained object must block the whole duplicate plan.
                protected=any(item.protected for item in ordered),
                reason_code="same_content_hash",
            )
        )
        del keep
    for entry in sorted(files, key=lambda item: item.object_id):
        if (entry.size_bytes or 0) < large_file_threshold_bytes:
            continue
        candidates.append(
            StorageCandidate(
                candidate_id=f"large:{entry.object_id}",
                kind="large_file",
                object_ids=(entry.object_id,),
                estimated_release_bytes=0,
                protected=entry.protected,
                reason_code="large_file_advisory",
            )
        )
    return StorageReport(
        snapshot_revision=snapshot.snapshot_revision,
        complete=snapshot.complete,
        total_size_bytes=sum(item.size_bytes or 0 for item in files),
        file_count=len(files),
        directory_count=directories,
        category_sizes=category_sizes,
        candidates=tuple(candidates),
        cleanup_allowed=snapshot.complete,
    )


def plan_cleanup(
    snapshot: StorageSnapshot,
    candidate_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return only review identifiers; execution is intentionally out of scope."""

    report = build_storage_report(snapshot)
    if not report.cleanup_allowed:
        raise StorageGovernanceError("scan_incomplete")
    known = {candidate.candidate_id for candidate in report.candidates}
    selected = tuple(dict.fromkeys(candidate_ids))
    if any(candidate_id not in known for candidate_id in selected):
        raise StorageGovernanceError("candidate_not_found")
    if any(
        candidate.protected
        for candidate in report.candidates
        if candidate.candidate_id in selected
    ):
        raise StorageGovernanceError("protected_candidate")
    return selected


__all__ = [
    "StorageCandidate",
    "StorageEntry",
    "StorageGovernanceError",
    "StorageReport",
    "StorageSnapshot",
    "build_storage_report",
    "plan_cleanup",
]
