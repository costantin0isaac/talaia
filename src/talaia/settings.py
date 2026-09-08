"""Application settings, loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration, read from ``TALAIA_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="TALAIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/talaia",
    )
    config_path: Path = Field(
        default=Path("config/monitors.yaml"),
        description="Path to the YAML file that is the source of truth for monitors.",
    )

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    retention_days: int = Field(
        default=30,
        ge=1,
        description="How long raw check results are kept before pruning.",
    )

    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None

    base_url: str = Field(
        default="http://localhost:9999",
        description="Public base URL of this instance, used for links in notifications.",
    )
    commit: str = Field(
        default="unknown",
        description="Commit this image was built from, reported by talaia_build_info.",
    )

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject a URL that would select a synchronous driver."""
        if not value.startswith("postgresql+asyncpg://"):
            msg = (
                "TALAIA_DATABASE_URL must use the asyncpg driver, "
                "i.e. start with 'postgresql+asyncpg://'"
            )
            raise ValueError(msg)
        return value

    @property
    def notifications_enabled(self) -> bool:
        """Whether a notifier is configured."""
        return bool(self.ntfy_url and self.ntfy_topic)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsing the environment on first use."""
    return Settings()
