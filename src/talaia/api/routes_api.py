"""JSON endpoints."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from talaia import __version__
from talaia.api.dependencies import get_session
from talaia.api.schemas import (
    CheckResultRead,
    Health,
    IncidentList,
    IncidentRead,
    MonitorDetail,
    MonitorList,
    MonitorRead,
    MonitorState,
    Readiness,
    ReloadResult,
    ResultList,
    Summary,
)
from talaia.config.loader import ConfigError
from talaia.config.reconciler import reconcile_file
from talaia.db import repository as repo
from talaia.db.models import Incident, Monitor, MonitorStatus
from talaia.db.models import MonitorState as MonitorStateRow
from talaia.engine.scheduler import Scheduler
from talaia.logging import get_logger
from talaia.metrics.registry import CONTENT_TYPE, Metrics, MonitorSample
from talaia.settings import Settings

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["api"])
public_router = APIRouter(tags=["public"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

DEFAULT_RESULT_HOURS = 24
MAX_RESULT_HOURS = 24 * 30
DEFAULT_INCIDENT_LIMIT = 50
MAX_INCIDENT_LIMIT = 500
DETAIL_RESULTS = 50
DETAIL_INCIDENTS = 10


def _state_schema(state: MonitorStateRow | None) -> MonitorState:
    """Project a state row, or the state a monitor has before its first check."""
    if state is None:
        return MonitorState(
            status=MonitorStatus.UNKNOWN,
            consecutive_failures=0,
            consecutive_successes=0,
            last_checked_at=None,
            last_latency_ms=None,
            last_error=None,
            status_changed_at=None,
        )
    return MonitorState.model_validate(state)


def _to_read(monitor: Monitor, state: MonitorStateRow | None) -> MonitorRead:
    return MonitorRead(
        name=monitor.name,
        type=monitor.type,
        target=monitor.target,
        group=monitor.group_name,
        description=monitor.description,
        interval_seconds=monitor.interval_seconds,
        timeout_seconds=monitor.timeout_seconds,
        enabled=monitor.enabled,
        active=monitor.active,
        state=_state_schema(state),
    )


def _to_incident(incident: Incident, monitor_name: str) -> IncidentRead:
    return IncidentRead(
        monitor=monitor_name,
        started_at=incident.started_at,
        resolved_at=incident.resolved_at,
        duration_seconds=incident.duration_seconds,
        cause=incident.cause,
    )


@router.get("/monitors", response_model=MonitorList)
async def list_monitors(session: SessionDep) -> MonitorList:
    """Return every active monitor with its current state."""
    pairs = await repo.list_states(session)
    return MonitorList(monitors=[_to_read(monitor, state) for monitor, state in pairs])


@router.get("/monitors/{name}", response_model=MonitorDetail)
async def get_monitor(name: str, session: SessionDep) -> MonitorDetail:
    """Return one monitor with its state, recent results and recent incidents."""
    monitor = await _require_monitor(session, name)
    since = datetime.now(UTC) - timedelta(hours=DEFAULT_RESULT_HOURS)

    results = await repo.list_check_results(session, monitor.id, since=since, limit=DETAIL_RESULTS)
    incidents = await repo.list_incidents(session, monitor_id=monitor.id, limit=DETAIL_INCIDENTS)

    return MonitorDetail(
        **_to_read(monitor, await repo.get_state(session, monitor.id)).model_dump(),
        uptime_24h=await repo.uptime_ratio(session, monitor.id, since=since),
        recent_results=[CheckResultRead.model_validate(result) for result in results],
        recent_incidents=[_to_incident(incident, monitor.name) for incident in incidents],
    )


@router.get("/monitors/{name}/results", response_model=ResultList)
async def monitor_results(
    name: str,
    session: SessionDep,
    hours: Annotated[int, Query(ge=1, le=MAX_RESULT_HOURS)] = DEFAULT_RESULT_HOURS,
) -> ResultList:
    """Return a monitor's raw results over the last ``hours`` hours, newest first."""
    monitor = await _require_monitor(session, name)
    since = datetime.now(UTC) - timedelta(hours=hours)
    results = await repo.list_check_results(session, monitor.id, since=since)
    return ResultList(
        monitor=monitor.name,
        hours=hours,
        results=[CheckResultRead.model_validate(result) for result in results],
    )


@router.get("/incidents", response_model=IncidentList)
async def list_incidents(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_INCIDENT_LIMIT)] = DEFAULT_INCIDENT_LIMIT,
    only_open: Annotated[bool, Query(alias="open")] = False,
) -> IncidentList:
    """Return incident history across every monitor, newest first."""
    rows = await repo.list_incidents_with_monitor(session, open_only=only_open, limit=limit)
    return IncidentList(incidents=[_to_incident(incident, name) for incident, name in rows])


@router.get("/summary", response_model=Summary)
async def summary(session: SessionDep) -> Summary:
    """Return counts by status, overall uptime and the open incident count."""
    counts = await repo.count_by_status(session)
    since = datetime.now(UTC) - timedelta(hours=24)
    return Summary(
        total=sum(counts.values()),
        up=counts.get(MonitorStatus.UP, 0),
        down=counts.get(MonitorStatus.DOWN, 0),
        unknown=counts.get(MonitorStatus.UNKNOWN, 0),
        paused=counts.get(MonitorStatus.PAUSED, 0),
        open_incidents=await repo.count_open_incidents(session),
        uptime_24h=await repo.overall_uptime_ratio(session, since=since),
    )


@router.post("/reload", response_model=ReloadResult)
async def reload(request: Request, session: SessionDep) -> ReloadResult:
    """Re-read ``monitors.yaml``, reconcile it, and resync the running tasks."""
    settings: Settings = request.app.state.settings
    scheduler: Scheduler = request.app.state.scheduler

    try:
        async with session.begin():
            report = await reconcile_file(session, settings.config_path)
    except ConfigError as error:
        # The reconciliation transaction rolled back, so the running configuration stands.
        log.warning("reload rejected an invalid configuration", error=str(error))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error

    await scheduler.sync()
    log.info("configuration reloaded", changed=report.changed)

    return ReloadResult(
        changed=report.changed,
        inserted=list(report.inserted),
        updated=list(report.updated),
        reactivated=list(report.reactivated),
        deactivated=list(report.deactivated),
        unchanged=list(report.unchanged),
        monitors_running=len(scheduler.running_monitors),
    )


@public_router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness: the process is running and serving."""
    return Health(status="ok", version=__version__)


@public_router.get("/readyz", response_model=Readiness)
async def readyz(request: Request, session: SessionDep, response: Response) -> Readiness:
    """Readiness: the database answers and the scheduler is started."""
    database = await _database_reachable(session)
    scheduler: Scheduler | None = getattr(request.app.state, "scheduler", None)
    running = scheduler is not None and scheduler.is_started

    ready = database and running
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "not ready", database=database, scheduler=running)


@public_router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, session: SessionDep) -> Response:
    """Expose Prometheus metrics, read from the database at scrape time."""
    collectors: Metrics = request.app.state.metrics
    samples = [
        MonitorSample(
            name=monitor.name,
            type=monitor.type.value,
            group=monitor.group_name,
            status=state.status,
            last_latency_ms=state.last_latency_ms,
            consecutive_failures=state.consecutive_failures,
        )
        for monitor, state in await repo.list_states(session)
    ]
    return Response(content=collectors.render(samples), media_type=CONTENT_TYPE)


async def _require_monitor(session: AsyncSession, name: str) -> Monitor:
    """Return the named monitor, or raise the 404 the API promises."""
    monitor = await repo.get_monitor_by_name(session, name)
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no monitor named {name!r}"
        )
    return monitor


async def _database_reachable(session: AsyncSession) -> bool:
    """Ask the database the cheapest question there is."""
    try:
        await session.execute(select(1))
    except SQLAlchemyError as error:
        log.warning("readiness probe could not reach the database", error=str(error))
        return False
    return True
