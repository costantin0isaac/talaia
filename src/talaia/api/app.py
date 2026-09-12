"""FastAPI application factory and lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from talaia import __version__
from talaia.api.routes_api import public_router, router
from talaia.checks.http import HttpClients
from talaia.checks.registry import build_registry
from talaia.config.loader import ConfigError
from talaia.config.reconciler import reconcile_file
from talaia.db.engine import create_engine, create_session_factory
from talaia.engine.retention import RetentionTask
from talaia.engine.scheduler import Scheduler
from talaia.logging import configure_logging, get_logger
from talaia.metrics.registry import Metrics
from talaia.notify.base import Notifier, NullNotifier
from talaia.notify.ntfy import NtfyNotifier
from talaia.settings import Settings, get_settings

log = get_logger(__name__)

CONNECTION_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)


def _build_notifier(settings: Settings) -> Notifier:
    """Return the ntfy notifier, or one that discards everything if no topic is set."""
    url, topic = settings.ntfy_url, settings.ntfy_topic
    if not url or not topic:
        log.info("notifications are disabled; no ntfy topic configured")
        return NullNotifier()
    return NtfyNotifier(url=url, topic=topic, token=settings.ntfy_token)


def _build_clients() -> HttpClients:
    """Create the two long-lived HTTP clients used by the checker."""
    return HttpClients(
        verifying=httpx.AsyncClient(limits=CONNECTION_LIMITS, verify=True),
        insecure=httpx.AsyncClient(limits=CONNECTION_LIMITS, verify=False),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop everything the application owns."""
    settings: Settings = app.state.settings

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    clients = _build_clients()
    metrics = Metrics(version=__version__, commit=settings.commit)
    notifier = _build_notifier(settings)
    scheduler = Scheduler(
        session_factory,
        build_registry(clients),
        recorder=metrics,
        notifier=notifier,
        base_url=settings.base_url,
    )
    retention = RetentionTask(session_factory, retention_days=settings.retention_days)

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.scheduler = scheduler
    app.state.retention = retention
    app.state.metrics = metrics
    app.state.notifier = notifier

    async with session_factory() as session, session.begin():
        try:
            await reconcile_file(session, settings.config_path)
        except ConfigError:
            log.exception("configuration is invalid; keeping what is already in the database")
            raise

    await scheduler.start()
    await retention.start()
    log.info("talaia started", version=__version__, monitors=len(scheduler.running_monitors))

    try:
        yield
    finally:
        await retention.stop()
        await scheduler.stop()
        await notifier.aclose()
        await clients.verifying.aclose()
        await clients.insecure.aclose()
        await engine.dispose()
        log.info("talaia stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, log_format=resolved.log_format)

    app = FastAPI(
        title="Talaia",
        version=__version__,
        summary="A self-hosted uptime monitor for a homelab.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.metrics = Metrics(version=__version__, commit=resolved.commit)
    app.include_router(public_router)
    app.include_router(router)
    return app
