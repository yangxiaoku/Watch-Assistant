from watch_assistant.services.search import make_cache_key


def test_cache_key_is_versioned_by_movie_id():
    assert make_cache_key(12345) == "tmdb:12345:queries:v1"
