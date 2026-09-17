"""Rendering of durations, timestamps, percentages and latencies."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from talaia.formatting import (
    format_clock,
    format_duration,
    format_latency,
    format_percentage,
    format_timestamp,
    set_display_timezone,
    timezone_label,
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


@pytest.fixture
def madrid() -> Iterator[None]:
    """Render in Europe/Madrid for one test, then put UTC back."""
    set_display_timezone("Europe/Madrid")
    try:
        yield
    finally:
        set_display_timezone(None)


class TestDisplayTimezone:
    def test_utc_by_default(self) -> None:
        assert format_timestamp(AT) == "2026-03-14 09:30:05 UTC"

    def test_a_configured_zone_shifts_the_clock_and_names_itself(self, madrid: None) -> None:
        """Same instant, different wall clock, and the label says which."""
        assert format_timestamp(AT) == "2026-03-14 10:30:05 CET"

    def test_summer_time_is_handled_by_the_zone_not_by_us(self, madrid: None) -> None:
        summer = datetime(2026, 7, 14, 9, 30, 5, tzinfo=UTC)

        assert format_timestamp(summer) == "2026-07-14 11:30:05 CEST"

    def test_the_clock_helper_follows_the_zone(self, madrid: None) -> None:
        assert format_clock(AT) == "10:30"

    def test_the_label_follows_the_zone(self, madrid: None) -> None:
        assert timezone_label(AT) == "CET"

    def test_none_means_utc(self) -> None:
        set_display_timezone("Europe/Madrid")
        set_display_timezone(None)

        assert format_timestamp(AT).endswith("UTC")

    def test_an_unknown_zone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown timezone"):
            set_display_timezone("Mars/Olympus_Mons")

    def test_an_aware_input_in_another_zone_is_converted(self, madrid: None) -> None:
        """Storage is UTC, but nothing should break if a value arrives with an offset."""
        tokyo = AT.astimezone(ZoneInfo("Asia/Tokyo"))

        assert format_timestamp(tokyo) == "2026-03-14 10:30:05 CET"
