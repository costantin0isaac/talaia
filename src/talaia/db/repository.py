"""All database queries.

Services call these functions; they never build SQL themselves.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from talaia.db.models import (
    CheckResult,
    DailyUptime,
    Incident,
    Monitor,
    MonitorState,
    MonitorStatus,
    Session,
    User,
)

NotificationKind = Literal["down", "up"]

NOTIFICATION_COLUMNS: dict[NotificationKind, InstrumentedAttribute[datetime | None]] = {
    "down": Incident.notified_down_at,
    "up": Incident.notified_up_at,
}


async def list_monitors(session: AsyncSession, *, active_only: bool = True) -> Sequence[Monitor]:
    """Return monitors ordered by group then name."""
    statement = select(Monitor).order_by(Monitor.group_name, Monitor.name)
    if active_only:
        statement = statement.where(Monitor.active.is_(True))
    return (await session.scalars(statement)).all()


async def list_schedulable_monitors(session: AsyncSession) -> Sequence[Monitor]:
    """Return the monitors the scheduler should be running."""
    statement = (
        select(Monitor)
        .where(Monitor.active.is_(True), Monitor.enabled.is_(True))
        .order_by(Monitor.name)
    )
    return (await session.scalars(statement)).all()


async def get_monitor_by_name(session: AsyncSession, name: str) -> Monitor | None:
    """Return the monitor with this name, active or not."""
    monitor: Monitor | None = await session.scalar(select(Monitor).where(Monitor.name == name))
    return monitor


async def get_state(session: AsyncSession, monitor_id: int) -> MonitorState | None:
    """Return the state row for a monitor."""
    return await session.get(MonitorState, monitor_id)


async def ensure_state(session: AsyncSession, monitor_id: int) -> MonitorState:
    """Return the state row for a monitor, creating it if absent."""
    state = await session.get(MonitorState, monitor_id)
    if state is None:
        state = MonitorState(monitor_id=monitor_id, status=MonitorStatus.UNKNOWN)
        session.add(state)
        await session.flush()
    return state


async def list_states(session: AsyncSession) -> Sequence[tuple[Monitor, MonitorState]]:
    """Return every active monitor with its state, for the dashboard and metrics."""
    statement = (
        select(Monitor, MonitorState)
        .join(MonitorState, MonitorState.monitor_id == Monitor.id)
        .where(Monitor.active.is_(True))
        .order_by(Monitor.group_name, Monitor.name)
    )
    return [(monitor, state) for monitor, state in (await session.execute(statement)).all()]


async def count_by_status(session: AsyncSession) -> dict[MonitorStatus, int]:
    """Return the number of active monitors in each status."""
    statement = (
        select(MonitorState.status, func.count())
        .join(Monitor, Monitor.id == MonitorState.monitor_id)
        .where(Monitor.active.is_(True))
        .group_by(MonitorState.status)
    )
    return dict((await session.execute(statement)).all())  # type: ignore[arg-type]


async def record_check_result(
    session: AsyncSession,
    *,
    monitor_id: int,
    checked_at: datetime,
    success: bool,
    latency_ms: int | None = None,
    status_code: int | None = None,
    error: str | None = None,
) -> CheckResult:
    """Insert the outcome of one check."""
    result = CheckResult(
        monitor_id=monitor_id,
        checked_at=checked_at,
        success=success,
        latency_ms=latency_ms,
        status_code=status_code,
        error=error,
    )
    session.add(result)
    await session.flush()
    return result


async def list_check_results(
    session: AsyncSession, monitor_id: int, *, since: datetime, limit: int = 1000
) -> Sequence[CheckResult]:
    """Return a monitor's results since ``since``, newest first."""
    statement = (
        select(CheckResult)
        .where(CheckResult.monitor_id == monitor_id, CheckResult.checked_at >= since)
        .order_by(CheckResult.checked_at.desc())
        .limit(limit)
    )
    return (await session.scalars(statement)).all()


async def list_latest_results_by_monitor(
    session: AsyncSession, monitor_ids: Sequence[int], *, limit: int = 40
) -> dict[int, list[CheckResult]]:
    """Return the newest ``limit`` results for each monitor, in one query.

    A window function rather than one query per monitor: the dashboard renders every
    monitor's strip at once, and the per-monitor form would not survive a long list.
    """
    if not monitor_ids:
        return {}

    ranked = (
        select(
            CheckResult,
            func.row_number()
            .over(
                partition_by=CheckResult.monitor_id,
                order_by=CheckResult.checked_at.desc(),
            )
            .label("position"),
        )
        .where(CheckResult.monitor_id.in_(monitor_ids))
        .subquery()
    )
    result = aliased(CheckResult, ranked)
    statement = select(result).where(ranked.c.position <= limit).order_by(result.checked_at.desc())

    grouped: dict[int, list[CheckResult]] = {monitor_id: [] for monitor_id in monitor_ids}
    for row in (await session.scalars(statement)).all():
        grouped[row.monitor_id].append(row)
    return grouped


async def uptime_ratios(session: AsyncSession, *, since: datetime) -> dict[int, float]:
    """Return the success ratio of every monitor that has results since ``since``."""
    statement = (
        select(
            CheckResult.monitor_id,
            func.count(),
            func.count().filter(CheckResult.success.is_(True)),
        )
        .where(CheckResult.checked_at >= since)
        .group_by(CheckResult.monitor_id)
    )
    rows = (await session.execute(statement)).all()
    return {
        monitor_id: float(successful) / float(total)
        for monitor_id, total, successful in rows
        if total
    }


async def uptime_since_day(session: AsyncSession, monitor_id: int, *, since: date) -> float | None:
    """Return uptime from the daily rollups, which outlive the pruning of raw results."""
    statement = select(
        func.sum(DailyUptime.total_checks), func.sum(DailyUptime.successful_checks)
    ).where(DailyUptime.monitor_id == monitor_id, DailyUptime.day >= since)
    total, successful = (await session.execute(statement)).one()
    if not total:
        return None
    return float(successful) / float(total)


async def uptime_ratio(session: AsyncSession, monitor_id: int, *, since: datetime) -> float | None:
    """Return the fraction of successful checks since ``since``, or None if there were none."""
    statement = select(func.count(), func.count().filter(CheckResult.success.is_(True))).where(
        CheckResult.monitor_id == monitor_id, CheckResult.checked_at >= since
    )
    total, successful = (await session.execute(statement)).one()
    if not total:
        return None
    return float(successful) / float(total)


async def overall_uptime_ratio(session: AsyncSession, *, since: datetime) -> float | None:
    """Return the fraction of successful checks across all active monitors."""
    statement = (
        select(func.count(), func.count().filter(CheckResult.success.is_(True)))
        .select_from(CheckResult)
        .join(Monitor, Monitor.id == CheckResult.monitor_id)
        .where(CheckResult.checked_at >= since, Monitor.active.is_(True))
    )
    total, successful = (await session.execute(statement)).one()
    if not total:
        return None
    return float(successful) / float(total)


async def open_incident(
    session: AsyncSession, *, monitor_id: int, started_at: datetime, cause: str
) -> Incident:
    """Open an incident for a monitor that has just gone down."""
    incident = Incident(monitor_id=monitor_id, started_at=started_at, cause=cause)
    session.add(incident)
    await session.flush()
    return incident


async def get_open_incident(session: AsyncSession, monitor_id: int) -> Incident | None:
    """Return the monitor's unresolved incident, if it has one."""
    statement = select(Incident).where(
        Incident.monitor_id == monitor_id, Incident.resolved_at.is_(None)
    )
    incident: Incident | None = await session.scalar(statement)
    return incident


async def resolve_incident(
    session: AsyncSession, incident: Incident, resolved_at: datetime
) -> None:
    """Close an incident and record how long it lasted."""
    incident.resolved_at = resolved_at
    incident.duration_seconds = int((resolved_at - incident.started_at).total_seconds())


async def get_latest_incident(session: AsyncSession, monitor_id: int) -> Incident | None:
    """Return the monitor's most recent incident, open or resolved."""
    statement = (
        select(Incident)
        .where(Incident.monitor_id == monitor_id)
        .order_by(Incident.started_at.desc())
        .limit(1)
    )
    incident: Incident | None = await session.scalar(statement)
    return incident


async def claim_notification(
    session: AsyncSession, incident_id: int, *, kind: NotificationKind, at: datetime
) -> bool:
    """Stamp an incident as notified, and report whether this caller won the stamp.

    The stamp is set only if it was unset, in one statement, so an event is announced at
    most once however many times the announcement is attempted.
    """
    column = NOTIFICATION_COLUMNS[kind]
    statement = (
        update(Incident)
        .where(Incident.id == incident_id, column.is_(None))
        .values({column: at})
        .returning(Incident.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def list_incidents(
    session: AsyncSession,
    *,
    monitor_id: int | None = None,
    open_only: bool = False,
    limit: int = 50,
) -> Sequence[Incident]:
    """Return incidents, newest first."""
    statement = select(Incident).order_by(Incident.started_at.desc()).limit(limit)
    if monitor_id is not None:
        statement = statement.where(Incident.monitor_id == monitor_id)
    if open_only:
        statement = statement.where(Incident.resolved_at.is_(None))
    return (await session.scalars(statement)).all()


async def list_incidents_with_monitor(
    session: AsyncSession, *, open_only: bool = False, limit: int = 50
) -> Sequence[tuple[Incident, str]]:
    """Return incidents across all monitors, newest first, each with its monitor's name."""
    statement = (
        select(Incident, Monitor.name)
        .join(Monitor, Monitor.id == Incident.monitor_id)
        .order_by(Incident.started_at.desc())
        .limit(limit)
    )
    if open_only:
        statement = statement.where(Incident.resolved_at.is_(None))
    rows = (await session.execute(statement)).all()
    return [(incident, name) for incident, name in rows]


async def count_open_incidents(session: AsyncSession) -> int:
    """Return how many incidents are currently unresolved."""
    statement = (
        select(func.count())
        .select_from(Incident)
        .join(Monitor, Monitor.id == Incident.monitor_id)
        .where(Incident.resolved_at.is_(None), Monitor.active.is_(True))
    )
    return (await session.execute(statement)).scalar_one()


async def aggregate_check_results(
    session: AsyncSession, *, start: datetime, end: datetime
) -> Sequence[tuple[int, int, int, int | None]]:
    """Summarise results in a window as ``(monitor_id, total, successful, avg_latency_ms)``."""
    statement = (
        select(
            CheckResult.monitor_id,
            func.count(),
            func.count().filter(CheckResult.success.is_(True)),
            func.avg(CheckResult.latency_ms),
        )
        .where(CheckResult.checked_at >= start, CheckResult.checked_at < end)
        .group_by(CheckResult.monitor_id)
    )
    rows = (await session.execute(statement)).all()
    return [
        (monitor_id, total, successful, round(average) if average is not None else None)
        for monitor_id, total, successful, average in rows
    ]


async def days_missing_rollups(session: AsyncSession, *, since: date, until: date) -> list[date]:
    """Return the UTC days in ``[since, until]`` that hold results but lack a rollup.

    A day counts as missing if any one monitor with results on it has no ``daily_uptime``
    row, which is what every day looks like after a stretch of Talaia not running.
    """
    day = func.date(func.timezone("UTC", CheckResult.checked_at))
    start = datetime.combine(since, time.min, tzinfo=UTC)
    end = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)
    has_rollup = (
        select(DailyUptime.monitor_id)
        .where(DailyUptime.monitor_id == CheckResult.monitor_id, DailyUptime.day == day)
        .exists()
    )
    statement = (
        select(day.label("day"))
        .where(CheckResult.checked_at >= start, CheckResult.checked_at < end, ~has_rollup)
        .group_by(day)
        .order_by(day)
    )
    return [row.day for row in (await session.execute(statement)).all()]


async def upsert_daily_uptime(
    session: AsyncSession,
    *,
    monitor_id: int,
    day: date,
    total_checks: int,
    successful_checks: int,
    avg_latency_ms: int | None,
) -> None:
    """Insert or refresh the rollup row for one monitor and day."""
    statement = insert(DailyUptime).values(
        monitor_id=monitor_id,
        day=day,
        total_checks=total_checks,
        successful_checks=successful_checks,
        avg_latency_ms=avg_latency_ms,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[DailyUptime.monitor_id, DailyUptime.day],
            set_={
                "total_checks": statement.excluded.total_checks,
                "successful_checks": statement.excluded.successful_checks,
                "avg_latency_ms": statement.excluded.avg_latency_ms,
            },
        )
    )


async def delete_check_results_before(
    session: AsyncSession, cutoff: datetime, *, batch_size: int = 10_000
) -> int:
    """Delete one batch of results older than ``cutoff``. Returns the number deleted."""
    subquery = (
        select(CheckResult.id).where(CheckResult.checked_at < cutoff).limit(batch_size).subquery()
    )
    statement = delete(CheckResult).where(CheckResult.id.in_(select(subquery.c.id)))
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


async def deactivate_missing_monitors(session: AsyncSession, keep_names: Sequence[str]) -> int:
    """Soft-delete active monitors whose names are absent from ``keep_names``."""
    statement = (
        update(Monitor)
        .where(Monitor.active.is_(True), Monitor.name.notin_(keep_names))
        .values(active=False)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


async def get_user_by_name(session: AsyncSession, username: str) -> User | None:
    """Return the user with this username, active or not."""
    user: User | None = await session.scalar(select(User).where(User.username == username))
    return user


async def list_users(session: AsyncSession) -> Sequence[User]:
    """Return every user, by name."""
    return (await session.scalars(select(User).order_by(User.username))).all()


async def count_users(session: AsyncSession) -> int:
    """Return how many users exist, so startup can warn when nobody can log in."""
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def create_user(session: AsyncSession, *, username: str, password_hash: str) -> User:
    """Insert a user."""
    user = User(username=username, password_hash=password_hash, active=True)
    session.add(user)
    await session.flush()
    return user


async def create_session(
    session: AsyncSession, *, token_hash: str, user_id: int, expires_at: datetime
) -> Session:
    """Record a login."""
    row = Session(token_hash=token_hash, user_id=user_id, expires_at=expires_at)
    session.add(row)
    await session.flush()
    return row


async def get_session_user(session: AsyncSession, token_hash: str, *, now: datetime) -> User | None:
    """Return the active user behind an unexpired session token.

    Expiry and the user's active flag are part of the lookup, so a disabled account or a
    stale token cannot authenticate however the caller behaves.
    """
    statement = (
        select(User)
        .join(Session, Session.user_id == User.id)
        .where(
            Session.token_hash == token_hash,
            Session.expires_at > now,
            User.active.is_(True),
        )
    )
    user: User | None = await session.scalar(statement)
    return user


async def touch_session(
    session: AsyncSession, token_hash: str, *, now: datetime, not_before: datetime
) -> bool:
    """Record that a session was used, at most once per interval.

    The dashboard polls a partial per monitor every 15 seconds, so an unconditional write
    here would turn every page view into a burst of writes for one informational column.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Session)
            .where(
                Session.token_hash == token_hash,
                or_(Session.last_seen_at.is_(None), Session.last_seen_at < not_before),
            )
            .values(last_seen_at=now)
        ),
    )
    return result.rowcount > 0


async def list_sessions_for_user(session: AsyncSession, user_id: int) -> Sequence[Session]:
    """Return a user's sessions, newest first, expired ones included until pruned."""
    statement = (
        select(Session).where(Session.user_id == user_id).order_by(Session.created_at.desc())
    )
    return (await session.scalars(statement)).all()


async def find_sessions_by_prefix(
    session: AsyncSession, user_id: int, prefix: str
) -> Sequence[Session]:
    """Return a user's sessions whose token hash starts with ``prefix``.

    The full hash is 64 characters; the operator types the first few. Returning every
    match lets the caller refuse an ambiguous prefix instead of guessing.
    """
    statement = select(Session).where(
        Session.user_id == user_id, Session.token_hash.startswith(prefix)
    )
    return (await session.scalars(statement)).all()


async def delete_session(session: AsyncSession, token_hash: str) -> None:
    """Forget one session, on logout."""
    await session.execute(delete(Session).where(Session.token_hash == token_hash))


async def delete_sessions_for_user(session: AsyncSession, user_id: int) -> int:
    """Forget every session of one user, used when their password changes."""
    result = cast(
        CursorResult[Any],
        await session.execute(delete(Session).where(Session.user_id == user_id)),
    )
    return result.rowcount


async def delete_expired_sessions(session: AsyncSession, *, now: datetime) -> int:
    """Drop sessions that can no longer authenticate anyone."""
    result = cast(
        CursorResult[Any],
        await session.execute(delete(Session).where(Session.expires_at <= now)),
    )
    return result.rowcount
