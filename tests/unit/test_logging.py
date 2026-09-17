"""Tests for the logging configuration."""

import json
import logging

import pytest

from talaia.logging import QuietAccessLog, configure_logging, get_logger


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


class TestQuietAccessLog:
    """The filter sees uvicorn's access records: (client, method, path, version, status)."""

    @staticmethod
    def record(path: str, status: int = 200) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1", "GET", path, "1.1", status),
            exc_info=None,
        )

    @pytest.mark.parametrize(
        "path",
        ["/healthz", "/readyz", "/metrics", "/partials/summary", "/static/style.css?v=1"],
    )
    def test_routine_requests_are_dropped(self, path: str) -> None:
        assert QuietAccessLog().filter(self.record(path)) is False

    @pytest.mark.parametrize("path", ["/", "/monitors/web", "/api/monitors", "/login"])
    def test_everything_else_is_kept(self, path: str) -> None:
        assert QuietAccessLog().filter(self.record(path)) is True

    @pytest.mark.parametrize("status", [401, 404, 500, 503])
    def test_failures_are_always_kept(self, status: int) -> None:
        """A 503 on /readyz is precisely what the log exists to show."""
        assert QuietAccessLog().filter(self.record("/readyz", status)) is True

    def test_records_of_another_shape_pass_through(self) -> None:
        other = logging.LogRecord("x", logging.INFO, "", 0, "plain message", None, None)

        assert QuietAccessLog().filter(other) is True

    def test_it_is_installed_on_the_access_logger(self) -> None:
        configure_logging()

        assert any(
            isinstance(f, QuietAccessLog) for f in logging.getLogger("uvicorn.access").filters
        )
