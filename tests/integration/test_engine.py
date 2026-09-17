"""The state machine applied against a real database, and the scheduler's lifecycle."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.checks.base import CheckOutcome
from talaia.checks.registry import CheckerRegistry
from talaia.config.schema import MonitorConfig, MonitorType
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.engine.scheduler import Scheduler, apply_outcome

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
OK = CheckOutcome(success=True, latency_ms=12, status_code=200)
BAD = CheckOutcome(success=False, latency_ms=None, error="connection refused")


async def make_monitor(
    session: AsyncSession,
    name: str = "web",
    *,
    failure_threshold: int = 3,
    recovery_threshold: int = 2,
    monitor_type: MonitorType = MonitorType.HTTP,
    enabled: bool = True,
    interval_seconds: int = 60,
) -> Monitor:
    monitor = Monitor(
        name=name,
        type=monitor_type,
        target="http://10.0.0.1",
        group_name=None,
        description=None,
        interval_seconds=interval_seconds,
        timeout_seconds=10,
        failure_threshold=failure_threshold,
        recovery_threshold=recovery_threshold,
        enabled=enabled,
        active=True,
        config={},
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def until(predicate: Callable[[], Awaitable[bool]], *, seconds: float = 10.0) -> None:
    """Wait until ``predicate`` is true, polling the database.

    An asyncio.Event cannot be used here: the state being waited on is written by a
    different connection, so it can only be observed by querying.
    """
    async with asyncio.timeout(seconds):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def feed(
    session: AsyncSession, monitor: Monitor, outcomes: str, *, start: datetime = NOW
) -> list[bool]:
    """Apply a string of 's'uccess / 'f'ailure outcomes, returning notification flags."""
    notifications = []
    for index, character in enumerate(outcomes):
        change = await apply_outcome(
            session,
            monitor,
            OK if character == "s" else BAD,
            checked_at=start + timedelta(minutes=index),
        )
        notifications.append(change.notifies)
    return notifications


class TestApplyOutcome:
    async def test_records_every_check(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        await feed(session, monitor, "sfs")

        results = await repo.list_check_results(session, monitor.id, since=NOW - timedelta(days=1))
        assert len(results) == 3

    async def test_state_row_is_created_and_updated(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        await feed(session, monitor, "s")

        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.consecutive_successes == 1
        assert state.last_latency_ms == 12
        assert state.last_checked_at == NOW

    async def test_failure_records_the_error(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        await feed(session, monitor, "f")

        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.last_error == "connection refused"
        assert state.last_latency_ms is None

    async def test_crossing_the_failure_threshold_opens_one_incident(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, failure_threshold=3)

        notifications = await feed(session, monitor, "fff")

        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.DOWN
        assert notifications == [False, False, True]
        incidents = await repo.list_incidents(session, monitor_id=monitor.id)
        assert len(incidents) == 1
        assert incidents[0].cause == "connection refused"
        assert incidents[0].resolved_at is None

    async def test_staying_down_does_not_open_a_second_incident(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session)

        notifications = await feed(session, monitor, "f" * 20)

        assert notifications.count(True) == 1
        assert len(await repo.list_incidents(session, monitor_id=monitor.id)) == 1

    async def test_recovery_resolves_the_incident_with_a_duration(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, failure_threshold=3, recovery_threshold=2)

        notifications = await feed(session, monitor, "fffss")

        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.UP
        assert notifications == [False, False, True, False, True]
        incidents = await repo.list_incidents(session, monitor_id=monitor.id)
        assert len(incidents) == 1
        assert incidents[0].resolved_at is not None
        assert incidents[0].duration_seconds == 120

    async def test_unknown_to_up_opens_nothing_and_stays_silent(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, recovery_threshold=2)

        notifications = await feed(session, monitor, "ss")

        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.UP
        assert notifications == [False, False]
        assert await repo.list_incidents(session, monitor_id=monitor.id) == []

    async def test_status_changed_at_is_set_only_on_a_transition(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, recovery_threshold=1)

        await feed(session, monitor, "s")
        state = await repo.get_state(session, monitor.id)
        assert state is not None
        first_change = state.status_changed_at
        assert first_change == NOW

        await feed(session, monitor, "ss", start=NOW + timedelta(hours=1))
        await session.refresh(state)
        assert state.status_changed_at == first_change

    async def test_a_second_outage_opens_a_second_incident(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session, failure_threshold=2, recovery_threshold=1)

        await feed(session, monitor, "ffsff")

        incidents = await repo.list_incidents(session, monitor_id=monitor.id)
        assert len(incidents) == 2
        assert sum(1 for incident in incidents if incident.resolved_at is None) == 1


class FakeChecker:
    """Returns queued outcomes without touching the network."""

    def __init__(self, outcome: CheckOutcome = OK) -> None:
        self.outcome = outcome
        self.calls: list[str] = []
        self.called = asyncio.Event()

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        self.calls.append(monitor.name)
        self.called.set()
        return self.outcome


class TestScheduler:
    async def test_starts_a_task_per_schedulable_monitor(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup, "one")
            await make_monitor(setup, "two")
            await make_monitor(setup, "off", enabled=False)
            await setup.commit()

        checker = FakeChecker()
        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: checker}), jitter=False
        )
        try:
            await scheduler.start()

            assert scheduler.running_monitors == frozenset({"one", "two"})
            await asyncio.wait_for(checker.called.wait(), timeout=10)
        finally:
            await scheduler.stop()

        assert scheduler.running_monitors == frozenset()

    async def test_monitors_of_unsupported_types_are_not_scheduled(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup, "pingable", monitor_type=MonitorType.ICMP)
            await setup.commit()

        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: FakeChecker()}), jitter=False
        )
        try:
            await scheduler.start()

            assert scheduler.running_monitors == frozenset()
        finally:
            await scheduler.stop()

    async def test_a_check_is_persisted_by_the_running_task(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup, "web")
            monitor_id = monitor.id
            await setup.commit()

        checker = FakeChecker()
        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: checker}), jitter=False
        )

        async def persisted() -> bool:
            async with committed_factory() as poll_session:
                rows = await repo.list_check_results(
                    poll_session, monitor_id, since=NOW - timedelta(days=1)
                )
                return len(rows) >= 1

        try:
            await scheduler.start()
            await until(persisted)
        finally:
            await scheduler.stop()

        async with committed_factory() as check_session:
            results = await repo.list_check_results(
                check_session, monitor_id, since=NOW - timedelta(days=1)
            )
            state = await repo.get_state(check_session, monitor_id)

        assert len(results) >= 1
        assert results[0].success is True
        assert state is not None
        assert state.consecutive_successes >= 1

    async def test_sync_leaves_unchanged_monitors_running(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup, "stable")
            await setup.commit()

        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: FakeChecker()}), jitter=False
        )
        try:
            await scheduler.start()
            async with committed_factory() as setup:
                await make_monitor(setup, "added")
                await setup.commit()

            await scheduler.sync()

            assert scheduler.running_monitors == frozenset({"stable", "added"})
        finally:
            await scheduler.stop()

    async def test_sync_stops_a_monitor_that_was_disabled(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup, "web")
            await setup.commit()
            monitor_id = monitor.id

        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: FakeChecker()}), jitter=False
        )
        try:
            await scheduler.start()
            assert scheduler.running_monitors == frozenset({"web"})

            async with committed_factory() as setup:
                row = await setup.get(Monitor, monitor_id)
                assert row is not None
                row.enabled = False
                await setup.commit()

            await scheduler.sync()

            assert scheduler.running_monitors == frozenset()
        finally:
            await scheduler.stop()

    async def test_a_checker_that_raises_does_not_kill_the_task(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Section 8.2: the loop must survive anything short of cancellation."""

        class Exploding:
            def __init__(self) -> None:
                self.calls = 0
                self.called_twice = asyncio.Event()

            async def check(self, monitor: MonitorConfig) -> CheckOutcome:
                self.calls += 1
                if self.calls >= 2:
                    self.called_twice.set()
                raise RuntimeError("checker exploded")

        async with committed_factory() as setup:
            await make_monitor(setup, "web", interval_seconds=1)
            await setup.commit()

        checker = Exploding()
        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: checker}), jitter=False
        )
        try:
            await scheduler.start()
            await asyncio.wait_for(checker.called_twice.wait(), timeout=10)

            assert scheduler.is_running("web")
        finally:
            await scheduler.stop()


class TestCheckCounter:
    async def test_a_check_that_is_not_persisted_is_not_counted(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The counter must never exceed what check_results holds."""
        recorded: list[str] = []

        class Recorder:
            def record_check(self, monitor: str, *, success: bool) -> None:
                recorded.append(monitor)

        scheduler = Scheduler(
            committed_factory,
            CheckerRegistry({MonitorType.HTTP: FakeChecker()}),
            jitter=False,
            recorder=Recorder(),
        )

        # No row for this monitor exists, so the write path returns before persisting.
        await scheduler._check_once(config_for("ghost"))

        assert recorded == []

    async def test_a_persisted_check_is_counted_once(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup, "web")
            await setup.commit()
        recorded: list[str] = []

        class Recorder:
            def record_check(self, monitor: str, *, success: bool) -> None:
                recorded.append(monitor)

        scheduler = Scheduler(
            committed_factory,
            CheckerRegistry({MonitorType.HTTP: FakeChecker()}),
            jitter=False,
            recorder=Recorder(),
        )

        await scheduler._check_once(config_for("web"))

        assert recorded == ["web"]


def config_for(name: str) -> MonitorConfig:
    return MonitorConfig(
        name=name,
        type=MonitorType.HTTP,
        target="http://10.0.0.1",
        group=None,
        description=None,
        interval=60,
        timeout=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        http=None,
        tls=None,
    )
