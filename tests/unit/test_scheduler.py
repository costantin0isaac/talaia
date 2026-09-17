"""Tests for the scheduler's planning and its per-monitor task loop."""

import asyncio

import pytest

from talaia.checks.base import CheckOutcome
from talaia.checks.registry import CheckerRegistry
from talaia.config.schema import HttpOptions, MonitorConfig, MonitorType
from talaia.db.models import Monitor
from talaia.engine.scheduler import FailureStreak, SyncPlan, plan_sync, to_monitor_config


def config(name: str, *, interval: int = 60, target: str = "http://10.0.0.1") -> MonitorConfig:
    return MonitorConfig(
        name=name,
        type=MonitorType.HTTP,
        target=target,
        group=None,
        description=None,
        interval=interval,
        timeout=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        http=None,
        tls=None,
    )


class TestPlanSync:
    def test_new_monitor_is_started(self) -> None:
        plan = plan_sync({}, {"web": config("web")})

        assert plan == SyncPlan(to_start=("web",), to_stop=(), unchanged=())

    def test_removed_monitor_is_stopped(self) -> None:
        plan = plan_sync({"web": config("web")}, {})

        assert plan == SyncPlan(to_start=(), to_stop=("web",), unchanged=())

    def test_unchanged_monitor_keeps_its_task(self) -> None:
        running = {"web": config("web")}

        plan = plan_sync(running, {"web": config("web")})

        assert plan == SyncPlan(to_start=(), to_stop=(), unchanged=("web",))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("interval", 30), ("target", "http://10.0.0.2")],
    )
    def test_changed_monitor_is_restarted(self, field: str, value: object) -> None:
        running = {"web": config("web")}

        plan = plan_sync(running, {"web": config("web", **{field: value})})  # type: ignore[arg-type]

        assert plan.to_stop == ("web",)
        assert plan.to_start == ("web",)
        assert plan.unchanged == ()

    def test_changing_one_monitor_does_not_disturb_another(self) -> None:
        running = {"a": config("a"), "b": config("b")}
        desired = {"a": config("a"), "b": config("b", interval=30)}

        plan = plan_sync(running, desired)

        assert plan.unchanged == ("a",)
        assert plan.to_stop == ("b",)
        assert plan.to_start == ("b",)

    def test_an_http_option_change_is_detected(self) -> None:
        running = {"web": config("web")}
        changed = config("web").model_copy(update={"http": HttpOptions(expected_status=[204])})

        plan = plan_sync(running, {"web": changed})

        assert plan.to_start == ("web",)


class RecordingChecker:
    """A checker that records calls and never touches the network."""

    def __init__(self, outcome: CheckOutcome | None = None) -> None:
        self.calls: list[str] = []
        self.outcome = outcome or CheckOutcome(success=True, latency_ms=1)
        self.called = asyncio.Event()

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        self.calls.append(monitor.name)
        self.called.set()
        return self.outcome


class ExplodingChecker:
    """A checker that violates its contract by raising."""

    def __init__(self) -> None:
        self.calls = 0
        self.called_twice = asyncio.Event()

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        self.calls += 1
        if self.calls >= 2:
            self.called_twice.set()
        raise RuntimeError("checker exploded")


class TestRegistry:
    def test_returns_none_for_an_unimplemented_type(self) -> None:
        registry = CheckerRegistry({MonitorType.HTTP: RecordingChecker()})

        assert registry.get(MonitorType.HTTP) is not None
        assert registry.get(MonitorType.ICMP) is None

    def test_reports_supported_types(self) -> None:
        registry = CheckerRegistry({MonitorType.HTTP: RecordingChecker()})

        assert registry.supported_types == frozenset({MonitorType.HTTP})


class TestToMonitorConfig:
    def test_projects_a_row_into_a_checker_config(self) -> None:
        row = Monitor(
            name="web",
            type=MonitorType.HTTP,
            target="http://10.0.0.1",
            group_name="services",
            description="the app",
            interval_seconds=30,
            timeout_seconds=5,
            failure_threshold=2,
            recovery_threshold=1,
            enabled=True,
            active=True,
            config={"expected_status": [200, 204], "method": "HEAD"},
        )

        result = to_monitor_config(row)

        assert result.name == "web"
        assert result.group == "services"
        assert result.interval == 30
        assert result.timeout == 5
        assert result.http is not None
        assert result.http.expected_status == [200, 204]
        assert result.http.method == "HEAD"

    def test_empty_config_becomes_no_http_options(self) -> None:
        row = Monitor(
            name="web",
            type=MonitorType.HTTP,
            target="http://10.0.0.1",
            group_name=None,
            description=None,
            interval_seconds=60,
            timeout_seconds=10,
            failure_threshold=3,
            recovery_threshold=2,
            enabled=True,
            active=True,
            config={},
        )

        assert to_monitor_config(row).http is None

    def test_non_http_monitor_has_no_http_options(self) -> None:
        row = Monitor(
            name="router",
            type=MonitorType.ICMP,
            target="10.0.0.1",
            group_name=None,
            description=None,
            interval_seconds=60,
            timeout_seconds=10,
            failure_threshold=3,
            recovery_threshold=2,
            enabled=True,
            active=True,
            config={},
        )

        assert to_monitor_config(row).http is None


class TestFailureStreak:
    def test_the_first_failure_is_the_one_worth_a_traceback(self) -> None:
        streak = FailureStreak()

        assert streak.record_failure() is True
        assert streak.record_failure() is False
        assert streak.record_failure() is False
        assert streak.count == 3

    def test_a_success_after_failures_is_a_recovery(self) -> None:
        streak = FailureStreak()
        streak.record_failure()

        assert streak.record_success() is True
        assert streak.count == 0

    def test_a_success_with_no_streak_is_just_a_success(self) -> None:
        assert FailureStreak().record_success() is False

    def test_a_new_streak_gets_its_own_traceback(self) -> None:
        streak = FailureStreak()
        streak.record_failure()
        streak.record_success()

        assert streak.record_failure() is True
