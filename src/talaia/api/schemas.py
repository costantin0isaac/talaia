"""Response models for the JSON API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from talaia.config.schema import MonitorType
from talaia.db.models import MonitorStatus


class MonitorState(BaseModel):
    """Current runtime state of a monitor."""

    model_config = ConfigDict(from_attributes=True)

    status: MonitorStatus
    consecutive_failures: int
    consecutive_successes: int
    last_checked_at: datetime | None
    last_latency_ms: int | None
    last_error: str | None
    status_changed_at: datetime | None


class MonitorRead(BaseModel):
    """A monitor's configuration together with its current state."""

    name: str
    type: MonitorType
    target: str
    group: str | None
    description: str | None
    interval_seconds: int
    timeout_seconds: int
    enabled: bool
    state: MonitorState


class MonitorList(BaseModel):
    """All active monitors."""

    monitors: list[MonitorRead]


class Summary(BaseModel):
    """Counts and headline figures for the whole instance."""

    total: int
    up: int
    down: int
    unknown: int
    paused: int
    open_incidents: int
    uptime_24h: float | None


class Health(BaseModel):
    """Liveness response."""

    status: str
    version: str
