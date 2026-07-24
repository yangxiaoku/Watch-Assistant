from datetime import UTC, datetime

from watch_assistant.schemas import (
    MediaType,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
)
from watch_assistant.services.validation import validate_and_rank_resources


def _resource(
    name: str,
    *,
    kind: ResourceKind = ResourceKind.MAGNET,
    source: str = "test",
) -> NormalizedResource:
    return NormalizedResource(
        kind=kind,
        canonical_key=f"{kind.value}:{name}",
        name=name,
        url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        source=source,
        captured_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def test_validation_rejects_keyword_collision_and_ranks_quality():
    movie = MovieMetadata(
        tmdb_id=27205,
        title="Inception",
        release_year=2010,
    )

    resources, rejected = validate_and_rank_resources(
        movie,
        [
            _resource("A Guide From Inception To Cloud"),
            _resource("Inception.2010.1080p.WEB-DL"),
            _resource("Inception.2010.2160p.BluRay"),
        ],
    )

    assert [item.name for item in resources] == [
        "Inception.2010.2160p.BluRay",
        "Inception.2010.1080p.WEB-DL",
    ]
    assert rejected == 1


def test_validation_keeps_tv_seasons_but_rejects_them_for_movies():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="Game of Thrones",
        release_year=2011,
    )
    movie = show.model_copy(update={"media_type": MediaType.MOVIE})
    candidates = [
        _resource("Game of Thrones S08E01 2019 1080p"),
        _resource("Game of Thrones Season 2 1080p"),
    ]

    tv_resources, tv_rejected = validate_and_rank_resources(show, candidates)
    movie_resources, movie_rejected = validate_and_rank_resources(movie, candidates)

    assert tv_resources == candidates
    assert tv_rejected == 0
    assert movie_resources == []
    assert movie_rejected == 2


def test_validation_filters_other_tv_seasons_when_one_is_selected():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="Game of Thrones",
        release_year=2011,
    )
    candidates = [
        _resource("Game of Thrones S02E01 2012 1080p"),
        _resource("Game of Thrones Season 1 2011 1080p"),
        _resource("Game of Thrones 第3季 2013 1080p"),
        _resource("Game of Thrones complete collection 1080p"),
    ]

    resources, rejected = validate_and_rank_resources(
        show, candidates, season_number=2
    )

    assert [item.name for item in resources] == [
        "Game of Thrones S02E01 2012 1080p",
        "Game of Thrones complete collection 1080p",
    ]
    assert rejected == 2


def test_validation_records_bounded_scores():
    movie = MovieMetadata(tmdb_id=7, title="Scored Movie", release_year=2026)
    resources, _ = validate_and_rank_resources(
        movie,
        [_resource("Scored Movie 2026 2160p BluRay")],
    )

    scores = resources[0].metadata
    assert all(0 <= scores[key] <= 100 for key in (
        "rank_score",
        "relevance_score",
        "completeness_score",
    ))


def test_validation_uses_episode_marker_to_disambiguate_short_tv_title():
    show = MovieMetadata(
        tmdb_id=4607,
        media_type=MediaType.TV,
        title="Lost",
        release_year=2004,
    )
    candidate = _resource("Lost S01E01")

    resources, rejected = validate_and_rank_resources(show, [candidate])

    assert resources == [candidate]
    assert rejected == 0


def test_validation_returns_all_legal_resources_without_count_caps():
    movie = MovieMetadata(tmdb_id=3, title="Long Movie Title", release_year=2026)
    candidates = [
        _resource(f"Long Movie Title 2026 1080p magnet-{index}")
        for index in range(55)
    ] + [
        _resource(
            f"Long Movie Title 2026 1080p share-{index}",
            kind=ResourceKind.SHARE,
        )
        for index in range(25)
    ]

    resources, rejected = validate_and_rank_resources(movie, candidates)

    assert len(resources) == 80
    assert rejected == 0


def test_source_penalty_demotes_an_unreliable_source():
    movie = MovieMetadata(tmdb_id=5, title="Reliable Movie", release_year=2026)
    high_quality = _resource(
        "Reliable Movie 2026 2160p", source="source:unreliable"
    )
    lower_quality = _resource(
        "Reliable Movie 2026 1080p", source="source:reliable"
    )

    resources, _ = validate_and_rank_resources(
        movie,
        [high_quality, lower_quality],
        source_penalties={"source:unreliable": 30},
    )

    assert [item.source for item in resources] == [
        "source:reliable",
        "source:unreliable",
    ]
