"""Application settings loaded from environment variables or a local .env file."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    platform_timeout_seconds: float = Field(default=20.0, gt=0)
    public_check_timeout_seconds: float = Field(default=10.0, gt=0)
    connect_retries: int = Field(default=5, ge=0, le=10)
    utm_tagging_enabled: bool = True
    utm_campaign: str = "automated_posting"
    timezone: str = "Asia/Kolkata"

    telegraph_access_token: SecretStr | None = None
    telegraph_author_name: str = "Automated Posting"

    mastodon_base_url: str = "https://mastodon.social"
    mastodon_access_token: SecretStr | None = None

    bluesky_service_url: str = "https://bsky.social"
    bluesky_handle: str | None = None
    bluesky_app_password: SecretStr | None = None

    github_token: SecretStr | None = None

    pastebin_api_dev_key: SecretStr | None = None

    @field_validator("timezone")
    @classmethod
    def _require_known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}; use an IANA name such as Asia/Kolkata or UTC") from None
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
