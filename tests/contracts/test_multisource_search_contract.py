from watch_assistant.services.normalize import normalize_pansou


def test_search_candidates_have_stable_cross_source_identity_and_safe_source_ids():
    magnet_hex = "00443214c74254b635cf84653a56d7c675be77df"
    resources = normalize_pansou(
        {
            "merged_by_type": {
                "magnet": [
                    {
                        "url": f"magnet:?xt=urn:btih:{magnet_hex.upper()}&dn=Movie",
                        "name": "Movie 2010 1080p",
                        "source": "pansou:plugin",
                    },
                    {
                        "url": (
                            "magnet:?xt=urn:btih:ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
                            "&dn=Movie"
                        ),
                        "name": "Movie 2010 1080p",
                        "source": "prowlarr:indexer",
                    },
                    {
                        "url": (
                            "magnet:?xt=urn:btih:"
                            "1234567890abcdef1234567890abcdef12345678&dn=Movie"
                        ),
                        "name": "Movie 2010 720p",
                        "source": "https://indexer.test/search?redaction_marker=example-marker",
                    },
                ],
                "115": [
                    {
                        "url": "https://115.com/s/share-code",
                        "note": "Movie 2010 share",
                        "source": "pansou:share",
                    }
                ],
            }
        }
    )

    assert len(resources) == 3
    assert resources[0].canonical_key == f"magnet:{magnet_hex}"
    assert resources[1].canonical_key.startswith("magnet:")
    assert resources[2].canonical_key == "share:115.com:share-code"
    assert all("https://" not in resource.source for resource in resources)
    assert all("example-marker" not in resource.source for resource in resources)
