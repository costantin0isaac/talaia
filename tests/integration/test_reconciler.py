"""Reconciliation of monitors.yaml into the database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from talaia.config.loader import ConfigError
from talaia.config.reconciler import reconcile, reconcile_file
from talaia.config.schema import MonitorsFile, MonitorType
from talaia.db import repository as repo
from talaia.db.models import MonitorStatus
from talaia.engine.scheduler import to_monitor_config

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

ONE = """
monitors:
  - name: web
    type: http
    target: http://10.0.0.1
    group: services
"""

TWO = """
monitors:
  - name: web
    type: http
    target: http://10.0.0.1
    group: services
  - name: router
    type: icmp
    target: 10.0.0.2
"""

CHANGED = """
monitors:
  - name: web
    type: http
    target: http://10.0.0.99
    group: infra
    interval: 30
"""


def parse(text: str) -> MonitorsFile:
    import yaml

    return MonitorsFile.model_validate(yaml.safe_load(text))


class TestInsert:
    async def test_new_monitor_is_inserted_and_active(self, session: AsyncSession) -> None:
        report = await reconcile(session, parse(ONE))

        assert report.inserted == ("web",)
        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        assert monitor.active is True
        assert monitor.group_name == "services"
        assert monitor.interval_seconds == 60

    async def test_state_row_is_created(self, session: AsyncSession) -> None:
        await reconcile(session, parse(ONE))

        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.UNKNOWN

    async def test_http_block_is_stored_as_json(self, session: AsyncSession) -> None:
        await reconcile(
            session,
            parse(ONE.replace("group: services", "group: services\n    http:\n      method: HEAD")),
        )

        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        assert monitor.config["method"] == "HEAD"

    async def test_running_twice_changes_nothing_the_second_time(
        self, session: AsyncSession
    ) -> None:
        await reconcile(session, parse(TWO))

        report = await reconcile(session, parse(TWO))

        assert report.changed is False
        assert sorted(report.unchanged) == ["router", "web"]


class TestUpdate:
    async def test_changed_fields_are_updated_in_place(self, session: AsyncSession) -> None:
        await reconcile(session, parse(ONE))
        original = await repo.get_monitor_by_name(session, "web")
        assert original is not None
        original_id = original.id

        report = await reconcile(session, parse(CHANGED))

        assert report.updated == ("web",)
        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        assert monitor.id == original_id
        assert monitor.target == "http://10.0.0.99"
        assert monitor.group_name == "infra"
        assert monitor.interval_seconds == 30

    async def test_history_and_open_incident_survive_an_update(self, session: AsyncSession) -> None:
        await reconcile(session, parse(ONE))
        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        await repo.record_check_result(
            session, monitor_id=monitor.id, checked_at=NOW, success=False
        )
        await repo.open_incident(session, monitor_id=monitor.id, started_at=NOW, cause="down")

        await reconcile(session, parse(CHANGED))

        assert len(await repo.list_check_results(session, monitor.id, since=NOW)) == 1
        assert await repo.get_open_incident(session, monitor.id) is not None


class TestSoftDelete:
    async def test_absent_monitor_is_deactivated_not_deleted(self, session: AsyncSession) -> None:
        await reconcile(session, parse(TWO))
        router = await repo.get_monitor_by_name(session, "router")
        assert router is not None
        await repo.record_check_result(session, monitor_id=router.id, checked_at=NOW, success=True)

        report = await reconcile(session, parse(ONE))

        assert report.deactivated == ("router",)
        still_there = await repo.get_monitor_by_name(session, "router")
        assert still_there is not None
        assert still_there.active is False
        assert len(await repo.list_check_results(session, router.id, since=NOW)) == 1

    async def test_deactivated_monitor_is_absent_from_the_active_list(
        self, session: AsyncSession
    ) -> None:
        await reconcile(session, parse(TWO))

        await reconcile(session, parse(ONE))

        assert [m.name for m in await repo.list_monitors(session)] == ["web"]

    async def test_reappearing_monitor_reuses_the_same_row(self, session: AsyncSession) -> None:
        await reconcile(session, parse(TWO))
        router = await repo.get_monitor_by_name(session, "router")
        assert router is not None
        original_id = router.id
        await repo.record_check_result(session, monitor_id=router.id, checked_at=NOW, success=True)
        await reconcile(session, parse(ONE))

        report = await reconcile(session, parse(TWO))

        assert report.reactivated == ("router",)
        revived = await repo.get_monitor_by_name(session, "router")
        assert revived is not None
        assert revived.id == original_id
        assert revived.active is True
        assert len(await repo.list_check_results(session, original_id, since=NOW)) == 1


class TestEnabledFlag:
    async def test_disabled_monitor_is_paused(self, session: AsyncSession) -> None:
        await reconcile(session, parse(ONE.replace("group: services", "enabled: false")))

        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.PAUSED

    async def test_re_enabling_returns_the_monitor_to_unknown(self, session: AsyncSession) -> None:
        await reconcile(session, parse(ONE.replace("group: services", "enabled: false")))

        await reconcile(session, parse(ONE))

        monitor = await repo.get_monitor_by_name(session, "web")
        assert monitor is not None
        state = await repo.get_state(session, monitor.id)
        assert state is not None
        assert state.status is MonitorStatus.UNKNOWN


class TestInvalidConfiguration:
    async def test_invalid_file_leaves_the_database_untouched(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await reconcile(session, parse(TWO))
        before = [(m.name, m.target, m.active) for m in await repo.list_monitors(session)]

        bad = tmp_path / "monitors.yaml"
        bad.write_text("monitors:\n  - name: web\n    type: http\n    intervall: 5\n")

        with pytest.raises(ConfigError):
            await reconcile_file(session, bad)

        after = [(m.name, m.target, m.active) for m in await repo.list_monitors(session)]
        assert after == before

    async def test_missing_file_raises_config_error(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        with pytest.raises(ConfigError, match="not found"):
            await reconcile_file(session, tmp_path / "absent.yaml")


class TestRealConfigFile:
    async def test_the_committed_config_reconciles(self, session: AsyncSession) -> None:
        report = await reconcile_file(session, Path("config/monitors.yaml"))

        assert len(report.inserted) == 5
        # Ordered by group then name; example-disabled has no group, so it sorts last.
        assert [m.name for m in await repo.list_monitors(session)] == [
            "example-certificate",
            "example-router",
            "example-ssh",
            "example-webapp",
            "example-disabled",
        ]


class TestRenameIsDeleteAndCreate:
    async def test_renaming_creates_a_new_row_and_retires_the_old_one(
        self, session: AsyncSession
    ) -> None:
        """Documented behaviour: name is the identity key."""
        await reconcile(session, parse(ONE))
        original = await repo.get_monitor_by_name(session, "web")
        assert original is not None
        original_id = original.id

        report = await reconcile(session, parse(ONE.replace("name: web", "name: webapp")))

        assert report.inserted == ("webapp",)
        assert report.deactivated == ("web",)
        old = await repo.get_monitor_by_name(session, "web")
        assert old is not None
        assert old.id == original_id
        assert old.active is False
        expected_uptime_since = NOW - timedelta(days=1)
        assert await repo.uptime_ratio(session, original_id, since=expected_uptime_since) is None


class TestTlsMonitors:
    async def test_a_tls_block_survives_the_round_trip(self, session: AsyncSession) -> None:
        """YAML -> database -> MonitorConfig, which is what the checker actually receives."""
        config = MonitorsFile.model_validate(
            {
                "monitors": [
                    {
                        "name": "cert",
                        "type": "tls",
                        "target": "example.com:8443",
                        "interval": 3600,
                        "tls": {"warn_days": 21, "server_name": "www.example.com"},
                    }
                ]
            }
        )

        await reconcile(session, config)

        row = await repo.get_monitor_by_name(session, "cert")
        assert row is not None
        assert row.type is MonitorType.TLS

        projected = to_monitor_config(row)
        assert projected.tls is not None
        assert projected.tls.warn_days == 21
        assert projected.tls.server_name == "www.example.com"

    async def test_a_tls_monitor_without_a_block_gets_the_defaults(
        self, session: AsyncSession
    ) -> None:
        config = MonitorsFile.model_validate(
            {"monitors": [{"name": "cert", "type": "tls", "target": "example.com"}]}
        )

        await reconcile(session, config)

        row = await repo.get_monitor_by_name(session, "cert")
        assert row is not None
        assert to_monitor_config(row).tls is None

    async def test_the_database_rejects_an_unknown_monitor_type(
        self, session: AsyncSession
    ) -> None:
        """The CHECK constraint rewritten in migration 0003 has to still be doing its job."""
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO monitors (name, type, target, interval_seconds, "
                    "timeout_seconds, failure_threshold, recovery_threshold, enabled, "
                    "active, config) VALUES ('bogus', 'smtp', 'example.com', 60, 10, 3, 2, "
                    "true, true, '{}'::jsonb)"
                )
            )
        await session.rollback()

    async def test_the_database_accepts_the_tls_type(self, session: AsyncSession) -> None:
        await session.execute(
            text(
                "INSERT INTO monitors (name, type, target, interval_seconds, "
                "timeout_seconds, failure_threshold, recovery_threshold, enabled, "
                "active, config) VALUES ('accepted', 'tls', 'example.com', 60, 10, 3, 2, "
                "true, true, '{}'::jsonb)"
            )
        )

        assert await repo.get_monitor_by_name(session, "accepted") is not None
