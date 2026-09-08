"""Structured logging configuration.

The application logs *events with fields*, not formatted sentences::

    log.info("check completed", monitor="grafana", success=True, latency_ms=42)

In production that is rendered as one JSON object per line, which a log shipper can index
without regex parsing. In development it is rendered as coloured key-value output, which a
human can read. The call site is identical in both cases — that is the point of structlog.

Third-party libraries (uvicorn, sqlalchemy, alembic) use the standard library's logging
module and know nothing about structlog. They are routed through the same rendering
pipeline via :class:`structlog.stdlib.ProcessorFormatter`, so a deployment emits exactly
one log format rather than JSON interleaved with plain text.
"""

import logging
import sys

import structlog

from talaia.settings import LogFormat, LogLevel

# Loggers that install their own handlers at import time. Left alone, uvicorn would print
# its access log in its own format, bypassing everything configured here.
_THIRD_PARTY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "sqlalchemy.engine",
    "alembic",
)


def configure_logging(level: LogLevel = "INFO", log_format: LogFormat = "json") -> None:
    """Configure structlog and the standard library logging module together.

    Call this once, as early as possible in process startup — before anything else has a
    chance to emit a log line. Calling it again reconfigures cleanly; it is not additive.

    Args:
        level: Minimum severity to emit.
        log_format: ``"json"`` for machine-readable output, ``"console"`` for humans.
    """
    numeric_level = getattr(logging, level)

    # Processors form a pipeline: each receives the event dictionary and returns it,
    # enriched. These are shared by structlog events and by standard-library records, so
    # both end up carrying the same fields.
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,  # fields bound to the async task context
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        # UTC and ISO-8601, always. Local time in logs is a recurring source of confusion
        # when correlating against Prometheus, which is UTC.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hands the event dictionary to the stdlib handler below instead of rendering
            # it here. This is what unifies the two logging worlds.
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
        # Applied only to records from libraries that never went through structlog.
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
    root.handlers = [handler]  # replace, so repeated calls do not duplicate output
    root.setLevel(numeric_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = True  # let the root handler above do the rendering


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to ``name``.

    Modules should call this once at import time with ``__name__`` and reuse the result.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
