"""Projection of monitors.yaml into the database.

The file is the source of truth for configuration; the database keeps state and history.
Reconciliation runs in one transaction, so a partially applied configuration is never
visible, and monitors that disappear from the file are soft-deleted rather than dropped.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from talaia.config.loader import load_config
from talaia.config.schema import MonitorConfig, MonitorsFile
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What reconciliation changed."""

    inserted: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    reactivated: tuple[str, ...] = ()
    deactivated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything at all changed."""
        return bool(self.inserted or self.updated or self.reactivated or self.deactivated)


@dataclass
class _Changes:
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


async def reconcile(session: AsyncSession, config: MonitorsFile) -> ReconcileReport:
    """Apply a validated configuration to the database."""
    desired = config.resolve()
    existing = {
        monitor.name: monitor for monitor in await repo.list_monitors(session, active_only=False)
    }
    changes = _Changes()

    for monitor_config in desired:
        row = existing.get(monitor_config.name)
        if row is None:
            session.add(_new_monitor(monitor_config))
            changes.inserted.append(monitor_config.name)
            continue

        reactivated = not row.active
        modified = _apply(row, monitor_config)
        if reactivated:
            changes.reactivated.append(monitor_config.name)
        elif modified:
            changes.updated.append(monitor_config.name)
        else:
            changes.unchanged.append(monitor_config.name)

    await session.flush()

    changes.deactivated = await _deactivate_absent(session, [m.name for m in desired])
    await _sync_state_rows(session, desired)
    await session.flush()

    report = ReconcileReport(
        inserted=tuple(changes.inserted),
        updated=tuple(changes.updated),
        reactivated=tuple(changes.reactivated),
        deactivated=tuple(changes.deactivated),
        unchanged=tuple(changes.unchanged),
    )
    log.info(
        "configuration reconciled",
        inserted=len(report.inserted),
        updated=len(report.updated),
        reactivated=len(report.reactivated),
        deactivated=len(report.deactivated),
        unchanged=len(report.unchanged),
    )
    return report


async def reconcile_file(session: AsyncSession, path: Path) -> ReconcileReport:
    """Read, validate and apply the configuration file.

    Raises:
        ConfigError: The file is missing or invalid. Nothing is written, so the
            configuration already in the database keeps running.
    """
    return await reconcile(session, load_config(path))


def _new_monitor(config: MonitorConfig) -> Monitor:
    monitor = Monitor(name=config.name, active=True)
    _apply(monitor, config)
    return monitor


def _apply(row: Monitor, config: MonitorConfig) -> bool:
    """Copy configuration onto a row, returning whether anything changed."""
    values = {
        "type": config.type,
        "target": config.target,
        "group_name": config.group,
        "description": config.description,
        "interval_seconds": config.interval,
        "timeout_seconds": config.timeout,
        "failure_threshold": config.failure_threshold,
        "recovery_threshold": config.recovery_threshold,
        "enabled": config.enabled,
        "active": True,
        "config": config.http.model_dump(mode="json") if config.http else {},
    }
    changed = False
    for attribute, value in values.items():
        if getattr(row, attribute, None) != value:
            setattr(row, attribute, value)
            changed = True
    return changed


async def _deactivate_absent(session: AsyncSession, keep: Sequence[str]) -> list[str]:
    """Soft-delete active monitors that are no longer in the file."""
    active = await repo.list_monitors(session, active_only=True)
    absent = [monitor for monitor in active if monitor.name not in set(keep)]
    for monitor in absent:
        monitor.active = False
    return [monitor.name for monitor in absent]


async def _sync_state_rows(session: AsyncSession, desired: Sequence[MonitorConfig]) -> None:
    """Ensure every configured monitor has a state row with a consistent status."""
    by_name = {config.name: config for config in desired}
    for monitor in await repo.list_monitors(session, active_only=True):
        config = by_name.get(monitor.name)
        if config is None:
            continue
        state = await repo.ensure_state(session, monitor.id)
        if not config.enabled:
            state.status = MonitorStatus.PAUSED
        elif state.status is MonitorStatus.PAUSED:
            state.status = MonitorStatus.UNKNOWN
            state.consecutive_failures = 0
            state.consecutive_successes = 0
