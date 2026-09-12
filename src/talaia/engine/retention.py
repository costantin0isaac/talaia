"""Hourly maintenance: daily rollups, then pruning of raw results."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.db import repository as repo
from talaia.logging import get_logger

log = get_logger(__name__)

Clock = Callable[[], datetime]

DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_BATCH_SIZE = 10_000
MAX_BATCHES = 1_000


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return the half-open UTC interval covering ``day``."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one maintenance pass did."""

    days_rolled_up: tuple[date, ...]
    rows_written: int
    results_deleted: int


class RetentionTask:
    """Refreshes daily rollups and prunes raw results, once per hour.

    Rollups are written before pruning, so a short retention window cannot delete the
    results a rollup is about to summarise.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retention_days: int,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._retention_days = retention_days
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Whether the maintenance loop is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Begin running maintenance in the background."""
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="talaia-retention")

    async def stop(self) -> None:
        """Cancel the maintenance loop and wait for it to finish."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_once(self) -> MaintenanceReport:
        """Refresh rollups for yesterday and today, then prune old results."""
        now = self._clock()
        days = (now.date() - timedelta(days=1), now.date())

        rows_written = 0
        for day in days:
            rows_written += await self._roll_up(day)

        deleted = await self._prune(now - timedelta(days=self._retention_days))

        report = MaintenanceReport(
            days_rolled_up=days, rows_written=rows_written, results_deleted=deleted
        )
        log.info(
            "maintenance completed",
            rows_written=report.rows_written,
            results_deleted=report.results_deleted,
            retention_days=self._retention_days,
        )
        return report

    async def _roll_up(self, day: date) -> int:
        """Write the rollup rows for one day."""
        start, end = day_bounds(day)
        async with self._session_factory() as session, session.begin():
            totals = await repo.aggregate_check_results(session, start=start, end=end)
            for monitor_id, total, successful, average in totals:
                await repo.upsert_daily_uptime(
                    session,
                    monitor_id=monitor_id,
                    day=day,
                    total_checks=total,
                    successful_checks=successful,
                    avg_latency_ms=average,
                )
        return len(totals)

    async def _prune(self, cutoff: datetime) -> int:
        """Delete results older than ``cutoff``, one committed batch at a time."""
        deleted_total = 0
        for _ in range(MAX_BATCHES):
            async with self._session_factory() as session, session.begin():
                deleted = await repo.delete_check_results_before(
                    session, cutoff, batch_size=self._batch_size
                )
            deleted_total += deleted
            if deleted == 0:
                return deleted_total

        log.warning("pruning stopped at the batch limit", batches=MAX_BATCHES)
        return deleted_total

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maintenance pass failed")
            await asyncio.sleep(self._interval_seconds)
