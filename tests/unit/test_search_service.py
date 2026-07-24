from watch_assistant.schemas import MediaType
from watch_assistant.services.search import make_cache_key


def test_cache_key_is_versioned_by_movie_id():
    assert make_cache_key(12345) == "tmdb:movie:12345:queries:v2"
    assert make_cache_key(12345, MediaType.TV) == "tmdb:tv:12345:queries:v2"
