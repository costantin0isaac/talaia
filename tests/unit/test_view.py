"""The pure view layer behind the dashboard templates."""

from datetime import UTC, datetime, timedelta

import pytest

from talaia.config.schema import MonitorType
from talaia.db.models import CheckResult, Incident, Monitor, MonitorState, MonitorStatus
from talaia.web import view

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)


def result(
    *, minutes_ago: int, success: bool = True, latency_ms: int | None = 20, error: str | None = None
) -> CheckResult:
    return CheckResult(
        monitor_id=1,
        checked_at=NOW - timedelta(minutes=minutes_ago),
        success=success,
        latency_ms=latency_ms,
        status_code=200 if success else None,
        error=error,
    )


def monitor(name: str = "web", *, group: str | None = "services") -> Monitor:
    return Monitor(
        name=name,
        type=MonitorType.HTTP,
        target="http://10.0.0.1",
        group_name=group,
        description=None,
        interval_seconds=60,
        timeout_seconds=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        active=True,
        config={},
    )


def state(status: MonitorStatus = MonitorStatus.UP, **kwargs: object) -> MonitorState:
    defaults: dict[str, object] = {
        "monitor_id": 1,
        "status": status,
        "consecutive_failures": 0,
        "consecutive_successes": 1,
        "last_checked_at": NOW,
        "last_latency_ms": 20,
        "last_error": None,
        "status_changed_at": NOW,
    }
    defaults.update(kwargs)
    return MonitorState(**defaults)


class TestStatusStrip:
    def test_is_padded_to_a_fixed_width(self) -> None:
        """Every strip is the same length, so the dashboard columns line up."""
        segments = view.status_strip([result(minutes_ago=1)], size=5)

        assert len(segments) == 5
        assert [segment.state for segment in segments] == ["empty"] * 4 + ["ok"]

    def test_oldest_first(self) -> None:
        newest = result(minutes_ago=1, success=False)
        oldest = result(minutes_ago=5)

        segments = view.status_strip([newest, oldest], size=2)

        assert [segment.state for segment in segments] == ["ok", "fail"]

    def test_only_the_newest_fit(self) -> None:
        results = [result(minutes_ago=index) for index in range(10)]

        segments = view.status_strip(results, size=3)

        assert len(segments) == 3

    def test_a_successful_check_is_labelled_with_its_latency(self) -> None:
        segments = view.status_strip([result(minutes_ago=1, latency_ms=42)], size=1)

        assert "42 ms" in segments[0].title

    def test_a_failed_check_is_labelled_with_its_error(self) -> None:
        segments = view.status_strip(
            [result(minutes_ago=1, success=False, latency_ms=None, error="refused")], size=1
        )

        assert "refused" in segments[0].title

    def test_no_results_is_an_empty_strip(self) -> None:
        segments = view.status_strip([], size=4)

        assert {segment.state for segment in segments} == {"empty"}


class TestMonitorRow:
    def test_projects_state(self) -> None:
        row = view.monitor_row(monitor(), state(MonitorStatus.DOWN), uptime_24h=0.5)

        assert row.status == "down"
        assert row.status_label == "DOWN"
        assert row.uptime_24h == "50.00%"
        assert row.last_latency == "20 ms"

    def test_a_monitor_without_state_reads_as_unknown(self) -> None:
        row = view.monitor_row(monitor(), None)

        assert row.status == "unknown"
        assert row.last_checked == "never"
        assert row.last_latency == "—"
        assert row.uptime_24h == "—"


class TestGroupRows:
    def test_groups_are_alphabetical(self) -> None:
        rows = [
            view.monitor_row(monitor("a"), None),
            view.monitor_row(monitor("b"), None),
        ]

        groups = view.group_rows(rows, {"a": "web", "b": "infra"})

        assert [group.name for group in groups] == ["infra", "web"]

    def test_ungrouped_monitors_come_last(self) -> None:
        """A monitor with no group should not sort above the named groups."""
        rows = [view.monitor_row(monitor("a"), None), view.monitor_row(monitor("b"), None)]

        groups = view.group_rows(rows, {"a": None, "b": "web"})

        assert [group.name for group in groups] == ["web", "ungrouped"]

    def test_rows_keep_their_order_within_a_group(self) -> None:
        rows = [view.monitor_row(monitor(name), None) for name in ("one", "two", "three")]

        groups = view.group_rows(rows, dict.fromkeys(("one", "two", "three"), "infra"))

        assert [row.name for row in groups[0].rows] == ["one", "two", "three"]

    def test_no_monitors_is_no_groups(self) -> None:
        assert view.group_rows([], {}) == ()


class TestLatencyChart:
    def test_no_results_has_no_data(self) -> None:
        chart = view.latency_chart([])

        assert chart.has_data is False
        assert chart.polyline == ""

    def test_points_run_left_to_right_in_time(self) -> None:
        results = [result(minutes_ago=0), result(minutes_ago=30), result(minutes_ago=60)]

        chart = view.latency_chart(results)

        xs = [point.x for point in chart.points]
        assert xs == sorted(xs)

    def test_the_tallest_latency_sits_at_the_top(self) -> None:
        results = [result(minutes_ago=10, latency_ms=10), result(minutes_ago=0, latency_ms=100)]

        chart = view.latency_chart(results)

        tallest = min(chart.points, key=lambda point: point.y)
        assert chart.max_latency == 100
        assert tallest.y == pytest.approx(chart.plot_top)

    def test_a_single_result_does_not_divide_by_a_zero_span(self) -> None:
        chart = view.latency_chart([result(minutes_ago=0)])

        assert len(chart.points) == 1

    def test_failures_are_marked_but_not_plotted(self) -> None:
        results = [
            result(minutes_ago=10),
            result(minutes_ago=5, success=False, latency_ms=None, error="timeout"),
        ]

        chart = view.latency_chart(results)

        assert len(chart.points) == 1
        assert len(chart.failures) == 1
        assert chart.sample_count == 2

    def test_every_point_stays_inside_the_plot_area(self) -> None:
        """Not merely inside the viewBox: a point over the gutters would sit on a label."""
        results = [result(minutes_ago=index, latency_ms=index * 7 + 1) for index in range(20)]

        chart = view.latency_chart(results)

        assert all(chart.plot_left <= point.x <= chart.plot_right for point in chart.points)
        assert all(chart.plot_top <= point.y <= chart.plot_bottom for point in chart.points)

    def test_only_failures_still_draws_something(self) -> None:
        chart = view.latency_chart(
            [result(minutes_ago=1, success=False, latency_ms=None, error="down")]
        )

        assert chart.has_data is True
        assert chart.polyline == ""


class TestIncidentRows:
    def test_a_resolved_incident_shows_its_recorded_duration(self) -> None:
        incident = Incident(
            monitor_id=1,
            started_at=NOW - timedelta(hours=1),
            resolved_at=NOW - timedelta(minutes=54),
            duration_seconds=360,
            cause="timeout",
        )

        rows = view.incident_rows([incident], now=NOW)

        assert rows[0].duration == "6m"
        assert rows[0].ongoing is False

    def test_an_open_incident_is_measured_up_to_now(self) -> None:
        incident = Incident(
            monitor_id=1,
            started_at=NOW - timedelta(minutes=5),
            resolved_at=None,
            duration_seconds=None,
            cause="refused",
        )

        rows = view.incident_rows([incident], now=NOW)

        assert rows[0].ongoing is True
        assert rows[0].duration == "5m"
        assert rows[0].resolved_at == "—"


class TestChartAxes:
    def test_the_latency_axis_runs_from_zero_to_the_peak(self) -> None:
        results = [result(minutes_ago=10, latency_ms=10), result(minutes_ago=0, latency_ms=80)]

        chart = view.latency_chart(results)

        assert [tick.label for tick in chart.latency_ticks] == ["0", "40", "80"]

    def test_the_zero_tick_sits_on_the_baseline(self) -> None:
        chart = view.latency_chart([result(minutes_ago=0, latency_ms=50)])

        zero = next(tick for tick in chart.latency_ticks if tick.label == "0")
        assert zero.position == pytest.approx(chart.plot_bottom)

    def test_the_time_axis_is_labelled_as_a_clock(self) -> None:
        results = [result(minutes_ago=60), result(minutes_ago=30), result(minutes_ago=0)]

        chart = view.latency_chart(results)

        assert [tick.label for tick in chart.time_ticks] == ["11:00", "11:30", "12:00"]

    def test_time_ticks_span_the_plot_area(self) -> None:
        results = [result(minutes_ago=60), result(minutes_ago=0)]

        chart = view.latency_chart(results)

        assert chart.time_ticks[0].position == pytest.approx(chart.plot_left)
        assert chart.time_ticks[-1].position == pytest.approx(chart.plot_right)

    def test_a_single_reading_does_not_repeat_its_tick(self) -> None:
        """Three identical labels stacked on one pixel read as a rendering bug."""
        chart = view.latency_chart([result(minutes_ago=0)])

        assert len(chart.time_ticks) == 1

    def test_an_empty_chart_has_no_ticks(self) -> None:
        chart = view.latency_chart([])

        assert chart.latency_ticks == ()
        assert chart.time_ticks == ()
