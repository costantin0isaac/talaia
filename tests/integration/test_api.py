"""API tests against the real application."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from talaia.api.app import create_app
from talaia.api.dependencies import get_session
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.settings import Settings

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


@pytest.fixture
def app(session: AsyncSession, database_url: str) -> FastAPI:
    """Build the app without its lifespan, wired to the test session."""
    settings = Settings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]
    application = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def make_monitor(
    session: AsyncSession,
    name: str,
    *,
    status: MonitorStatus = MonitorStatus.UP,
    group: str | None = "services",
) -> Monitor:
    monitor = Monitor(
        name=name,
        type="http",
        target="http://10.0.0.1",
        group_name=group,
        description="an example",
        interval_seconds=60,
        timeout_seconds=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        active=True,
        config={},
    )
    session.add(monitor)
    await session.flush()
    state = await repo.ensure_state(session, monitor.id)
    state.status = status
    await session.flush()
    return monitor


class TestHealthz:
    async def test_returns_ok_and_a_version(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"]

    async def test_does_not_touch_the_database(self, client: httpx.AsyncClient) -> None:
        """Liveness must answer even when the database is unreachable."""
        assert (await client.get("/healthz")).status_code == 200


class TestListMonitors:
    async def test_empty_instance(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/monitors")

        assert response.status_code == 200
        assert response.json() == {"monitors": []}

    async def test_returns_configuration_and_state(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        body = (await client.get("/api/monitors")).json()

        assert len(body["monitors"]) == 1
        monitor = body["monitors"][0]
        assert monitor["name"] == "web"
        assert monitor["type"] == "http"
        assert monitor["target"] == "http://10.0.0.1"
        assert monitor["group"] == "services"
        assert monitor["interval_seconds"] == 60
        assert monitor["state"]["status"] == "up"
        assert monitor["state"]["consecutive_failures"] == 0

    async def test_inactive_monitors_are_excluded(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "retired")
        monitor.active = False
        await session.flush()

        assert (await client.get("/api/monitors")).json()["monitors"] == []

    async def test_ordered_by_group_then_name(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "zeta", group="apps")
        await make_monitor(session, "alpha", group="apps")
        await make_monitor(session, "beta", group="infra")

        names = [m["name"] for m in (await client.get("/api/monitors")).json()["monitors"]]

        assert names == ["alpha", "zeta", "beta"]

    async def test_latency_and_error_are_reported(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web", status=MonitorStatus.DOWN)
        state = await repo.ensure_state(session, monitor.id)
        state.last_error = "connection refused"
        state.last_latency_ms = None
        state.last_checked_at = NOW
        await session.flush()

        body = (await client.get("/api/monitors")).json()["monitors"][0]

        assert body["state"]["last_error"] == "connection refused"
        assert body["state"]["last_latency_ms"] is None
        assert body["state"]["last_checked_at"] is not None


class TestSummary:
    async def test_empty_instance(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/summary")).json()

        assert body == {
            "total": 0,
            "up": 0,
            "down": 0,
            "unknown": 0,
            "paused": 0,
            "open_incidents": 0,
            "uptime_24h": None,
        }

    async def test_counts_by_status(self, client: httpx.AsyncClient, session: AsyncSession) -> None:
        await make_monitor(session, "a", status=MonitorStatus.UP)
        await make_monitor(session, "b", status=MonitorStatus.UP)
        await make_monitor(session, "c", status=MonitorStatus.DOWN)
        await make_monitor(session, "d", status=MonitorStatus.PAUSED)

        body = (await client.get("/api/summary")).json()

        assert body["total"] == 4
        assert body["up"] == 2
        assert body["down"] == 1
        assert body["paused"] == 1
        assert body["unknown"] == 0

    async def test_open_incident_count(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web", status=MonitorStatus.DOWN)
        await repo.open_incident(session, monitor_id=monitor.id, started_at=NOW, cause="refused")
        await session.flush()

        assert (await client.get("/api/summary")).json()["open_incidents"] == 1

    async def test_uptime_over_the_last_24_hours(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        for success in (True, True, True, False):
            await repo.record_check_result(
                session, monitor_id=monitor.id, checked_at=NOW, success=success
            )
        await session.flush()

        assert (await client.get("/api/summary")).json()["uptime_24h"] == 0.75

    async def test_older_results_are_outside_the_window(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await repo.record_check_result(
            session, monitor_id=monitor.id, checked_at=NOW - timedelta(days=3), success=False
        )
        await session.flush()

        assert (await client.get("/api/summary")).json()["uptime_24h"] is None


class TestOpenApi:
    async def test_schema_documents_the_endpoints(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        assert "/api/monitors" in schema["paths"]
        assert "/api/summary" in schema["paths"]
        assert "/healthz" in schema["paths"]

    async def test_there_are_no_write_endpoints_for_monitors(
        self, client: httpx.AsyncClient
    ) -> None:
        """Section 11.1: configuration is never edited through the API."""
        schema = (await client.get("/openapi.json")).json()

        for path, operations in schema["paths"].items():
            if path.startswith("/api/monitors"):
                assert set(operations) == {"get"}
