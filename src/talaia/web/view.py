"""View models for the dashboard.

Everything the templates need is computed here, so the markup only interpolates values.
The functions are pure, which is what makes the presentation layer testable at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from talaia.db.models import CheckResult, Incident, Monitor, MonitorState, MonitorStatus
from talaia.formatting import format_duration, format_latency, format_percentage, format_timestamp

STRIP_SIZE = 40

CHART_WIDTH = 720
CHART_HEIGHT = 180
CHART_PADDING = 8

SegmentState = Literal["ok", "fail", "empty"]


@dataclass(frozen=True, slots=True)
class Segment:
    """One check in the status strip."""

    state: SegmentState
    title: str


@dataclass(frozen=True, slots=True)
class MonitorRow:
    """A monitor as one line of the dashboard."""

    name: str
    type: str
    target: str
    status: str
    status_label: str
    last_latency: str
    uptime_24h: str
    last_checked: str
    last_error: str | None
    certificate: str | None
    segments: tuple[Segment, ...]


@dataclass(frozen=True, slots=True)
class Group:
    """Monitors sharing a ``group`` in the YAML."""

    name: str
    rows: tuple[MonitorRow, ...]


@dataclass(frozen=True, slots=True)
class SummaryBar:
    """The headline figures above the dashboard."""

    total: int
    up: int
    down: int
    unknown: int
    paused: int
    open_incidents: int
    uptime_24h: str


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One plotted latency."""

    x: float
    y: float
    label: str


@dataclass(frozen=True, slots=True)
class LatencyChart:
    """A latency series already projected into SVG coordinates."""

    width: int
    height: int
    polyline: str
    points: tuple[ChartPoint, ...]
    failures: tuple[float, ...]
    max_latency: int
    sample_count: int

    @property
    def has_data(self) -> bool:
        """Whether there is anything worth drawing."""
        return bool(self.points or self.failures)


@dataclass(frozen=True, slots=True)
class IncidentRow:
    """An incident as one line of the detail page's table."""

    started_at: str
    resolved_at: str
    duration: str
    cause: str
    ongoing: bool


@dataclass(frozen=True, slots=True)
class MonitorDetailView:
    """Everything the monitor detail page renders."""

    row: MonitorRow
    description: str | None
    interval_seconds: int
    timeout_seconds: int
    failure_threshold: int
    recovery_threshold: int
    enabled: bool
    active: bool
    group: str | None
    uptime_7d: str
    uptime_30d: str
    chart: LatencyChart
    incidents: tuple[IncidentRow, ...]


def status_strip(results: Sequence[CheckResult], *, size: int = STRIP_SIZE) -> tuple[Segment, ...]:
    """Render the newest ``size`` results oldest-first, padded so every strip lines up."""
    newest_first = list(results[:size])
    padding = tuple(
        Segment(state="empty", title="no data") for _ in range(size - len(newest_first))
    )
    segments = tuple(
        Segment(
            state="ok" if result.success else "fail",
            title=_segment_title(result),
        )
        for result in reversed(newest_first)
    )
    return padding + segments


def _segment_title(result: CheckResult) -> str:
    when = format_timestamp(result.checked_at)
    if result.success:
        return f"{when} · {format_latency(result.latency_ms)}"
    return f"{when} · {result.error or 'failed'}"


def format_certificate(expires_in_days: int | None) -> str | None:
    """Render remaining certificate validity, or ``None`` for a monitor that has none."""
    if expires_in_days is None:
        return None
    if expires_in_days < 0:
        return f"expired {abs(expires_in_days)}d ago"
    return f"expires in {expires_in_days}d"


def monitor_row(
    monitor: Monitor,
    state: MonitorState | None,
    *,
    results: Sequence[CheckResult] = (),
    uptime_24h: float | None = None,
) -> MonitorRow:
    """Project a monitor and its state into one dashboard line."""
    status = state.status if state is not None else MonitorStatus.UNKNOWN
    latency = state.last_latency_ms if state is not None else None
    checked_at = state.last_checked_at if state is not None else None
    expires_in = state.last_expires_in_days if state is not None else None
    return MonitorRow(
        name=monitor.name,
        type=monitor.type.value,
        target=monitor.target,
        status=status.value,
        status_label=status.value.upper(),
        last_latency=format_latency(latency),
        uptime_24h=format_percentage(uptime_24h),
        last_checked=format_timestamp(checked_at) if checked_at else "never",
        last_error=state.last_error if state is not None else None,
        certificate=format_certificate(expires_in),
        segments=status_strip(results),
    )


def group_rows(rows: Sequence[MonitorRow], groups: dict[str, str | None]) -> tuple[Group, ...]:
    """Bucket rows by their monitor's group, ungrouped monitors last."""
    buckets: dict[str, list[MonitorRow]] = {}
    for row in rows:
        buckets.setdefault(groups.get(row.name) or "", []).append(row)

    named = sorted((name for name in buckets if name), key=str.lower)
    ordered = [*named, ""] if "" in buckets else named
    return tuple(Group(name=name or "ungrouped", rows=tuple(buckets[name])) for name in ordered)


def latency_chart(
    results: Sequence[CheckResult],
    *,
    width: int = CHART_WIDTH,
    height: int = CHART_HEIGHT,
) -> LatencyChart:
    """Project a latency series into SVG coordinates, oldest on the left.

    Coordinates are computed here rather than in the template so the geometry can be
    tested, and so the page needs no charting library.
    """
    ordered = sorted(results, key=lambda result: result.checked_at)
    successes = [result for result in ordered if result.success and result.latency_ms is not None]
    failures = [result for result in ordered if not result.success]

    if not ordered:
        return LatencyChart(
            width=width,
            height=height,
            polyline="",
            points=(),
            failures=(),
            max_latency=0,
            sample_count=0,
        )

    max_latency = max((result.latency_ms or 0 for result in successes), default=0) or 1
    span = _time_span(ordered)
    plot_width = width - 2 * CHART_PADDING
    plot_height = height - 2 * CHART_PADDING

    def x_for(moment: datetime) -> float:
        offset = (moment - ordered[0].checked_at).total_seconds()
        return CHART_PADDING + plot_width * (offset / span)

    points = tuple(
        ChartPoint(
            x=round(x_for(result.checked_at), 2),
            y=round(CHART_PADDING + plot_height * (1 - (result.latency_ms or 0) / max_latency), 2),
            label=_segment_title(result),
        )
        for result in successes
    )

    return LatencyChart(
        width=width,
        height=height,
        polyline=" ".join(f"{point.x},{point.y}" for point in points),
        points=points,
        failures=tuple(round(x_for(result.checked_at), 2) for result in failures),
        max_latency=max_latency,
        sample_count=len(ordered),
    )


def _time_span(results: Sequence[CheckResult]) -> float:
    """Return the seconds covered, never zero so a single point does not divide by it."""
    span = (results[-1].checked_at - results[0].checked_at).total_seconds()
    return span or 1.0


def incident_rows(incidents: Sequence[Incident], *, now: datetime) -> tuple[IncidentRow, ...]:
    """Project incidents into table rows, an unresolved one measured up to ``now``."""
    rows = []
    for incident in incidents:
        ongoing = incident.resolved_at is None
        seconds = (
            incident.duration_seconds
            if incident.duration_seconds is not None
            else int((now - incident.started_at).total_seconds())
        )
        rows.append(
            IncidentRow(
                started_at=format_timestamp(incident.started_at),
                resolved_at=format_timestamp(incident.resolved_at) if incident.resolved_at else "—",
                duration=format_duration(max(seconds, 0)),
                cause=incident.cause,
                ongoing=ongoing,
            )
        )
    return tuple(rows)
