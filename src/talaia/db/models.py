"""SQLAlchemy models.

The database stores state and history. Monitor configuration is projected here from
``monitors.yaml`` by the reconciler and is never edited through the application.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from talaia.config.schema import MonitorType

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class MonitorStatus(StrEnum):
    """Current state of a monitor."""

    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"
    PAUSED = "paused"


def _enum_column(enum_type: type[StrEnum], length: int) -> Enum:
    """Return a VARCHAR-backed enum column that stores the member values."""
    return Enum(
        enum_type,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
    )


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Monitor(Base):
    """A configured monitor, projected from the YAML file."""

    __tablename__ = "monitors"
    __table_args__ = (Index("ix_monitors_active_enabled", "active", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[MonitorType] = mapped_column(_enum_column(MonitorType, 16))
    target: Mapped[str] = mapped_column(Text)
    group_name: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    failure_threshold: Mapped[int] = mapped_column(Integer)
    recovery_threshold: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    state: Mapped[MonitorState] = relationship(
        back_populates="monitor", cascade="all, delete-orphan", uselist=False
    )


class MonitorState(Base):
    """Current runtime state, kept apart from configuration."""

    __tablename__ = "monitor_states"

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[MonitorStatus] = mapped_column(
        _enum_column(MonitorStatus, 16), default=MonitorStatus.UNKNOWN
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_expires_in_days: Mapped[int | None] = mapped_column(Integer)

    monitor: Mapped[Monitor] = relationship(back_populates="state")


class CheckResult(Base):
    """The outcome of one individual check. High volume, pruned by retention."""

    __tablename__ = "check_results"
    __table_args__ = (
        Index("ix_check_results_monitor_id_checked_at", "monitor_id", text("checked_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Incident(Base):
    """A continuous period during which a monitor was down. Never pruned."""

    __tablename__ = "incidents"
    __table_args__ = (
        Index(
            "uq_incidents_monitor_id_open",
            "monitor_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    cause: Mapped[str] = mapped_column(Text)
    notified_down_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyUptime(Base):
    """Daily rollup, so history survives the pruning of check results."""

    __tablename__ = "daily_uptime"

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    total_checks: Mapped[int] = mapped_column(Integer)
    successful_checks: Mapped[int] = mapped_column(Integer)
    avg_latency_ms: Mapped[int | None] = mapped_column(Integer)


class User(Base):
    """Someone who may log in. Created from the CLI; there is no signup."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    """One logged-in browser.

    Only a hash of the token is stored, so a copy of this table cannot be replayed as a
    set of live sessions.
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
