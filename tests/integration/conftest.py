from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
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
