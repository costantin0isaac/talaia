"""HTML endpoints: the dashboard, the monitor detail page and their HTMX partials."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from talaia.api.dependencies import AuthenticatedUser, get_session
from talaia.db import repository as repo
from talaia.db.models import Monitor, MonitorStatus
from talaia.formatting import format_latency, format_percentage
from talaia.web import view
from talaia.web.templates_env import templates

router = APIRouter(tags=["web"], include_in_schema=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

UPTIME_WINDOW_HOURS = 24
DETAIL_INCIDENTS = 20
CHART_HOURS = 24
POLL_SECONDS = 15

# Offered on the detail page. The API already accepts 1-720; these are the useful ones.
CHART_WINDOWS = (1, 24, 168)
INCIDENT_PAGE_LIMIT = 100


def wants_html(request: Request) -> bool:
    """Whether a failure on this path should be rendered as a page rather than JSON.

    HTMX ignores the body of a non-2xx response, so partials are left as JSON too.
    """
    path = request.url.path
    return not path.startswith(("/api", "/partials", "/metrics", "/healthz", "/readyz"))


def unauthenticated(request: Request, exc: StarletteHTTPException) -> Response:
    """Turn a 401 into whatever the caller can act on.

    A browser is sent to the login form with its destination remembered. HTMX is told to
    navigate, because a partial swapped into a row could never show a login form. Anything
    else — curl, Prometheus, a script — gets the JSON it asked for.
    """
    if request.headers.get("HX-Request") == "true":
        response = Response(status_code=status.HTTP_401_UNAUTHORIZED)
        response.headers["HX-Redirect"] = "/login"
        return response

    if wants_html(request):
        destination = request.url.path
        if request.url.query:
            destination = f"{destination}?{request.url.query}"
        target = quote(destination, safe="/")
        return RedirectResponse(f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)

    return JSONResponse({"detail": exc.detail}, status_code=status.HTTP_401_UNAUTHORIZED)


async def not_found(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    """Render a 404 as a page for the UI, so a mistyped URL is not a JSON blob."""
    return templates.TemplateResponse(
        request,
        "not_found.html",
        {"detail": exc.detail},
        status_code=status.HTTP_404_NOT_FOUND,
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: SessionDep, user: AuthenticatedUser) -> HTMLResponse:
    """Render every active monitor, grouped, with its status strip."""
    groups, summary = await _dashboard_state(session)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"groups": groups, "summary": summary, "poll_seconds": POLL_SECONDS, "user": user},
    )


@router.get("/partials/summary", response_class=HTMLResponse)
async def summary_partial(request: Request, session: SessionDep) -> HTMLResponse:
    """Re-render the summary bar for HTMX."""
    _, summary = await _dashboard_state(session)
    return templates.TemplateResponse(
        request,
        "partials/summary.html",
        {"summary": summary, "poll_seconds": POLL_SECONDS},
    )


@router.get("/partials/monitors/{name}/row", response_class=HTMLResponse)
async def monitor_row_partial(name: str, request: Request, session: SessionDep) -> HTMLResponse:
    """Re-render one dashboard row for HTMX, which swaps it in place."""
    monitor = await _require_monitor(session, name)
    since = datetime.now(UTC) - timedelta(hours=UPTIME_WINDOW_HOURS)
    results = await repo.list_latest_results_by_monitor(session, [monitor.id])
    row = view.monitor_row(
        monitor,
        await repo.get_state(session, monitor.id),
        results=results.get(monitor.id, []),
        uptime_24h=await repo.uptime_ratio(session, monitor.id, since=since),
    )
    return templates.TemplateResponse(
        request,
        "partials/monitor_row.html",
        {"row": row, "poll_seconds": POLL_SECONDS},
    )


@router.get("/incidents", response_class=HTMLResponse)
async def incidents_page(
    request: Request,
    session: SessionDep,
    user: AuthenticatedUser,
    only_open: Annotated[bool, Query(alias="open")] = False,
) -> HTMLResponse:
    """Render incident history across every monitor."""
    rows = await repo.list_incidents_with_monitor(
        session, open_only=only_open, limit=INCIDENT_PAGE_LIMIT
    )
    now = datetime.now(UTC)
    incidents = [
        (name, row)
        for (incident, name), row in zip(
            rows, view.incident_rows([incident for incident, _ in rows], now=now), strict=True
        )
    ]
    return templates.TemplateResponse(
        request,
        "incidents.html",
        {"incidents": incidents, "only_open": only_open, "user": user},
    )


@router.get("/monitors/{name}", response_class=HTMLResponse)
async def monitor_detail(
    name: str,
    request: Request,
    session: SessionDep,
    user: AuthenticatedUser,
    hours: Annotated[int, Query(ge=1, le=720)] = CHART_HOURS,
) -> HTMLResponse:
    """Render one monitor's configuration, uptime, latency chart and incidents."""
    monitor = await _require_monitor(session, name)
    now = datetime.now(UTC)
    since = now - timedelta(hours=UPTIME_WINDOW_HOURS)
    today = now.date()

    strip = await repo.list_latest_results_by_monitor(session, [monitor.id])
    chart_results = await repo.list_check_results(
        session, monitor.id, since=now - timedelta(hours=hours)
    )
    incidents = await repo.list_incidents(session, monitor_id=monitor.id, limit=DETAIL_INCIDENTS)

    detail = view.MonitorDetailView(
        row=view.monitor_row(
            monitor,
            await repo.get_state(session, monitor.id),
            results=strip.get(monitor.id, []),
            uptime_24h=await repo.uptime_ratio(session, monitor.id, since=since),
        ),
        description=monitor.description,
        interval_seconds=monitor.interval_seconds,
        timeout_seconds=monitor.timeout_seconds,
        failure_threshold=monitor.failure_threshold,
        recovery_threshold=monitor.recovery_threshold,
        enabled=monitor.enabled,
        active=monitor.active,
        group=monitor.group_name,
        uptime_7d=format_percentage(
            await repo.uptime_since_day(session, monitor.id, since=today - timedelta(days=6))
        ),
        uptime_30d=format_percentage(
            await repo.uptime_since_day(session, monitor.id, since=today - timedelta(days=29))
        ),
        latency_7d=format_latency(
            await repo.average_latency_since_day(
                session, monitor.id, since=today - timedelta(days=6)
            )
        ),
        latency_30d=format_latency(
            await repo.average_latency_since_day(
                session, monitor.id, since=today - timedelta(days=29)
            )
        ),
        chart=view.latency_chart(chart_results),
        incidents=view.incident_rows(incidents, now=now),
    )

    return templates.TemplateResponse(
        request,
        "monitor_detail.html",
        {
            "detail": detail,
            "chart_hours": hours,
            "chart_windows": CHART_WINDOWS,
            "user": user,
        },
    )


async def _dashboard_state(session: AsyncSession) -> tuple[tuple[view.Group, ...], view.SummaryBar]:
    """Build the whole dashboard in a fixed number of queries, whatever the monitor count."""
    pairs = await repo.list_states(session)
    since = datetime.now(UTC) - timedelta(hours=UPTIME_WINDOW_HOURS)

    monitor_ids = [monitor.id for monitor, _ in pairs]
    strips = await repo.list_latest_results_by_monitor(session, monitor_ids)
    ratios = await repo.uptime_ratios(session, since=since)

    rows = [
        view.monitor_row(
            monitor,
            state,
            results=strips.get(monitor.id, []),
            uptime_24h=ratios.get(monitor.id),
        )
        for monitor, state in pairs
    ]
    groups = view.group_rows(rows, {monitor.name: monitor.group_name for monitor, _ in pairs})

    counts = await repo.count_by_status(session)
    summary = view.SummaryBar(
        total=sum(counts.values()),
        up=counts.get(MonitorStatus.UP, 0),
        down=counts.get(MonitorStatus.DOWN, 0),
        unknown=counts.get(MonitorStatus.UNKNOWN, 0),
        paused=counts.get(MonitorStatus.PAUSED, 0),
        open_incidents=await repo.count_open_incidents(session),
        uptime_24h=format_percentage(await repo.overall_uptime_ratio(session, since=since)),
        updated_at=datetime.now(UTC),
    )
    return groups, summary


async def _require_monitor(session: AsyncSession, name: str) -> Monitor:
    """Return the named monitor, or raise a 404 the error page can render."""
    monitor = await repo.get_monitor_by_name(session, name)
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no monitor named {name!r}"
        )
    return monitor
