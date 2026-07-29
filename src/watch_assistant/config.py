"""Runtime configuration loaded from environment variables."""

import json
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    web_password_hash: SecretStr = Field(
        min_length=1, validation_alias="WEB_PASSWORD_HASH"
    )
    script_token_hash: SecretStr = Field(
        min_length=1, validation_alias="SCRIPT_TOKEN_HASH"
    )
    pansou_base_url: str = Field(min_length=1, validation_alias="PANSOU_BASE_URL")
    tmdb_base_url: str = Field(
        default="https://api.themoviedb.org/3",
        min_length=1,
        validation_alias="TMDB_BASE_URL",
    )
    tgto_base_url: str = Field(min_length=1, validation_alias="TGTO_BASE_URL")
    tgto_contract_path: Path = Field(
        default=Path("config/tgto-contract.json"),
        validation_alias="TGTO_CONTRACT_PATH",
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
    p115_cookie_path: Path = Field(
        default=Path("/etc/watch-assistant/p115-cookie"),
        validation_alias="P115_COOKIE_PATH",
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


def load_tgto_contract(path: Path | str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as contract_file:
        contract = json.load(contract_file)
    if not isinstance(contract, dict):
        raise TypeError("TgtoDrive contract root must be an object")
    return contract
