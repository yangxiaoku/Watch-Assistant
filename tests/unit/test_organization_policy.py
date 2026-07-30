from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.organization_policy import (
    OrganizationConflictPolicy,
    compare_versions,
    evidence_from_parse,
)


def _evidence(name: str, size: int = 1_000):
    return evidence_from_parse(parse_media_filename(name), size_bytes=size)


def test_policy_compares_dimensions_in_declared_order():
    policy = OrganizationConflictPolicy(
        prefer_remux=True,
        prefer_resolution=True,
        prefer_dolby=True,
        conflict_mode=0,
    )
    result = compare_versions(
        _evidence("movie.1080p.BluRay.mkv"),
        _evidence("movie.720p.WEB-DL.mkv"),
        policy,
    )
    assert result.outcome == "candidate"
    assert result.dimension == "remux"


def test_policy_never_guesses_unknown_evidence():
    result = compare_versions(
        _evidence("movie.mkv"),
        _evidence("movie.mkv"),
        OrganizationConflictPolicy(conflict_mode=0),
    )
    assert result.outcome == "review"
    assert result.reason == "version_evidence_tie"


def test_multi_version_keeps_dolby_and_non_dolby_separate():
    result = compare_versions(
        _evidence("movie.1080p.DV.mkv"),
        _evidence("movie.1080p.HDR.WEB-DL.mkv"),
        OrganizationConflictPolicy(multi_version_enabled=True, conflict_mode=0),
    )
    assert result.outcome == "coexist"
    assert result.reason == "dolby_multi_version"


def test_mode_two_only_allows_same_name_check():
    result = compare_versions(
        _evidence("movie.2160p.mkv", 4_000),
        _evidence("movie.720p.mkv", 1_000),
        OrganizationConflictPolicy(conflict_mode=2),
    )
    assert result.outcome == "existing"
    assert result.reason == "same_name_only"
