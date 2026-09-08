"""Application settings, loaded from the environment.

Every runtime knob is an environment variable prefixed ``TALAIA_``, parsed and validated
once into a single :class:`Settings` object. Validation happens at import of the settings
object, so a missing or malformed variable fails the process immediately"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration for the whole application.

    Values come from the process environment, falling back to a local ``.env`` file for
    development convenience. ``.env`` is never committed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TALAIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown TALAIA_* variables are ignored
        extra="ignore",
    )

    # --- Database ---------------------------------------------------------------------
    # Required
    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/talaia",
    )

    # --- Monitor configuration file ---------------------------------------------------
    config_path: Path = Field(
        default=Path("config/monitors.yaml"),
        description="Path to the YAML file that is the source of truth for monitors.",
    )

    # --- Logging ----------------------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = Field(
        default="json",
        description="'json' for production log shipping, 'console' for readable local output.",
    )

    # --- Retention --------------------------------------------------------------------
    retention_days: int = Field(
        default=30,
        ge=1,
        description="How long raw check results are kept before pruning. Rollups survive.",
    )

    # --- Notifications ----------------------------------------------------------------
    # optional: with no topic configured, notifications are disabled
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None

    base_url: str = Field(
        default="http://localhost:9999",
        description="Public base URL of this instance, used to build links inside notifications.",
    )

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject a synchronous driver URL.

        ``postgresql://`` silently selects psycopg2, which this application does not
        install and could not use from async code. Catching it here turns an obscure
        import error at first query into a clear message at startup.
        """
        if not value.startswith("postgresql+asyncpg://"):
            msg = (
                "TALAIA_DATABASE_URL must use the asyncpg driver, "
                "i.e. start with 'postgresql+asyncpg://'"
            )
            raise ValueError(msg)
        return value

    @property
    def notifications_enabled(self) -> bool:
        # Whether enough is configured to actually send a notification
        return bool(self.ntfy_url and self.ntfy_topic)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    #Return the process-wide settings, constructing them on first use.
    return Settings()
