from datetime import UTC, datetime

from watch_assistant.schemas import (
    MediaType,
    MovieMetadata,
    NormalizedResource,
    ResourceKind,
    SeasonMetadata,
)
from watch_assistant.services.validation import (
    _season_numbers,
    validate_and_rank_resources,
)


def _resource(
    name: str,
    *,
    kind: ResourceKind = ResourceKind.MAGNET,
    source: str = "test",
    seeders: int | None = None,
    size_bytes: int | None = None,
) -> NormalizedResource:
    return NormalizedResource(
        kind=kind,
        canonical_key=f"{kind.value}:{name}",
        name=name,
        url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        source=source,
        captured_at=datetime(2026, 7, 24, tzinfo=UTC),
        seeders=seeders,
        size_bytes=size_bytes,
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


def test_validation_rejects_complete_collection_for_movie():
    movie = MovieMetadata(tmdb_id=27205, title="Inception", release_year=2010)

    resources, rejected = validate_and_rank_resources(
        movie, [_resource("Inception 2010 Complete Series 1080p")]
    )

    assert resources == []
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
        _resource("Game of Thrones S01-S03 2012 1080p"),
        _resource("Game of Thrones S02-S03 2012 1080p"),
        _resource("Game of Thrones Season 2-3 2012 1080p"),
        _resource("Game of Thrones S02 全集 2012 1080p"),
        _resource("Game of Thrones S02 全季 2012 1080p"),
        _resource("Game of Thrones S02 Complete Season 2012 1080p"),
        _resource("Game of Thrones S02 Full Season 2012 1080p"),
        _resource("Game of Thrones S02 Complete Series 2012 1080p"),
        _resource("Game of Thrones S02 Complete Collection 2012 1080p"),
        _resource("Game of Thrones S02 Collection 2012 1080p"),
        _resource("Game of Thrones S02 Seasons 1-3 2012 1080p"),
        _resource("Game of Thrones S02 Seasons 2-3 2012 1080p"),
        _resource("Game of Thrones 2012 1080p"),
    ]

    resources, rejected = validate_and_rank_resources(show, candidates, season_number=2)

    assert {item.name for item in resources} == {
        "Game of Thrones S02E01 2012 1080p",
        "Game of Thrones S01-S03 2012 1080p",
        "Game of Thrones S02-S03 2012 1080p",
        "Game of Thrones Season 2-3 2012 1080p",
        "Game of Thrones S02 全集 2012 1080p",
        "Game of Thrones S02 全季 2012 1080p",
        "Game of Thrones S02 Complete Season 2012 1080p",
        "Game of Thrones S02 Full Season 2012 1080p",
        "Game of Thrones S02 Complete Series 2012 1080p",
        "Game of Thrones S02 Complete Collection 2012 1080p",
        "Game of Thrones S02 Collection 2012 1080p",
        "Game of Thrones S02 Seasons 1-3 2012 1080p",
        "Game of Thrones S02 Seasons 2-3 2012 1080p",
    }
    assert rejected == 4

    exact = next(item for item in resources if item.name.endswith("S02E01 2012 1080p"))
    ranged = next(item for item in resources if item.name.endswith("S01-S03 2012 1080p"))
    assert ranged.metadata["relevance_score"] < exact.metadata["relevance_score"]


def test_season_numbers_do_not_treat_years_as_seasons():
    assert _season_numbers("Complete Season 2 2012 1080p") == {2}
    assert _season_numbers("S02 Complete Season 2012 1080p") == {2}
    assert _season_numbers("S02 Full Season 2012 1080p") == {2}
    assert _season_numbers("Seasons 1-3 2012 1080p") == {1, 3}
    assert _season_numbers("S02 Seasons 1-3 2012 1080p") == {1, 2, 3}
    assert _season_numbers("S02 Seasons 2-3 2012 1080p") == {2, 3}
    assert _season_numbers("S02-S03 2012 1080p") == {2, 3}
    assert _season_numbers("Season 2-3 2012 1080p") == {2, 3}
    assert _season_numbers("第2季至第3季 2012 1080p") == {2, 3}


def test_validation_keeps_later_tv_season_in_all_seasons_search():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="Game of Thrones",
        release_year=2011,
    )

    resources, rejected = validate_and_rank_resources(
        show, [_resource("Game of Thrones S08 2019 1080p")]
    )

    assert [item.name for item in resources] == ["Game of Thrones S08 2019 1080p"]
    assert rejected == 0


def test_validation_uses_tv_season_air_date_for_year_filter():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="Game of Thrones",
        release_year=2011,
        seasons=[
            SeasonMetadata(
                season_number=8,
                name="Season 8",
                episode_count=6,
                air_date="2019-05-19",
            )
        ],
    )
    resources, rejected = validate_and_rank_resources(
        show,
        [
            _resource("Game of Thrones S08 2019 1080p"),
            _resource("Game of Thrones S08 2018 1080p"),
            _resource("Game of Thrones S08 2016 1080p"),
        ],
        season_number=8,
    )

    assert [item.name for item in resources] == [
        "Game of Thrones S08 2019 1080p",
        "Game of Thrones S08 2018 1080p",
    ]
    assert rejected == 1


def test_validation_does_not_filter_selected_tv_season_without_air_date():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="Game of Thrones",
        release_year=2011,
        seasons=[
            SeasonMetadata(
                season_number=8,
                name="Season 8",
                episode_count=6,
            )
        ],
    )

    resources, rejected = validate_and_rank_resources(
        show,
        [_resource("Game of Thrones S08 2019 1080p")],
        season_number=8,
    )

    assert [item.name for item in resources] == ["Game of Thrones S08 2019 1080p"]
    assert rejected == 0


def test_validation_still_rejects_movie_year_far_from_release_year():
    movie = MovieMetadata(tmdb_id=27205, title="Inception", release_year=2010)

    resources, rejected = validate_and_rank_resources(
        movie, [_resource("Inception 2016 1080p")]
    )

    assert resources == []
    assert rejected == 1


def test_validation_ranks_exact_year_above_adjacent_and_missing_year():
    movie = MovieMetadata(tmdb_id=1, title="The Matrix", release_year=1999)
    resources, rejected = validate_and_rank_resources(
        movie,
        [
            _resource("The Matrix 2000 1080p"),
            _resource("The Matrix 1080p"),
            _resource("The Matrix 1999 1080p"),
        ],
    )

    assert [item.name for item in resources] == [
        "The Matrix 1999 1080p",
        "The Matrix 2000 1080p",
        "The Matrix 1080p",
    ]
    assert rejected == 0


def test_validation_uses_selected_season_air_year_for_ranking():
    show = MovieMetadata(
        tmdb_id=1399,
        media_type=MediaType.TV,
        title="The Show",
        release_year=2010,
        seasons=[
            SeasonMetadata(
                season_number=2,
                name="Season 2",
                episode_count=10,
                air_date="2012-04-01",
            )
        ],
    )
    resources, rejected = validate_and_rank_resources(
        show,
        [
            _resource("The Show Season 2 2011 1080p"),
            _resource("The Show Season 2 1080p"),
            _resource("The Show Season 2 2012 1080p"),
        ],
        season_number=2,
    )

    assert [item.name for item in resources] == [
        "The Show Season 2 2012 1080p",
        "The Show Season 2 2011 1080p",
        "The Show Season 2 1080p",
    ]
    assert rejected == 0


def test_validation_scores_zero_seeders_and_size_below_valid_data():
    movie = MovieMetadata(tmdb_id=2, title="A Long Movie", release_year=2026)
    resources, rejected = validate_and_rank_resources(
        movie,
        [
            _resource(
                "A Long Movie 2026 1080p",
                seeders=0,
                size_bytes=0,
            ),
            _resource(
                "A Long Movie 2026 1080p",
                seeders=20,
                size_bytes=2 * 1024**3,
            ).model_copy(update={"canonical_key": "magnet:valid"}),
        ],
    )

    assert resources[0].canonical_key == "magnet:valid"
    assert (
        resources[0].metadata["completeness_score"]
        > resources[1].metadata["completeness_score"]
    )
    assert rejected == 0


def test_validation_records_bounded_scores():
    movie = MovieMetadata(tmdb_id=7, title="Scored Movie", release_year=2026)
    resources, _ = validate_and_rank_resources(
        movie,
        [_resource("Scored Movie 2026 2160p BluRay")],
    )

    scores = resources[0].metadata
    assert all(
        0 <= scores[key] <= 100
        for key in (
            "rank_score",
            "relevance_score",
            "completeness_score",
        )
    )


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
        _resource(f"Long Movie Title 2026 1080p magnet-{index}") for index in range(55)
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
    high_quality = _resource("Reliable Movie 2026 2160p", source="source:unreliable")
    lower_quality = _resource("Reliable Movie 2026 1080p", source="source:reliable")

    resources, _ = validate_and_rank_resources(
        movie,
        [high_quality, lower_quality],
        source_penalties={"source:unreliable": 30},
    )

    assert [item.source for item in resources] == [
        "source:reliable",
        "source:unreliable",
    ]
