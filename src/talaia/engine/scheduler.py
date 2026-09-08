"""One asyncio task per monitor, and the persistence of each result."""

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.checks.base import CheckOutcome
from talaia.checks.registry import CheckerRegistry
from talaia.config.schema import HttpOptions, MonitorConfig, MonitorType
from talaia.db import repository as repo
from talaia.db.models import Monitor
from talaia.engine.state import StateChange, StateSnapshot, Transition, evaluate
from talaia.logging import get_logger

log = get_logger(__name__)

Clock = Callable[[], datetime]


class ResultRecorder(Protocol):
    """Counts completed checks for the metrics endpoint."""

    def record_check(self, monitor: str, *, success: bool) -> None:
        """Count one check."""
        ...


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def to_monitor_config(monitor: Monitor) -> MonitorConfig:
    """Project a database row back into the configuration a checker consumes."""
    http = (
        HttpOptions.model_validate(monitor.config)
        if monitor.type is MonitorType.HTTP and monitor.config
        else None
    )
    return MonitorConfig(
        name=monitor.name,
        type=monitor.type,
        target=monitor.target,
        group=monitor.group_name,
        description=monitor.description,
        interval=monitor.interval_seconds,
        timeout=monitor.timeout_seconds,
        failure_threshold=monitor.failure_threshold,
        recovery_threshold=monitor.recovery_threshold,
        enabled=monitor.enabled,
        http=http,
    )


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Which monitors the scheduler must start, stop and leave alone."""

    to_start: tuple[str, ...]
    to_stop: tuple[str, ...]
    unchanged: tuple[str, ...]


def plan_sync(running: dict[str, MonitorConfig], desired: dict[str, MonitorConfig]) -> SyncPlan:
    """Compare running tasks with the desired set.

    A monitor whose configuration is unchanged keeps its task, and therefore its position
    within its interval.
    """
    to_stop = tuple(sorted(name for name in running if desired.get(name) != running[name]))
    to_start = tuple(sorted(name for name in desired if running.get(name) != desired[name]))
    unchanged = tuple(sorted(name for name in desired if running.get(name) == desired[name]))
    return SyncPlan(to_start=to_start, to_stop=to_stop, unchanged=unchanged)


async def apply_outcome(
    session: AsyncSession,
    monitor: Monitor,
    outcome: CheckOutcome,
    *,
    checked_at: datetime,
) -> StateChange:
    """Record a result, advance the state machine and open or close an incident.

    Everything here happens in the caller's transaction, so a crash cannot leave a
    notification describing an incident that was never written.
    """
    await repo.record_check_result(
        session,
        monitor_id=monitor.id,
        checked_at=checked_at,
        success=outcome.success,
        latency_ms=outcome.latency_ms,
        status_code=outcome.status_code,
        error=outcome.error,
    )

    state = await repo.ensure_state(session, monitor.id)
    change = evaluate(
        StateSnapshot(
            status=state.status,
            consecutive_failures=state.consecutive_failures,
            consecutive_successes=state.consecutive_successes,
        ),
        success=outcome.success,
        failure_threshold=monitor.failure_threshold,
        recovery_threshold=monitor.recovery_threshold,
    )

    state.status = change.current.status
    state.consecutive_failures = change.current.consecutive_failures
    state.consecutive_successes = change.current.consecutive_successes
    state.last_checked_at = checked_at
    state.last_latency_ms = outcome.latency_ms
    state.last_error = outcome.error
    if change.transition is not Transition.NONE:
        state.status_changed_at = checked_at

    if change.opens_incident:
        await repo.open_incident(
            session,
            monitor_id=monitor.id,
            started_at=checked_at,
            cause=outcome.error or "check failed",
        )
    elif change.resolves_incident:
        incident = await repo.get_open_incident(session, monitor.id)
        if incident is not None:
            await repo.resolve_incident(session, incident, checked_at)

    await session.flush()
    return change


class Scheduler:
    """Owns one asyncio task per active, enabled monitor."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: CheckerRegistry,
        *,
        clock: Clock = utc_now,
        jitter: bool = True,
        recorder: ResultRecorder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._clock = clock
        self._jitter = jitter
        self._recorder = recorder
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._configs: dict[str, MonitorConfig] = {}
        self._stopping = asyncio.Event()

    @property
    def running_monitors(self) -> frozenset[str]:
        """Names of the monitors currently being checked."""
        return frozenset(self._tasks)

    def is_running(self, name: str) -> bool:
        """Whether this monitor has a task that is still alive."""
        task = self._tasks.get(name)
        return task is not None and not task.done()

    async def start(self) -> None:
        """Spawn a task for every schedulable monitor."""
        self._stopping.clear()
        await self.sync()

    async def sync(self) -> None:
        """Reconcile the running tasks with the monitors in the database."""
        desired = await self._desired_configs()
        plan = plan_sync(self._configs, desired)

        for name in plan.to_stop:
            await self._stop_task(name)
        for name in plan.to_start:
            self._start_task(desired[name])

        log.info(
            "scheduler synced",
            started=len(plan.to_start),
            stopped=len(plan.to_stop),
            unchanged=len(plan.unchanged),
        )

    async def stop(self) -> None:
        """Cancel every task and wait for them to finish."""
        self._stopping.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._configs.clear()
        log.info("scheduler stopped")

    async def _desired_configs(self) -> dict[str, MonitorConfig]:
        async with self._session_factory() as session:
            monitors = await repo.list_schedulable_monitors(session)
            configs: dict[str, MonitorConfig] = {}
            for monitor in monitors:
                if self._registry.get(monitor.type) is None:
                    log.warning(
                        "no checker for monitor type, not scheduling",
                        monitor=monitor.name,
                        type=monitor.type.value,
                    )
                    continue
                configs[monitor.name] = to_monitor_config(monitor)
            return configs

    def _start_task(self, config: MonitorConfig) -> None:
        self._configs[config.name] = config
        self._tasks[config.name] = asyncio.create_task(
            self._run(config), name=f"talaia-monitor-{config.name}"
        )

    async def _stop_task(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        self._configs.pop(name, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, config: MonitorConfig) -> None:
        """Check one monitor forever, at its configured interval."""
        if self._jitter:
            await asyncio.sleep(random.uniform(0, config.interval))

        while not self._stopping.is_set():
            started = time.perf_counter()
            try:
                await self._check_once(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The task must survive anything short of cancellation; a dead task would
                # stop monitoring this target silently.
                log.exception("check cycle failed", monitor=config.name)

            elapsed = time.perf_counter() - started
            await asyncio.sleep(max(0.0, config.interval - elapsed))

    async def _check_once(self, config: MonitorConfig) -> None:
        checker = self._registry.get(config.type)
        if checker is None:
            return

        outcome = await checker.check(config)
        checked_at = self._clock()
        if self._recorder is not None:
            self._recorder.record_check(config.name, success=outcome.success)

        async with self._session_factory() as session, session.begin():
            monitor = await repo.get_monitor_by_name(session, config.name)
            if monitor is None:
                return
            change = await apply_outcome(session, monitor, outcome, checked_at=checked_at)

        if change.notifies:
            self._announce(config, change, outcome)

    def _announce(self, config: MonitorConfig, change: StateChange, outcome: CheckOutcome) -> None:
        """Log a state change. Notifications are delivered from Phase 2 onwards."""
        log.info(
            "monitor state changed",
            monitor=config.name,
            status=change.current.status.value,
            previous=change.previous.status.value,
            error=outcome.error,
        )
