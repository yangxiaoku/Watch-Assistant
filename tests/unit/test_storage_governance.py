import pytest

from watch_assistant.services.storage_governance import (
    StorageEntry,
    StorageGovernanceError,
    StorageSnapshot,
    build_storage_report,
    plan_cleanup,
)


def _entry(object_id: str, *, size: int = 100, content_hash: str | None = None, protected: bool = False):
    return StorageEntry(
        object_id=object_id,
        name=f"{object_id}.mkv",
        size_bytes=size,
        content_hash=content_hash,
        protected=protected,
        category="电影",
    )


def test_report_counts_storage_and_exact_duplicate_candidates():
    report = build_storage_report(
        StorageSnapshot(
            snapshot_revision=3,
            complete=True,
            entries=(_entry("a", content_hash="hash"), _entry("b", content_hash="hash"), _entry("c", size=50)),
        )
    )

    assert report.total_size_bytes == 250
    assert report.file_count == 3
    assert report.category_sizes == {"电影": 250}
    assert report.cleanup_allowed is True
    assert report.candidates[0].kind == "exact_duplicate"
    assert report.candidates[0].estimated_release_bytes == 100


def test_incomplete_snapshot_never_allows_cleanup_planning():
    snapshot = StorageSnapshot(snapshot_revision=None, complete=False, entries=(_entry("a"),))
    report = build_storage_report(snapshot)

    assert report.cleanup_allowed is False
    with pytest.raises(StorageGovernanceError, match="scan_incomplete"):
        plan_cleanup(snapshot, ("large:a",))


def test_protected_candidate_cannot_enter_plan():
    snapshot = StorageSnapshot(
        snapshot_revision=1,
        complete=True,
        entries=(_entry("a", content_hash="hash", protected=True), _entry("b", content_hash="hash")),
    )

    with pytest.raises(StorageGovernanceError, match="protected_candidate"):
        plan_cleanup(snapshot, ("duplicate:hash",))


def test_duplicate_object_ids_are_rejected():
    with pytest.raises(StorageGovernanceError, match="duplicate_object_id"):
        build_storage_report(StorageSnapshot(None, False, (_entry("a"), _entry("a"))))
