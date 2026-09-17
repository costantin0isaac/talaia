"""Logging in, staying in, being kept out, and logging out."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.conftest import COOKIE_NAME, TEST_PASSWORD, TEST_PASSWORD_HASH, login

from talaia.api.app import create_app
from talaia.api.dependencies import get_session
from talaia.auth.passwords import hash_password
from talaia.auth.throttle import LoginThrottle
from talaia.auth.tokens import hash_token, new_token
from talaia.db import repository as repo
from talaia.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def app(session: AsyncSession, database_url: str) -> FastAPI:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, session_cookie_secure=False
    )
    application = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override
    return application


@pytest_asyncio.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def carrying(client: httpx.AsyncClient, token: str) -> httpx.AsyncClient:
    """Attach a session cookie to the client; httpx deprecates per-request cookies."""
    client.cookies.set(COOKIE_NAME, token)
    return client


async def make_user(session: AsyncSession, username: str = "isaac", *, active: bool = True) -> None:
    user = await repo.create_user(session, username=username, password_hash=TEST_PASSWORD_HASH)
    user.active = active
    await session.flush()


class TestGuard:
    @pytest.mark.parametrize("path", ["/", "/monitors/web"])
    async def test_pages_redirect_to_the_login_form(
        self, anonymous: httpx.AsyncClient, path: str
    ) -> None:
        response = await anonymous.get(path)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")

    async def test_the_destination_is_remembered(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.get("/monitors/web")

        assert "next=/monitors/web" in response.headers["location"]

    async def test_the_query_string_is_remembered_too(self, anonymous: httpx.AsyncClient) -> None:
        """Signing in from a 7-day chart must land back on the 7-day chart."""
        response = await anonymous.get("/monitors/web?hours=168")

        assert "next=/monitors/web%3Fhours%3D168" in response.headers["location"]

    async def test_a_remembered_query_string_survives_the_round_trip(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        response = await anonymous.post(
            "/login",
            data={
                "username": "isaac",
                "password": TEST_PASSWORD,
                "next": "/monitors/web?hours=168",
            },
        )

        assert response.headers["location"] == "/monitors/web?hours=168"

    @pytest.mark.parametrize("path", ["/api/monitors", "/api/summary", "/api/incidents"])
    async def test_the_api_answers_401_in_json(
        self, anonymous: httpx.AsyncClient, path: str
    ) -> None:
        response = await anonymous.get(path)

        assert response.status_code == 401
        assert response.json()["detail"] == "authentication required"

    async def test_htmx_is_told_to_navigate(self, anonymous: httpx.AsyncClient) -> None:
        """A login form swapped into a table row would be invisible; htmx must redirect."""
        response = await anonymous.get("/partials/summary", headers={"HX-Request": "true"})

        assert response.status_code == 401
        assert response.headers["HX-Redirect"] == "/login"

    @pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics", "/login"])
    async def test_probes_and_login_stay_open(
        self, anonymous: httpx.AsyncClient, path: str
    ) -> None:
        """Prometheus and the orchestrator have no cookie and never will."""
        response = await anonymous.get(path)

        assert response.status_code in (200, 503)

    async def test_a_garbage_cookie_is_not_a_session(self, anonymous: httpx.AsyncClient) -> None:
        response = await carrying(anonymous, "made-up").get("/api/monitors")

        assert response.status_code == 401


class TestLogin:
    async def test_good_credentials_start_a_session(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": TEST_PASSWORD}
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert COOKIE_NAME in response.cookies

    async def test_the_cookie_is_hardened(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": TEST_PASSWORD}
        )

        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=lax" in header

    async def test_the_session_then_works(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)
        await anonymous.post("/login", data={"username": "isaac", "password": TEST_PASSWORD})

        response = await anonymous.get("/api/monitors")

        assert response.status_code == 200

    async def test_the_next_destination_is_honoured(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        response = await anonymous.post(
            "/login",
            data={"username": "isaac", "password": TEST_PASSWORD, "next": "/monitors/web"},
        )

        assert response.headers["location"] == "/monitors/web"

    @pytest.mark.parametrize(
        "target",
        [
            "http://evil.example",
            "//evil.example",
            "/\\evil.example",
            "/\\\\evil.example",
            "javascript:x",
        ],
    )
    async def test_an_offsite_next_is_refused(
        self, anonymous: httpx.AsyncClient, session: AsyncSession, target: str
    ) -> None:
        """An open redirect turns a login page into a phishing tool."""
        await make_user(session)

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": TEST_PASSWORD, "next": target}
        )

        assert response.headers["location"] == "/"

    async def test_a_wrong_password_is_refused(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": "not the password"}
        )

        assert response.status_code == 401
        assert COOKIE_NAME not in response.cookies

    async def test_an_unknown_user_is_refused(self, anonymous: httpx.AsyncClient) -> None:
        response = await anonymous.post(
            "/login", data={"username": "nobody", "password": TEST_PASSWORD}
        )

        assert response.status_code == 401

    async def test_the_message_does_not_say_which_half_was_wrong(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Distinguishing them would confirm which usernames exist."""
        await make_user(session)

        wrong_password = await anonymous.post(
            "/login", data={"username": "isaac", "password": "wrong"}
        )
        wrong_user = await anonymous.post(
            "/login", data={"username": "ghost", "password": TEST_PASSWORD}
        )

        assert "Wrong username or password." in wrong_password.text
        assert "Wrong username or password." in wrong_user.text

    async def test_a_disabled_user_cannot_log_in(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session, active=False)

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": TEST_PASSWORD}
        )

        assert response.status_code == 401

    async def test_the_login_is_recorded(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)

        await anonymous.post("/login", data={"username": "isaac", "password": TEST_PASSWORD})

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert user.last_login_at is not None


class TestSessionLifetime:
    async def test_an_expired_session_is_rejected(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        user = await repo.create_user(session, username="stale", password_hash=TEST_PASSWORD_HASH)
        token = new_token()
        await repo.create_session(
            session,
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.flush()

        response = await carrying(anonymous, token).get("/api/monitors")

        assert response.status_code == 401

    async def test_disabling_a_user_kills_their_live_session(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The flag is part of the lookup, so no cleanup step can be forgotten."""
        token = await login(session, username="soon-gone")
        client = carrying(anonymous, token)
        assert (await client.get("/api/monitors")).status_code == 200

        user = await repo.get_user_by_name(session, "soon-gone")
        assert user is not None
        user.active = False
        await session.flush()

        assert (await client.get("/api/monitors")).status_code == 401

    async def test_the_token_is_never_stored_in_the_clear(self, session: AsyncSession) -> None:
        """A dump of the sessions table must not be replayable."""
        token = await login(session, username="hashed")

        rows = await repo.get_session_user(session, hash_token(token), now=datetime.now(UTC))
        assert rows is not None

        direct = await repo.get_session_user(session, token, now=datetime.now(UTC))
        assert direct is None

    async def test_using_a_session_records_it(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        token = await login(session, username="seen")

        await carrying(anonymous, token).get("/api/monitors")

        row = await session.get(
            __import__("talaia.db.models", fromlist=["Session"]).Session, hash_token(token)
        )
        assert row is not None
        assert row.last_seen_at is not None


class TestLogout:
    async def test_logging_out_clears_the_cookie_and_the_row(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)
        await anonymous.post("/login", data={"username": "isaac", "password": TEST_PASSWORD})

        response = await anonymous.post("/logout")

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert (await anonymous.get("/api/monitors")).status_code == 401

    async def test_logging_out_without_a_session_is_harmless(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        response = await anonymous.post("/logout")

        assert response.status_code == 303


class TestPasswordRotation:
    async def test_changing_a_password_ends_every_session(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        token = await login(session, username="rotating")
        user = await repo.get_user_by_name(session, "rotating")
        assert user is not None

        ended = await repo.delete_sessions_for_user(session, user.id)
        user.password_hash = hash_password("a-brand-new-password")
        await session.flush()

        assert ended == 1
        assert (await carrying(anonymous, token).get("/api/monitors")).status_code == 401


class TestExpiredSessionPruning:
    async def test_only_expired_rows_are_removed(self, session: AsyncSession) -> None:
        live = await login(session, username="live")
        user = await repo.create_user(session, username="dead", password_hash=TEST_PASSWORD_HASH)
        await repo.create_session(
            session,
            token_hash=hash_token(new_token()),
            user_id=user.id,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await session.flush()

        removed = await repo.delete_expired_sessions(session, now=datetime.now(UTC))

        assert removed == 1
        assert await repo.get_session_user(session, hash_token(live), now=datetime.now(UTC))


class TestLoginRateLimit:
    async def test_attempts_are_refused_once_the_budget_is_spent(
        self, app: FastAPI, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)
        app.state.login_throttle = LoginThrottle(
            max_attempts=3, lockout_seconds=60, max_lockout_seconds=900
        )

        for _ in range(3):
            attempt = await anonymous.post(
                "/login", data={"username": "isaac", "password": "wrong"}
            )
            assert attempt.status_code == 401

        refused = await anonymous.post("/login", data={"username": "isaac", "password": "wrong"})

        assert refused.status_code == 429
        assert "Too many attempts" in refused.text
        assert int(refused.headers["Retry-After"]) > 0

    async def test_the_right_password_is_refused_too_while_locked_out(
        self, app: FastAPI, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Otherwise the limit would be trivially bypassed by guessing correctly."""
        await make_user(session)
        app.state.login_throttle = LoginThrottle(
            max_attempts=1, lockout_seconds=60, max_lockout_seconds=900
        )
        await anonymous.post("/login", data={"username": "isaac", "password": "wrong"})

        response = await anonymous.post(
            "/login", data={"username": "isaac", "password": TEST_PASSWORD}
        )

        assert response.status_code == 429
        assert COOKIE_NAME not in response.cookies

    async def test_a_success_clears_the_count(
        self, app: FastAPI, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await make_user(session)
        app.state.login_throttle = LoginThrottle(
            max_attempts=3, lockout_seconds=60, max_lockout_seconds=900
        )
        for _ in range(2):
            await anonymous.post("/login", data={"username": "isaac", "password": "wrong"})

        good = await anonymous.post("/login", data={"username": "isaac", "password": TEST_PASSWORD})
        assert good.status_code == 303

        for _ in range(2):
            again = await anonymous.post("/login", data={"username": "isaac", "password": "wrong"})
            assert again.status_code == 401

    async def test_an_unknown_username_still_counts(
        self, app: FastAPI, anonymous: httpx.AsyncClient
    ) -> None:
        """Otherwise the budget is bypassed by varying the username."""
        app.state.login_throttle = LoginThrottle(
            max_attempts=2, lockout_seconds=60, max_lockout_seconds=900
        )

        for name in ("alice", "bob"):
            await anonymous.post("/login", data={"username": name, "password": "wrong"})

        refused = await anonymous.post("/login", data={"username": "carol", "password": "wrong"})

        assert refused.status_code == 429

    async def test_the_default_budget_leaves_room_for_typos(
        self, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Five is generous for a human and still stops a guessing run."""
        await make_user(session)

        for _ in range(4):
            attempt = await anonymous.post(
                "/login", data={"username": "isaac", "password": "wrong"}
            )
            assert attempt.status_code == 401

        good = await anonymous.post("/login", data={"username": "isaac", "password": TEST_PASSWORD})

        assert good.status_code == 303


class TestApiToken:
    @staticmethod
    def with_token(app: FastAPI, token: str | None) -> None:
        app.state.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=app.state.settings.database_url,
            api_token=token,
            session_cookie_secure=False,
        )

    async def test_the_api_is_closed_when_no_token_is_set(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """An unset setting must not become an accidental way in."""
        response = await anonymous.get(
            "/api/monitors", headers={"Authorization": "Bearer anything"}
        )

        assert response.status_code == 401

    async def test_a_token_opens_the_api(self, app: FastAPI, anonymous: httpx.AsyncClient) -> None:
        self.with_token(app, "ci-token")

        response = await anonymous.get(
            "/api/monitors", headers={"Authorization": "Bearer ci-token"}
        )

        assert response.status_code == 200

    async def test_reload_is_reachable_with_a_token(
        self, app: FastAPI, anonymous: httpx.AsyncClient
    ) -> None:
        """The point of the whole thing: CI can apply a config change."""
        self.with_token(app, "ci-token")

        class FakeScheduler:
            running_monitors: frozenset[str] = frozenset()

            async def sync(self) -> None:
                return None

        app.state.scheduler = FakeScheduler()

        response = await anonymous.post("/api/reload", headers={"Authorization": "Bearer ci-token"})

        assert response.status_code == 200

    @pytest.mark.parametrize("header", ["Bearer wrong", "Basic ci-token", "ci-token"])
    async def test_a_wrong_token_is_refused(
        self, app: FastAPI, anonymous: httpx.AsyncClient, header: str
    ) -> None:
        self.with_token(app, "ci-token")

        response = await anonymous.get("/api/monitors", headers={"Authorization": header})

        assert response.status_code == 401

    async def test_the_token_does_not_open_the_pages(
        self, app: FastAPI, anonymous: httpx.AsyncClient
    ) -> None:
        """A token is for callers that read JSON; a page has nothing to say to one."""
        self.with_token(app, "ci-token")

        response = await anonymous.get("/", headers={"Authorization": "Bearer ci-token"})

        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    async def test_a_session_still_works_when_a_token_is_configured(
        self, app: FastAPI, anonymous: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        self.with_token(app, "ci-token")
        token = await login(session, username="person")

        response = await carrying(anonymous, token).get("/api/monitors")

        assert response.status_code == 200
