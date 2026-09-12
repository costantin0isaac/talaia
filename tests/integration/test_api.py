"""API tests against the real application."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from talaia.api.app import create_app
from talaia.api.dependencies import get_session
from talaia.config.schema import MonitorType
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
        type=MonitorType.HTTP,
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


class TestMetrics:
    async def test_exposes_prometheus_text(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "talaia_build_info" in response.text

    async def test_reflects_monitor_state_from_the_database(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web", status=MonitorStatus.DOWN)
        state = await repo.ensure_state(session, monitor.id)
        state.last_latency_ms = 250
        state.consecutive_failures = 4
        await session.flush()

        text = (await client.get("/metrics")).text

        assert 'talaia_check_up{group="services",monitor="web",type="http"} 0.0' in text
        assert (
            'talaia_check_duration_seconds{group="services",monitor="web",type="http"} 0.25' in text
        )
        assert 'talaia_monitor_consecutive_failures{monitor="web"} 4.0' in text
        assert 'talaia_monitors_total{status="down"} 1.0' in text

    async def test_inactive_monitors_are_not_exported(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "retired")
        monitor.active = False
        await session.flush()

        assert "retired" not in (await client.get("/metrics")).text

    async def test_is_not_in_the_openapi_schema(self, client: httpx.AsyncClient) -> None:
        """The exposition format is not JSON, so it does not belong in the API docs."""
        schema = (await client.get("/openapi.json")).json()

        assert "/metrics" not in schema["paths"]


class FakeScheduler:
    """Stands in for the scheduler the lifespan would have put on app.state."""

    def __init__(self, *, started: bool = True) -> None:
        self.is_started = started
        self.running_monitors: frozenset[str] = frozenset({"example-webapp"})
        self.syncs = 0

    async def sync(self) -> None:
        self.syncs += 1


class UnreachableSession:
    """A session whose every query fails, as if the database were gone."""

    async def execute(self, *args: object, **kwargs: object) -> object:
        msg = "connection refused"
        raise OperationalError(msg, None, Exception(msg))


CONFIG = """
defaults:
  interval: 60
  timeout: 10

monitors:
  - name: alpha
    type: http
    target: http://10.0.0.1
    group: services
"""

CHANGED_CONFIG = """
defaults:
  interval: 60
  timeout: 10

monitors:
  - name: alpha
    type: http
    target: http://10.0.0.1
    group: services
  - name: beta
    type: icmp
    target: 10.0.0.2
"""


def write_config(path: Path, text: str) -> None:
    """Rewrite the configuration file, as an operator editing it would."""
    path.write_text(text, encoding="utf-8")


def remove_config(path: Path) -> None:
    """Delete the configuration file, as a bad bind mount would."""
    path.unlink()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "monitors.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def reload_app(
    session: AsyncSession, database_url: str, config_path: Path, scheduler: FakeScheduler
) -> FastAPI:
    """Build an app whose config path is a file the test can rewrite."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, config_path=config_path
    )
    application = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override
    application.state.scheduler = scheduler
    return application


@pytest_asyncio.fixture
async def reload_client(reload_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=reload_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def make_result(
    session: AsyncSession,
    monitor: Monitor,
    *,
    at: datetime,
    success: bool = True,
    latency_ms: int | None = 12,
    error: str | None = None,
) -> None:
    await repo.record_check_result(
        session,
        monitor_id=monitor.id,
        checked_at=at,
        success=success,
        latency_ms=latency_ms,
        error=error,
    )


class TestMonitorDetail:
    async def test_returns_configuration_and_state(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web", status=MonitorStatus.DOWN)

        response = await client.get("/api/monitors/web")

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "web"
        assert body["target"] == "http://10.0.0.1"
        assert body["group"] == "services"
        assert body["active"] is True
        assert body["state"]["status"] == "down"

    async def test_includes_recent_results_newest_first(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_result(session, monitor, at=NOW - timedelta(minutes=5), latency_ms=10)
        await make_result(session, monitor, at=NOW - timedelta(minutes=1), latency_ms=20)

        response = await client.get("/api/monitors/web")

        results = response.json()["recent_results"]
        assert [result["latency_ms"] for result in results] == [20, 10]

    async def test_includes_recent_incidents(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW - timedelta(hours=1), cause="timeout"
        )

        response = await client.get("/api/monitors/web")

        incidents = response.json()["recent_incidents"]
        assert len(incidents) == 1
        assert incidents[0]["monitor"] == "web"
        assert incidents[0]["cause"] == "timeout"
        assert incidents[0]["resolved_at"] is None

    async def test_reports_uptime_over_24_hours(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_result(session, monitor, at=NOW - timedelta(minutes=1), success=True)
        await make_result(session, monitor, at=NOW - timedelta(minutes=2), success=False)

        response = await client.get("/api/monitors/web")

        assert response.json()["uptime_24h"] == 0.5

    async def test_uptime_is_null_without_results(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/api/monitors/web")

        assert response.json()["uptime_24h"] is None

    async def test_an_unknown_name_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/monitors/nope")

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]

    async def test_a_soft_deleted_monitor_keeps_its_history(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Removing a monitor from the YAML must not hide the history it already has."""
        monitor = await make_monitor(session, "web")
        monitor.active = False
        await session.flush()

        response = await client.get("/api/monitors/web")

        assert response.status_code == 200
        assert response.json()["active"] is False

    async def test_a_monitor_without_a_state_row_reads_as_unknown(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = Monitor(
            name="fresh",
            type=MonitorType.HTTP,
            target="http://10.0.0.5",
            group_name=None,
            description=None,
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

        response = await client.get("/api/monitors/fresh")

        assert response.status_code == 200
        assert response.json()["state"]["status"] == "unknown"


class TestMonitorResults:
    async def test_defaults_to_the_last_24_hours(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_result(session, monitor, at=NOW - timedelta(hours=1))
        await make_result(session, monitor, at=NOW - timedelta(hours=30))

        response = await client.get("/api/monitors/web/results")

        body = response.json()
        assert body["monitor"] == "web"
        assert body["hours"] == 24
        assert len(body["results"]) == 1

    async def test_a_wider_window_reaches_further_back(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_result(session, monitor, at=NOW - timedelta(hours=30))

        response = await client.get("/api/monitors/web/results", params={"hours": 48})

        assert len(response.json()["results"]) == 1

    async def test_results_carry_the_failure_detail(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_result(
            session,
            monitor,
            at=NOW - timedelta(minutes=1),
            success=False,
            latency_ms=None,
            error="connection refused",
        )

        response = await client.get("/api/monitors/web/results")

        result = response.json()["results"][0]
        assert result["success"] is False
        assert result["error"] == "connection refused"
        assert result["latency_ms"] is None

    @pytest.mark.parametrize("hours", [0, -1, 721])
    async def test_an_out_of_range_window_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession, hours: int
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/api/monitors/web/results", params={"hours": hours})

        assert response.status_code == 422

    async def test_an_unknown_name_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/monitors/nope/results")

        assert response.status_code == 404


class TestIncidents:
    async def test_empty_instance(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/incidents")

        assert response.status_code == 200
        assert response.json() == {"incidents": []}

    async def test_returns_incidents_newest_first_with_monitor_names(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        one = await make_monitor(session, "one")
        two = await make_monitor(session, "two")
        await repo.open_incident(
            session, monitor_id=one.id, started_at=NOW - timedelta(hours=2), cause="older"
        )
        await repo.open_incident(
            session, monitor_id=two.id, started_at=NOW - timedelta(hours=1), cause="newer"
        )

        response = await client.get("/api/incidents")

        incidents = response.json()["incidents"]
        assert [incident["monitor"] for incident in incidents] == ["two", "one"]
        assert incidents[0]["cause"] == "newer"

    async def test_resolved_incidents_carry_their_duration(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW - timedelta(hours=1), cause="timeout"
        )
        await repo.resolve_incident(session, incident, NOW - timedelta(minutes=54))
        await session.flush()

        response = await client.get("/api/incidents")

        assert response.json()["incidents"][0]["duration_seconds"] == 360

    async def test_open_filters_out_resolved_ones(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        one = await make_monitor(session, "one")
        two = await make_monitor(session, "two")
        resolved = await repo.open_incident(
            session, monitor_id=one.id, started_at=NOW - timedelta(hours=2), cause="over"
        )
        await repo.resolve_incident(session, resolved, NOW - timedelta(hours=1))
        await repo.open_incident(
            session, monitor_id=two.id, started_at=NOW - timedelta(minutes=5), cause="ongoing"
        )
        await session.flush()

        response = await client.get("/api/incidents", params={"open": "true"})

        incidents = response.json()["incidents"]
        assert [incident["cause"] for incident in incidents] == ["ongoing"]

    async def test_limit_caps_the_history(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        for minutes in range(3):
            incident = await repo.open_incident(
                session,
                monitor_id=monitor.id,
                started_at=NOW - timedelta(hours=minutes + 1),
                cause=f"incident {minutes}",
            )
            await repo.resolve_incident(session, incident, NOW - timedelta(minutes=minutes))
            await session.flush()

        response = await client.get("/api/incidents", params={"limit": 2})

        assert len(response.json()["incidents"]) == 2

    @pytest.mark.parametrize("limit", [0, 501])
    async def test_an_out_of_range_limit_is_rejected(
        self, client: httpx.AsyncClient, limit: int
    ) -> None:
        response = await client.get("/api/incidents", params={"limit": limit})

        assert response.status_code == 422


class TestReload:
    async def test_applies_the_file_and_resyncs_the_scheduler(
        self, reload_client: httpx.AsyncClient, scheduler: FakeScheduler
    ) -> None:
        response = await reload_client.post("/api/reload")

        assert response.status_code == 200
        body = response.json()
        assert body["changed"] is True
        assert body["inserted"] == ["alpha"]
        assert scheduler.syncs == 1

    async def test_a_second_reload_changes_nothing(self, reload_client: httpx.AsyncClient) -> None:
        await reload_client.post("/api/reload")

        response = await reload_client.post("/api/reload")

        body = response.json()
        assert body["changed"] is False
        assert body["unchanged"] == ["alpha"]

    async def test_picks_up_a_monitor_added_to_the_file(
        self, reload_client: httpx.AsyncClient, config_path: Path
    ) -> None:
        await reload_client.post("/api/reload")
        write_config(config_path, CHANGED_CONFIG)

        response = await reload_client.post("/api/reload")

        assert response.json()["inserted"] == ["beta"]

    async def test_a_monitor_removed_from_the_file_is_deactivated(
        self, reload_client: httpx.AsyncClient, config_path: Path
    ) -> None:
        write_config(config_path, CHANGED_CONFIG)
        await reload_client.post("/api/reload")
        write_config(config_path, CONFIG)

        response = await reload_client.post("/api/reload")

        assert response.json()["deactivated"] == ["beta"]

    async def test_an_invalid_file_is_rejected_without_changing_anything(
        self, reload_client: httpx.AsyncClient, config_path: Path, session: AsyncSession
    ) -> None:
        """A broken config must never take down monitoring that is currently working."""
        await reload_client.post("/api/reload")
        write_config(config_path, "monitors:\n  - name: alpha\n    intervall: 60\n")

        response = await reload_client.post("/api/reload")

        assert response.status_code == 422
        assert "intervall" in response.json()["detail"]
        assert [monitor.name for monitor in await repo.list_monitors(session)] == ["alpha"]

    async def test_a_missing_file_is_rejected(
        self, reload_client: httpx.AsyncClient, config_path: Path
    ) -> None:
        remove_config(config_path)

        response = await reload_client.post("/api/reload")

        assert response.status_code == 422
        assert "not found" in response.json()["detail"]

    async def test_a_rejected_reload_does_not_resync(
        self, reload_client: httpx.AsyncClient, config_path: Path, scheduler: FakeScheduler
    ) -> None:
        write_config(config_path, "not: a monitors file\n")

        await reload_client.post("/api/reload")

        assert scheduler.syncs == 0


class TestReadyz:
    async def test_ready_when_the_database_and_scheduler_are_up(
        self, reload_client: httpx.AsyncClient
    ) -> None:
        response = await reload_client.get("/readyz")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": True, "scheduler": True}

    async def test_not_ready_before_the_scheduler_starts(
        self, reload_app: FastAPI, reload_client: httpx.AsyncClient
    ) -> None:
        reload_app.state.scheduler = FakeScheduler(started=False)

        response = await reload_client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["scheduler"] is False

    async def test_not_ready_without_a_scheduler_at_all(self, client: httpx.AsyncClient) -> None:
        """The app fixture never ran the lifespan, so nothing put a scheduler on the state."""
        response = await client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["scheduler"] is False

    async def test_not_ready_when_the_database_is_unreachable(
        self, reload_app: FastAPI, reload_client: httpx.AsyncClient
    ) -> None:
        async def broken() -> AsyncIterator[UnreachableSession]:
            yield UnreachableSession()

        reload_app.dependency_overrides[get_session] = broken

        response = await reload_client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["database"] is False

    async def test_is_documented(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/openapi.json")

        assert "/readyz" in response.json()["paths"]
