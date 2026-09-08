"""Tests for the logging configuration."""

import json
import logging

import pytest

from talaia.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_logging() -> None:
    """Leave the root logger as the test session found it."""
    logging.getLogger().handlers = []


class TestConfigureLogging:
    def test_json_output_is_one_object_per_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", log_format="json")
        get_logger("talaia.test").info("check completed", monitor="grafana", latency_ms=42)

        record = json.loads(capsys.readouterr().out.strip())

        assert record["event"] == "check completed"
        assert record["monitor"] == "grafana"
        assert record["latency_ms"] == 42
        assert record["level"] == "info"
        assert record["logger"] == "talaia.test"

    def test_timestamps_are_utc_iso8601(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", log_format="json")
        get_logger("talaia.test").info("event")

        timestamp = json.loads(capsys.readouterr().out.strip())["timestamp"]

        assert timestamp.endswith("Z")

    def test_console_output_is_not_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", log_format="console")
        get_logger("talaia.test").info("check completed", monitor="grafana")

        output = capsys.readouterr().out

        assert "check completed" in output
        assert "grafana" in output

    def test_messages_below_the_level_are_suppressed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="WARNING", log_format="json")
        log = get_logger("talaia.test")
        log.info("suppressed")
        log.warning("emitted")

        lines = capsys.readouterr().out.strip().splitlines()

        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "emitted"

    def test_standard_library_loggers_share_the_format(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", log_format="json")
        logging.getLogger("uvicorn.error").warning("started")

        record = json.loads(capsys.readouterr().out.strip())

        assert record["event"] == "started"
        assert record["logger"] == "uvicorn.error"
        assert record["level"] == "warning"

    def test_repeated_configuration_does_not_duplicate_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", log_format="json")
        configure_logging(level="INFO", log_format="json")
        get_logger("talaia.test").info("once")

        assert len(capsys.readouterr().out.strip().splitlines()) == 1
