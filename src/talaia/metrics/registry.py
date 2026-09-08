"""Prometheus collectors.

Per-monitor values are read from the database at scrape time rather than kept in
module-level gauges the scheduler has to keep in step, which removes a whole class of
drift bugs. The only exception is the check counter, which must survive across scrapes and
is therefore held in memory for the life of the process.

Label cardinality is deliberately bounded: only ``monitor``, ``type``, ``group``,
``result`` and ``status`` are ever used. A URL, an IP address, an error message or a
status code must never become a label.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    disable_created_metrics,
    generate_latest,
)

from talaia.db.models import MonitorStatus

CONTENT_TYPE = CONTENT_TYPE_LATEST

disable_created_metrics()  # type: ignore[no-untyped-call]


@dataclass(frozen=True, slots=True)
class MonitorSample:
    """One monitor's current state, as the metrics endpoint sees it."""

    name: str
    type: str
    group: str | None
    status: MonitorStatus
    last_latency_ms: int | None
    consecutive_failures: int


class Metrics:
    """Owns the process-lifetime counters and renders the exposition text."""

    def __init__(self, *, version: str, commit: str) -> None:
        self._registry = CollectorRegistry()
        self._checks = Counter(
            "talaia_checks",
            "Checks performed since this process started.",
            ["monitor", "result"],
            registry=self._registry,
        )
        build_info = Gauge(
            "talaia_build_info",
            "Build information for the running instance.",
            ["version", "commit"],
            registry=self._registry,
        )
        build_info.labels(version=version, commit=commit).set(1)

    def record_check(self, monitor: str, *, success: bool) -> None:
        """Count one completed check."""
        self._checks.labels(monitor=monitor, result="success" if success else "failure").inc()

    def render(self, samples: Sequence[MonitorSample]) -> bytes:
        """Return the full exposition text for one scrape."""
        return generate_latest(self._registry) + generate_latest(_scrape_registry(samples))


def _scrape_registry(samples: Sequence[MonitorSample]) -> CollectorRegistry:
    """Build a registry describing the state of every monitor right now."""
    registry = CollectorRegistry()

    up = Gauge(
        "talaia_check_up",
        "Whether the monitor is up. Absent while its status is unknown or paused.",
        ["monitor", "type", "group"],
        registry=registry,
    )
    duration = Gauge(
        "talaia_check_duration_seconds",
        "Latency of the most recent check.",
        ["monitor", "type", "group"],
        registry=registry,
    )
    consecutive_failures = Gauge(
        "talaia_monitor_consecutive_failures",
        "Consecutive failed checks, useful for detecting flapping.",
        ["monitor"],
        registry=registry,
    )
    monitors_total = Gauge(
        "talaia_monitors_total",
        "Number of active monitors in each status.",
        ["status"],
        registry=registry,
    )

    counts = dict.fromkeys(MonitorStatus, 0)

    for sample in samples:
        labels = {"monitor": sample.name, "type": sample.type, "group": sample.group or ""}
        counts[sample.status] += 1

        if sample.status is MonitorStatus.UP:
            up.labels(**labels).set(1)
        elif sample.status is MonitorStatus.DOWN:
            up.labels(**labels).set(0)

        if sample.last_latency_ms is not None:
            duration.labels(**labels).set(sample.last_latency_ms / 1000)

        consecutive_failures.labels(monitor=sample.name).set(sample.consecutive_failures)

    for status, count in counts.items():
        monitors_total.labels(status=status.value).set(count)

    return registry
