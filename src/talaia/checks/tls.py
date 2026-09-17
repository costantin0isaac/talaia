"""TLS checker: certificate validity and time left before expiry.

Certificates are verified exactly as a browser would verify them. That is deliberate: a
checker that accepted any certificate in order to read its expiry date would stay green
through a hostname mismatch or a broken chain, which are the failures that actually take a
site down. The cost is that a self-signed certificate fails here, which is the honest
answer for a monitor named "is my TLS healthy".
"""

import asyncio
import socket
import ssl
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from talaia.checks.base import CheckOutcome, failure
from talaia.config.schema import DEFAULT_TLS_PORT, MonitorConfig, TlsOptions, split_tls_target

SECONDS_PER_DAY = 86400

Clock = Callable[[], datetime] | None

DEFAULT_OPTIONS = TlsOptions()


def days_until(expires_at: datetime, *, now: datetime) -> int:
    """Whole days of validity left, negative once the certificate has expired."""
    return int((expires_at - now).total_seconds() // SECONDS_PER_DAY)


def certificate_expiry(peer_cert: dict[str, object]) -> datetime | None:
    """Read ``notAfter`` from a verified peer certificate."""
    not_after = peer_cert.get("notAfter")
    if not isinstance(not_after, str):
        return None
    try:
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)
    except ValueError:
        return None


class TlsChecker:
    """Checks that a TLS endpoint presents a valid certificate that is not about to expire."""

    def __init__(self, *, clock: Clock = None) -> None:
        self._now = clock or _utc_now
        # Building a context reads the system CA bundle from disk; once is enough.
        self._context = ssl.create_default_context()

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        """Complete a TLS handshake and measure the certificate's remaining validity."""
        options = monitor.tls or DEFAULT_OPTIONS
        host, port = split_tls_target(monitor.target)
        if port is None:
            return failure(f"invalid target {monitor.target!r}; expected host or host:port")

        writer: asyncio.StreamWriter | None = None
        start = time.perf_counter()
        try:
            async with asyncio.timeout(monitor.timeout):
                _, writer = await asyncio.open_connection(
                    host,
                    port,
                    ssl=self._context,
                    server_hostname=options.server_name or host,
                )
            latency_ms = int((time.perf_counter() - start) * 1000)
            peer_cert = writer.get_extra_info("peercert")
        except TimeoutError:
            return failure(f"timeout after {monitor.timeout}s")
        except ssl.SSLCertVerificationError as exc:
            return failure(f"certificate rejected: {_reason(exc)}")
        except ssl.SSLError as exc:
            return failure(f"TLS handshake failed: {_reason(exc)}")
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

        if not peer_cert:
            return failure("the server presented no certificate", latency_ms=latency_ms)

        expires_at = certificate_expiry(peer_cert)
        if expires_at is None:
            return failure("could not read the certificate expiry date", latency_ms=latency_ms)

        remaining = days_until(expires_at, now=self._now())
        if remaining < 0:
            return failure(f"certificate expired {abs(remaining)}d ago", latency_ms=latency_ms)
        if remaining < options.warn_days:
            return CheckOutcome(
                success=False,
                latency_ms=latency_ms,
                error=f"certificate expires in {remaining}d",
                expires_in_days=remaining,
            )

        return CheckOutcome(success=True, latency_ms=latency_ms, expires_in_days=remaining)


def _reason(exc: ssl.SSLError) -> str:
    """Describe an SSL error without assuming the attributes the C layer usually sets.

    They are absent on an instance built any other way, and an AttributeError raised here
    would escape the checker -- exactly the failure the never-raises rule exists to stop.
    """
    for attribute in ("verify_message", "reason"):
        value = getattr(exc, attribute, None)
        if value:
            return str(value)
    return str(exc) or type(exc).__name__


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


async def _close(writer: asyncio.StreamWriter | None) -> None:
    """Close a connection, ignoring anything that goes wrong on the way out."""
    if writer is None:
        return
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


__all__ = ["DEFAULT_TLS_PORT", "TlsChecker", "certificate_expiry", "days_until"]
