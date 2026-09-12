"""Notifications driven by the scheduler against a real database."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.checks.base import CheckOutcome
from talaia.checks.registry import CheckerRegistry
from talaia.config.schema import MonitorConfig, MonitorType
from talaia.db import repository as repo
from talaia.db.models import Monitor
from talaia.engine.scheduler import Scheduler, apply_outcome
from talaia.notify.base import Notification

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
OK = CheckOutcome(success=True, latency_ms=12, status_code=200)
BAD = CheckOutcome(success=False, latency_ms=None, error="connection refused")

BASE_URL = "http://talaia.lan"


async def make_monitor(session: AsyncSession, name: str = "web") -> Monitor:
    monitor = Monitor(
        name=name,
        type=MonitorType.HTTP,
        target="http://10.0.0.1",
        group_name=None,
        description=None,
        interval_seconds=1,
        timeout_seconds=10,
        failure_threshold=1,
        recovery_threshold=1,
        enabled=True,
        active=True,
        config={},
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def until(predicate: Callable[[], bool], *, seconds: float = 10.0) -> None:
    """Wait until ``predicate`` is true, yielding to the scheduler's tasks in between."""
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


class SwitchableChecker:
    """Returns whatever outcome the test currently wants."""

    def __init__(self, outcome: CheckOutcome = OK) -> None:
        self.outcome = outcome
        self.calls = 0

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        self.calls += 1
        return self.outcome


class RecordingNotifier:
    """Captures what would have been delivered."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)

    async def aclose(self) -> None:
        return None


class ExplodingNotifier:
    """A notifier that is broken in the worst way a notifier can be."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, notification: Notification) -> None:
        self.attempts += 1
        msg = "the notifier is on fire"
        raise RuntimeError(msg)

    async def aclose(self) -> None:
        return None


def build(
    factory: async_sessionmaker[AsyncSession],
    checker: SwitchableChecker,
    notifier: RecordingNotifier | ExplodingNotifier,
) -> Scheduler:
    return Scheduler(
        factory,
        CheckerRegistry({MonitorType.HTTP: checker}),
        jitter=False,
        notifier=notifier,
        base_url=BASE_URL,
    )


class TestClaim:
    async def test_the_first_claim_wins(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="boom"
        )

        assert await repo.claim_notification(session, incident.id, kind="down", at=NOW) is True

    async def test_a_second_claim_is_refused(self, session: AsyncSession) -> None:
        """This is what stops a restart mid-incident announcing it twice."""
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="boom"
        )
        await repo.claim_notification(session, incident.id, kind="down", at=NOW)

        assert await repo.claim_notification(session, incident.id, kind="down", at=NOW) is False

    async def test_the_two_kinds_are_claimed_independently(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="boom"
        )
        await repo.claim_notification(session, incident.id, kind="down", at=NOW)

        assert await repo.claim_notification(session, incident.id, kind="up", at=NOW) is True

    async def test_the_stamps_are_written(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="boom"
        )
        resolved = NOW + timedelta(minutes=6)

        await repo.claim_notification(session, incident.id, kind="down", at=NOW)
        await repo.claim_notification(session, incident.id, kind="up", at=resolved)

        await session.refresh(incident)
        assert incident.notified_down_at == NOW
        assert incident.notified_up_at == resolved


class TestLatestIncident:
    async def test_returns_the_newest_one(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        older = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="old"
        )
        await repo.resolve_incident(session, older, NOW + timedelta(minutes=5))
        newest = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW + timedelta(hours=1), cause="new"
        )

        assert (await repo.get_latest_incident(session, monitor.id)) is newest

    async def test_a_resolved_incident_is_still_the_latest(self, session: AsyncSession) -> None:
        """The recovery message is built from an incident that is no longer open."""
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="boom"
        )
        await repo.resolve_incident(session, incident, NOW + timedelta(minutes=6))
        await session.flush()

        assert (await repo.get_latest_incident(session, monitor.id)) is incident

    async def test_a_monitor_that_never_failed_has_none(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        assert (await repo.get_latest_incident(session, monitor.id)) is None


class TestScheduledNotifications:
    async def test_going_down_is_announced_and_stamped(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await setup.commit()

        notifier = RecordingNotifier()
        scheduler = build(committed_factory, SwitchableChecker(BAD), notifier)
        try:
            await scheduler.start()
            await until(lambda: _sent(notifier, 1))
        finally:
            await scheduler.stop()

        assert notifier.sent[0].title == "🔴 web is DOWN"
        assert notifier.sent[0].priority == "high"
        assert "Error: connection refused" in notifier.sent[0].body
        assert notifier.sent[0].link == f"{BASE_URL}/monitors/web"

        async with committed_factory() as check:
            incident = await repo.get_latest_incident(check, monitor_id)
            assert incident is not None
            assert incident.notified_down_at is not None

    async def test_recovering_is_announced_with_the_downtime(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await setup.commit()

        notifier = RecordingNotifier()
        checker = SwitchableChecker(BAD)
        scheduler = build(committed_factory, checker, notifier)
        try:
            await scheduler.start()
            await until(lambda: _sent(notifier, 1))
            checker.outcome = OK
            await until(lambda: _sent(notifier, 2))
        finally:
            await scheduler.stop()

        assert notifier.sent[1].title == "🟢 web recovered"
        assert notifier.sent[1].priority == "default"
        assert "Down for" in notifier.sent[1].body

        async with committed_factory() as check:
            incident = await repo.get_latest_incident(check, monitor_id)
            assert incident is not None
            assert incident.notified_up_at is not None

    async def test_a_long_outage_produces_exactly_two_messages(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Notifications follow transitions, not failed checks, so there is no cooldown."""
        async with committed_factory() as setup:
            await make_monitor(setup)
            await setup.commit()

        notifier = RecordingNotifier()
        checker = SwitchableChecker(BAD)
        scheduler = build(committed_factory, checker, notifier)
        try:
            await scheduler.start()
            await until(lambda: checker.calls >= 3)
            checker.outcome = OK
            await until(lambda: _sent(notifier, 2))
            settled = checker.calls + 2
            await until(lambda: _checked(checker, settled))
        finally:
            await scheduler.stop()

        assert [notification.title for notification in notifier.sent] == [
            "🔴 web is DOWN",
            "🟢 web recovered",
        ]

    async def test_a_broken_notifier_does_not_stop_the_checks(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await setup.commit()

        notifier = ExplodingNotifier()
        checker = SwitchableChecker(BAD)
        scheduler = build(committed_factory, checker, notifier)
        try:
            await scheduler.start()
            await until(lambda: notifier.attempts >= 1)
            calls_when_it_blew_up = checker.calls
            await until(lambda: _checked(checker, calls_when_it_blew_up + 2))
        finally:
            await scheduler.stop()

        async with committed_factory() as check:
            results = await repo.list_check_results(
                check, monitor_id, since=NOW - timedelta(days=1)
            )
            assert len(results) >= 2

    async def test_no_notifier_is_not_an_error(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup)
            await setup.commit()

        checker = SwitchableChecker(BAD)
        scheduler = Scheduler(
            committed_factory, CheckerRegistry({MonitorType.HTTP: checker}), jitter=False
        )
        try:
            await scheduler.start()
            await until(lambda: checker.calls >= 2)
        finally:
            await scheduler.stop()


class TestSilentTransitions:
    async def test_the_first_successful_check_announces_nothing(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Coming up from unknown is startup, not an event."""
        async with committed_factory() as setup:
            await make_monitor(setup)
            await setup.commit()

        notifier = RecordingNotifier()
        checker = SwitchableChecker(OK)
        scheduler = build(committed_factory, checker, notifier)
        try:
            await scheduler.start()
            await until(lambda: checker.calls >= 3)
        finally:
            await scheduler.stop()

        assert notifier.sent == []

    async def test_a_failure_below_the_threshold_announces_nothing(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session)
        monitor.failure_threshold = 3
        await session.flush()

        change = await apply_outcome(session, monitor, BAD, checked_at=NOW)

        assert change.notifies is False


def _sent(notifier: RecordingNotifier, count: int) -> bool:
    return len(notifier.sent) >= count


def _checked(checker: SwitchableChecker, count: int) -> bool:
    return checker.calls >= count
