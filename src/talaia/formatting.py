"""Rendering durations and timestamps for people to read.

Shared by the notifier, the web UI and the CLI, which must phrase the same facts the same
way. The display timezone is module state, set once at startup: threading it through every
caller would mean passing it down to individual table cells, and every one of them wants
the same answer.

Storage stays UTC everywhere. This affects only what a person reads.
"""

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24


def format_duration(seconds: int) -> str:
    """Render a downtime in the two largest units that apply."""
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds}s"

    minutes, remaining_seconds = divmod(seconds, SECONDS_PER_MINUTE)
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes}m {remaining_seconds}s" if remaining_seconds else f"{minutes}m"

    hours, remaining_minutes = divmod(minutes, MINUTES_PER_HOUR)
    if hours < HOURS_PER_DAY:
        return f"{hours}h {remaining_minutes}m" if remaining_minutes else f"{hours}h"

    days, remaining_hours = divmod(hours, HOURS_PER_DAY)
    return f"{days}d {remaining_hours}h" if remaining_hours else f"{days}d"


_display_zone: tzinfo = UTC


def set_display_timezone(name: str | None) -> None:
    """Choose the timezone timestamps are rendered in. ``None`` means UTC.

    Raises:
        ValueError: The name is not in the IANA database.
    """
    global _display_zone
    if not name:
        _display_zone = UTC
        return
    try:
        _display_zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"unknown timezone {name!r}; use an IANA name such as 'Europe/Madrid'"
        raise ValueError(msg) from exc


def display_timezone() -> tzinfo:
    """Return the timezone timestamps are currently rendered in."""
    return _display_zone


def format_timestamp(moment: datetime) -> str:
    """Render a moment as a wall clock in the display timezone, named so it is unambiguous."""
    return moment.astimezone(_display_zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_clock(moment: datetime) -> str:
    """Render just the time of day, for a chart axis."""
    return moment.astimezone(_display_zone).strftime("%H:%M")


def timezone_label(moment: datetime | None = None) -> str:
    """Return the short name of the display timezone, such as ``UTC`` or ``CEST``."""
    reference = moment or datetime.now(UTC)
    return reference.astimezone(_display_zone).strftime("%Z")


def format_percentage(ratio: float | None, *, digits: int = 2) -> str:
    """Render a 0-1 ratio as a percentage, or an em dash when there is nothing to show."""
    if ratio is None:
        return "—"
    return f"{ratio * 100:.{digits}f}%"


def format_latency(latency_ms: int | None) -> str:
    """Render a latency in milliseconds, or an em dash when the check failed."""
    if latency_ms is None:
        return "—"
    return f"{latency_ms} ms"


def format_window(hours: int) -> str:
    """Name a time window the way a person would say it: 1h, 24h, 7d, 30d."""
    if hours < 2 * HOURS_PER_DAY or hours % HOURS_PER_DAY:
        return f"{hours}h"
    return f"{hours // HOURS_PER_DAY}d"
