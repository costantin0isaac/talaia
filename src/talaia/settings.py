"""Application settings, loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from talaia.formatting import set_display_timezone

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

    session_ttl_hours: int = Field(
        default=720,
        ge=1,
        description="How long a login lasts before it must be repeated.",
    )
    session_cookie_name: str = "talaia_session"
    session_cookie_secure: bool | None = Field(
        default=None,
        description=(
            "Mark the session cookie Secure. Unset, it follows the scheme of base_url: "
            "https means secure. Set explicitly only to override that."
        ),
    )
    notify_on_startup: bool = Field(
        default=True,
        description=(
            "Send a low-priority notification when Talaia starts. Proves the notification "
            "path works after a deploy without having to break something."
        ),
    )

    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone for displayed times, such as Europe/Madrid. Unset means UTC. "
            "Storage is always UTC; this changes only what is rendered."
        ),
    )

    metrics_token: str | None = Field(
        default=None,
        description=(
            "Bearer token required to scrape /metrics. Unset, the endpoint is open, which "
            "is safe only while something else keeps it off the public internet."
        ),
    )

    login_max_attempts: int = Field(
        default=5,
        ge=1,
        description="Failed logins allowed from one address before attempts are refused.",
    )
    login_lockout_seconds: int = Field(
        default=60,
        ge=1,
        description="How long the first lockout lasts; it doubles with each further failure.",
    )
    login_max_lockout_seconds: int = Field(
        default=900,
        ge=1,
        description="Ceiling on the doubling lockout.",
    )

    proxy_ips: str | None = Field(
        default=None,
        description=(
            "Comma-separated addresses of reverse proxies whose X-Forwarded-* headers are "
            "trusted, or '*' for any. Unset, only 127.0.0.1 is trusted."
        ),
    )

    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None

    grafana_url: str | None = Field(
        default=None,
        description="Grafana instance to link to from the masthead. Hidden when unset.",
    )

    base_url: str = Field(
        default="http://localhost:9999",
        description="Public base URL of this instance, used for links in notifications.",
    )
    commit: str = Field(
        default="unknown",
        description="Commit this image was built from, reported by talaia_build_info.",
    )

    @field_validator("timezone")
    @classmethod
    def _require_known_timezone(cls, value: str | None) -> str | None:
        """Reject a timezone the system cannot resolve, at startup rather than at render."""
        if value:
            set_display_timezone(value)
        return value

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
    def cookie_secure(self) -> bool:
        """Whether the session cookie carries the Secure flag.

        A Secure cookie is never sent over plain http://, so a wrong value here looks like
        a login that succeeds and then silently bounces back to the form. Deriving it from
        ``base_url`` removes the way to get it wrong by default.
        """
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.base_url.lower().startswith("https://")

    @property
    def notifications_enabled(self) -> bool:
        """Whether a notifier is configured."""
        return bool(self.ntfy_url and self.ntfy_topic)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsing the environment on first use."""
    return Settings()
