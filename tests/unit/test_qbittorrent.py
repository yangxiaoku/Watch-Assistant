"""Unit tests for the qBittorrent adapter's magnet enrichment."""

from urllib.parse import quote

from watch_assistant.adapters.qbittorrent import (
    _FALLBACK_HTTP_TRACKERS,
    _with_fallback_http_trackers,
)


def _encoded(tracker: str) -> str:
    return f"tr={quote(tracker, safe='')}"


def test_infohash_only_magnet_gets_http_trackers():
    magnet = "magnet:?xt=urn:btih:d7b21ca3a5556b8012a65b37b9484f2e0c0bdda5"
    enriched = _with_fallback_http_trackers(magnet)

    assert enriched.startswith(magnet)
    for tracker in _FALLBACK_HTTP_TRACKERS:
        assert _encoded(tracker) in enriched
    assert "&tr=" in enriched


def test_magnet_with_existing_http_tracker_keeps_it_and_adds_fallbacks():
    magnet = (
        "magnet:?xt=urn:btih:d7b21ca3a5556b8012a65b37b9484f2e0c0bdda5"
        "&tr=http%3A%2F%2Fp4p.arenabg.com%3A1337%2Fannounce"
    )
    enriched = _with_fallback_http_trackers(magnet)

    assert "p4p.arenabg.com" in enriched
    for tracker in _FALLBACK_HTTP_TRACKERS:
        assert _encoded(tracker) in enriched


def test_non_magnet_is_returned_unchanged():
    assert _with_fallback_http_trackers("https://example.com/x.mkv") == (
        "https://example.com/x.mkv"
    )
    assert _with_fallback_http_trackers("") == ""
