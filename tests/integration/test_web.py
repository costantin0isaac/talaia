"""The dashboard and detail pages, rendered against a real database."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.conftest import COOKIE_NAME, login

from talaia.api.app import create_app
from talaia.api.dependencies import get_session
from talaia.config.schema import MonitorType
from talaia.db import repository as repo
from talaia.db.models import DailyUptime, Monitor, MonitorStatus
from talaia.settings import Settings

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


@pytest.fixture
def app(session: AsyncSession, database_url: str) -> FastAPI:
    settings = Settings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]
    application = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Build a client carrying a valid session cookie; every guarded route needs one."""
    token = await login(session)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={COOKIE_NAME: token}
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Build a client with no cookie, for checking that the guard actually guards."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def make_monitor(
    session: AsyncSession,
    name: str,
    *,
    status: MonitorStatus = MonitorStatus.UP,
    group: str | None = "services",
    latency_ms: int | None = 20,
    last_error: str | None = None,
    monitor_type: MonitorType = MonitorType.HTTP,
    expires_in_days: int | None = None,
) -> Monitor:
    monitor = Monitor(
        name=name,
        type=monitor_type,
        target=f"http://10.0.0.1/{name}",
        group_name=group,
        description="an example monitor",
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
    state.last_checked_at = NOW
    state.last_latency_ms = latency_ms
    state.last_error = last_error
    state.last_expires_in_days = expires_in_days
    await session.flush()
    return monitor


async def make_results(session: AsyncSession, monitor: Monitor, pattern: str) -> None:
    """Record one result per character, 's'uccess or 'f'ailure, oldest first."""
    for index, character in enumerate(pattern):
        success = character == "s"
        await repo.record_check_result(
            session,
            monitor_id=monitor.id,
            checked_at=NOW - timedelta(minutes=len(pattern) - index),
            success=success,
            latency_ms=20 if success else None,
            error=None if success else "connection refused",
        )


class TestDashboard:
    async def test_renders_an_empty_instance(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "No monitors configured" in response.text

    async def test_shows_a_monitor_with_its_group_and_target(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web", group="services")

        response = await client.get("/")

        assert "web" in response.text
        assert "services" in response.text
        assert "http://10.0.0.1/web" in response.text

    async def test_groups_monitors(self, client: httpx.AsyncClient, session: AsyncSession) -> None:
        await make_monitor(session, "web", group="services")
        await make_monitor(session, "router", group="infra")

        response = await client.get("/")

        assert response.text.index("infra") < response.text.index("services")

    async def test_the_summary_bar_counts_by_status(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "up-one")
        await make_monitor(session, "down-one", status=MonitorStatus.DOWN)

        response = await client.get("/")

        assert 'id="summary"' in response.text
        assert "open incidents" in response.text

    async def test_each_row_polls_its_own_partial(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The page must not reload itself; every row refreshes independently."""
        await make_monitor(session, "web")

        response = await client.get("/")

        assert 'hx-get="/partials/monitors/web/row"' in response.text
        assert 'hx-trigger="every 15s"' in response.text
        assert 'hx-swap="outerHTML"' in response.text

    async def test_the_strip_shows_successes_and_failures(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ssfs")

        response = await client.get("/")

        assert "segment-ok" in response.text
        assert "segment-fail" in response.text

    async def test_inactive_monitors_are_not_shown(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "gone")
        monitor.active = False
        await session.flush()

        response = await client.get("/")

        assert "gone" not in response.text

    async def test_the_page_links_its_own_assets(self, client: httpx.AsyncClient) -> None:
        """No CDN: the dashboard has to work on a network with no way out."""
        response = await client.get("/")

        assert "/static/style.css" in response.text
        assert "/static/htmx.min.js" in response.text
        assert "http://cdn" not in response.text


class TestPartials:
    async def test_the_row_partial_renders_one_row(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "sss")

        response = await client.get("/partials/monitors/web/row")

        assert response.status_code == 200
        assert response.text.strip().startswith("<tr")
        assert 'id="monitor-web"' in response.text

    async def test_the_row_partial_keeps_polling_itself(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A swapped-in row without the trigger would silently stop updating."""
        await make_monitor(session, "web")

        response = await client.get("/partials/monitors/web/row")

        assert 'hx-trigger="every 15s"' in response.text

    async def test_the_row_partial_is_a_404_for_an_unknown_monitor(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/partials/monitors/nope/row")

        assert response.status_code == 404

    async def test_the_summary_partial_keeps_polling_itself(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/partials/summary")

        assert response.status_code == 200
        assert 'hx-get="/partials/summary"' in response.text

    async def test_partials_are_not_in_the_openapi_schema(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/openapi.json")

        paths = response.json()["paths"]
        assert not [path for path in paths if path.startswith("/partials")]
        assert "/" not in paths


class TestMonitorDetail:
    async def test_renders_configuration_and_state(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert response.status_code == 200
        assert "an example monitor" in response.text
        assert "http://10.0.0.1/web" in response.text
        assert "Failure threshold" in response.text

    async def test_an_unknown_monitor_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/monitors/nope")

        assert response.status_code == 404

    async def test_shows_the_uptime_windows(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ssfs")
        session.add(
            DailyUptime(
                monitor_id=monitor.id,
                day=NOW.date(),
                total_checks=100,
                successful_checks=99,
                avg_latency_ms=20,
            )
        )
        await session.flush()

        response = await client.get("/monitors/web")

        assert "24 hours" in response.text
        assert "7 days" in response.text
        assert "30 days" in response.text
        assert "99.00%" in response.text

    async def test_draws_a_latency_chart(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ssss")

        response = await client.get("/monitors/web")

        assert "<svg" in response.text
        assert "chart-line" in response.text

    async def test_says_so_when_there_is_nothing_to_chart(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert "No checks recorded in this window yet." in response.text

    async def test_lists_incidents(self, client: httpx.AsyncClient, session: AsyncSession) -> None:
        monitor = await make_monitor(session, "web")
        incident = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW - timedelta(hours=1), cause="timeout"
        )
        await repo.resolve_incident(session, incident, NOW - timedelta(minutes=54))
        await session.flush()

        response = await client.get("/monitors/web")

        assert "timeout" in response.text
        assert "6m" in response.text

    async def test_an_open_incident_is_marked_ongoing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web", status=MonitorStatus.DOWN)
        await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW - timedelta(minutes=5), cause="refused"
        )
        await session.flush()

        response = await client.get("/monitors/web")

        assert "and counting" in response.text

    async def test_says_so_when_there_are_no_incidents(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert "No incidents recorded." in response.text

    async def test_a_soft_deleted_monitor_is_still_reachable(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "gone")
        monitor.active = False
        await session.flush()

        response = await client.get("/monitors/gone")

        assert response.status_code == 200
        assert "removed from YAML" in response.text

    async def test_the_last_error_is_shown(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(
            session, "web", status=MonitorStatus.DOWN, latency_ms=None, last_error="refused"
        )

        response = await client.get("/monitors/web")

        assert "refused" in response.text


class TestStaticFiles:
    async def test_htmx_is_vendored(self, client: httpx.AsyncClient) -> None:
        """No CDN dependency: the file has to be served by this application."""
        response = await client.get("/static/htmx.min.js")

        assert response.status_code == 200
        assert "htmx" in response.text[:200]

    async def test_the_stylesheet_is_served(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/static/style.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    @pytest.mark.parametrize(
        "path",
        [
            "/static/favicon.svg",
            "/static/favicon-16.png",
            "/static/favicon-32.png",
            "/static/favicon-48.png",
            "/static/apple-touch-icon.png",
            "/static/mark.svg",
        ],
    )
    async def test_the_brand_assets_are_served(self, client: httpx.AsyncClient, path: str) -> None:
        response = await client.get(path)

        assert response.status_code == 200

    async def test_the_favicon_adapts_to_the_browser_theme(self, client: httpx.AsyncClient) -> None:
        """A bare currentColor renders black, which is invisible on a dark browser tab."""
        response = await client.get("/static/favicon.svg")

        assert "prefers-color-scheme: dark" in response.text

    async def test_pages_declare_a_favicon(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.get("/login")

        assert 'rel="icon"' in response.text
        assert "/static/favicon.svg" in response.text


class TestNotFoundPage:
    async def test_a_missing_page_renders_as_html(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/monitors/nope")

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert "Back to the dashboard" in response.text

    async def test_an_unrouted_url_renders_as_html(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/nothing-here")

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]

    async def test_the_api_still_answers_in_json(self, client: httpx.AsyncClient) -> None:
        """A UI nicety must not change the shape of an API error."""
        response = await client.get("/api/monitors/nope")

        assert response.status_code == 404
        assert response.json()["detail"] == "no monitor named 'nope'"

    async def test_partials_stay_json(self, client: httpx.AsyncClient) -> None:
        """HTMX ignores a non-2xx body, so a whole page here would only be wasted bytes."""
        response = await client.get("/partials/monitors/nope/row")

        assert response.status_code == 404
        assert "application/json" in response.headers["content-type"]


class TestAuthenticatedChrome:
    async def test_the_masthead_shows_who_is_signed_in(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")

        assert "tester" in response.text
        assert "Sign out" in response.text

    async def test_the_login_page_has_no_sign_out(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.get("/login")

        assert response.status_code == 200
        assert "Sign out" not in response.text
        assert 'name="password"' in response.text

    async def test_the_detail_page_shows_the_user_too(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert "Sign out" in response.text


class TestCertificateDisplay:
    async def test_the_detail_page_shows_remaining_validity(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "cert", monitor_type=MonitorType.TLS, expires_in_days=45)

        response = await client.get("/monitors/cert")

        assert "expires in 45d" in response.text
        assert "certificate" in response.text

    async def test_an_expired_certificate_reads_as_past(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(
            session,
            "cert",
            monitor_type=MonitorType.TLS,
            status=MonitorStatus.DOWN,
            expires_in_days=-2,
        )

        response = await client.get("/monitors/cert")

        assert "expired 2d ago" in response.text

    async def test_the_dashboard_row_carries_it_too(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "cert", monitor_type=MonitorType.TLS, expires_in_days=45)

        response = await client.get("/")

        assert "expires in 45d" in response.text

    async def test_monitors_without_a_certificate_show_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert "expires in" not in response.text


class TestTheme:
    async def test_the_page_does_not_hardcode_a_theme(self, anonymous: httpx.AsyncClient) -> None:
        """With no attribute, the operating system preference decides."""
        response = await anonymous.get("/login")

        assert '<html lang="en">' in response.text

    async def test_the_toggle_is_offered(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.get("/login")

        assert 'id="theme-toggle"' in response.text
        assert 'aria-label="Switch between the light and dark theme"' in response.text

    async def test_the_choice_is_applied_before_first_paint(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """Applying it later would flash the wrong theme on every navigation."""
        response = await anonymous.get("/login")

        head = response.text[: response.text.index("</head>")]
        assert "talaia-theme" in head

    async def test_both_palettes_are_defined(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/static/style.css")

        assert ":root {" in response.text
        assert "prefers-color-scheme: dark" in response.text
        assert ':root[data-theme="dark"]' in response.text

    async def test_no_colour_is_defined_only_inside_a_media_query(
        self, client: httpx.AsyncClient
    ) -> None:
        """Every token needs a value before any media query, or the light theme is bare."""
        text = (await client.get("/static/style.css")).text
        base = text[text.index(":root {") : text.index("@media")]

        for token in ("--bg", "--text", "--accent", "--up", "--down", "--paused", "--unknown"):
            assert f"{token}:" in base, f"{token} has no light-theme value"


class TestGrafanaLink:
    async def test_it_is_hidden_when_unset(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")

        assert ">Grafana<" not in response.text

    async def test_it_appears_when_configured(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=app.state.settings.database_url,
            grafana_url="https://grafana.example.org",
        )

        response = await client.get("/")

        assert 'href="https://grafana.example.org"' in response.text
        assert ">Grafana<" in response.text


class TestChartAxes:
    async def test_the_axes_are_labelled(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ssss")

        response = await client.get("/monitors/web")

        assert "chart-tick-y" in response.text
        assert "chart-tick-x" in response.text
        assert ">ms<" in response.text
        assert ">UTC<" in response.text

    async def test_the_accessible_name_says_what_is_plotted(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ss")

        response = await client.get("/monitors/web")

        assert "latency in milliseconds over the last 24 hours" in response.text


class TestWordmark:
    @pytest.mark.parametrize("path", ["/", "/login"])
    async def test_the_tab_title_is_lowercase(self, client: httpx.AsyncClient, path: str) -> None:
        response = await client.get(path)

        assert "<title>" in response.text
        title = response.text.split("<title>")[1].split("</title>")[0]
        assert "talaia" in title
        assert "Talaia" not in title


class TestStaticCaching:
    async def test_static_files_say_how_long_to_cache(self, client: httpx.AsyncClient) -> None:
        """Without this, browsers guess and an edited stylesheet takes hours to appear."""
        response = await client.get("/static/style.css")

        assert "max-age=31536000" in response.headers["cache-control"]
        assert "immutable" in response.headers["cache-control"]

    async def test_asset_urls_carry_a_version_stamp(self, anonymous: httpx.AsyncClient) -> None:
        """A year-long cache is only safe because a changed file is a different URL."""
        response = await anonymous.get("/login")

        assert re.search(r"/static/style\.css\?v=\d+", response.text)
        assert re.search(r"/static/htmx\.min\.js\?v=\d+", response.text)

    async def test_the_stamp_follows_the_file(self, anonymous: httpx.AsyncClient) -> None:
        from talaia.web.templates_env import STATIC_DIR, static_url

        stamp = int((STATIC_DIR / "style.css").stat().st_mtime)

        assert static_url("style.css") == f"/static/style.css?v={stamp}"

    async def test_a_missing_file_still_produces_a_url(self) -> None:
        from talaia.web.templates_env import static_url

        assert static_url("nope.css") == "/static/nope.css"


class TestDashboardFilter:
    async def test_the_filter_buttons_are_offered(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/")

        for value in ("all", "down", "unknown", "paused", "up"):
            assert f'data-filter-value="{value}"' in response.text

    async def test_rows_carry_their_status(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Filtering is done in CSS against this attribute, so HTMX swaps obey it."""
        await make_monitor(session, "web", status=MonitorStatus.DOWN)

        response = await client.get("/")

        assert 'data-status="down"' in response.text

    async def test_the_row_partial_carries_it_too(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web", status=MonitorStatus.DOWN)

        response = await client.get("/partials/monitors/web/row")

        assert 'data-status="down"' in response.text

    async def test_there_is_an_empty_state(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")

        assert "Nothing matches that filter." in response.text


class TestGroupSummary:
    async def test_the_header_counts_its_monitors(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "one", group="infra")
        await make_monitor(session, "two", group="infra", status=MonitorStatus.DOWN)

        response = await client.get("/")

        assert "1 down · 1 up" in response.text


class TestFreshness:
    async def test_the_summary_reports_when_it_last_updated(
        self, client: httpx.AsyncClient
    ) -> None:
        """A dashboard whose polling has stopped otherwise looks perfectly healthy."""
        response = await client.get("/")

        assert 'class="stat-value freshness"' in response.text
        assert re.search(r'data-updated="\d{10}"', response.text)

    async def test_the_partial_refreshes_it(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/partials/summary")

        assert "data-updated=" in response.text


class TestChartWindow:
    async def test_the_default_window_is_24_hours(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web")

        assert "Latency, last 24h" in response.text

    async def test_the_window_can_be_changed(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web", params={"hours": 168})

        assert "Latency, last 168h" in response.text
        assert 'aria-current="page"' in response.text

    async def test_the_window_bounds_a_query(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await repo.record_check_result(
            session,
            monitor_id=monitor.id,
            checked_at=NOW - timedelta(hours=40),
            success=True,
            latency_ms=20,
        )

        narrow = await client.get("/monitors/web", params={"hours": 1})
        wide = await client.get("/monitors/web", params={"hours": 168})

        assert "No checks recorded in this window yet." in narrow.text
        assert "<svg" in wide.text

    @pytest.mark.parametrize("hours", [0, -1, 721])
    async def test_an_out_of_range_window_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession, hours: int
    ) -> None:
        await make_monitor(session, "web")

        response = await client.get("/monitors/web", params={"hours": hours})

        assert response.status_code == 422


class TestStripSpan:
    async def test_the_strip_says_what_it_covers(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Forty segments say nothing about whether they span forty minutes or hours."""
        monitor = await make_monitor(session, "web")
        await make_results(session, monitor, "ssss")

        response = await client.get("/monitors/web")

        assert "strip-span" in response.text
        assert "→" in response.text


class TestIncidentsPage:
    async def test_it_lists_incidents_across_monitors(
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
        await session.flush()

        response = await client.get("/incidents")

        assert response.status_code == 200
        assert response.text.index("newer") < response.text.index("older")

    async def test_open_only_filters(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        resolved = await repo.open_incident(
            session, monitor_id=monitor.id, started_at=NOW - timedelta(hours=3), cause="over"
        )
        await repo.resolve_incident(session, resolved, NOW - timedelta(hours=2))
        await session.flush()

        response = await client.get("/incidents", params={"open": "true"})

        assert "over" not in response.text
        assert "Nothing is down right now." in response.text

    async def test_it_links_to_each_monitor(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        monitor = await make_monitor(session, "web")
        await repo.open_incident(session, monitor_id=monitor.id, started_at=NOW, cause="timeout")
        await session.flush()

        response = await client.get("/incidents")

        assert 'href="/monitors/web"' in response.text

    async def test_it_is_in_the_masthead(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")

        assert 'href="/incidents"' in response.text

    async def test_it_needs_a_session(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.get("/incidents")

        assert response.status_code == 303


class TestStatusShape:
    async def test_status_is_not_carried_by_colour_alone(self, client: httpx.AsyncClient) -> None:
        """Roughly one man in twelve cannot separate the green from the red."""
        response = await client.get("/static/style.css")

        assert "border-radius: 2px" in response.text  # down is a square
        assert "rotate(45deg)" in response.text  # paused is a diamond
        assert "border: 2px solid var(--unknown)" in response.text  # unknown is hollow
