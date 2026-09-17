"""The TLS checker, against a real TLS server on a loopback port."""

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest

from talaia.checks.tls import TlsChecker, certificate_expiry, days_until
from talaia.config.schema import MonitorConfig, MonitorType, TlsOptions

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)


def monitor(target: str = "localhost:443", *, timeout: int = 5, **options: object) -> MonitorConfig:
    return MonitorConfig(
        name="cert",
        type=MonitorType.TLS,
        target=target,
        group=None,
        description=None,
        interval=60,
        timeout=timeout,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        http=None,
        tls=TlsOptions(**options) if options else None,  # type: ignore[arg-type]
    )


def clock(moment: datetime = NOW) -> Callable[[], datetime]:
    return lambda: moment


@pytest.fixture
async def closed_port() -> AsyncIterator[int]:
    """Bind a port, then release it, so connecting to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    yield port


@pytest.fixture
async def plain_server() -> AsyncIterator[int]:
    """Start a TCP server that accepts connections but speaks no TLS."""
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    async with server:
        yield port


class TestDaysUntil:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(days=30), 30),
            (timedelta(days=1, hours=1), 1),
            (timedelta(hours=23), 0),
            (timedelta(hours=-1), -1),
            (timedelta(days=-5), -5),
        ],
    )
    def test_counts_whole_days(self, delta: timedelta, expected: int) -> None:
        assert days_until(NOW + delta, now=NOW) == expected

    def test_an_expired_certificate_is_negative(self) -> None:
        """The sign is what separates 'renew soon' from 'already broken'."""
        assert days_until(NOW - timedelta(days=3), now=NOW) < 0


class TestCertificateExpiry:
    def test_reads_the_openssl_format(self) -> None:
        expiry = certificate_expiry({"notAfter": "Jun  1 12:00:00 2027 GMT"})

        assert expiry == datetime(2027, 6, 1, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize("cert", [{}, {"notAfter": None}, {"notAfter": "nonsense"}])
    def test_an_unreadable_date_is_none(self, cert: dict[str, object]) -> None:
        assert certificate_expiry(cert) is None


class TestFailures:
    async def test_a_closed_port_is_refused(self, closed_port: int) -> None:
        outcome = await TlsChecker().check(monitor(f"127.0.0.1:{closed_port}"))

        assert outcome.success is False
        assert outcome.error == "connection refused"
        assert outcome.expires_in_days is None

    async def test_a_server_that_does_not_speak_tls_fails_cleanly(self, plain_server: int) -> None:
        outcome = await TlsChecker().check(monitor(f"127.0.0.1:{plain_server}"))

        assert outcome.success is False
        assert outcome.error is not None
        assert "Traceback" not in outcome.error

    async def test_dns_failure(self) -> None:
        outcome = await TlsChecker().check(monitor("no-such-host.invalid:443"))

        assert outcome.success is False
        assert outcome.error == "DNS lookup failed"

    async def test_an_invalid_port_does_not_raise(self) -> None:
        outcome = await TlsChecker().check(monitor("example.com:99999"))

        assert outcome.success is False
        assert outcome.error is not None
        assert "expected host or host:port" in outcome.error

    async def test_a_timeout_is_reported_with_its_budget(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        async def never(*args: object, **kwargs: object) -> tuple[object, object]:
            await asyncio.sleep(10)
            raise AssertionError

        monkeypatch.setattr(asyncio, "open_connection", never)

        outcome = await TlsChecker().check(monitor(timeout=1))

        assert outcome.success is False
        assert outcome.error == "timeout after 1s"

    async def test_an_unexpected_exception_becomes_an_outcome(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The Section 7.1 invariant: a checker never raises into the scheduler."""

        async def explode(*args: object, **kwargs: object) -> tuple[object, object]:
            msg = "something nobody predicted"
            raise RuntimeError(msg)

        monkeypatch.setattr(asyncio, "open_connection", explode)

        outcome = await TlsChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert "RuntimeError" in outcome.error


class TestExpiryThreshold:
    """The expiry decision, exercised through a stubbed handshake."""

    @staticmethod
    def _with_expiry(monkeypatch, expires_at: datetime) -> None:  # type: ignore[no-untyped-def]
        class FakeWriter:
            def get_extra_info(self, name: str) -> object:
                stamp = expires_at.strftime("%b %d %H:%M:%S %Y GMT")
                return {"notAfter": stamp} if name == "peercert" else None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        async def connect(*args: object, **kwargs: object) -> tuple[object, FakeWriter]:
            return object(), FakeWriter()

        monkeypatch.setattr(asyncio, "open_connection", connect)

    async def test_a_healthy_certificate_succeeds(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_expiry(monkeypatch, NOW + timedelta(days=60))

        outcome = await TlsChecker(clock=clock()).check(monitor())

        assert outcome.success is True
        assert outcome.expires_in_days == 60
        assert outcome.error is None
        assert outcome.latency_ms is not None

    async def test_the_warning_window_fails_the_check(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Failing is the point: it is what produces the notification in time to renew."""
        self._with_expiry(monkeypatch, NOW + timedelta(days=5))

        outcome = await TlsChecker(clock=clock()).check(monitor(warn_days=14))

        assert outcome.success is False
        assert outcome.error == "certificate expires in 5d"
        assert outcome.expires_in_days == 5

    async def test_the_threshold_is_configurable(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_expiry(monkeypatch, NOW + timedelta(days=5))

        outcome = await TlsChecker(clock=clock()).check(monitor(warn_days=3))

        assert outcome.success is True

    async def test_the_boundary_day_still_passes(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_expiry(monkeypatch, NOW + timedelta(days=14, hours=1))

        outcome = await TlsChecker(clock=clock()).check(monitor(warn_days=14))

        assert outcome.success is True

    async def test_an_expired_certificate_says_how_long_ago(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_expiry(monkeypatch, NOW - timedelta(days=3))

        outcome = await TlsChecker(clock=clock()).check(monitor())

        assert outcome.success is False
        assert outcome.error == "certificate expired 3d ago"

    async def test_the_default_threshold_applies_without_a_tls_block(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        self._with_expiry(monkeypatch, NOW + timedelta(days=10))

        outcome = await TlsChecker(clock=clock()).check(monitor())

        assert outcome.success is False
        assert outcome.expires_in_days == 10


class TestVerification:
    async def test_a_rejected_certificate_reports_the_reason(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Verification stays on, so a bad chain or wrong hostname is a failure."""

        async def reject(*args: object, **kwargs: object) -> tuple[object, object]:
            raise ssl.SSLCertVerificationError(
                ssl.SSL_ERROR_SSL, "certificate verify failed: self-signed certificate"
            )

        monkeypatch.setattr(asyncio, "open_connection", reject)

        outcome = await TlsChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert outcome.error.startswith("certificate rejected:")

    async def test_a_handshake_error_is_not_a_traceback(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        async def broken(*args: object, **kwargs: object) -> tuple[object, object]:
            raise ssl.SSLError(1, "WRONG_VERSION_NUMBER")

        monkeypatch.setattr(asyncio, "open_connection", broken)

        outcome = await TlsChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert outcome.error.startswith("TLS handshake failed:")

    async def test_a_server_presenting_no_certificate_fails(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        class Writer:
            def get_extra_info(self, name: str) -> object:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        async def connect(*args: object, **kwargs: object) -> tuple[object, Writer]:
            return object(), Writer()

        monkeypatch.setattr(asyncio, "open_connection", connect)

        outcome = await TlsChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error == "the server presented no certificate"


class TestContextReuse:
    async def test_the_ssl_context_is_built_once_per_checker(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Each build re-reads the CA bundle from disk; per check that is pure waste."""
        calls = 0
        real = ssl.create_default_context

        def counting(*args: object, **kwargs: object) -> ssl.SSLContext:
            nonlocal calls
            calls += 1
            return real()

        monkeypatch.setattr(ssl, "create_default_context", counting)

        async def refuse(*args: object, **kwargs: object) -> tuple[object, object]:
            raise ConnectionRefusedError

        monkeypatch.setattr(asyncio, "open_connection", refuse)

        checker = TlsChecker()
        await checker.check(monitor())
        await checker.check(monitor())

        assert calls == 1
