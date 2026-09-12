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
    last_expires_in_days: int | None


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
    active: bool
    state: MonitorState


class MonitorList(BaseModel):
    """All active monitors."""

    monitors: list[MonitorRead]


class CheckResultRead(BaseModel):
    """One recorded check."""

    model_config = ConfigDict(from_attributes=True)

    checked_at: datetime
    success: bool
    latency_ms: int | None
    status_code: int | None
    error: str | None


class IncidentRead(BaseModel):
    """A period during which a monitor was down."""

    monitor: str
    started_at: datetime
    resolved_at: datetime | None
    duration_seconds: int | None
    cause: str


class MonitorDetail(MonitorRead):
    """Everything the detail page needs about one monitor."""

    uptime_24h: float | None
    recent_results: list[CheckResultRead]
    recent_incidents: list[IncidentRead]


class ResultList(BaseModel):
    """A window of raw results for one monitor."""

    monitor: str
    hours: int
    results: list[CheckResultRead]


class IncidentList(BaseModel):
    """Incident history across monitors."""

    incidents: list[IncidentRead]


class ReloadResult(BaseModel):
    """What re-reading ``monitors.yaml`` changed."""

    changed: bool
    inserted: list[str]
    updated: list[str]
    reactivated: list[str]
    deactivated: list[str]
    unchanged: list[str]
    monitors_running: int


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


class Readiness(BaseModel):
    """Readiness response: whether the dependencies this process needs are working."""

    status: str
    database: bool
    scheduler: bool
