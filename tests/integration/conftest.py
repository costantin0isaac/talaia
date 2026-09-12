from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from talaia.auth.passwords import hash_password
from talaia.auth.tokens import hash_token, new_token
from talaia.db import repository as repo
from talaia.db.engine import create_engine

pytestmark = pytest.mark.integration

TEST_PASSWORD = "a-long-enough-test-password"

# Hashed once: argon2 is deliberately slow, and every authenticated test would pay for it.
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)

COOKIE_NAME = "talaia_session"


async def login(session: AsyncSession, *, username: str = "tester", active: bool = True) -> str:
    """Create a user with a live session and return the token its browser would hold."""
    user = await repo.create_user(session, username=username, password_hash=TEST_PASSWORD_HASH)
    user.active = active
    token = new_token()
    await repo.create_session(
        session,
        token_hash=hash_token(token),
        user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await session.flush()
    return token


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Start a PostgreSQL container for the session and yield its asyncpg URL."""
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    """Return an Alembic config pointing at the test database."""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session", autouse=True)
def _migrated(alembic_config: Config) -> None:
    """Build the schema by running the real migrations, not metadata.create_all()."""
    command.upgrade(alembic_config, "head")


@pytest_asyncio.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Yield a session whose work is rolled back when the test ends."""
    engine = create_engine(database_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with factory() as db_session:
        try:
            yield db_session
        finally:
            await db_session.close()
            await transaction.rollback()
            await connection.close()
            await engine.dispose()


@pytest_asyncio.fixture
async def committed_factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory whose writes really commit.

    Needed by tests that run components on their own connections, which cannot see data
    held inside the rolled-back transaction of the ``session`` fixture. Every table is
    truncated afterwards.
    """
    engine = create_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE monitors, monitor_states, check_results, incidents, "
                    "daily_uptime RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()
