"""The migrations must build exactly the schema the models describe."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class TestMigrations:
    def test_models_match_the_migrations(self, alembic_config: Config) -> None:
        """Fail if a model changed without a matching migration."""
        command.check(alembic_config)

    async def test_check_results_index_is_descending(self, session: AsyncSession) -> None:
        definition = await session.scalar(
            text(
                "select indexdef from pg_indexes "
                "where indexname = 'ix_check_results_monitor_id_checked_at'"
            )
        )

        assert definition is not None
        assert "checked_at DESC" in definition

    async def test_open_incident_index_is_partial(self, session: AsyncSession) -> None:
        definition = await session.scalar(
            text("select indexdef from pg_indexes where indexname = 'uq_incidents_monitor_id_open'")
        )

        assert definition is not None
        assert "UNIQUE" in definition
        assert "resolved_at IS NULL" in definition

    async def test_status_column_rejects_unknown_values(self, session: AsyncSession) -> None:
        constraint = await session.scalar(
            text(
                "select pg_get_constraintdef(oid) from pg_constraint "
                "where conname = 'ck_monitor_states_monitorstatus'"
            )
        )

        assert constraint is not None
        assert "paused" in constraint

    async def test_timestamps_are_timezone_aware(self, session: AsyncSession) -> None:
        data_type = await session.scalar(
            text(
                "select data_type from information_schema.columns "
                "where table_name = 'check_results' and column_name = 'checked_at'"
            )
        )

        assert data_type == "timestamp with time zone"
