"""Maintenance against a real database."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.config.schema import MonitorType
from talaia.db import repository as repo
from talaia.db.models import DailyUptime, Monitor
from talaia.engine.retention import RetentionTask

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
YESTERDAY = NOW.date() - timedelta(days=1)
TODAY = NOW.date()


async def make_monitor(session: AsyncSession, name: str = "web") -> Monitor:
    monitor = Monitor(
        name=name,
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
    session.add(monitor)
    await session.flush()
    return monitor


def task(factory: async_sessionmaker[AsyncSession], *, retention_days: int = 30) -> RetentionTask:
    return RetentionTask(factory, retention_days=retention_days, clock=lambda: NOW)


async def rollups(session: AsyncSession, monitor_id: int) -> list[DailyUptime]:
    statement = (
        select(DailyUptime).where(DailyUptime.monitor_id == monitor_id).order_by(DailyUptime.day)
    )
    return list((await session.scalars(statement)).all())


class TestRollup:
    async def test_counts_totals_and_averages_latency(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            for success, latency in [(True, 10), (True, 30), (False, None)]:
                await repo.record_check_result(
                    setup,
                    monitor_id=monitor_id,
                    checked_at=NOW,
                    success=success,
                    latency_ms=latency,
                )
            await setup.commit()

        await task(committed_factory).run_once()

        async with committed_factory() as check:
            rows = await rollups(check, monitor_id)

        today = next(row for row in rows if row.day == TODAY)
        assert today.total_checks == 3
        assert today.successful_checks == 2
        assert today.avg_latency_ms == 20

    async def test_covers_yesterday_and_today_only(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            for days_ago in (0, 1, 2):
                await repo.record_check_result(
                    setup,
                    monitor_id=monitor_id,
                    checked_at=NOW - timedelta(days=days_ago),
                    success=True,
                )
            await setup.commit()

        await task(committed_factory).run_once()

        async with committed_factory() as check:
            days = [row.day for row in await rollups(check, monitor_id)]

        assert days == [YESTERDAY, TODAY]

    async def test_refreshes_an_existing_row_rather_than_failing(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await repo.record_check_result(
                setup, monitor_id=monitor_id, checked_at=NOW, success=True
            )
            await setup.commit()

        await task(committed_factory).run_once()
        async with committed_factory() as setup:
            await repo.record_check_result(
                setup, monitor_id=monitor_id, checked_at=NOW, success=False
            )
            await setup.commit()
        await task(committed_factory).run_once()

        async with committed_factory() as check:
            today = next(row for row in await rollups(check, monitor_id) if row.day == TODAY)

        assert today.total_checks == 2
        assert today.successful_checks == 1

    async def test_a_day_without_latencies_records_none(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await repo.record_check_result(
                setup, monitor_id=monitor_id, checked_at=NOW, success=False, latency_ms=None
            )
            await setup.commit()

        await task(committed_factory).run_once()

        async with committed_factory() as check:
            today = next(row for row in await rollups(check, monitor_id) if row.day == TODAY)

        assert today.avg_latency_ms is None


class TestPrune:
    async def test_deletes_only_results_past_the_retention_window(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            for days_ago in (40, 31, 29, 0):
                await repo.record_check_result(
                    setup,
                    monitor_id=monitor_id,
                    checked_at=NOW - timedelta(days=days_ago),
                    success=True,
                )
            await setup.commit()

        report = await task(committed_factory, retention_days=30).run_once()

        assert report.results_deleted == 2
        async with committed_factory() as check:
            remaining = await repo.list_check_results(
                check, monitor_id, since=NOW - timedelta(days=365)
            )
        assert len(remaining) == 2

    async def test_deletes_in_batches(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            for index in range(25):
                await repo.record_check_result(
                    setup,
                    monitor_id=monitor_id,
                    checked_at=NOW - timedelta(days=40, minutes=index),
                    success=True,
                )
            await setup.commit()

        maintenance = RetentionTask(
            committed_factory, retention_days=30, batch_size=10, clock=lambda: NOW
        )
        report = await maintenance.run_once()

        assert report.results_deleted == 25
        async with committed_factory() as check:
            assert (
                await repo.list_check_results(check, monitor_id, since=NOW - timedelta(days=365))
                == []
            )

    async def test_nothing_to_prune_is_not_an_error(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            await make_monitor(setup)
            await setup.commit()

        report = await task(committed_factory).run_once()

        assert report.results_deleted == 0


class TestOrdering:
    async def test_rollups_are_written_before_results_are_pruned(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """With a one-day window, pruning first would destroy yesterday's history."""
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            for success in (True, True, False):
                await repo.record_check_result(
                    setup,
                    monitor_id=monitor_id,
                    checked_at=NOW - timedelta(days=1, hours=1),
                    success=success,
                )
            await setup.commit()

        report = await task(committed_factory, retention_days=1).run_once()

        assert report.results_deleted == 3
        async with committed_factory() as check:
            rows = await rollups(check, monitor_id)
            yesterday = next(row for row in rows if row.day == YESTERDAY)
            remaining = await repo.list_check_results(
                check, monitor_id, since=NOW - timedelta(days=365)
            )

        assert yesterday.total_checks == 3
        assert yesterday.successful_checks == 2
        assert remaining == []


class TestLifecycle:
    async def test_runs_on_start_and_stops_cleanly(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with committed_factory() as setup:
            monitor = await make_monitor(setup)
            monitor_id = monitor.id
            await repo.record_check_result(
                setup, monitor_id=monitor_id, checked_at=NOW, success=True
            )
            await setup.commit()

        maintenance = RetentionTask(
            committed_factory, retention_days=30, interval_seconds=3600, clock=lambda: NOW
        )
        try:
            await maintenance.start()

            async with asyncio.timeout(10):
                while True:
                    async with committed_factory() as check:
                        if await rollups(check, monitor_id):
                            break
                    await asyncio.sleep(0.02)

            assert maintenance.is_running
        finally:
            await maintenance.stop()

        assert maintenance.is_running is False

    async def test_a_failing_pass_does_not_kill_the_loop(
        self, committed_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        maintenance = RetentionTask(
            committed_factory, retention_days=30, interval_seconds=1, clock=lambda: NOW
        )
        calls = 0
        original = maintenance.run_once

        async def failing() -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("maintenance exploded")
            return await original()

        maintenance.run_once = failing  # type: ignore[method-assign]
        try:
            await maintenance.start()

            async with asyncio.timeout(10):
                while calls < 2:  # noqa: ASYNC110
                    await asyncio.sleep(0.02)

            assert maintenance.is_running
        finally:
            await maintenance.stop()
