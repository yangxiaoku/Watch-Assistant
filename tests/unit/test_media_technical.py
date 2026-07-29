import pytest

from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.media_technical import (
    DeclaredTechnicalTags,
    InspectionDepth,
    MeasuredTechnicalObservation,
    TechnicalReason,
    TechnicalStatus,
    declared_tags_from_parse,
    inspect_observation,
)


def test_filename_declarations_are_kept_separate_from_measured_fields():
    declared = declared_tags_from_parse(parse_media_filename("Movie.2160p.HDR10.Atmos.mkv"))
    assert declared.resolution == "2160p"
    assert declared.hdr == "HDR10"
    assert declared.atmos is True


def test_l1_report_marks_declared_mismatch_as_suspect():
    report = inspect_observation(
        MeasuredTechnicalObservation(
            container="matroska",
            duration_seconds=7200,
            video_track_count=1,
            video_width=1920,
            video_height=1080,
            video_codec="H.264",
            hdr="HDR10",
            atmos=False,
        ),
        depth=InspectionDepth.L1,
        declared=DeclaredTechnicalTags(resolution="2160p", video_codec="H.265", atmos=True),
    )
    assert report.status == TechnicalStatus.SUSPECT
    assert report.reasons == (TechnicalReason.DECLARED_METADATA_MISMATCH,)
    assert set(report.mismatches) == {"resolution", "video_codec", "atmos"}


def test_l2_success_is_only_sampled_ok():
    report = inspect_observation(
        MeasuredTechnicalObservation(
            container="mp4",
            duration_seconds=90,
            video_track_count=1,
            video_height=720,
            sample_points=3,
            sample_successes=3,
        ),
        depth=InspectionDepth.L2,
    )
    assert report.status == TechnicalStatus.SAMPLED_OK


def test_l2_partial_sampling_is_suspect_and_unknown_data_stays_unknown():
    partial = inspect_observation(
        MeasuredTechnicalObservation(container="mkv", duration_seconds=1, video_track_count=1, sample_points=3, sample_successes=2),
        depth=InspectionDepth.L2,
    )
    assert partial.status == TechnicalStatus.SUSPECT
    assert TechnicalReason.SAMPLE_DECODE_FAILED in partial.reasons

    unknown = inspect_observation(MeasuredTechnicalObservation(), depth=InspectionDepth.L1)
    assert unknown.status == TechnicalStatus.UNKNOWN
    assert TechnicalReason.INSUFFICIENT_DATA in unknown.reasons


def test_probe_failure_is_stable_and_no_sensitive_input_is_modeled():
    report = inspect_observation(
        MeasuredTechnicalObservation(probe_error=TechnicalReason.DIRECT_LINK_UNAVAILABLE),
        depth=InspectionDepth.L1,
    )
    assert report.status == TechnicalStatus.FAILED
    assert report.reasons == (TechnicalReason.DIRECT_LINK_UNAVAILABLE,)

    with pytest.raises(ValueError):
        MeasuredTechnicalObservation(sample_successes=2, sample_points=1)
