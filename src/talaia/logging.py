"""Structured logging configuration."""

import logging
import sys

import structlog

from talaia.settings import LogFormat, LogLevel

# Libraries that install their own handlers and must be redirected to ours.
_THIRD_PARTY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "sqlalchemy.engine",
    "alembic",
)


# Requests that arrive constantly by design and say nothing when they succeed: the
# container healthcheck, Prometheus, the dashboard's per-row polling, and assets.
QUIET_PATH_PREFIXES = ("/healthz", "/readyz", "/metrics", "/partials/", "/static/")


class QuietAccessLog(logging.Filter):
    """Drop uvicorn access lines for routine, high-frequency, uninteresting requests.

    Failures are always kept: a 500 on /readyz is exactly the kind of thing the log is for.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False to drop the record."""
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status_code = str(args[2]), args[4]
        if isinstance(status_code, int) and status_code >= 400:
            return True
        return not path.startswith(QUIET_PATH_PREFIXES)


def configure_logging(level: LogLevel = "INFO", log_format: LogFormat = "json") -> None:
    """Configure structlog and the standard library logging module.

    Call once at startup. Standard-library records are routed through the same
    renderer, so the process emits a single log format.

    Args:
        level: Minimum severity to emit.
        log_format: ``"json"`` for machine-readable output, ``"console"`` for humans.
    """
    numeric_level = getattr(logging, level)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared_processors, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = True

    access = logging.getLogger("uvicorn.access")
    access.filters = [QuietAccessLog()]


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
