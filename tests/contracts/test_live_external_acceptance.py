from scripts.live_external_acceptance import _magnet_urls


def test_external_acceptance_deduplicates_magnets_by_infohash_not_full_query():
    payload = {
        "merged_by_type": {
            "movie": [
                {
                    "url": "magnet:?xt=urn:btih:" + "A" * 40 + "&tr=udp://one",
                    "source": "one",
                },
                {
                    "url": "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=udp://two",
                    "source": "two",
                },
            ]
        }
    }

    magnets, sources = _magnet_urls(payload, 10)

    assert len(magnets) == 1
    assert sum(sources.values()) == 2
