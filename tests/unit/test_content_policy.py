import pytest

from watch_assistant.services.content_policy import (
    ContentPolicy,
    normalize_keyword,
    normalize_keywords,
)


def test_high_confidence_resource_rules_avoid_broad_false_positives():
    policy = ContentPolicy()
    assert policy.resource_reason("FC2-PPV release") == "suspicious"
    assert policy.resource_reason("CamRip 1080p") == "low_quality"
    assert policy.resource_reason("Sex Education S01 1080p") is None
    assert policy.resource_reason("normal TS transport stream") is None
    assert policy.resource_reason("normal m2ts remux") is None


def test_keyword_normalization_and_ascii_token_boundaries():
    policy = ContentPolicy(blocked_keywords=normalize_keywords(["  Foo－Bar "]))
    assert normalize_keyword("ＦＯＯ－ＢＡＲ") == "foo bar"
    assert policy.resource_reason("a foo-bar release") == "keyword"
    assert policy.resource_reason("foobar release") is None


@pytest.mark.parametrize("value", ["a", "x" * 41, "   "])
def test_keyword_length_is_bounded(value: str):
    with pytest.raises(ValueError):
        normalize_keywords([value])


def test_keyword_count_is_bounded():
    with pytest.raises(ValueError):
        normalize_keywords([f"k{i:02d}" for i in range(51)])
