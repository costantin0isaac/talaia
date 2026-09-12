"""Rendering durations and timestamps for people to read.

Shared by the notifier and the web UI, which must phrase the same facts the same way.
"""

from datetime import UTC, datetime

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


def format_timestamp(moment: datetime) -> str:
    """Render a moment as a UTC wall clock, matching the log timestamps."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


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
