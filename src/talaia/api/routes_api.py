"""JSON endpoints."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from talaia import __version__
from talaia.api.dependencies import get_session
from talaia.api.schemas import Health, MonitorList, MonitorRead, MonitorState, Summary
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.db.models import MonitorState as MonitorStateRow
from talaia.metrics.registry import CONTENT_TYPE, Metrics, MonitorSample

router = APIRouter(prefix="/api", tags=["api"])
public_router = APIRouter(tags=["public"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _to_read(monitor: Monitor, state: MonitorStateRow) -> MonitorRead:
    return MonitorRead(
        name=monitor.name,
        type=monitor.type,
        target=monitor.target,
        group=monitor.group_name,
        description=monitor.description,
        interval_seconds=monitor.interval_seconds,
        timeout_seconds=monitor.timeout_seconds,
        enabled=monitor.enabled,
        state=MonitorState.model_validate(state),
    )


@router.get("/monitors", response_model=MonitorList)
async def list_monitors(session: SessionDep) -> MonitorList:
    """Return every active monitor with its current state."""
    pairs = await repo.list_states(session)
    return MonitorList(monitors=[_to_read(monitor, state) for monitor, state in pairs])


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


@public_router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness: the process is running and serving."""
    return Health(status="ok", version=__version__)


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
