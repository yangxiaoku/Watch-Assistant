import pytest

from scripts.p115_production_probe_restore import (
    ProbeCandidate,
    RestoreError,
    _history_original_name,
    _plan_digest,
)


def test_restore_history_requires_one_safe_old_name_for_the_requested_file():
    response = {
        "state": True,
        "errno": 0,
        "data": [
            {"file_id": "101", "file_old_name": "original.wav"},
        ],
    }

    assert _history_original_name(
        response, file_id="101", temporary_name="wa-prod-probe-abcdef123456.wav"
    ) == "original.wav"


def test_restore_history_rejects_ambiguous_old_names():
    response = {
        "state": True,
        "data": [
            {"file_id": "101", "file_old_name": "first.wav"},
            {"file_id": "101", "file_old_name": "second.wav"},
        ],
    }

    with pytest.raises(RestoreError, match="rename_history_not_unique"):
        _history_original_name(
            response, file_id="101", temporary_name="wa-prod-probe-abcdef123456.wav"
        )


def test_restore_digest_is_stable_and_does_not_render_names():
    candidate = ProbeCandidate("101", "7", "wa-prod-probe-abcdef123456.wav")
    digest = _plan_digest(candidate, "original.wav")

    assert len(digest) == 64
    assert digest == _plan_digest(candidate, "original.wav")
    assert "original.wav" not in digest
    assert "wa-prod-probe" not in digest
