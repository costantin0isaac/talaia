"""Tests for the repository layer against a real PostgreSQL instance."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from talaia.config.schema import MonitorType
from talaia.db import repository as repo
from talaia.db.models import DailyUptime, Monitor, MonitorStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def make_monitor(
    session: AsyncSession,
    name: str = "web",
    *,
    monitor_type: MonitorType = MonitorType.HTTP,
    target: str = "http://10.0.0.1",
    group_name: str | None = "services",
    enabled: bool = True,
    active: bool = True,
) -> Monitor:
    monitor = Monitor(
        name=name,
        type=monitor_type,
        target=target,
        group_name=group_name,
        description=None,
        interval_seconds=60,
        timeout_seconds=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=enabled,
        active=active,
        config={},
    )
    session.add(monitor)
    await session.flush()
    return monitor


class TestMonitorQueries:
    async def test_round_trips_a_monitor(self, session: AsyncSession) -> None:
        await make_monitor(session, "grafana")

        found = await repo.get_monitor_by_name(session, "grafana")

        assert found is not None
        assert found.type is MonitorType.HTTP
        assert found.config == {}
        assert found.created_at.tzinfo is not None

    async def test_unknown_name_returns_none(self, session: AsyncSession) -> None:
        assert await repo.get_monitor_by_name(session, "absent") is None

    async def test_duplicate_name_is_rejected_by_the_database(self, session: AsyncSession) -> None:
        await make_monitor(session, "web")

        with pytest.raises(IntegrityError):
            await make_monitor(session, "web")

    async def test_list_monitors_excludes_inactive_by_default(self, session: AsyncSession) -> None:
        await make_monitor(session, "live")
        await make_monitor(session, "retired", active=False)

        assert [m.name for m in await repo.list_monitors(session)] == ["live"]
        names = [m.name for m in await repo.list_monitors(session, active_only=False)]
        assert sorted(names) == ["live", "retired"]

    async def test_schedulable_excludes_disabled_and_inactive(self, session: AsyncSession) -> None:
        await make_monitor(session, "runs")
        await make_monitor(session, "paused", enabled=False)
        await make_monitor(session, "gone", active=False)

        assert [m.name for m in await repo.list_schedulable_monitors(session)] == ["runs"]

    async def test_invalid_status_is_rejected_by_the_check_constraint(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session)
        state = await repo.ensure_state(session, monitor.id)
        state.status = "sideways"  # type: ignore[assignment]

        with pytest.raises(IntegrityError):
            await session.flush()


class TestState:
    async def test_ensure_state_creates_then_reuses_the_row(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        first = await repo.ensure_state(session, monitor.id)
        second = await repo.ensure_state(session, monitor.id)

        assert first is second
        assert first.status is MonitorStatus.UNKNOWN
        assert first.consecutive_failures == 0

    async def test_count_by_status_ignores_inactive_monitors(self, session: AsyncSession) -> None:
        up = await make_monitor(session, "up-one")
        down = await make_monitor(session, "down-one")
        retired = await make_monitor(session, "retired", active=False)
        (await repo.ensure_state(session, up.id)).status = MonitorStatus.UP
        (await repo.ensure_state(session, down.id)).status = MonitorStatus.DOWN
        (await repo.ensure_state(session, retired.id)).status = MonitorStatus.UP
        await session.flush()

        counts = await repo.count_by_status(session)

        assert counts == {MonitorStatus.UP: 1, MonitorStatus.DOWN: 1}

    async def test_list_states_pairs_monitors_with_their_state(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session, "web")
        await repo.ensure_state(session, monitor.id)
        await session.flush()

        pairs = await repo.list_states(session)

        assert [(m.name, s.status) for m, s in pairs] == [("web", MonitorStatus.UNKNOWN)]


class TestCheckResults:
    async def test_records_and_reads_back_results(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        await repo.record_check_result(
            session, monitor_id=monitor.id, checked_at=NOW, success=True, latency_ms=42
        )

        results = await repo.list_check_results(session, monitor.id, since=NOW - timedelta(hours=1))

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].latency_ms == 42

    async def test_results_are_returned_newest_first(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        for minutes in (0, 5, 10):
            await repo.record_check_result(
                session,
                monitor_id=monitor.id,
                checked_at=NOW + timedelta(minutes=minutes),
                success=True,
            )

        results = await repo.list_check_results(session, monitor.id, since=NOW)

        assert [r.checked_at for r in results] == sorted(
            (r.checked_at for r in results), reverse=True
        )

    async def test_window_excludes_older_results(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        await repo.record_check_result(
            session, monitor_id=monitor.id, checked_at=NOW - timedelta(days=2), success=True
        )
        await repo.record_check_result(session, monitor_id=monitor.id, checked_at=NOW, success=True)

        results = await repo.list_check_results(session, monitor.id, since=NOW - timedelta(hours=1))

        assert len(results) == 1

    async def test_uptime_ratio(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        for success in (True, True, True, False):
            await repo.record_check_result(
                session, monitor_id=monitor.id, checked_at=NOW, success=success
            )

        assert await repo.uptime_ratio(session, monitor.id, since=NOW - timedelta(hours=1)) == 0.75

    async def test_uptime_ratio_is_none_without_results(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)

        assert await repo.uptime_ratio(session, monitor.id, since=NOW) is None

    async def test_deleting_a_monitor_cascades_to_its_results(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        await repo.record_check_result(session, monitor_id=monitor.id, checked_at=NOW, success=True)

        await session.delete(monitor)
        await session.flush()

        assert await repo.list_check_results(session, monitor.id, since=NOW) == []


class TestIncidents:
    async def test_a_second_open_incident_is_rejected_by_the_database(
        self, session: AsyncSession
    ) -> None:
        """The partial unique index is the real guarantee, not application logic."""
        monitor = await make_monitor(session)
        await repo.open_incident(session, monitor_id=monitor.id, started_at=NOW, cause="timeout")

        with pytest.raises(IntegrityError):
            await repo.open_incident(
                session, monitor_id=monitor.id, started_at=NOW, cause="timeout again"
            )

    async def test_a_new_incident_is_allowed_once_the_previous_one_is_resolved(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session)
        first = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="timeout"
        )
        await repo.resolve_incident(session, first, NOW + timedelta(minutes=6))
        await session.flush()

        second = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW + timedelta(hours=1), cause="refused"
        )

        assert second.id != first.id

    async def test_two_monitors_may_each_have_an_open_incident(self, session: AsyncSession) -> None:
        one = await make_monitor(session, "one")
        two = await make_monitor(session, "two")

        await repo.open_incident(session, monitor_id=one.id, started_at=NOW, cause="a")
        await repo.open_incident(session, monitor_id=two.id, started_at=NOW, cause="b")

        assert await repo.count_open_incidents(session) == 2

    async def test_resolving_computes_the_duration(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="timeout"
        )

        await repo.resolve_incident(session, incident, NOW + timedelta(minutes=6, seconds=30))

        assert incident.duration_seconds == 390

    async def test_get_open_incident_returns_none_once_resolved(
        self, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session)
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW, cause="timeout"
        )
        assert await repo.get_open_incident(session, monitor.id) is not None

        await repo.resolve_incident(session, incident, NOW + timedelta(minutes=1))
        await session.flush()

        assert await repo.get_open_incident(session, monitor.id) is None

    async def test_open_incidents_of_inactive_monitors_are_not_counted(
        self, session: AsyncSession
    ) -> None:
        retired = await make_monitor(session, "retired", active=False)
        await repo.open_incident(session, monitor_id=retired.id, started_at=NOW, cause="x")

        assert await repo.count_open_incidents(session) == 0


class TestRetention:
    async def test_deletes_only_results_older_than_the_cutoff(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        for days in (40, 35, 1):
            await repo.record_check_result(
                session, monitor_id=monitor.id, checked_at=NOW - timedelta(days=days), success=True
            )

        deleted = await repo.delete_check_results_before(session, NOW - timedelta(days=30))

        assert deleted == 2
        remaining = await repo.list_check_results(
            session, monitor.id, since=NOW - timedelta(days=60)
        )
        assert len(remaining) == 1

    async def test_deletes_in_batches(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        for index in range(5):
            await repo.record_check_result(
                session,
                monitor_id=monitor.id,
                checked_at=NOW - timedelta(days=40, minutes=index),
                success=True,
            )

        assert await repo.delete_check_results_before(session, NOW, batch_size=2) == 2
        assert await repo.delete_check_results_before(session, NOW, batch_size=2) == 2
        assert await repo.delete_check_results_before(session, NOW, batch_size=2) == 1


class TestDailyUptime:
    async def test_insert_then_refresh_the_same_day(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session)
        day = NOW.date()

        await repo.upsert_daily_uptime(
            session,
            monitor_id=monitor.id,
            day=day,
            total_checks=10,
            successful_checks=9,
            avg_latency_ms=40,
        )
        await repo.upsert_daily_uptime(
            session,
            monitor_id=monitor.id,
            day=day,
            total_checks=20,
            successful_checks=19,
            avg_latency_ms=45,
        )

        refreshed = await session.get(DailyUptime, (monitor.id, day))

        assert refreshed is not None
        assert refreshed.total_checks == 20
        assert refreshed.successful_checks == 19


class TestDeactivation:
    async def test_missing_monitors_are_soft_deleted(self, session: AsyncSession) -> None:
        await make_monitor(session, "kept")
        removed = await make_monitor(session, "removed")
        await repo.record_check_result(session, monitor_id=removed.id, checked_at=NOW, success=True)

        count = await repo.deactivate_missing_monitors(session, ["kept"])
        await session.flush()
        session.expire_all()

        assert count == 1
        assert [m.name for m in await repo.list_monitors(session)] == ["kept"]
        still_there = await repo.get_monitor_by_name(session, "removed")
        assert still_there is not None
        assert still_there.active is False
        assert len(await repo.list_check_results(session, removed.id, since=NOW)) == 1


class TestAverageLatencySinceDay:
    @staticmethod
    async def rollup(
        session: AsyncSession,
        monitor_id: int,
        *,
        day: date,
        checks: int,
        latency: int | None,
    ) -> None:
        await repo.upsert_daily_uptime(
            session,
            monitor_id=monitor_id,
            day=day,
            total_checks=checks,
            successful_checks=checks,
            avg_latency_ms=latency,
        )

    async def test_no_rollups_is_none(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session, "web")

        assert (
            await repo.average_latency_since_day(session, monitor.id, since=date(2026, 1, 1))
            is None
        )

    async def test_weights_by_the_number_of_checks(self, session: AsyncSession) -> None:
        """A quiet day must not weigh the same as a busy one."""
        monitor = await make_monitor(session, "web")
        await self.rollup(session, monitor.id, day=date(2026, 3, 13), checks=90, latency=10)
        await self.rollup(session, monitor.id, day=date(2026, 3, 14), checks=10, latency=110)

        mean = await repo.average_latency_since_day(session, monitor.id, since=date(2026, 3, 13))

        assert mean == 20  # (90*10 + 10*110) / 100, not the flat average of 60

    async def test_days_with_no_latency_are_excluded(self, session: AsyncSession) -> None:
        """A day where every check failed has no latency, which is not the same as zero."""
        monitor = await make_monitor(session, "web")
        await self.rollup(session, monitor.id, day=date(2026, 3, 13), checks=10, latency=50)
        await self.rollup(session, monitor.id, day=date(2026, 3, 14), checks=10, latency=None)

        mean = await repo.average_latency_since_day(session, monitor.id, since=date(2026, 3, 13))

        assert mean == 50

    async def test_earlier_days_are_outside_the_window(self, session: AsyncSession) -> None:
        monitor = await make_monitor(session, "web")
        await self.rollup(session, monitor.id, day=date(2026, 3, 1), checks=10, latency=999)
        await self.rollup(session, monitor.id, day=date(2026, 3, 14), checks=10, latency=20)

        mean = await repo.average_latency_since_day(session, monitor.id, since=date(2026, 3, 10))

        assert mean == 20

    async def test_another_monitor_is_not_counted(self, session: AsyncSession) -> None:
        mine = await make_monitor(session, "mine")
        theirs = await make_monitor(session, "theirs")
        await self.rollup(session, mine.id, day=date(2026, 3, 14), checks=10, latency=20)
        await self.rollup(session, theirs.id, day=date(2026, 3, 14), checks=10, latency=900)

        assert await repo.average_latency_since_day(session, mine.id, since=date(2026, 3, 1)) == 20


class TestStripWindow:
    async def test_results_outside_the_window_are_not_ranked(self, session: AsyncSession) -> None:
        """The bound is what stops the window function scanning a whole month."""
        monitor = await make_monitor(session, "web")
        now = datetime.now(UTC)
        for hours_ago in (1, 100):
            await repo.record_check_result(
                session,
                monitor_id=monitor.id,
                checked_at=now - timedelta(hours=hours_ago),
                success=True,
            )

        bounded = await repo.list_latest_results_by_monitor(
            session, [monitor.id], since=now - timedelta(days=2)
        )
        unbounded = await repo.list_latest_results_by_monitor(session, [monitor.id])

        assert len(bounded[monitor.id]) == 1
        assert len(unbounded[monitor.id]) == 2
