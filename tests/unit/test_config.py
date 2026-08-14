import secrets
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from watch_assistant.config import Settings

BASE_SETTINGS = {
    "DATABASE_URL": "sqlite+aiosqlite:///test.db",
    "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    "TMDB_API_KEY": "tmdb-key",
    "WEB_PASSWORD_HASH": "web-hash",
    "SCRIPT_TOKEN_HASH": "script-hash",
    "PANSOU_BASE_URL": "http://pansou.test",
}


def make_settings(**overrides: object) -> Settings:
    values = {**BASE_SETTINGS, **overrides}
    return Settings(**values)


def test_encryption_key_must_be_a_valid_fernet_key_at_config_time():
    """L4:弱/错 ENCRYPTION_KEY 必须启动期即失败,而不是拖到首次加密。"""
    values = dict(BASE_SETTINGS)
    values["ENCRYPTION_KEY"] = "not-a-fernet-key"
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(**values)


def test_domain_encryption_key_override_is_validated_at_config_time():
    """F6:分域轮换密钥同样在配置期校验 Fernet 格式。"""
    values = dict(BASE_SETTINGS)
    values["ENCRYPTION_KEY_WEBHOOK"] = "broken"
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(**values)
    values["ENCRYPTION_KEY_WEBHOOK"] = Fernet.generate_key().decode("ascii")
    settings = Settings(**values)
    assert settings.encryption_key_webhook.get_secret_value()


def test_inspection_settings_have_production_defaults():
    settings = make_settings()

    assert settings.web_username == "admin"
    assert settings.web_auth_bootstrap_enabled is False
    assert settings.prowlarr_enabled is False
    assert settings.prowlarr_configured is False
    assert settings.prowlarr_allowed_private_addresses == ""
    assert settings.prowlarr_timeout_seconds == 12
    assert settings.prowlarr_max_concurrency == 4
    assert settings.diagnostics_token.get_secret_value() == ""
    assert settings.organization_plan_enabled is False
    assert settings.organization_execution_enabled is False
    assert settings.organization_write_enabled is False
    assert settings.p115_organization_contract_evidence_path is None
    assert settings.permanent_delete_enabled is False
    assert settings.permanent_delete_contract_verified is False
    assert settings.strm_full_enabled is False
    assert settings.strm_incremental_enabled is False
    assert settings.strm_cleanup_enabled is False
    assert settings.strm_playback_enabled is False
    assert settings.strm_playback_contract_verified is False
    assert settings.inspection_concurrency == 8
    assert settings.inspection_item_timeout_seconds == 30
    assert settings.inspection_poll_interval_seconds == 0.75
    assert settings.inspection_request_timeout_seconds == 10
    assert settings.web_session_ttl_hours == 12


def test_admin_bootstrap_can_replace_the_environment_password_hash():
    values = dict(BASE_SETTINGS)
    values.pop("WEB_PASSWORD_HASH")
    bootstrap_password = secrets.token_urlsafe(24)
    settings = Settings(
        **values,
        WEB_AUTH_BOOTSTRAP_ENABLED=True,
        WEB_AUTH_BOOTSTRAP_PASSWORD=bootstrap_password,
    )

    assert settings.web_username == "admin"
    assert settings.web_password_hash is None
    assert settings.web_auth_bootstrap_enabled is True
    assert (
        settings.web_auth_bootstrap_password.get_secret_value()
        == bootstrap_password
    )


def test_admin_bootstrap_requires_a_separate_password():
    values = dict(BASE_SETTINGS)
    values.pop("WEB_PASSWORD_HASH")

    with pytest.raises(ValidationError, match="WEB_AUTH_BOOTSTRAP_PASSWORD"):
        Settings(**values, WEB_AUTH_BOOTSTRAP_ENABLED=True)


def test_admin_bootstrap_rejects_a_configured_password_hash():
    with pytest.raises(ValidationError, match="WEB_PASSWORD_HASH"):
        make_settings(
            WEB_AUTH_BOOTSTRAP_ENABLED=True,
            WEB_AUTH_BOOTSTRAP_PASSWORD=secrets.token_urlsafe(24),
        )


def test_web_password_hash_is_required_when_admin_bootstrap_is_disabled():
    values = dict(BASE_SETTINGS)
    values.pop("WEB_PASSWORD_HASH")

    with pytest.raises(ValidationError, match="WEB_PASSWORD_HASH"):
        Settings(**values)


def test_admin_bootstrap_rejects_a_non_admin_username():
    with pytest.raises(ValidationError, match="WEB_USERNAME=admin"):
        make_settings(
            WEB_USERNAME="operator",
            WEB_AUTH_BOOTSTRAP_ENABLED=True,
        )


def test_organization_plan_flag_can_be_enabled_explicitly():
    assert (
        make_settings(ORGANIZATION_PLAN_ENABLED="true").organization_plan_enabled
        is True
    )


def test_diagnostics_token_can_be_configured_separately_from_script_token():
    settings = make_settings(
        WATCH_ASSISTANT_DIAGNOSTICS_TOKEN="deployment-diagnostics-token"
    )

    assert (
        settings.diagnostics_token.get_secret_value()
        == "deployment-diagnostics-token"
    )


def test_organization_execution_flag_can_be_enabled_explicitly():
    assert (
        make_settings(
            ORGANIZATION_EXECUTION_ENABLED="true"
        ).organization_execution_enabled
        is True
    )


def test_write_and_delete_contract_flags_are_independent():
    settings = make_settings(
        ORGANIZATION_WRITE_ENABLED="true",
        PERMANENT_DELETE_ENABLED="true",
        PERMANENT_DELETE_CONTRACT_VERIFIED="true",
    )
    assert settings.organization_write_enabled is True
    assert settings.permanent_delete_enabled is True
    assert settings.permanent_delete_contract_verified is True


def test_organization_contract_evidence_path_is_non_secret_configuration():
    expected = "/var/lib/watch-assistant/p115-organization-contract.json"
    settings = make_settings(P115_ORGANIZATION_CONTRACT_EVIDENCE_PATH=expected)

    assert settings.p115_organization_contract_evidence_path == Path(expected)


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


def test_prowlarr_requires_url_and_api_key_when_enabled():
    with pytest.raises(ValidationError, match="PROWLARR_BASE_URL"):
        make_settings(PROWLARR_ENABLED=True)
    with pytest.raises(ValidationError, match="PROWLARR_API_KEY"):
        make_settings(PROWLARR_ENABLED=True, PROWLARR_BASE_URL="http://prowlarr.test")


def test_prowlarr_is_configured_only_with_explicit_complete_settings():
    api_key = secrets.token_urlsafe(24)
    settings = make_settings(
        PROWLARR_ENABLED=True,
        PROWLARR_BASE_URL="http://prowlarr.test",
        PROWLARR_API_KEY=api_key,
    )

    assert settings.prowlarr_configured is True


def test_prowlarr_accepts_an_explicit_private_endpoint_allowlist():
    settings = make_settings(
        PROWLARR_ENABLED=True,
        PROWLARR_BASE_URL="http://127.0.0.1:9696",
        PROWLARR_API_KEY=secrets.token_urlsafe(24),
        PROWLARR_ALLOWED_PRIVATE_ADDRESSES="127.0.0.1",
    )

    assert settings.prowlarr_allowed_private_addresses == "127.0.0.1"


@pytest.mark.parametrize(
    "value",
    ["127.0.0.0/8", "127.0.*", "8.8.8.8", "127.0.0.1,"],
)
def test_prowlarr_rejects_non_exact_private_endpoint_allowlist(value: str):
    with pytest.raises(ValidationError, match="PROWLARR_ALLOWED_PRIVATE_ADDRESSES"):
        make_settings(PROWLARR_ALLOWED_PRIVATE_ADDRESSES=value)


def test_prowlarr_indexer_ids_default_to_empty():
    settings = make_settings()
    assert settings.prowlarr_fast_ids == ()
    assert settings.prowlarr_slow_ids == ()


def test_prowlarr_indexer_ids_parse_comma_string():
    settings = make_settings(
        PROWLARR_FAST_INDEXER_IDS="11,16,17",
        PROWLARR_SLOW_INDEXER_IDS="10,18",
    )
    assert settings.prowlarr_fast_ids == (11, 16, 17)
    assert settings.prowlarr_slow_ids == (10, 18)


def test_prowlarr_indexer_ids_accept_empty():
    settings = make_settings(PROWLARR_FAST_INDEXER_IDS="", PROWLARR_SLOW_INDEXER_IDS="")
    assert settings.prowlarr_fast_ids == ()
    assert settings.prowlarr_slow_ids == ()


@pytest.mark.parametrize(
    "fast,slow",
    [
        ("11,16,17", ""),
        ("", "10,18"),
    ],
)
def test_prowlarr_indexer_ids_must_be_set_together(fast: str, slow: str):
    with pytest.raises(
        ValidationError, match="must be set together"
    ):
        make_settings(
            PROWLARR_FAST_INDEXER_IDS=fast,
            PROWLARR_SLOW_INDEXER_IDS=slow,
        )


def test_prowlarr_indexer_ids_must_be_disjoint():
    with pytest.raises(ValidationError, match="must be disjoint"):
        make_settings(
            PROWLARR_FAST_INDEXER_IDS="11,16",
            PROWLARR_SLOW_INDEXER_IDS="16,18",
        )


@pytest.mark.parametrize(
    "value",
    [
        "11,abc",
        "11,-1",
        "11,11",
    ],
)
def test_prowlarr_indexer_ids_reject_bad_tokens(value: str):
    with pytest.raises(ValidationError):
        make_settings(
            PROWLARR_FAST_INDEXER_IDS=value,
            PROWLARR_SLOW_INDEXER_IDS="10,18",
        )


def test_p115_checkin_config_defaults_and_validation():
    valid_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        DATABASE_URL="sqlite:///x.db",
        ENCRYPTION_KEY=valid_key,
        TMDB_API_KEY="k",
        SCRIPT_TOKEN_HASH="h",
        PANSOU_BASE_URL="http://p",
        WEB_PASSWORD_HASH="h",
    )
    assert settings.p115_check_in_enabled is False
    assert settings.p115_check_in_time == "00:05"

    settings = Settings(
        DATABASE_URL="sqlite:///x.db", ENCRYPTION_KEY=valid_key,
        TMDB_API_KEY="k", SCRIPT_TOKEN_HASH="h", PANSOU_BASE_URL="http://p",
        WEB_PASSWORD_HASH="h",
        P115_CHECK_IN_ENABLED="true", P115_CHECK_IN_TIME="09:30",
    )
    assert settings.p115_check_in_enabled is True
    assert settings.p115_check_in_time == "09:30"

    with pytest.raises(ValidationError):
        Settings(DATABASE_URL="sqlite:///x.db", ENCRYPTION_KEY=valid_key,
                 TMDB_API_KEY="k", SCRIPT_TOKEN_HASH="h", PANSOU_BASE_URL="http://p",
                 WEB_PASSWORD_HASH="h",
                 P115_CHECK_IN_TIME="25:00")
