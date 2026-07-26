import pytest

from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import MediaType, MovieCollectionResponse, MovieMetadata
from watch_assistant.services.search import SearchService


def _movie(tmdb_id: int, title: str, *, adult: bool = False) -> MovieMetadata:
    return MovieMetadata(
        tmdb_id=tmdb_id,
        title=title,
        original_title=title,
        overview=None,
        poster_path=None,
        adult=adult,
    )


class FakeTmdb:
    async def get_feed(self, feed, media_type=MediaType.MOVIE):
        return [_movie(1, "Safe"), _movie(2, "Adult", adult=True)]

    async def get_feed_page(self, feed, *, media_type=MediaType.MOVIE, page=1):
        return MovieCollectionResponse(
            results=[_movie(1, "Safe"), _movie(2, "Adult", adult=True)],
            page=page,
            total_pages=1,
            total_results=2,
        )

    async def search_movies(self, query):
        return [_movie(1, "Safe"), _movie(2, "Adult", adult=True)]

    async def search_media(self, query, *, page=1):
        return await self.get_feed_page("search", page=page)


@pytest.mark.asyncio
async def test_tmdb_adult_items_are_excluded_from_catalog_and_search(tmp_path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    await initialize_database(database.engine)
    service = SearchService(
        database.session_factory,
        tmdb_client=FakeTmdb(),
        pansou_client=None,
        crypto=None,
    )

    catalog = await service.get_home_catalog()
    assert [item.title for item in catalog.popular] == ["Safe"]
    assert [item.title for item in await service.search_movies("x")] == ["Safe"]
    collection = await service.search_media("x", page=1)
    assert [item.title for item in collection.results] == ["Safe"]
    assert collection.total_results == 1
    await database.engine.dispose()
