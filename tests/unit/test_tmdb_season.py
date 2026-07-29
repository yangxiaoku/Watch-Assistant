from watch_assistant.adapters.tmdb import _parse_season


def test_parse_tmdb_season_keeps_independent_identity_and_episode_summary():
    result = _parse_season(
        {
            "id": 456,
            "name": "Season 2",
            "overview": "Season overview",
            "air_date": "2025-01-01",
            "poster_path": "/poster.jpg",
            "episode_count": 1,
            "vote_average": 8.4,
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Episode 1",
                    "overview": "Episode overview",
                    "air_date": "2025-01-02",
                    "runtime": 45,
                    "vote_average": 8.1,
                }
            ],
        },
        series_tmdb_id=1399,
        season_number=2,
        language="zh-CN",
    )

    assert result.tmdb_season_id == 456
    assert result.series_tmdb_id == 1399
    assert result.overview_language == "zh-CN"
    assert result.episodes[0].episode_number == 1
    assert result.episodes[0].runtime == 45

