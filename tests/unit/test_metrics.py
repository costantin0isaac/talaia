"""Tests for the Prometheus exposition."""

import pytest

from talaia.db.models import MonitorStatus
from talaia.metrics.registry import Metrics, MonitorSample

LABEL_NAMES = {"monitor", "type", "group", "result", "status", "version", "commit"}


def sample(
    name: str = "web",
    *,
    monitor_type: str = "http",
    group: str | None = "services",
    status: MonitorStatus = MonitorStatus.UP,
    last_latency_ms: int | None = 42,
    consecutive_failures: int = 0,
    last_expires_in_days: int | None = None,
) -> MonitorSample:
    return MonitorSample(
        name=name,
        type=monitor_type,
        group=group,
        status=status,
        last_latency_ms=last_latency_ms,
        consecutive_failures=consecutive_failures,
        last_expires_in_days=last_expires_in_days,
    )


def render(*samples: MonitorSample, metrics: Metrics | None = None) -> str:
    collectors = metrics or Metrics(version="1.2.3", commit="abc1234")
    return collectors.render(list(samples)).decode()


def series(text: str, metric: str) -> list[str]:
    """Return the sample lines for one metric name."""
    return [
        line
        for line in text.splitlines()
        if line.startswith(f"{metric}{{") or line.startswith(f"{metric} ")
    ]


class TestBuildInfo:
    def test_reports_version_and_commit(self) -> None:
        text = render()

        assert 'talaia_build_info{commit="abc1234",version="1.2.3"} 1.0' in text


class TestCheckUp:
    def test_up_monitor_is_one(self) -> None:
        text = render(sample(status=MonitorStatus.UP))

        assert 'talaia_check_up{group="services",monitor="web",type="http"} 1.0' in text

    def test_down_monitor_is_zero(self) -> None:
        text = render(sample(status=MonitorStatus.DOWN))

        assert 'talaia_check_up{group="services",monitor="web",type="http"} 0.0' in text

    @pytest.mark.parametrize("status", [MonitorStatus.UNKNOWN, MonitorStatus.PAUSED])
    def test_unknown_and_paused_are_absent(self, status: MonitorStatus) -> None:
        """Section 9: absent rather than a misleading zero."""
        text = render(sample(status=status))

        assert series(text, "talaia_check_up") == []

    def test_missing_group_becomes_an_empty_label(self) -> None:
        text = render(sample(group=None))

        assert 'group=""' in text


class TestDuration:
    def test_latency_is_reported_in_seconds(self) -> None:
        text = render(sample(last_latency_ms=1500))

        assert (
            'talaia_check_duration_seconds{group="services",monitor="web",type="http"} 1.5' in text
        )

    def test_absent_when_there_is_no_latency(self) -> None:
        text = render(sample(last_latency_ms=None))

        assert series(text, "talaia_check_duration_seconds") == []


class TestConsecutiveFailures:
    def test_reported_per_monitor(self) -> None:
        text = render(sample(consecutive_failures=4))

        assert 'talaia_monitor_consecutive_failures{monitor="web"} 4.0' in text


class TestMonitorsTotal:
    def test_counts_each_status(self) -> None:
        text = render(
            sample("a", status=MonitorStatus.UP),
            sample("b", status=MonitorStatus.UP),
            sample("c", status=MonitorStatus.DOWN),
            sample("d", status=MonitorStatus.PAUSED),
        )

        assert 'talaia_monitors_total{status="up"} 2.0' in text
        assert 'talaia_monitors_total{status="down"} 1.0' in text
        assert 'talaia_monitors_total{status="paused"} 1.0' in text

    def test_statuses_with_no_monitors_are_still_reported_as_zero(self) -> None:
        """A missing series would break a Grafana panel; an explicit zero does not."""
        text = render(sample(status=MonitorStatus.UP))

        assert 'talaia_monitors_total{status="down"} 0.0' in text
        assert 'talaia_monitors_total{status="unknown"} 0.0' in text


class TestChecksTotal:
    def test_counter_survives_across_scrapes(self) -> None:
        """Section 9: a process-lifetime counter, not derived from the database."""
        metrics = Metrics(version="1", commit="c")

        metrics.record_check("web", success=True)
        render(sample(), metrics=metrics)
        metrics.record_check("web", success=True)
        text = render(sample(), metrics=metrics)

        assert 'talaia_checks_total{monitor="web",result="success"} 2.0' in text

    def test_failures_are_counted_separately(self) -> None:
        metrics = Metrics(version="1", commit="c")

        metrics.record_check("web", success=True)
        metrics.record_check("web", success=False)
        metrics.record_check("web", success=False)
        text = render(metrics=metrics)

        assert 'talaia_checks_total{monitor="web",result="success"} 1.0' in text
        assert 'talaia_checks_total{monitor="web",result="failure"} 2.0' in text

    def test_the_metric_is_not_named_total_total(self) -> None:
        metrics = Metrics(version="1", commit="c")
        metrics.record_check("web", success=True)

        assert "talaia_checks_total_total" not in render(metrics=metrics)


class TestCardinality:
    def test_only_the_permitted_labels_are_used(self) -> None:
        """Section 9: never a URL, an IP, an error message or a status code."""
        text = render(
            sample("web", group="services"),
            sample("router", monitor_type="icmp", group=None, status=MonitorStatus.DOWN),
        )

        used = {
            pair.split("=", 1)[0]
            for line in text.splitlines()
            if not line.startswith("#") and "{" in line
            for pair in line.split("{", 1)[1].rsplit("}", 1)[0].split(",")
            if pair
        }

        assert used <= LABEL_NAMES

    def test_no_created_series_are_emitted(self) -> None:
        metrics = Metrics(version="1", commit="c")
        metrics.record_check("web", success=True)

        assert "_created" not in render(metrics=metrics)


class TestEmptyInstance:
    def test_renders_without_monitors(self) -> None:
        text = render()

        assert "talaia_build_info" in text
        assert 'talaia_monitors_total{status="up"} 0.0' in text


class TestCertificateDays:
    def test_a_tls_monitor_exports_its_remaining_days(self) -> None:
        text = render(sample("cert", monitor_type="tls", last_expires_in_days=45))

        assert (
            'talaia_certificate_days_remaining{group="services",monitor="cert",type="tls"} 45.0'
            in text
        )

    def test_an_expired_certificate_exports_a_negative_value(self) -> None:
        """Below zero is the signal Grafana needs to distinguish 'gone' from 'soon'."""
        text = render(sample("cert", monitor_type="tls", last_expires_in_days=-3))

        assert series(text, "talaia_certificate_days_remaining")[0].endswith("-3.0")

    def test_monitors_without_a_certificate_export_no_series(self) -> None:
        """An HTTP monitor has no expiry, and an empty series would read as zero days."""
        text = render(sample("web"), sample("ping", monitor_type="icmp"))

        assert series(text, "talaia_certificate_days_remaining") == []

    def test_the_metric_is_documented(self) -> None:
        text = render(sample("cert", monitor_type="tls", last_expires_in_days=45))

        assert "# HELP talaia_certificate_days_remaining" in text

    def test_no_new_label_names_are_introduced(self) -> None:
        """Label cardinality stays bounded; a hostname must never become a label."""
        text = render(sample("cert", monitor_type="tls", last_expires_in_days=45))

        for line in series(text, "talaia_certificate_days_remaining"):
            labels = line[line.index("{") + 1 : line.index("}")]
            names = {pair.split("=")[0] for pair in labels.split(",")}
            assert names <= LABEL_NAMES
