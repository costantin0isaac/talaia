from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from talaia.db.engine import create_engine

pytestmark = pytest.mark.integration


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
