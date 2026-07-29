from datetime import UTC, datetime, timedelta

import pytest

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
    assert check_inventory(snapshot, name="Other.2024.1080p.mkv") is InventoryDecision.INDEX_INCOMPLETE


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


def test_duplicate_object_ids_are_rejected():
    with pytest.raises(InventoryError, match="duplicate_object_id"):
        build_snapshot(
            (InventoryFile("same", "Movie.2024.mkv"), InventoryFile("same", "Other.2024.mkv")),
            complete=True,
            captured_at=NOW,
            now=NOW,
        )
