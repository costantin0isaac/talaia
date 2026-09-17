"""Tests for environment-driven settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from talaia.formatting import set_display_timezone
from talaia.settings import Settings, get_settings

VALID_URL = "postgresql+asyncpg://talaia:pw@db:5432/talaia"


def build(**overrides: object) -> Settings:
    """Construct settings from explicit values, ignoring any local .env file."""
    values: dict[str, object] = {"database_url": VALID_URL, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDatabaseUrl:
    def test_asyncpg_url_is_accepted(self) -> None:
        assert build().database_url == VALID_URL

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://talaia:pw@db:5432/talaia",
            "postgres://talaia:pw@db:5432/talaia",
            "postgresql+psycopg2://talaia:pw@db:5432/talaia",
            "sqlite+aiosqlite:///talaia.db",
        ],
    )
    def test_non_asyncpg_url_is_rejected(self, url: str) -> None:
        with pytest.raises(ValidationError, match="asyncpg"):
            build(database_url=url)

    def test_missing_url_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TALAIA_DATABASE_URL", raising=False)

        with pytest.raises(ValidationError, match="database_url"):
            Settings(_env_file=None)  # type: ignore[call-arg]


class TestDefaults:
    def test_defaults_are_applied(self) -> None:
        settings = build()

        assert settings.config_path == Path("config/monitors.yaml")
        assert settings.log_level == "INFO"
        assert settings.log_format == "json"
        assert settings.retention_days == 30
        assert settings.base_url == "http://localhost:9999"

    def test_retention_days_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            build(retention_days=0)

    @pytest.mark.parametrize("value", ["TRACE", "warn", ""])
    def test_invalid_log_level_is_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            build(log_level=value)

    def test_invalid_log_format_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build(log_format="logfmt")

    def test_unknown_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TALAIA_DATABASE_URL", VALID_URL)
        monkeypatch.setenv("TALAIA_SOMETHING_ELSE", "x")

        assert Settings(_env_file=None).database_url == VALID_URL  # type: ignore[call-arg]


class TestNotificationsEnabled:
    @pytest.mark.parametrize(
        ("url", "topic", "expected"),
        [
            ("https://ntfy.example.org", "alerts", True),
            ("https://ntfy.example.org", None, False),
            (None, "alerts", False),
            (None, None, False),
        ],
    )
    def test_requires_both_url_and_topic(
        self, url: str | None, topic: str | None, expected: bool
    ) -> None:
        assert build(ntfy_url=url, ntfy_topic=topic).notifications_enabled is expected


class TestEnvironmentParsing:
    def test_values_are_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TALAIA_DATABASE_URL", VALID_URL)
        monkeypatch.setenv("TALAIA_LOG_FORMAT", "console")
        monkeypatch.setenv("TALAIA_RETENTION_DAYS", "7")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.log_format == "console"
        assert settings.retention_days == 7

    def test_get_settings_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TALAIA_DATABASE_URL", VALID_URL)
        get_settings.cache_clear()

        assert get_settings() is get_settings()

        get_settings.cache_clear()


class TestCookieSecure:
    def test_follows_an_https_base_url(self) -> None:
        assert build(base_url="https://talaia.example.org").cookie_secure is True

    def test_follows_an_http_base_url(self) -> None:
        """Plain http:// would never send a Secure cookie back, so it must not be set."""
        assert build(base_url="http://10.0.0.5:9999").cookie_secure is False

    def test_the_default_base_url_is_not_secure(self) -> None:
        assert build().cookie_secure is False

    @pytest.mark.parametrize("explicit", [True, False])
    def test_an_explicit_setting_wins(self, explicit: bool) -> None:
        secure_url = build(base_url="https://x.example", session_cookie_secure=explicit)
        plain_url = build(base_url="http://x.example", session_cookie_secure=explicit)

        assert secure_url.cookie_secure is explicit
        assert plain_url.cookie_secure is explicit


class TestProxyIps:
    def test_defaults_to_unset(self) -> None:
        assert build().proxy_ips is None

    def test_accepts_a_list(self) -> None:
        assert build(proxy_ips="10.0.0.6,10.0.0.7").proxy_ips == "10.0.0.6,10.0.0.7"


class TestTimezone:
    def test_defaults_to_unset(self) -> None:
        assert build().timezone is None

    def test_a_known_zone_is_accepted(self) -> None:
        assert build(timezone="Europe/Madrid").timezone == "Europe/Madrid"
        set_display_timezone(None)

    def test_an_unknown_zone_fails_at_startup(self) -> None:
        """Better a refusal to start than every timestamp silently wrong."""
        with pytest.raises(ValidationError, match="unknown timezone"):
            build(timezone="Mars/Olympus_Mons")


class TestStartupNotification:
    def test_it_is_on_by_default(self) -> None:
        """A deploy that silently stopped notifying is the failure worth catching."""
        assert build().notify_on_startup is True

    def test_it_can_be_turned_off(self) -> None:
        assert build(notify_on_startup=False).notify_on_startup is False
