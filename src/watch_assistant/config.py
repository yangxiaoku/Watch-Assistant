"""Runtime configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from watch_assistant.services.prowlarr_endpoint import (
    ProwlarrEndpointRejected,
    normalize_prowlarr_base_url,
    parse_prowlarr_allowed_private_addresses,
)


def _parse_indexer_ids(value: str | None) -> tuple[int, ...]:
    """Parse a comma-separated indexer ID string into a tuple of ints."""
    if not value:
        return ()
    parsed: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(
                "PROWLARR_*_INDEXER_IDS must contain non-negative integers"
            )
        item = int(token)
        if item in parsed:
            raise ValueError(
                "PROWLARR_*_INDEXER_IDS must not contain duplicates"
            )
        parsed.append(item)
    return tuple(parsed)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        env_ignore_empty=True,
    )

    database_url: str = Field(min_length=1, validation_alias="DATABASE_URL")
    encryption_key: SecretStr = Field(min_length=1, validation_alias="ENCRYPTION_KEY")
    tmdb_api_key: SecretStr = Field(min_length=1, validation_alias="TMDB_API_KEY")
    web_username: str = Field(
        default="admin",
        min_length=1,
        max_length=64,
        validation_alias="WEB_USERNAME",
    )
    web_password_hash: SecretStr | None = Field(
        default=None, validation_alias="WEB_PASSWORD_HASH"
    )
    web_auth_bootstrap_enabled: bool = Field(
        default=False, validation_alias="WEB_AUTH_BOOTSTRAP_ENABLED"
    )
    web_auth_bootstrap_password: SecretStr | None = Field(
        default=None, validation_alias="WEB_AUTH_BOOTSTRAP_PASSWORD"
    )
    script_token_hash: SecretStr = Field(
        min_length=1, validation_alias="SCRIPT_TOKEN_HASH"
    )
    diagnostics_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="WATCH_ASSISTANT_DIAGNOSTICS_TOKEN"
    )
    pansou_base_url: str = Field(min_length=1, validation_alias="PANSOU_BASE_URL")
    tmdb_base_url: str = Field(
        default="https://api.themoviedb.org/3",
        min_length=1,
        validation_alias="TMDB_BASE_URL",
    )
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")
    web_session_ttl_hours: int = Field(
        default=12, ge=1, le=720, validation_alias="WEB_SESSION_TTL_HOURS"
    )
    cache_warm_enabled: bool = Field(
        default=True, validation_alias="CACHE_WARM_ENABLED"
    )
    cache_warm_timezone: str = Field(
        default="Asia/Hong_Kong",
        min_length=1,
        validation_alias="CACHE_WARM_TIMEZONE",
    )
    pansou_max_concurrency: int = Field(
        default=6, ge=1, le=32, validation_alias="PANSOU_MAX_CONCURRENCY"
    )
    prowlarr_enabled: bool = Field(
        default=False, validation_alias="PROWLARR_ENABLED"
    )
    prowlarr_base_url: str = Field(
        default="", validation_alias="PROWLARR_BASE_URL"
    )
    prowlarr_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="PROWLARR_API_KEY"
    )
    prowlarr_allowed_private_addresses: str = Field(
        default="", validation_alias="PROWLARR_ALLOWED_PRIVATE_ADDRESSES"
    )
    prowlarr_timeout_seconds: float = Field(
        default=12,
        ge=1,
        le=60,
        validation_alias="PROWLARR_TIMEOUT_SECONDS",
    )
    prowlarr_max_concurrency: int = Field(
        default=4, ge=1, le=16, validation_alias="PROWLARR_MAX_CONCURRENCY"
    )
    # Fast indexers are queried in real-time during a resource search; slow
    # indexers (e.g. those behind FlareSolverr) are queried in the background
    # and merged into the cache.  Both must be set together (see the model
    # validator) and must be disjoint.  Empty strings preserve the legacy
    # behaviour of querying every enabled indexer in real-time.  Stored as
    # comma-separated strings because pydantic-settings JSON-decodes complex
    # types from env vars before validators run.
    prowlarr_fast_indexer_ids: str = Field(
        default="", validation_alias="PROWLARR_FAST_INDEXER_IDS"
    )
    prowlarr_slow_indexer_ids: str = Field(
        default="", validation_alias="PROWLARR_SLOW_INDEXER_IDS"
    )
    cache_warm_concurrency: int = Field(
        default=3, ge=1, le=16, validation_alias="CACHE_WARM_CONCURRENCY"
    )
    subscription_scheduler_enabled: bool = Field(
        default=False, validation_alias="SUBSCRIPTION_SCHEDULER_ENABLED"
    )
    subscription_scheduler_interval_seconds: int = Field(
        default=300,
        ge=5,
        le=86_400,
        validation_alias="SUBSCRIPTION_SCHEDULER_INTERVAL_SECONDS",
    )
    inspection_enabled: bool = Field(
        default=False, validation_alias="INSPECTION_ENABLED"
    )
    inspection_concurrency: int = Field(
        default=8, ge=1, le=16, validation_alias="INSPECTION_CONCURRENCY"
    )
    inspection_item_timeout_seconds: float = Field(
        default=30,
        ge=5,
        le=120,
        validation_alias="INSPECTION_ITEM_TIMEOUT_SECONDS",
    )
    inspection_final_timeout_seconds: float | None = Field(
        default=None,
        ge=30,
        le=300,
        validation_alias="INSPECTION_FINAL_TIMEOUT_SECONDS",
    )
    inspection_poll_interval_seconds: float = Field(
        default=0.75,
        ge=0.25,
        le=5,
        validation_alias="INSPECTION_POLL_INTERVAL_SECONDS",
    )
    inspection_request_timeout_seconds: float = Field(
        default=10,
        ge=1,
        le=30,
        validation_alias="INSPECTION_REQUEST_TIMEOUT_SECONDS",
    )
    p115_enabled: bool = Field(default=False, validation_alias="P115_ENABLED")
    organization_plan_enabled: bool = Field(
        default=False, validation_alias="ORGANIZATION_PLAN_ENABLED"
    )
    organization_execution_enabled: bool = Field(
        default=False, validation_alias="ORGANIZATION_EXECUTION_ENABLED"
    )
    organization_write_enabled: bool = Field(
        default=False, validation_alias="ORGANIZATION_WRITE_ENABLED"
    )
    organization_write_contract_verified: bool = Field(
        default=False, validation_alias="ORGANIZATION_WRITE_CONTRACT_VERIFIED"
    )
    permanent_delete_enabled: bool = Field(
        default=False, validation_alias="PERMANENT_DELETE_ENABLED"
    )
    permanent_delete_contract_verified: bool = Field(
        default=False, validation_alias="PERMANENT_DELETE_CONTRACT_VERIFIED"
    )
    strm_full_enabled: bool = Field(
        default=False, validation_alias="STRM_FULL_ENABLED"
    )
    strm_incremental_enabled: bool = Field(
        default=False, validation_alias="STRM_INCREMENTAL_ENABLED"
    )
    strm_cleanup_enabled: bool = Field(
        default=False, validation_alias="STRM_CLEANUP_ENABLED"
    )
    strm_playback_enabled: bool = Field(
        default=False, validation_alias="STRM_PLAYBACK_ENABLED"
    )
    strm_playback_contract_verified: bool = Field(
        default=False, validation_alias="STRM_PLAYBACK_CONTRACT_VERIFIED"
    )
    strm_output_root: Path = Field(
        default=Path("./data/strm"), validation_alias="STRM_OUTPUT_ROOT"
    )
    strm_playback_url_prefix: str = Field(
        default="http://127.0.0.1:8115/api/v1/strm/play",
        min_length=1,
        validation_alias="STRM_PLAYBACK_URL_PREFIX",
    )
    strm_playback_allowed_networks: str = Field(
        default="127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7",
        min_length=1,
        validation_alias="STRM_PLAYBACK_ALLOWED_NETWORKS",
    )
    p115_cookie_path: Path = Field(
        default=Path("/etc/watch-assistant/p115-cookie"),
        validation_alias="P115_COOKIE_PATH",
    )
    p115_organization_contract_evidence_path: Path | None = Field(
        default=None, validation_alias="P115_ORGANIZATION_CONTRACT_EVIDENCE_PATH"
    )
    p115_target_cid: int | None = Field(
        default=None, ge=0, validation_alias="P115_TARGET_CID"
    )
    p115_max_concurrency: int = Field(
        default=1, ge=1, le=4, validation_alias="P115_MAX_CONCURRENCY"
    )
    qbittorrent_base_url: str = Field(
        default="", validation_alias="QBITTORRENT_BASE_URL"
    )
    qbittorrent_username: SecretStr = Field(
        default=SecretStr(""), validation_alias="QBITTORRENT_USERNAME"
    )
    qbittorrent_password: SecretStr = Field(
        default=SecretStr(""), validation_alias="QBITTORRENT_PASSWORD"
    )

    @property
    def inspection_configured(self) -> bool:
        return self.inspection_enabled and bool(
            self.qbittorrent_base_url
            and self.qbittorrent_username.get_secret_value()
            and self.qbittorrent_password.get_secret_value()
        )

    @property
    def p115_configured(self) -> bool:
        return self.p115_enabled and self.p115_target_cid is not None

    @property
    def prowlarr_configured(self) -> bool:
        return self.prowlarr_enabled and bool(
            self.prowlarr_base_url.strip()
            and self.prowlarr_api_key.get_secret_value()
        )

    @property
    def prowlarr_fast_ids(self) -> tuple[int, ...]:
        return _parse_indexer_ids(self.prowlarr_fast_indexer_ids)

    @property
    def prowlarr_slow_ids(self) -> tuple[int, ...]:
        return _parse_indexer_ids(self.prowlarr_slow_indexer_ids)

    @model_validator(mode="after")
    def validate_web_auth_configuration(self) -> "Settings":
        if (
            self.web_password_hash is None
            or not self.web_password_hash.get_secret_value()
        ) and not self.web_auth_bootstrap_enabled:
            raise ValueError(
                "WEB_PASSWORD_HASH is required unless "
                "WEB_AUTH_BOOTSTRAP_ENABLED=true"
            )
        if self.web_auth_bootstrap_enabled and self.web_username != "admin":
            raise ValueError(
                "WEB_AUTH_BOOTSTRAP_ENABLED requires WEB_USERNAME=admin"
            )
        if self.web_auth_bootstrap_enabled and (
            self.web_password_hash is not None
            and self.web_password_hash.get_secret_value()
        ):
            raise ValueError(
                "WEB_PASSWORD_HASH must be empty when "
                "WEB_AUTH_BOOTSTRAP_ENABLED=true"
            )
        if self.web_auth_bootstrap_enabled and (
            self.web_auth_bootstrap_password is None
            or not self.web_auth_bootstrap_password.get_secret_value()
        ):
            raise ValueError(
                "WEB_AUTH_BOOTSTRAP_PASSWORD is required when "
                "WEB_AUTH_BOOTSTRAP_ENABLED=true"
            )
        return self

    @model_validator(mode="after")
    def validate_prowlarr_configuration(self) -> "Settings":
        try:
            parse_prowlarr_allowed_private_addresses(
                self.prowlarr_allowed_private_addresses
            )
            if self.prowlarr_base_url.strip():
                normalize_prowlarr_base_url(self.prowlarr_base_url)
        except ProwlarrEndpointRejected as exc:
            raise ValueError(
                "PROWLARR_BASE_URL or PROWLARR_ALLOWED_PRIVATE_ADDRESSES is invalid"
            ) from exc
        if self.prowlarr_enabled and not self.prowlarr_base_url.strip():
            raise ValueError("PROWLARR_ENABLED requires PROWLARR_BASE_URL")
        if self.prowlarr_enabled and not self.prowlarr_api_key.get_secret_value():
            raise ValueError("PROWLARR_ENABLED requires PROWLARR_API_KEY")
        if bool(self.prowlarr_fast_ids) != bool(self.prowlarr_slow_ids):
            raise ValueError(
                "PROWLARR_FAST_INDEXER_IDS and PROWLARR_SLOW_INDEXER_IDS "
                "must be set together"
            )
        if set(self.prowlarr_fast_ids) & set(self.prowlarr_slow_ids):
            raise ValueError(
                "PROWLARR_FAST_INDEXER_IDS and PROWLARR_SLOW_INDEXER_IDS "
                "must be disjoint"
            )
        return self
