"""Command-line user management.

There is no sign-up page and no user administration in the UI, so this is how accounts
come into being:

    python -m talaia.auth add isaac
    python -m talaia.auth list
    python -m talaia.auth passwd isaac
    python -m talaia.auth disable isaac
"""

import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from talaia.auth.passwords import WeakPasswordError, hash_password
from talaia.db import repository as repo
from talaia.db.engine import create_engine, create_session_factory
from talaia.formatting import format_timestamp
from talaia.settings import get_settings

USAGE = "usage: python -m talaia.auth {add|list|passwd|disable|enable} [username]"


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


async def show(session: AsyncSession, _: str) -> int:
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


COMMANDS: dict[str, tuple[Callable[[AsyncSession, str], Awaitable[int]], bool]] = {
    "add": (add, True),
    "list": (show, False),
    "passwd": (passwd, True),
    "disable": (disable, True),
    "enable": (enable, True),
}


async def run(command: str, username: str) -> int:
    """Open a database session and run one command in it."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            handler, _ = COMMANDS[command]
            return await handler(session, username)
    finally:
        await engine.dispose()


def main(argv: list[str]) -> int:
    """Parse the arguments and run the command."""
    if not argv or argv[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2

    command = argv[0]
    _, needs_username = COMMANDS[command]
    if needs_username and len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2

    return asyncio.run(run(command, argv[1] if len(argv) > 1 else ""))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
