"""TCP checker: success is a completed handshake."""

import asyncio
import socket
import time
from contextlib import suppress

from talaia.checks.base import CheckOutcome, failure
from talaia.config.schema import MonitorConfig


def split_host_port(target: str) -> tuple[str, int]:
    """Split a ``host:port`` target, tolerating a bracketed IPv6 address."""
    host, _, port = target.rpartition(":")
    return host.strip("[]"), int(port)


class TcpChecker:
    """Checks that a TCP port accepts a connection."""

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        """Open a connection to the target and close it again."""
        writer: asyncio.StreamWriter | None = None
        try:
            host, port = split_host_port(monitor.target)
        except ValueError:
            return failure(f"invalid target {monitor.target!r}; expected host:port")

        start = time.perf_counter()
        try:
            async with asyncio.timeout(monitor.timeout):
                _, writer = await asyncio.open_connection(host, port)
            latency_ms = int((time.perf_counter() - start) * 1000)
        except TimeoutError:
            return failure(f"timeout after {monitor.timeout}s")
        except ConnectionRefusedError:
            return failure("connection refused")
        except socket.gaierror:
            return failure("DNS lookup failed")
        except OSError as exc:
            return failure(f"connection failed: {exc.strerror or exc}")
        except Exception as exc:
            return failure(f"unexpected error: {type(exc).__name__}: {exc}")
        finally:
            await _close(writer)

        return CheckOutcome(success=True, latency_ms=latency_ms)


async def _close(writer: asyncio.StreamWriter | None) -> None:
    """Close a connection, ignoring anything that goes wrong on the way out."""
    if writer is None:
        return
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
