"""The one Jinja environment, shared by every module that renders a page."""

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def static_url(path: str) -> str:
    """Return a static path stamped with the file's modification time.

    Static responses are cached for a year, so the URL has to change when the file does.
    Deriving the stamp from mtime rather than the release version means an edit is visible
    on the next refresh in development too, which a version stamp would not give.
    """
    try:
        stamp = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={stamp}"


def site_context(request: Request) -> dict[str, Any]:
    """Values every page needs, so routes do not each have to pass them."""
    settings = getattr(request.app.state, "settings", None)
    return {
        "grafana_url": getattr(settings, "grafana_url", None),
        "static_url": static_url,
    }


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[site_context])
