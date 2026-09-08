"""Tests for the HTTP checker, with respx intercepting httpx."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from talaia.checks.http import HttpChecker, HttpClients
from talaia.config.schema import HttpOptions, MonitorConfig, MonitorType

TARGET = "http://10.0.0.1/health"


def monitor(**http_options: object) -> MonitorConfig:
    return MonitorConfig(
        name="web",
        type=MonitorType.HTTP,
        target=TARGET,
        group=None,
        description=None,
        interval=60,
        timeout=10,
        failure_threshold=3,
        recovery_threshold=2,
        enabled=True,
        http=HttpOptions(**http_options),  # type: ignore[arg-type]
    )


@pytest.fixture
async def checker() -> AsyncIterator[HttpChecker]:
    async with httpx.AsyncClient() as verifying, httpx.AsyncClient(verify=False) as insecure:
        yield HttpChecker(HttpClients(verifying=verifying, insecure=insecure))


class TestStatus:
    @respx.mock
    async def test_expected_status_succeeds(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(200))

        outcome = await checker.check(monitor())

        assert outcome.success is True
        assert outcome.status_code == 200
        assert outcome.latency_ms is not None
        assert outcome.error is None

    @respx.mock
    async def test_unexpected_status_fails_with_a_useful_message(
        self, checker: HttpChecker
    ) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(500))

        outcome = await checker.check(monitor())

        assert outcome.success is False
        assert outcome.error == "HTTP 500, expected 200"
        assert outcome.status_code == 500
        assert outcome.latency_ms is not None

    @respx.mock
    async def test_several_acceptable_statuses(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(204))

        outcome = await checker.check(monitor(expected_status=[200, 204]))

        assert outcome.success is True

    @respx.mock
    async def test_error_names_every_expected_status(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(403))

        outcome = await checker.check(monitor(expected_status=[200, 204]))

        assert outcome.error == "HTTP 403, expected 200, 204"


class TestBody:
    @respx.mock
    async def test_expected_body_present_succeeds(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(200, text="all systems ok"))

        outcome = await checker.check(monitor(expected_body="systems ok"))

        assert outcome.success is True

    @respx.mock
    async def test_expected_body_absent_fails(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(200, text="maintenance"))

        outcome = await checker.check(monitor(expected_body="systems ok"))

        assert outcome.success is False
        assert outcome.error == "body did not contain expected string"
        assert outcome.status_code == 200

    @respx.mock
    async def test_body_is_not_read_when_no_expectation_is_configured(
        self, checker: HttpChecker
    ) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(200, text="x" * 1000))

        outcome = await checker.check(monitor())

        assert outcome.success is True

    @respx.mock
    async def test_large_body_is_capped(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(return_value=httpx.Response(200, text="a" * 500_000))

        outcome = await checker.check(monitor(expected_body="a"))

        assert outcome.success is True


class TestRedirects:
    @respx.mock
    async def test_redirect_fails_when_not_following(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(
            return_value=httpx.Response(301, headers={"Location": "http://10.0.0.1/new"})
        )

        outcome = await checker.check(monitor())

        assert outcome.success is False
        assert outcome.error == "HTTP 301, expected 200"

    @respx.mock
    async def test_redirect_is_followed_when_configured(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(
            return_value=httpx.Response(301, headers={"Location": "http://10.0.0.1/new"})
        )
        respx.get("http://10.0.0.1/new").mock(return_value=httpx.Response(200))

        outcome = await checker.check(monitor(follow_redirects=True))

        assert outcome.success is True


class TestNetworkFailures:
    @respx.mock
    async def test_timeout(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=httpx.TimeoutException("timed out"))

        outcome = await checker.check(monitor())

        assert outcome.success is False
        assert outcome.error == "timeout after 10s"
        assert outcome.latency_ms is None

    @respx.mock
    async def test_connection_refused(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=httpx.ConnectError("[Errno 111] Connection refused"))

        outcome = await checker.check(monitor())

        assert outcome.error == "connection refused"
        assert outcome.latency_ms is None

    @respx.mock
    async def test_dns_failure(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=httpx.ConnectError("Name or service not known"))

        outcome = await checker.check(monitor())

        assert outcome.error == "DNS lookup failed"

    @respx.mock
    async def test_tls_error(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=httpx.ConnectError("certificate verify failed"))

        outcome = await checker.check(monitor())

        assert outcome.error == "TLS certificate error"

    @respx.mock
    async def test_too_many_redirects(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=httpx.TooManyRedirects("loop"))

        outcome = await checker.check(monitor(follow_redirects=True))

        assert outcome.error == "too many redirects"


class TestNeverRaises:
    """Section 7.1: an exception escaping a checker would kill its scheduler task."""

    @respx.mock
    async def test_an_unexpected_exception_becomes_an_outcome(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=ZeroDivisionError("something absurd"))

        outcome = await checker.check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert "ZeroDivisionError" in outcome.error

    @respx.mock
    async def test_the_error_message_is_bounded(self, checker: HttpChecker) -> None:
        respx.get(TARGET).mock(side_effect=RuntimeError("x" * 5000))

        outcome = await checker.check(monitor())

        assert outcome.error is not None
        assert len(outcome.error) <= 200


class TestRequestShape:
    @respx.mock
    async def test_sends_a_talaia_user_agent(self, checker: HttpChecker) -> None:
        route = respx.get(TARGET).mock(return_value=httpx.Response(200))

        await checker.check(monitor())

        assert route.calls.last.request.headers["user-agent"].startswith("talaia/")

    @respx.mock
    async def test_sends_configured_headers(self, checker: HttpChecker) -> None:
        route = respx.get(TARGET).mock(return_value=httpx.Response(200))

        await checker.check(monitor(headers={"X-Probe": "talaia"}))

        assert route.calls.last.request.headers["x-probe"] == "talaia"

    @respx.mock
    async def test_uses_the_configured_method(self, checker: HttpChecker) -> None:
        route = respx.head(TARGET).mock(return_value=httpx.Response(200))

        outcome = await checker.check(monitor(method="HEAD"))

        assert outcome.success is True
        assert route.calls.last.request.method == "HEAD"
