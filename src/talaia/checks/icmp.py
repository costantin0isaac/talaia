"""ICMP checker, using unprivileged datagram sockets."""

import time

from icmplib import async_ping
from icmplib.exceptions import (
    DestinationUnreachable,
    ICMPLibError,
    NameLookupError,
    SocketPermissionError,
    TimeExceeded,
)

from talaia.checks.base import CheckOutcome, failure
from talaia.config.schema import MonitorConfig

PERMISSION_ERROR = (
    "ping socket permission denied; the host is missing "
    "net.ipv4.ping_group_range (see README troubleshooting)"
)


class IcmpChecker:
    """Checks that a host answers a single ping.

    Unprivileged mode is used so the container needs no CAP_NET_RAW; the host must allow
    the container's GID range to open ping sockets instead.
    """

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        """Send one echo request and report whether it was answered."""
        start = time.perf_counter()
        try:
            host = await async_ping(
                monitor.target,
                count=1,
                timeout=monitor.timeout,
                privileged=False,
            )
        except SocketPermissionError:
            return failure(PERMISSION_ERROR)
        except NameLookupError:
            return failure("DNS lookup failed")
        except DestinationUnreachable:
            return failure("host unreachable")
        except TimeExceeded:
            return failure("TTL exceeded in transit")
        except ICMPLibError as exc:
            return failure(f"ping failed: {exc}")
        except Exception as exc:
            return failure(f"unexpected error: {type(exc).__name__}: {exc}")

        if not host.is_alive:
            return failure(f"no reply within {monitor.timeout}s")

        latency_ms = int((time.perf_counter() - start) * 1000)
        return CheckOutcome(success=True, latency_ms=latency_ms)
