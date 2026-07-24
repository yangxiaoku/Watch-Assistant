"""Runtime configuration loaded from environment variables."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", case_sensitive=False, populate_by_name=True
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
    tgto_base_url: str = Field(min_length=1, validation_alias="TGTO_BASE_URL")
