"""Rendering of durations, timestamps, percentages and latencies."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from talaia.formatting import (
    format_duration,
    format_latency,
    format_percentage,
    format_timestamp,
)

AT = datetime(2026, 3, 14, 9, 30, 5, tzinfo=UTC)


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (45, "45s"),
            (60, "1m"),
            (372, "6m 12s"),
            (3600, "1h"),
            (3900, "1h 5m"),
            (86400, "1d"),
            (93600, "1d 2h"),
        ],
    )
    def test_renders_the_two_largest_units(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestFormatTimestamp:
    def test_renders_utc(self) -> None:
        assert format_timestamp(AT) == "2026-03-14 09:30:05 UTC"

    def test_converts_from_another_offset(self) -> None:
        """Timestamps always read as UTC, whatever offset the row carried."""
        madrid = AT.astimezone(timezone(timedelta(hours=2)))

        assert format_timestamp(madrid) == "2026-03-14 09:30:05 UTC"


class TestFormatPercentage:
    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [(1.0, "100.00%"), (0.5, "50.00%"), (0.9999, "99.99%"), (0.0, "0.00%")],
    )
    def test_renders_a_ratio(self, ratio: float, expected: str) -> None:
        assert format_percentage(ratio) == expected

    def test_nothing_to_show_is_a_dash(self) -> None:
        """A monitor with no checks yet has no uptime, which is not the same as zero."""
        assert format_percentage(None) == "—"


class TestFormatLatency:
    def test_renders_milliseconds(self) -> None:
        assert format_latency(42) == "42 ms"

    def test_a_failed_check_has_no_latency(self) -> None:
        assert format_latency(None) == "—"
