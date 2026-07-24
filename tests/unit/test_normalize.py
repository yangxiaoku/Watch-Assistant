from datetime import UTC, datetime

import pytest

from watch_assistant.adapters.tmdb import build_search_queries
from watch_assistant.schemas import MovieMetadata, ResourceKind
from watch_assistant.services.normalize import normalize_pansou


@pytest.fixture
def pansou_fixture():
    return {
        "total": 5,
        "merged_by_type": {
            "magnet": [
                {
                    "url": "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Movie",
                    "note": "Movie 2024 2160p",
                    "datetime": "2026-07-23T10:30:00Z",
                    "source": "plugin:first",
                },
                {
                    "url": "magnet:?dn=Duplicate&xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
                    "note": "Duplicate",
                    "datetime": "2026-07-22T10:30:00Z",
                    "source": "plugin:second",
                },
                {"url": "https://example.test/not-a-resource", "note": "Ignored"},
            ],
            "115": [
                {
                    "url": "https://115.com/s/shareCode1?password=abcd",
                    "password": "abcd",
                    "note": "Movie 2024 BluRay 42GB",
                    "datetime": "2026-07-23 12:00:00",
                    "source": "plugin:share",
                    "size": "42 GB",
                    "seeders": 8,
                },
                {
                    "url": "https://www.115.com/s/shareCode1",
                    "password": "abcd",
                    "note": "Duplicate share",
                    "source": "plugin:duplicate",
                },
            ],
        },
    }


def test_magnet_results_dedupe_by_infohash(pansou_fixture):
    resources = normalize_pansou(
        pansou_fixture,
        share_domains=("115.com",),
        captured_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    magnets = [item for item in resources if item.kind == ResourceKind.MAGNET]

    assert len(magnets) == 1
    assert magnets[0].canonical_key == (
        "magnet:abcdef0123456789abcdef0123456789abcdef01"
    )
    assert magnets[0].size_bytes is None
    assert magnets[0].seeders is None
    assert magnets[0].metadata["note"] == "Movie 2024 2160p"


def test_115_results_require_configured_domain_and_dedupe_by_share_id(
    pansou_fixture,
):
    pansou_fixture["merged_by_type"]["115"].append(
        {
            "url": "https://115.com.evil.test/s/shareCode1",
            "note": "Must be ignored",
            "source": "invalid",
        }
    )

    resources = normalize_pansou(pansou_fixture, share_domains=("115.com",))
    shares = [item for item in resources if item.kind == ResourceKind.SHARE]

    assert len(shares) == 1
    assert shares[0].canonical_key == "share:115.com:shareCode1"
    assert shares[0].password == "abcd"
    assert shares[0].size_bytes == 42 * 1024**3
    assert shares[0].seeders == 8


def test_tmdb_queries_are_unique_and_limited_to_two():
    movie = MovieMetadata(
        tmdb_id=123,
        title="盗梦空间",
        original_title="Inception",
        release_year=2010,
    )

    assert build_search_queries(movie) == ["盗梦空间 2010", "Inception 2010"]

    duplicate_title = movie.model_copy(update={"original_title": " 盗梦空间 "})
    assert build_search_queries(duplicate_title) == ["盗梦空间 2010"]
