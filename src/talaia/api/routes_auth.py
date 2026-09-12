"""Login and logout.

These routes are the only ones outside the guard besides the probes, so everything here
assumes an anonymous caller.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from talaia.api.dependencies import get_session
from talaia.auth.passwords import hash_password, needs_rehash, verify_password
from talaia.auth.tokens import hash_token, new_token
from talaia.db import repository as repo
from talaia.logging import get_logger
from talaia.settings import Settings
from talaia.web.templates_env import templates

log = get_logger(__name__)

router = APIRouter(tags=["auth"], include_in_schema=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Verifying this on an unknown username costs the same as verifying a real one, so the
# response time does not reveal which usernames exist.
DUMMY_HASH = hash_password("a password that is never anyone's")

INVALID_CREDENTIALS = "Wrong username or password."


def safe_next(target: str | None) -> str:
    """Return a local redirect target, refusing anything that leaves this site."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request, next_path: Annotated[str, Query(alias="next")] = "/"
) -> HTMLResponse:
    """Render the login form."""
    return templates.TemplateResponse(
        request, "login.html", {"next": safe_next(next_path), "error": None}
    )


@router.post("/login")
async def login(
    request: Request,
    session: SessionDep,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_path: Annotated[str, Form(alias="next")] = "/",
) -> Response:
    """Check credentials and, if they are good, start a session."""
    settings: Settings = request.app.state.settings
    destination = safe_next(next_path)

    user = await repo.get_user_by_name(session, username)
    stored_hash = user.password_hash if user is not None else DUMMY_HASH
    matched = verify_password(stored_hash, password)

    if user is None or not user.active or not matched:
        log.warning("failed login", username=username)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": destination, "error": INVALID_CREDENTIALS},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    now = datetime.now(UTC)
    token = new_token()
    await repo.create_session(
        session,
        token_hash=hash_token(token),
        user_id=user.id,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    await session.commit()

    log.info("login", username=user.username)
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token, settings=settings)
    return response


@router.post("/logout")
async def logout(request: Request, session: SessionDep) -> Response:
    """End the current session and clear the cookie."""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await repo.delete_session(session, hash_token(token))
        await session.commit()

    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return response


def set_session_cookie(response: Response, token: str, *, settings: Settings) -> None:
    """Attach the session cookie.

    ``HttpOnly`` keeps it away from scripts, ``SameSite=Lax`` stops another site posting
    with it, and ``Secure`` withholds it from plain HTTP — which is why it has to be
    switched off for LAN access over http://.
    """
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
