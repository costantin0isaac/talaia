"""Tests for the ICMP checker's error handling.

The ping path itself is not exercised: it depends on kernel and container settings that
cannot be relied on in CI. These tests cover the mapping from icmplib outcomes and
exceptions onto CheckOutcome, which is the part this project owns.
"""

from typing import Any

import pytest
from icmplib.exceptions import (
    DestinationUnreachable,
    ICMPLibError,
    NameLookupError,
    SocketPermissionError,
    TimeExceeded,
)

from talaia.checks import icmp as icmp_module
from talaia.checks.icmp import IcmpChecker
from talaia.config.schema import MonitorConfig, MonitorType


def monitor(target: str = "10.0.0.1", *, timeout: int = 5) -> MonitorConfig:
    return MonitorConfig(
        name="router",
        type=MonitorType.ICMP,
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


class FakeReply:
    """The minimum icmplib's ICMPError subclasses need to construct a message."""

    code = 0


class FakeHost:
    def __init__(self, *, is_alive: bool, avg_rtt: float = 1.0) -> None:
        self.is_alive = is_alive
        self.avg_rtt = avg_rtt


def patch_ping(monkeypatch: pytest.MonkeyPatch, result: object) -> list[dict[str, Any]]:
    """Replace icmplib's async_ping, recording how it was called."""
    calls: list[dict[str, Any]] = []

    async def fake(address: str, **kwargs: Any) -> object:
        calls.append({"address": address, **kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(icmp_module, "async_ping", fake)
    return calls


class TestSuccess:
    async def test_a_reply_is_a_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_ping(monkeypatch, FakeHost(is_alive=True, avg_rtt=4.2))

        outcome = await IcmpChecker().check(monitor())

        assert outcome.success is True
        assert outcome.latency_ms is not None
        assert outcome.error is None

    async def test_no_reply_is_a_failure_with_no_latency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_ping(monkeypatch, FakeHost(is_alive=False))

        outcome = await IcmpChecker().check(monitor(timeout=3))

        assert outcome.success is False
        assert outcome.error == "no reply within 3s"
        assert outcome.latency_ms is None


class TestPingArguments:
    async def test_uses_unprivileged_mode_and_a_single_packet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section 7.2: unprivileged sockets, one packet per check."""
        calls = patch_ping(monkeypatch, FakeHost(is_alive=True))

        await IcmpChecker().check(monitor("10.0.0.9", timeout=7))

        assert calls == [{"address": "10.0.0.9", "count": 1, "timeout": 7, "privileged": False}]


class TestErrors:
    async def test_permission_error_points_at_the_sysctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_ping(monkeypatch, SocketPermissionError("not permitted"))

        outcome = await IcmpChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert "ping_group_range" in outcome.error

    @pytest.mark.parametrize(
        ("exception", "expected"),
        [
            (NameLookupError("nope"), "DNS lookup failed"),
            (DestinationUnreachable(FakeReply()), "host unreachable"),
            (TimeExceeded(FakeReply()), "TTL exceeded in transit"),
        ],
    )
    async def test_known_errors_map_to_short_messages(
        self, monkeypatch: pytest.MonkeyPatch, exception: Exception, expected: str
    ) -> None:
        patch_ping(monkeypatch, exception)

        assert (await IcmpChecker().check(monitor())).error == expected

    async def test_other_icmplib_errors_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_ping(monkeypatch, ICMPLibError("something odd"))

        outcome = await IcmpChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert outcome.error.startswith("ping failed")

    async def test_an_unexpected_exception_becomes_an_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_ping(monkeypatch, ZeroDivisionError("absurd"))

        outcome = await IcmpChecker().check(monitor())

        assert outcome.success is False
        assert outcome.error is not None
        assert "ZeroDivisionError" in outcome.error

    async def test_the_error_message_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_ping(monkeypatch, RuntimeError("x" * 5000))

        outcome = await IcmpChecker().check(monitor())

        assert outcome.error is not None
        assert len(outcome.error) <= 200
