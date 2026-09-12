"""The committed Grafana dashboard and Prometheus rules must match the metrics we export.

A dashboard in a repository rots silently: nothing breaks when a metric is renamed, it
just quietly stops drawing. These tests tie both files to the real exposition.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from talaia.db.models import MonitorStatus
from talaia.metrics.registry import Metrics, MonitorSample

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "grafana" / "talaia-dashboard.json"
RULES_PATH = REPO_ROOT / "prometheus" / "talaia-rules.yml"

METRIC_PATTERN = re.compile(r"\btalaia_[a-z_]+\b")

REQUIRED_SEVERITIES = {"warning", "critical"}


def exported_metric_names() -> set[str]:
    """Render every metric Talaia can emit, and return the names."""
    metrics = Metrics(version="0.1.0", commit="abc1234")
    metrics.record_check("web", success=True)
    metrics.record_check("web", success=False)
    samples = [
        MonitorSample(
            name="web",
            type="http",
            group="services",
            status=MonitorStatus.UP,
            last_latency_ms=21,
            consecutive_failures=0,
        ),
        MonitorSample(
            name="cert",
            type="tls",
            group="infra",
            status=MonitorStatus.UP,
            last_latency_ms=35,
            consecutive_failures=0,
            last_expires_in_days=45,
        ),
    ]
    text = metrics.render(samples).decode()
    return {
        line.split("{")[0].split(" ")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def rules() -> dict[str, Any]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def exported() -> set[str]:
    return exported_metric_names()


def dashboard_expressions(dashboard: dict[str, Any]) -> list[str]:
    return [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]


def alert_rules(rules: dict[str, Any]) -> list[dict[str, Any]]:
    return [rule for group in rules["groups"] for rule in group["rules"]]


class TestDashboardStructure:
    def test_it_is_valid_json(self, dashboard: dict[str, Any]) -> None:
        assert dashboard["title"]
        assert dashboard["uid"] == "talaia-overview"

    def test_panel_ids_are_unique(self, dashboard: dict[str, Any]) -> None:
        """Grafana silently drops the second panel sharing an id."""
        ids = [panel["id"] for panel in dashboard["panels"]]

        assert len(ids) == len(set(ids))

    def test_every_panel_has_a_query(self, dashboard: dict[str, Any]) -> None:
        for panel in dashboard["panels"]:
            assert panel.get("targets"), f"{panel['title']} has no targets"
            for target in panel["targets"]:
                assert target["expr"].strip()

    def test_panels_do_not_overlap_on_the_grid(self, dashboard: dict[str, Any]) -> None:
        occupied: set[tuple[int, int]] = set()
        for panel in dashboard["panels"]:
            box = panel["gridPos"]
            cells = {
                (x, y)
                for x in range(box["x"], box["x"] + box["w"])
                for y in range(box["y"], box["y"] + box["h"])
            }
            assert not cells & occupied, f"{panel['title']} overlaps another panel"
            occupied |= cells

    def test_the_grid_is_24_columns_wide(self, dashboard: dict[str, Any]) -> None:
        for panel in dashboard["panels"]:
            box = panel["gridPos"]
            assert box["x"] + box["w"] <= 24, f"{panel['title']} runs off the grid"

    def test_the_datasource_is_a_variable(self, dashboard: dict[str, Any]) -> None:
        """A hard-coded datasource uid makes the dashboard unimportable elsewhere."""
        for panel in dashboard["panels"]:
            assert panel["datasource"]["uid"] == "${DS_PROMETHEUS}"

    def test_the_expected_variables_exist(self, dashboard: dict[str, Any]) -> None:
        names = {variable["name"] for variable in dashboard["templating"]["list"]}

        assert names == {"DS_PROMETHEUS", "group", "monitor"}


class TestDashboardMetrics:
    def test_every_referenced_metric_is_exported(
        self, dashboard: dict[str, Any], exported: set[str]
    ) -> None:
        for expression in dashboard_expressions(dashboard):
            for metric in METRIC_PATTERN.findall(expression):
                assert metric in exported, f"{metric} is not exported by /metrics"

    def test_the_variables_query_an_exported_metric(
        self, dashboard: dict[str, Any], exported: set[str]
    ) -> None:
        for variable in dashboard["templating"]["list"]:
            definition = variable.get("definition", "")
            for metric in METRIC_PATTERN.findall(definition):
                assert metric in exported

    def test_only_approved_labels_are_used(self, dashboard: dict[str, Any]) -> None:
        """Cardinality discipline: a hostname or status code must never become a label."""
        allowed = {"monitor", "type", "group", "result", "status", "version", "commit", "job"}
        for expression in dashboard_expressions(dashboard):
            for label in re.findall(r"(\w+)\s*=~?\s*\"", expression):
                assert label in allowed, f"unexpected label {label!r}"


class TestAlertRules:
    def test_alert_names_are_unique(self, rules: dict[str, Any]) -> None:
        names = [rule["alert"] for rule in alert_rules(rules)]

        assert len(names) == len(set(names))

    def test_every_alert_is_complete(self, rules: dict[str, Any]) -> None:
        for rule in alert_rules(rules):
            assert rule["expr"].strip(), rule["alert"]
            assert rule["for"], f"{rule['alert']} has no 'for', so it fires on one scrape"
            assert rule["labels"]["severity"] in REQUIRED_SEVERITIES, rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]
            assert rule["annotations"]["description"], rule["alert"]

    def test_every_referenced_metric_is_exported(
        self, rules: dict[str, Any], exported: set[str]
    ) -> None:
        for rule in alert_rules(rules):
            for metric in METRIC_PATTERN.findall(rule["expr"]):
                assert metric in exported, f"{rule['alert']} uses unexported {metric}"

    def test_nothing_alerts_on_a_single_monitor_being_down(self, rules: dict[str, Any]) -> None:
        """Talaia already notifies about that; two alerts per event trains you to ignore both.

        Pins the division of responsibility: Talaia owns state changes, Prometheus owns
        trends and owns watching Talaia itself.
        """
        for rule in alert_rules(rules):
            normalised = rule["expr"].replace(" ", "")
            assert "talaia_check_up==0" not in normalised, rule["alert"]

    def test_talaia_itself_is_watched(self, rules: dict[str, Any]) -> None:
        """The one thing Talaia can never report is its own death."""
        expressions = " ".join(rule["expr"] for rule in alert_rules(rules))

        assert 'up{job="talaia"} == 0' in expressions

    def test_the_certificate_warning_fires_before_talaia_does(self, rules: dict[str, Any]) -> None:
        """A quiet warning should precede the noisy one, not follow it."""
        from talaia.config.schema import TlsOptions

        warning = next(
            rule for rule in alert_rules(rules) if rule["alert"] == "TalaiaCertificateExpiringSoon"
        )
        threshold = int(re.search(r"<\s*(\d+)", warning["expr"]).group(1))  # type: ignore[union-attr]

        assert threshold > TlsOptions().warn_days
