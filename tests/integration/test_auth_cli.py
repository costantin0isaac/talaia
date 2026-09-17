"""The user-management CLI against a real database."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.conftest import TEST_PASSWORD, TEST_PASSWORD_HASH, login

from talaia.auth import __main__ as cli
from talaia.auth.passwords import verify_password
from talaia.auth.tokens import hash_token
from talaia.db import repository as repo
from talaia.settings import Settings

pytestmark = pytest.mark.integration

NEW_PASSWORD = "a-different-long-password"


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Feed scripted answers to the masked password prompt."""
    answers: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        return answers.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    yield answers


class TestAdd:
    async def test_creates_a_user_with_a_hashed_password(
        self, session: AsyncSession, typed: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        typed += [TEST_PASSWORD, TEST_PASSWORD]

        assert await cli.add(session, "isaac") == 0

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert user.active is True
        assert TEST_PASSWORD not in user.password_hash
        assert verify_password(user.password_hash, TEST_PASSWORD)
        assert "created isaac" in capsys.readouterr().out

    async def test_refuses_a_duplicate(
        self, session: AsyncSession, typed: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        await repo.create_user(session, username="isaac", password_hash=TEST_PASSWORD_HASH)

        assert await cli.add(session, "isaac") == 1
        assert "already exists" in capsys.readouterr().err
        assert typed == []  # never even asked for a password

    async def test_a_mismatched_password_creates_nothing(
        self, session: AsyncSession, typed: list[str]
    ) -> None:
        typed += [TEST_PASSWORD, "something else entirely"]

        assert await cli.add(session, "isaac") == 1
        assert await repo.get_user_by_name(session, "isaac") is None

    async def test_a_weak_password_creates_nothing(
        self, session: AsyncSession, typed: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        typed += ["short", "short"]

        assert await cli.add(session, "isaac") == 1
        assert await repo.get_user_by_name(session, "isaac") is None
        assert "at least 12" in capsys.readouterr().err


class TestList:
    async def test_says_how_to_fix_an_empty_instance(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.show(session, "") == 0
        assert "python -m talaia.auth add" in capsys.readouterr().out

    async def test_shows_state_and_last_login(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fresh = await repo.create_user(session, username="fresh", password_hash=TEST_PASSWORD_HASH)
        seen = await repo.create_user(session, username="seen", password_hash=TEST_PASSWORD_HASH)
        seen.last_login_at = datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
        fresh.active = False
        await session.flush()

        assert await cli.show(session, "") == 0

        out = capsys.readouterr().out
        assert "fresh" in out and "disabled" in out and "never" in out
        assert "seen" in out and "active" in out and "2026-03-14 09:30:00 UTC" in out


class TestPasswd:
    async def test_an_unknown_user_is_an_error(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.passwd(session, "ghost") == 1
        assert "no user 'ghost'" in capsys.readouterr().err

    async def test_changes_the_password_and_ends_every_session(
        self, session: AsyncSession, typed: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        token = await login(session, username="isaac")
        typed += [NEW_PASSWORD, NEW_PASSWORD]

        assert await cli.passwd(session, "isaac") == 0

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert verify_password(user.password_hash, NEW_PASSWORD)
        assert not verify_password(user.password_hash, TEST_PASSWORD)
        assert (
            await repo.get_session_user(session, hash_token(token), now=datetime.now(UTC)) is None
        )
        assert "1 session(s) ended" in capsys.readouterr().out

    async def test_a_mismatch_leaves_the_old_password_working(
        self, session: AsyncSession, typed: list[str]
    ) -> None:
        await repo.create_user(session, username="isaac", password_hash=TEST_PASSWORD_HASH)
        typed += [NEW_PASSWORD, "not the same"]

        assert await cli.passwd(session, "isaac") == 1

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert verify_password(user.password_hash, TEST_PASSWORD)

    async def test_a_weak_replacement_is_refused(
        self, session: AsyncSession, typed: list[str]
    ) -> None:
        await repo.create_user(session, username="isaac", password_hash=TEST_PASSWORD_HASH)
        typed += ["short", "short"]

        assert await cli.passwd(session, "isaac") == 1

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert verify_password(user.password_hash, TEST_PASSWORD)


class TestDisableEnable:
    async def test_disabling_ends_sessions_and_blocks_login(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str]
    ) -> None:
        token = await login(session, username="isaac")

        assert await cli.disable(session, "isaac") == 0

        user = await repo.get_user_by_name(session, "isaac")
        assert user is not None
        assert user.active is False
        assert (
            await repo.get_session_user(session, hash_token(token), now=datetime.now(UTC)) is None
        )
        assert "now disabled; 1 session(s) ended" in capsys.readouterr().out

    async def test_enabling_restores_the_account(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = await repo.create_user(session, username="isaac", password_hash=TEST_PASSWORD_HASH)
        user.active = False
        await session.flush()

        assert await cli.enable(session, "isaac") == 0

        assert user.active is True
        assert "now active; 0 session(s) ended" in capsys.readouterr().out

    @pytest.mark.parametrize("command", [cli.disable, cli.enable])
    async def test_an_unknown_user_is_an_error(
        self, session: AsyncSession, capsys: pytest.CaptureFixture[str], command: object
    ) -> None:
        assert await command(session, "ghost") == 1  # type: ignore[operator]
        assert "no user 'ghost'" in capsys.readouterr().err


class TestRun:
    async def test_opens_its_own_connection_from_settings(
        self,
        database_url: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The command path the container actually uses: settings -> engine -> session."""
        settings = Settings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]
        monkeypatch.setattr(cli, "get_settings", lambda: settings)

        assert await cli.run("list", "") == 0
        assert "no users" in capsys.readouterr().out
