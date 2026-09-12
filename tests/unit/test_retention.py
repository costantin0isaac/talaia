"""Tests for the pure parts of the maintenance task."""

from datetime import UTC, date, datetime

import pytest

from talaia.engine.retention import day_bounds


class TestDayBounds:
    def test_covers_the_whole_day_in_utc(self) -> None:
        start, end = day_bounds(date(2026, 3, 14))

        assert start == datetime(2026, 3, 14, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 15, 0, 0, tzinfo=UTC)

    def test_the_interval_is_half_open(self) -> None:
        """Midnight belongs to the next day, so a check is counted exactly once."""
        _, end_of_first = day_bounds(date(2026, 3, 14))
        start_of_second, _ = day_bounds(date(2026, 3, 15))

        assert end_of_first == start_of_second

    @pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25), date(2026, 12, 31)])
    def test_every_day_is_twenty_four_hours(self, day: date) -> None:
        """UTC has no daylight-saving transitions, unlike local time."""
        start, end = day_bounds(day)

        assert (end - start).total_seconds() == 86400
