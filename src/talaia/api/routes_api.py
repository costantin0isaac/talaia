"""JSON endpoints."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from talaia import __version__
from talaia.api.dependencies import get_session
from talaia.api.schemas import Health, MonitorList, MonitorRead, MonitorState, Summary
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.db.models import MonitorState as MonitorStateRow

router = APIRouter(prefix="/api", tags=["api"])
health_router = APIRouter(tags=["health"])

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


@health_router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness: the process is running and serving."""
    return Health(status="ok", version=__version__)
