from __future__ import annotations

import pytest

from watch_assistant.schemas import MediaType
from watch_assistant.services.media_classification import (
    ClassificationKind,
    ClassificationOverride,
    ClassificationRequest,
    ClassificationStatus,
    CompanionFile,
    NamingRuleConfig,
    RegionClass,
    build_naming_plan,
    plan_batch,
    plan_media,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchSource,
    MatchStatus,
    MediaKind,
    TmdbCandidate,
)
from watch_assistant.services.media_parser import parse_media_filename


def candidate(
    *,
    tmdb_id: int = 1,
    title: str = "Example Title",
    media_type: MediaType = MediaType.MOVIE,
    kind: MediaKind = MediaKind.MOVIE,
    year: int | None = 2024,
    countries: tuple[str, ...] = ("US",),
) -> TmdbCandidate:
    return TmdbCandidate(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        kind=kind,
        release_year=year,
        origin_countries=countries,
    )


def accepted(
    selected: TmdbCandidate, *, source: MatchSource = MatchSource.AUTOMATIC
) -> MatchDecision:
    return MatchDecision(
        status=MatchStatus.ACCEPTED,
        selected=selected,
        confidence=MatchConfidence.HIGH,
        score=96,
        source=source,
    )


def parsed(name: str):
    return parse_media_filename(name)


@pytest.mark.parametrize(
    ("kind", "media_type", "expected"),
    (
        (MediaKind.MOVIE, MediaType.MOVIE, ClassificationKind.MOVIE),
        (MediaKind.TV, MediaType.TV, ClassificationKind.TV),
        (MediaKind.ANIME, MediaType.MOVIE, ClassificationKind.ANIME),
        (MediaKind.DOCUMENTARY, MediaType.TV, ClassificationKind.DOCUMENTARY),
        (MediaKind.VARIETY, MediaType.TV, ClassificationKind.VARIETY),
    ),
)
def test_five_class_mapping(kind, media_type, expected):
    name = (
        "Media 2024 S01E01 1080p.mkv"
        if media_type is MediaType.TV
        else "Media 2024 1080p.mkv"
    )
    plan = plan_media(
        parsed(name), accepted(candidate(kind=kind, media_type=media_type))
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.classification is expected


@pytest.mark.parametrize(
    ("kind", "media_type", "expected_dir"),
    (
        (MediaKind.MOVIE, MediaType.MOVIE, "电影"),
        (MediaKind.TV, MediaType.TV, "剧集"),
        (MediaKind.ANIME, MediaType.MOVIE, "动画"),
        (MediaKind.DOCUMENTARY, MediaType.TV, "纪录片"),
        (MediaKind.VARIETY, MediaType.TV, "综艺"),
    ),
)
def test_chinese_category_directories(kind, media_type, expected_dir):
    name = (
        "Media 2024 S01E01 1080p.mkv"
        if media_type is MediaType.TV
        else "Media 2024 1080p.mkv"
    )
    plan = plan_media(
        parsed(name), accepted(candidate(kind=kind, media_type=media_type))
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.target_path.startswith(f"{expected_dir}/")


def test_default_library_root_is_empty():
    plan = plan_media(
        parsed("Root 2024 1080p.mkv"),
        accepted(candidate(countries=("US",))),
    )

    assert NamingRuleConfig().library_root == ""
    assert plan.target_path.startswith("电影/美国/")
    assert not plan.target_path.startswith("library/")


def test_animation_movie_defaults_to_anime_and_can_be_disabled():
    media = candidate(kind=MediaKind.ANIME, media_type=MediaType.MOVIE)
    parsed_media = parsed("Animated Movie 2024 1080p.mkv")

    assert (
        plan_media(parsed_media, accepted(media)).classification
        is ClassificationKind.ANIME
    )
    disabled = plan_media(
        parsed_media,
        accepted(media),
        rules=NamingRuleConfig(animation_as_anime=False),
    )
    assert disabled.classification is ClassificationKind.MOVIE


def test_optional_categories_and_year_grouping_are_applied_only_when_enabled():
    children = plan_media(
        parsed("\u513f\u7ae5\u6545\u4e8b 2024 1080p.mkv"),
        accepted(candidate(title="\u513f\u7ae5\u6545\u4e8b")),
        rules=NamingRuleConfig(include_children_category=True),
    )
    assert children.classification is ClassificationKind.CHILDREN
    assert children.target_path.startswith("少儿/")

    concert = plan_media(
        parsed("Summer Concert 2024 1080p.mkv"),
        accepted(candidate(title="Summer Concert")),
        rules=NamingRuleConfig(include_concert_category=True, year_grouping_enabled=True),
    )
    assert concert.classification is ClassificationKind.CONCERT
    assert "演唱会/美国/2024/" in concert.target_path


@pytest.mark.parametrize(
    ("country", "expected"),
    (
        ("CN", "中国"),
        ("HK", "香港"),
        ("MO", "澳门"),
        ("TW", "台湾"),
        ("US", "美国"),
        ("JP", "日本"),
        ("KR", "韩国"),
        ("GB", "英国"),
        ("UK", "英国"),
        ("IN", "印度"),
    ),
)
def test_region_mapping(country, expected):
    plan = plan_media(
        parsed("Region 2024 1080p.mkv"),
        accepted(candidate(countries=(country,))),
    )

    assert plan.region == expected
    assert plan.status is ClassificationStatus.PLANNED


def test_multiple_countries_use_stable_first_valid_country():
    first = plan_media(
        parsed("Joint 2024 1080p.mkv"),
        accepted(candidate(countries=("US", "CN"))),
    )
    reversed_countries = plan_media(
        parsed("Joint 2024 1080p.mkv"),
        accepted(candidate(countries=("CN", "US"))),
    )

    assert first == reversed_countries
    assert first.region == "中国"


def test_unmapped_country_falls_back_to_other():
    plan = plan_media(
        parsed("Odd Country 2024 1080p.mkv"),
        accepted(candidate(countries=("ZW",))),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.region == "其他"
    assert "/其他/" in plan.target_path


def test_missing_or_unknown_country_requires_review_and_manual_region_wins():
    missing = plan_media(
        parsed("Unknown Region 2024 1080p.mkv"), accepted(candidate(countries=()))
    )
    unknown = plan_media(
        parsed("Unknown Region 2024 1080p.mkv"), accepted(candidate(countries=("ZZ",)))
    )

    assert missing.status is ClassificationStatus.REVIEW_REQUIRED
    assert "origin_country_unknown" in missing.reasons
    assert missing.region == "其他"
    assert unknown.status is ClassificationStatus.REVIEW_REQUIRED
    assert unknown.region == "其他"
    locked = plan_media(
        parsed("Unknown Region 2024 1080p.mkv"),
        accepted(candidate(countries=())),
        override=ClassificationOverride(region=RegionClass.DOMESTIC),
    )
    assert locked.status is ClassificationStatus.PLANNED
    assert locked.region == "国产"


def test_low_confidence_match_requires_review():
    decision = MatchDecision(
        status=MatchStatus.ACCEPTED,
        selected=candidate(),
        confidence=MatchConfidence.MEDIUM,
    )

    plan = plan_media(parsed("Low Confidence 2024 1080p.mkv"), decision)

    assert plan.status is ClassificationStatus.REVIEW_REQUIRED
    assert plan.target_path is None


def test_missing_confidence_match_requires_review():
    decision = MatchDecision(
        status=MatchStatus.ACCEPTED,
        selected=candidate(),
        confidence=None,
    )

    plan = plan_media(parsed("Missing Confidence 2024 1080p.mkv"), decision)

    assert plan.status is ClassificationStatus.REVIEW_REQUIRED
    assert plan.target_path is None
    assert plan.executable is False


def test_missing_kind_and_type_conflict_are_conservative():
    unknown_kind = plan_media(
        parsed("Unknown Kind 2024 1080p.mkv"),
        accepted(candidate(kind=MediaKind.UNKNOWN)),
    )
    conflict = plan_media(
        parsed("Conflict 2024 S01E01.mkv"),
        accepted(candidate(media_type=MediaType.TV, kind=MediaKind.MOVIE)),
    )

    assert unknown_kind.classification is ClassificationKind.MOVIE
    assert unknown_kind.status is ClassificationStatus.REVIEW_REQUIRED
    assert "kind_unknown" in unknown_kind.reasons
    assert conflict.status is ClassificationStatus.REVIEW_REQUIRED
    assert "media_type_kind_conflict" in conflict.reasons


def test_parser_media_type_conflict_is_review_only():
    plan = plan_media(
        parsed("TV Hint S01E01.mkv"),
        accepted(candidate(media_type=MediaType.MOVIE, kind=MediaKind.MOVIE)),
    )

    assert plan.status is ClassificationStatus.REVIEW_REQUIRED
    assert "parser_media_type_conflict" in plan.reasons


def test_non_accepted_match_never_gets_an_executable_target():
    decision = MatchDecision(status=MatchStatus.NEEDS_REVIEW)
    plan = plan_media(parsed("Needs Review 2024 1080p.mkv"), decision)

    assert plan.status is ClassificationStatus.REVIEW_REQUIRED
    assert plan.target_path is None
    assert plan.executable is False


def test_movie_and_series_target_templates_keep_year_episode_range_and_technical_tags():
    movie = plan_media(
        parsed("Movie 2024 2160p WEB-DL HDR10+ Atmos.mkv"),
        accepted(candidate(tmdb_id=42, title="电影", countries=("CN",))),
    )
    series = plan_media(
        parsed("Series S01E02-E04 1080p BluRay H.265.mkv"),
        accepted(
            candidate(
                tmdb_id=43,
                title="シリーズ",
                media_type=MediaType.TV,
                kind=MediaKind.TV,
                countries=("JP",),
            )
        ),
    )

    assert movie.status is ClassificationStatus.PLANNED
    assert "电影/中国/电影 (2024) {tmdb-42}" in movie.target_path
    assert "2160p" in movie.target_path
    assert "WEB-DL" in movie.target_path
    assert "HDR10+" in movie.target_path
    assert "Atmos" in movie.target_path
    assert series.status is ClassificationStatus.PLANNED
    assert "剧集/日本/シリーズ (2024) {tmdb-43}/Season 01" in series.target_path
    assert "S01E02-E04" in series.target_path
    assert series.technical_tags == ("1080p", "BluRay", "H.265")


def test_region_directories_can_be_disabled_and_rule_version_is_carried():
    plan = plan_media(
        parsed("No Region Directory 2024 1080p.mkv"),
        accepted(candidate()),
        rules=NamingRuleConfig(region_enabled=False, rule_version="i06-test-2"),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.rule_version == "i06-test-2"
    assert "/美国/" not in plan.target_path
    assert plan.target_path.startswith("电影/")


def test_custom_naming_templates_are_rendered_without_io():
    plan = plan_media(
        parsed("Template 2024 1080p.mkv"),
        accepted(candidate(tmdb_id=9)),
        rules=NamingRuleConfig(
            movie_directory_template="{title} [{tmdb_tag}]",
            movie_file_template="{title}.{extension}",
            rule_version="i06-custom",
        ),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert (
        plan.target_path
        == "电影/美国/Example Title [{tmdb-9}]/Example Title.mkv"
    )


def test_companion_files_use_stable_keys_and_do_not_become_primary_media():
    primary = parsed("Movie 2024 1080p.mkv")
    subtitle = CompanionFile("file-subtitle-1", parsed("Movie 2024 CHS.srt"))
    nfo = CompanionFile("file-nfo-1", parsed("Movie 2024.nfo"))
    plan = plan_media(
        primary,
        accepted(candidate()),
        companions=(nfo, subtitle),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert [item.association_key for item in plan.companion_mappings] == [
        "file-nfo-1",
        "file-subtitle-1",
    ]
    assert plan.companion_mappings[1].target_name.endswith(".subtitle.chs.srt")
    assert plan.companion_mappings[0].target_name.endswith(".nfo.nfo")
    with pytest.raises(ValueError, match="association"):
        CompanionFile("../unsafe", subtitle.parsed)


def test_companion_target_conflicts_and_non_primary_inputs_are_safe():
    primary = parsed("Movie 2024 1080p.mkv")
    duplicate = CompanionFile("same", parsed("Movie 2024.a.srt"))
    duplicate_again = CompanionFile("same", parsed("Movie 2024.b.srt"))
    conflict = plan_media(
        primary,
        accepted(candidate()),
        companions=(duplicate, duplicate_again),
    )
    sample = plan_media(parsed("Movie.sample.mkv"), accepted(candidate()))

    assert conflict.status is ClassificationStatus.CONFLICT
    assert "duplicate_companion_key" in conflict.reasons
    assert sample.status is ClassificationStatus.UNSUPPORTED
    assert sample.target_path is None


def test_invalid_filename_characters_reserved_names_and_paths_are_sanitized():
    plan = plan_media(
        parsed(r"C:\private\..\source\CON:Bad*Title 2024 1080p.mkv"),
        accepted(candidate(title="CON:Bad*Title")),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.target_path is not None
    assert "private" not in plan.target_path
    assert ".." not in plan.target_path
    assert ":" not in plan.target_path
    assert "*" not in plan.target_path
    reserved = plan_media(
        parsed("CON 2024 1080p.mkv"), accepted(candidate(title="CON"))
    )
    assert "/_CON (2024)" in reserved.target_path


def test_batch_duplicate_targets_are_conflicts_and_order_is_stable():
    request = ClassificationRequest(
        parsed("Same Movie 2024 1080p.mkv"), accepted(candidate(tmdb_id=55))
    )
    plans = plan_batch((request, request))

    assert len(plans) == 2
    assert all(plan.status is ClassificationStatus.CONFLICT for plan in plans)
    assert all("target_conflict" in plan.reasons for plan in plans)
    assert plans[0].target_path == plans[1].target_path


def test_manual_lock_and_override_are_preserved():
    plan = build_naming_plan(
        parsed("Locked 2024 1080p.mkv"),
        accepted(candidate(kind=MediaKind.MOVIE), source=MatchSource.MANUAL),
        override=ClassificationOverride(
            classification=ClassificationKind.ANIME,
            region=RegionClass.DOMESTIC,
        ),
    )

    assert plan.status is ClassificationStatus.PLANNED
    assert plan.classification is ClassificationKind.ANIME
    assert plan.region == "国产"
    assert "manual_lock" in plan.reasons
    assert "manual_classification" in plan.reasons


def test_same_input_is_idempotent_and_representations_are_redacted():
    source = r"C:\private\secret\Movie 2024 1080p.mkv"
    first = plan_media(
        parsed(source), accepted(candidate(title="安全标题", tmdb_id=88))
    )
    second = plan_media(
        parsed(source), accepted(candidate(title="安全标题", tmdb_id=88))
    )

    assert first == second
    assert "private" not in repr(first)
    assert "secret" not in repr(first)
    assert "private" not in repr(first.evidence)
