"""Static file serving with an explicit caching policy.

Starlette sends ``ETag`` and ``Last-Modified`` but no ``Cache-Control``, which leaves
browsers to guess: they apply heuristic freshness, roughly a tenth of the file's age, and
stop revalidating. An edited stylesheet then takes hours to appear. Saying what we mean
instead removes the guesswork in both directions.
"""

from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# A year, because every URL carries a mtime stamp from ``static_url`` and so a changed
# file is a different URL. Without that stamp this would be far too long.
CACHE_SECONDS = 31_536_000


class CachedStaticFiles(StaticFiles):
    """Serve static files with a long, safe cache lifetime."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        """Attach the caching policy to whatever Starlette decided to send."""
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}, immutable"
        return response
