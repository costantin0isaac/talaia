"""Tests for the TCP checker, against a real local server."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from talaia.checks.tcp import TcpChecker, split_host_port
from talaia.config.schema import MonitorConfig, MonitorType


def monitor(target: str, *, timeout: int = 5) -> MonitorConfig:
    return MonitorConfig(
        name="port",
        type=MonitorType.TCP,
        target=target,
        group=None,
        description=None,
        interval=60,
        timeout=timeout,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        http=None,
        tls=None,
    )


@pytest.fixture
async def listening_port() -> AsyncIterator[int]:
    """Run a server on an ephemeral port for the duration of a test."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


@pytest.fixture
async def closed_port() -> int:
    """Return a port that was bound and then released, so nothing is listening."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return int(port)


class TestSplitHostPort:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("10.0.0.1:22", ("10.0.0.1", 22)),
            ("example.com:443", ("example.com", 443)),
            ("[::1]:8080", ("::1", 8080)),
        ],
    )
    def test_splits(self, target: str, expected: tuple[str, int]) -> None:
        assert split_host_port(target) == expected


class TestTcpChecker:
    async def test_open_port_succeeds(self, listening_port: int) -> None:
        outcome = await TcpChecker().check(monitor(f"127.0.0.1:{listening_port}"))

        assert outcome.success is True
        assert outcome.latency_ms is not None
        assert outcome.error is None
        assert outcome.status_code is None

    async def test_closed_port_is_refused(self, closed_port: int) -> None:
        outcome = await TcpChecker().check(monitor(f"127.0.0.1:{closed_port}"))

        assert outcome.success is False
        assert outcome.error == "connection refused"
        assert outcome.latency_ms is None

    async def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def never_connects(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        monkeypatch.setattr(asyncio, "open_connection", never_connects)

        outcome = await TcpChecker().check(monitor("127.0.0.1:1", timeout=1))

        assert outcome.success is False
        assert outcome.error == "timeout after 1s"
        assert outcome.latency_ms is None

    async def test_dns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        async def bad_name(*args: object, **kwargs: object) -> object:
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(asyncio, "open_connection", bad_name)

        outcome = await TcpChecker().check(monitor("nope.invalid:80"))

        assert outcome.error == "DNS lookup failed"

    async def test_other_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def unreachable(*args: object, **kwargs: object) -> object:
            raise OSError(113, "No route to host")

        monkeypatch.setattr(asyncio, "open_connection", unreachable)

        outcome = await TcpChecker().check(monitor("10.0.0.1:22"))

        assert outcome.success is False
        assert outcome.error is not None
        assert "No route to host" in outcome.error

    async def test_invalid_target_does_not_raise(self) -> None:
        outcome = await TcpChecker().check(monitor("no-port-here"))

        assert outcome.success is False
        assert outcome.error is not None
        assert "expected host:port" in outcome.error

    async def test_an_unexpected_exception_becomes_an_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def explode(*args: object, **kwargs: object) -> object:
            raise ZeroDivisionError("absurd")

        monkeypatch.setattr(asyncio, "open_connection", explode)

        outcome = await TcpChecker().check(monitor("10.0.0.1:22"))

        assert outcome.success is False
        assert outcome.error is not None
        assert "ZeroDivisionError" in outcome.error

    async def test_connections_are_closed(self, listening_port: int) -> None:
        """Repeated checks must not leak sockets."""
        checker = TcpChecker()

        for _ in range(20):
            assert (await checker.check(monitor(f"127.0.0.1:{listening_port}"))).success
