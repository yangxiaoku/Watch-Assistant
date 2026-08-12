from datetime import UTC, datetime, timedelta

import pytest

from watch_assistant.api.library import _scan_is_complete
from watch_assistant.library_models import LibraryScanRun, MediaLibrary
from watch_assistant.services.library_inventory import (
    DuplicateKind,
    FreshnessStatus,
    InventoryDecision,
    InventoryError,
    InventoryFile,
    build_identity,
    build_snapshot,
    calculate_freshness,
    check_inventory,
    search_inventory,
)

NOW = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)


def test_identity_does_not_depend_on_remote_path_or_object_id():
    left = build_identity(InventoryFile("file-1", "Movie.2024.1080p.mkv"))
    right = build_identity(InventoryFile("file-2", "Movie.2024.1080p.mkv"))

    assert left.identity.key == right.identity.key
    assert left.identity.confidence == "candidate"


def test_exact_digest_is_the_only_automatic_duplicate_blocker():
    snapshot = build_snapshot(
        (
            InventoryFile("one", "Movie.2024.1080p.mkv", content_digest="sha-a"),
            InventoryFile("two", "Movie.2024.2160p.mkv", content_digest="sha-a"),
            InventoryFile("three", "Movie.2024.1080p.mkv"),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    assert snapshot.duplicate_groups[0].kind is DuplicateKind.EXACT
    assert check_inventory(snapshot, content_digest="sha-a") is InventoryDecision.EXACT_DUPLICATE
    assert (
        check_inventory(snapshot, name="Movie.2024.1080p.mkv")
        is InventoryDecision.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    ("identity_field", "first", "second", "query"),
    (
        ("content_digest", "SHA-A", "sha-a", "sHa-A"),
        ("infohash", "ABC123", "abc123", "AbC123"),
    ),
)
def test_exact_identity_guard_matches_duplicate_group_normalization(
    identity_field, first, second, query
):
    snapshot = build_snapshot(
        (
            InventoryFile(
                "one",
                "Movie.2024.1080p.mkv",
                **{identity_field: first},
            ),
            InventoryFile(
                "two",
                "Movie.2024.2160p.mkv",
                **{identity_field: second},
            ),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    assert any(group.kind is DuplicateKind.EXACT for group in snapshot.duplicate_groups)
    assert (
        check_inventory(snapshot, **{identity_field: query})
        is InventoryDecision.EXACT_DUPLICATE
    )


def test_trusted_tmdb_identity_distinguishes_media_and_version_duplicates():
    snapshot = build_snapshot(
        (
            InventoryFile(
                "movie-1",
                "Movie.2024.1080p.WEB-DL.mkv",
                tmdb_id=7,
                media_type="movie",
            ),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    assert (
        check_inventory(snapshot, tmdb_id=7, media_type="movie", name="Movie.2024.2160p.BluRay.mkv")
        is InventoryDecision.MEDIA_DUPLICATE
    )
    assert (
        check_inventory(snapshot, tmdb_id=7, media_type="movie", name="Movie.2024.1080p.WEB-DL.mkv")
        is InventoryDecision.VERSION_DUPLICATE
    )


def test_tv_inventory_matches_the_requested_season_and_episode_range():
    snapshot = build_snapshot(
        (
            InventoryFile(
                "episode-1",
                "Show.S01E01.1080p.mkv",
                tmdb_id=1399,
                media_type="tv",
                season=1,
                episode_start=1,
                episode_end=1,
            ),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    assert (
        check_inventory(
            snapshot,
            tmdb_id=1399,
            media_type="tv",
            season=2,
            episode_start=1,
        )
        is InventoryDecision.NOT_FOUND
    )
    assert (
        check_inventory(
            snapshot,
            tmdb_id=1399,
            media_type="tv",
            season=1,
            episode_start=2,
        )
        is InventoryDecision.NOT_FOUND
    )


def test_incomplete_snapshot_never_claims_missing():
    snapshot = build_snapshot(
        (InventoryFile("one", "Movie.2024.1080p.mkv"),),
        complete=False,
        captured_at=NOW,
        now=NOW,
    )

    assert snapshot.freshness.status is FreshnessStatus.INCOMPLETE
    assert (
        check_inventory(snapshot, name="Other.2024.1080p.mkv")
        is InventoryDecision.INDEX_INCOMPLETE
    )


def test_freshness_has_explicit_unknown_and_stale_states():
    unknown = calculate_freshness(complete=True, captured_at=None, now=NOW)
    stale = calculate_freshness(
        complete=True,
        captured_at=NOW - timedelta(minutes=16),
        now=NOW,
    )

    assert unknown.status is FreshnessStatus.UNKNOWN
    assert stale.status is FreshnessStatus.STALE
    assert stale.age_seconds == 960


@pytest.mark.parametrize(
    "captured_at",
    (None, NOW - timedelta(minutes=16)),
)
def test_stale_or_unknown_snapshot_never_claims_missing(captured_at):
    snapshot = build_snapshot(
        (InventoryFile("one", "Movie.2024.1080p.mkv"),),
        complete=True,
        captured_at=captured_at,
        now=NOW,
    )

    assert check_inventory(snapshot, name="Other.2024.1080p.mkv") is InventoryDecision.INDEX_INCOMPLETE


def test_trusted_version_duplicate_groups_are_exposed():
    snapshot = build_snapshot(
        (
            InventoryFile(
                "movie-1",
                "Movie.2024.1080p.WEB-DL.mkv",
                tmdb_id=7,
                media_type="movie",
            ),
            InventoryFile(
                "movie-2",
                "Movie.2024.1080p.WEB-DL.mkv",
                tmdb_id=7,
                media_type="movie",
            ),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    version_groups = [
        group
        for group in snapshot.duplicate_groups
        if group.kind is DuplicateKind.VERSION
    ]
    assert len(version_groups) == 1
    assert version_groups[0].object_ids == ("movie-1", "movie-2")


def test_api_inventory_requires_a_completed_scoped_snapshot():
    library = MediaLibrary(
        id="library-1",
        name="library",
        root_directory_id="root-1",
        enabled=True,
        scope_verified=True,
    )
    def run(**overrides):
        values = {
            "id": "scan-1",
            "library_id": library.id,
            "root_directory_id": library.root_directory_id,
            "idempotency_key": "scan-key",
            "state": "completed",
            "complete": True,
            "snapshot_revision": 1,
        }
        values.update(overrides)
        return LibraryScanRun(**values)

    assert _scan_is_complete(run(), library) is True
    assert _scan_is_complete(run(state="failed"), library) is False
    assert _scan_is_complete(run(complete=False), library) is False
    assert _scan_is_complete(run(snapshot_revision=None), library) is False
    assert _scan_is_complete(
        run(root_directory_id="other-root"), library
    ) is False


def test_duplicate_object_ids_are_rejected():
    with pytest.raises(InventoryError, match="duplicate_object_id"):
        build_snapshot(
            (InventoryFile("same", "Movie.2024.mkv"), InventoryFile("same", "Other.2024.mkv")),
            complete=True,
            captured_at=NOW,
            now=NOW,
        )


def test_search_inventory_filters_by_query_kind_duplicate_and_paginates():
    """INV-008: 名称/媒体类型/重复状态过滤 + 分页。"""
    snapshot = build_snapshot(
        (
            InventoryFile("one", "Movie.A.2024.1080p.mkv", media_type="movie"),
            InventoryFile("two", "Movie.B.2024.2160p.mkv", media_type="movie", content_digest="sha-dup"),
            InventoryFile("three", "Show.S01E01.720p.mkv", media_type="tv"),
            InventoryFile("four", "Show.S01E02.1080p.mkv", media_type="tv", content_digest="sha-dup"),
            InventoryFile("five", "Unknown.file.bin"),
        ),
        complete=True,
        captured_at=NOW,
        now=NOW,
    )

    all_movies = search_inventory(snapshot, media_type="movie")
    assert all_movies.total == 2
    assert {item.file.object_id for item in all_movies.items} == {"one", "two"}

    # 名称子串(不区分大小写)
    name_hit = search_inventory(snapshot, query="s01e")
    assert name_hit.total == 2

    # 分页
    page_one = search_inventory(snapshot, limit=2, offset=0)
    page_two = search_inventory(snapshot, limit=2, offset=2)
    assert page_one.total == 5
    assert len(page_one.items) == 2
    assert len(page_two.items) == 2
    assert {item.file.object_id for item in page_one.items} != {
        item.file.object_id for item in page_two.items
    }

    # 重复过滤:两个 movie 共享 MediaIdentity(同名 Movie 2024)构成重复组
    duplicate_only = search_inventory(snapshot, duplicate_only=True)
    assert duplicate_only.total >= 2
    assert {item.file.object_id for item in duplicate_only.items} == {"two", "four"}


def test_search_inventory_rejects_invalid_pagination():
    snapshot = build_snapshot((), complete=True, captured_at=NOW, now=NOW)
    with pytest.raises(InventoryError, match="invalid_inventory_search"):
        search_inventory(snapshot, limit=0)
    with pytest.raises(InventoryError, match="invalid_inventory_search"):
        search_inventory(snapshot, offset=-1)
