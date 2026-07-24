from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from watch_assistant.adapters.tmdb import build_search_queries
from watch_assistant.schemas import MovieMetadata, ResourceKind
from watch_assistant.services.normalize import normalize_pansou

HEX_HASH = "abcdef0123456789abcdef0123456789abcdef01"


def _magnet_data(*items):
    return {"merged_by_type": {"magnet": list(items)}}


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


def test_malformed_share_url_does_not_abort_other_results():
    data = {
        "merged_by_type": {
            "115": [
                {"url": "https://[invalid/s/bad", "note": "Invalid"},
                {"url": "https://115.com/s/good", "note": "Valid"},
            ]
        }
    }

    resources = normalize_pansou(data, share_domains=("115.com",))

    assert [resource.canonical_key for resource in resources] == [
        "share:115.com:good"
    ]


@pytest.mark.parametrize(
    ("url", "canonical_key"),
    [
        (
            f"magnet:?xt=urn:btih:{HEX_HASH}&dn=Show&tr=udp%3A%2F%2Ftracker.test",
            f"magnet:{HEX_HASH}",
        ),
        (
            "magnet:?xt=urn:btih:ABCDEFGHIJKLMNOPQRSTUVWXYZ234567&dn=Show",
            "magnet:00443214c74254b635cf84653a56d7c675be77df",
        ),
    ],
)
def test_valid_magnet_requires_supported_btih(url, canonical_key):
    resources = normalize_pansou(_magnet_data({"url": url}))

    assert len(resources) == 1
    assert resources[0].canonical_key == canonical_key


@pytest.mark.parametrize(
    "url",
    [
        f"magnet://host/path?xt=urn:btih:{HEX_HASH}&dn=Show",
        f"magnet:payload?xt=urn:btih:{HEX_HASH}&dn=Show",
        f"magnet:?xt=urn:btih:{HEX_HASH}&dn=Show#fragment",
        "magnet:?xt=urn:btih:abc&dn=Show",
        "magnet:?xt=urn:btih:0000000000000000000000000000000Z&dn=Show",
        f"magnet:?xt=urn:btih:{'0' * 40}&dn=Show",
        "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&dn=Show",
        "magnet:?xt=urn:sha1:abcdef&dn=Show",
        "magnet:?dn=Show",
        f"https://example.test/?xt=urn:btih:{HEX_HASH}&dn=Show",
    ],
)
def test_invalid_magnet_uri_or_btih_is_filtered(url):
    assert normalize_pansou(_magnet_data({"url": url})) == []


def test_magnet_display_name_prefers_pansou_fields_then_dn():
    resources = normalize_pansou(
        _magnet_data(
            {
                "url": f"magnet:?xt=urn:btih:{1:040x}&dn=URI%20Name",
                "name": " PanSou Name ",
                "note": "PanSou Note",
            },
            {
                "url": f"magnet:?xt=urn:btih:{2:040x}&dn=URI%20Name",
                "note": " PanSou Note ",
            },
            {"url": f"magnet:?xt=urn:btih:{3:040x}&dn=URI%20Name"},
        )
    )

    assert [resource.name for resource in resources] == [
        "PanSou Name",
        "PanSou Note",
        "URI Name",
    ]
    assert [resource.metadata["label_source"] for resource in resources] == [
        "name",
        "note",
        "dn",
    ]


def test_magnet_without_dn_gets_safe_display_name_in_url():
    original = f"magnet:?xt=urn:btih:{HEX_HASH}&tr=udp%3A%2F%2Ftracker.test"

    resource = normalize_pansou(
        _magnet_data({"url": original, "note": "剧集 S01E02 / Episode 2"})
    )[0]

    query = parse_qs(urlsplit(resource.url).query)
    assert query["dn"] == ["剧集 S01E02 / Episode 2"]
    assert query["tr"] == ["udp://tracker.test"]
    assert resource.url != original


@pytest.mark.parametrize(
    "label",
    [
        "---",
        HEX_HASH,
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        f"magnet:?xt=urn:btih:{HEX_HASH}",
    ],
)
def test_obvious_magnet_shell_labels_are_filtered(label):
    data = _magnet_data({"url": f"magnet:?xt=urn:btih:{1:040x}", "name": label})

    assert normalize_pansou(data) == []


def test_magnet_without_display_information_is_filtered():
    data = _magnet_data({"url": f"magnet:?xt=urn:btih:{1:040x}"})

    assert normalize_pansou(data) == []


@pytest.mark.parametrize(
    "label",
    [
        "S01E02",
        "The Bear Season 2 Complete",
        "庆余年 第2季 第03集",
        "La Casa de Papel AKA Money Heist",
    ],
)
def test_tv_episode_and_alias_labels_are_not_filtered(label):
    data = _magnet_data(
        {"url": f"magnet:?xt=urn:btih:{1:040x}", "note": label}
    )

    assert normalize_pansou(data)[0].name == label


def test_duplicate_infohash_keeps_and_merges_richer_result():
    resources = normalize_pansou(
        _magnet_data(
            {
                "url": f"magnet:?xt=urn:btih:{HEX_HASH}",
                "note": "Show S01E01",
                "source": "plugin:first",
                "size": "4 GB",
            },
            {
                "url": f"magnet:?xt=urn:btih:{HEX_HASH}&dn=URI%20Name",
                "name": "Official Show S01E01",
                "source": "plugin:second",
                "seeders": 12,
                "password": "code",
                "images": ["poster.jpg"],
            },
        )
    )

    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == "Official Show S01E01"
    assert resource.metadata["label_source"] == "name"
    assert resource.metadata["note"] == "Show S01E01"
    assert resource.metadata["images"] == ["poster.jpg"]
    assert resource.size_bytes == 4 * 1024**3
    assert resource.seeders == 12
    assert resource.password == "code"


def test_tmdb_queries_include_title_and_year_fallbacks():
    movie = MovieMetadata(
        tmdb_id=123,
        title="盗梦空间",
        original_title="Inception",
        release_year=2010,
    )

    assert build_search_queries(movie) == [
        "盗梦空间",
        "盗梦空间 2010",
        "Inception",
        "Inception 2010",
    ]

    duplicate_title = movie.model_copy(update={"original_title": " 盗梦空间 "})
    assert build_search_queries(duplicate_title) == ["盗梦空间", "盗梦空间 2010"]
