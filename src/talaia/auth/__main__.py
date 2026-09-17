"""Command-line user management.

There is no sign-up page and no user administration in the UI, so this is how accounts
come into being:

    python -m talaia.auth add isaac
    python -m talaia.auth list
    python -m talaia.auth passwd isaac
    python -m talaia.auth disable isaac
    python -m talaia.auth sessions isaac
    python -m talaia.auth revoke isaac 3f9a1c
    python -m talaia.auth revoke isaac all
"""

import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from talaia.auth.passwords import WeakPasswordError, hash_password
from talaia.db import repository as repo
from talaia.db.engine import create_engine, create_session_factory
from talaia.formatting import format_timestamp
from talaia.settings import get_settings

USAGE = (
    "usage: python -m talaia.auth add|passwd|disable|enable <username>\n"
    "       python -m talaia.auth list\n"
    "       python -m talaia.auth sessions <username>\n"
    "       python -m talaia.auth revoke <username> <session-id>|all"
)

# Enough of the 64-character hash to be unambiguous in practice and short enough to type.
SESSION_ID_LENGTH = 12


def prompt_password() -> str | None:
    """Ask for a password twice, returning None if the two do not match."""
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("passwords do not match", file=sys.stderr)
        return None
    return first


async def add(session: AsyncSession, username: str) -> int:
    """Create a user."""
    if await repo.get_user_by_name(session, username) is not None:
        print(f"user {username!r} already exists", file=sys.stderr)
        return 1

    password = prompt_password()
    if password is None:
        return 1

    try:
        password_hash = hash_password(password)
    except WeakPasswordError as exc:
        print(exc, file=sys.stderr)
        return 1

    await repo.create_user(session, username=username, password_hash=password_hash)
    await session.commit()
    print(f"created {username}")
    return 0


async def show(session: AsyncSession) -> int:
    """List the users."""
    users = await repo.list_users(session)
    if not users:
        print("no users; create one with: python -m talaia.auth add <username>")
        return 0

    for user in users:
        state = "active" if user.active else "disabled"
        last = format_timestamp(user.last_login_at) if user.last_login_at else "never"
        print(f"{user.username:<20} {state:<9} last login: {last}")
    return 0


async def passwd(session: AsyncSession, username: str) -> int:
    """Change a password, ending every session that user had open."""
    user = await repo.get_user_by_name(session, username)
    if user is None:
        print(f"no user {username!r}", file=sys.stderr)
        return 1

    password = prompt_password()
    if password is None:
        return 1

    try:
        user.password_hash = hash_password(password)
    except WeakPasswordError as exc:
        print(exc, file=sys.stderr)
        return 1

    ended = await repo.delete_sessions_for_user(session, user.id)
    await session.commit()
    print(f"password changed for {username}; {ended} session(s) ended")
    return 0


async def set_active(session: AsyncSession, username: str, *, active: bool) -> int:
    """Enable or disable a user, ending their sessions when disabling."""
    user = await repo.get_user_by_name(session, username)
    if user is None:
        print(f"no user {username!r}", file=sys.stderr)
        return 1

    user.active = active
    ended = 0 if active else await repo.delete_sessions_for_user(session, user.id)
    await session.commit()
    print(f"{username} is now {'active' if active else 'disabled'}; {ended} session(s) ended")
    return 0


async def disable(session: AsyncSession, username: str) -> int:
    """Disable a user."""
    return await set_active(session, username, active=False)


async def enable(session: AsyncSession, username: str) -> int:
    """Enable a user."""
    return await set_active(session, username, active=True)


async def sessions(session: AsyncSession, username: str) -> int:
    """List a user's sessions, so a stray login can be found and revoked."""
    user = await repo.get_user_by_name(session, username)
    if user is None:
        print(f"no user {username!r}", file=sys.stderr)
        return 1

    rows = await repo.list_sessions_for_user(session, user.id)
    if not rows:
        print(f"{username} has no sessions")
        return 0

    now = datetime.now(UTC)
    print(f"{'id':<{SESSION_ID_LENGTH}} {'created':<23} {'last seen':<23} {'expires':<23} state")
    for row in rows:
        state = "expired" if row.expires_at <= now else "active"
        last_seen = format_timestamp(row.last_seen_at) if row.last_seen_at else "never"
        print(
            f"{row.token_hash[:SESSION_ID_LENGTH]} {format_timestamp(row.created_at):<23} "
            f"{last_seen:<23} {format_timestamp(row.expires_at):<23} {state}"
        )
    return 0


async def revoke(session: AsyncSession, username: str, session_id: str) -> int:
    """End one session by id prefix, or every session with ``all``."""
    user = await repo.get_user_by_name(session, username)
    if user is None:
        print(f"no user {username!r}", file=sys.stderr)
        return 1

    if session_id == "all":
        ended = await repo.delete_sessions_for_user(session, user.id)
        await session.commit()
        print(f"{ended} session(s) ended for {username}")
        return 0

    matches = await repo.find_sessions_by_prefix(session, user.id, session_id)
    if not matches:
        print(f"no session of {username!r} starts with {session_id!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(
            f"{session_id!r} matches {len(matches)} sessions; give more characters", file=sys.stderr
        )
        return 1

    await repo.delete_session(session, matches[0].token_hash)
    await session.commit()
    print(f"session {matches[0].token_hash[:SESSION_ID_LENGTH]} ended for {username}")
    return 0


Handler = Callable[..., Awaitable[int]]

# Each command with the number of arguments it takes after its own name.
COMMANDS: dict[str, tuple[Handler, int]] = {
    "add": (add, 1),
    "list": (show, 0),
    "passwd": (passwd, 1),
    "disable": (disable, 1),
    "enable": (enable, 1),
    "sessions": (sessions, 1),
    "revoke": (revoke, 2),
}


async def run(command: str, *args: str) -> int:
    """Open a database session and run one command in it."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            handler, _ = COMMANDS[command]
            return await handler(session, *args)
    finally:
        await engine.dispose()


def main(argv: list[str]) -> int:
    """Parse the arguments and run the command."""
    if not argv or argv[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2

    command, args = argv[0], argv[1:]
    _, arity = COMMANDS[command]
    if len(args) != arity:
        print(USAGE, file=sys.stderr)
        return 2

    return asyncio.run(run(command, *args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
