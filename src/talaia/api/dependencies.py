"""FastAPI dependencies."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talaia.auth.tokens import hash_token
from talaia.db import repository as repo
from talaia.db.models import User
from talaia.settings import Settings

# How stale last_seen_at may get before it is worth a write.
TOUCH_INTERVAL = timedelta(minutes=5)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session for the lifetime of one request."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, session: SessionDep) -> User | None:
    """Return whoever the session cookie identifies, or ``None`` for an anonymous caller."""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None

    now = datetime.now(UTC)
    token_hash = hash_token(token)
    user = await repo.get_session_user(session, token_hash, now=now)
    if user is not None:
        await repo.touch_session(session, token_hash, now=now, not_before=now - TOUCH_INTERVAL)

    # Authentication closes its own transaction. Reading opens one too, and a route that
    # then opens its own with ``session.begin()`` would fail on the second request of a
    # session -- which is how POST /api/reload broke when the guard was added.
    #
    # Deliberately a commit even when nothing was written: rollback would be one round
    # trip cheaper, but it also expires every object in the identity map, which turns a
    # later attribute read into a lazy load outside the async context.
    await session.commit()
    return user


CurrentUser = Annotated[User | None, Depends(current_user)]


async def require_user(user: CurrentUser) -> User:
    """Reject anonymous callers.

    Raising 401 rather than redirecting keeps this usable by both the API and the pages;
    the application turns it into a redirect for requests that wanted HTML.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    return user


AuthenticatedUser = Annotated[User, Depends(require_user)]
