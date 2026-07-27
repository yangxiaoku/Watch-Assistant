import pytest
from pydantic import ValidationError

from watch_assistant.config import Settings

BASE_SETTINGS = {
    "DATABASE_URL": "sqlite+aiosqlite:///test.db",
    "ENCRYPTION_KEY": "encryption-key",
    "TMDB_API_KEY": "tmdb-key",
    "WEB_PASSWORD_HASH": "web-hash",
    "SCRIPT_TOKEN_HASH": "script-hash",
    "PANSOU_BASE_URL": "http://pansou.test",
    "TGTO_BASE_URL": "http://tgto.test",
}


def make_settings(**overrides: object) -> Settings:
    values = {**BASE_SETTINGS, **overrides}
    return Settings(**values)


def test_inspection_settings_have_production_defaults():
    settings = make_settings()

    assert settings.organization_plan_enabled is False
    assert settings.organization_execution_enabled is False
    assert settings.inspection_concurrency == 8
    assert settings.inspection_item_timeout_seconds == 30
    assert settings.inspection_poll_interval_seconds == 0.75
    assert settings.inspection_request_timeout_seconds == 10
    assert settings.web_session_ttl_hours == 12


def test_organization_plan_flag_can_be_enabled_explicitly():
    assert (
        make_settings(ORGANIZATION_PLAN_ENABLED="true").organization_plan_enabled
        is True
    )


def test_organization_execution_flag_can_be_enabled_explicitly():
    assert (
        make_settings(
            ORGANIZATION_EXECUTION_ENABLED="true"
        ).organization_execution_enabled
        is True
    )


@pytest.mark.parametrize("value", [1, 720])
def test_web_session_ttl_accepts_configured_bounds(value):
    assert make_settings(WEB_SESSION_TTL_HOURS=value).web_session_ttl_hours == value


@pytest.mark.parametrize("value", [0, 721])
def test_web_session_ttl_rejects_out_of_bounds(value):
    with pytest.raises(ValidationError):
        make_settings(WEB_SESSION_TTL_HOURS=value)


@pytest.mark.parametrize(
    ("field", "lower", "upper"),
    [
        ("inspection_concurrency", 1, 16),
        ("inspection_item_timeout_seconds", 5, 120),
        ("inspection_poll_interval_seconds", 0.25, 5),
        ("inspection_request_timeout_seconds", 1, 30),
    ],
)
def test_inspection_settings_accept_bounds(field: str, lower: float, upper: float):
    assert getattr(make_settings(**{field: lower}), field) == lower
    assert getattr(make_settings(**{field: upper}), field) == upper


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inspection_concurrency", 0),
        ("inspection_concurrency", 17),
        ("inspection_item_timeout_seconds", 4.99),
        ("inspection_item_timeout_seconds", 120.01),
        ("inspection_poll_interval_seconds", 0.24),
        ("inspection_poll_interval_seconds", 5.01),
        ("inspection_request_timeout_seconds", 0.99),
        ("inspection_request_timeout_seconds", 30.01),
    ],
)
def test_inspection_settings_reject_out_of_bounds(field: str, value: float):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})
