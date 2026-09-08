"""HTTP checker."""

import time
from dataclasses import dataclass

import httpx

from talaia import __version__
from talaia.checks.base import CheckOutcome, failure
from talaia.config.schema import MonitorConfig

USER_AGENT = f"talaia/{__version__}"
MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class HttpClients:
    """The two long-lived clients, one verifying TLS certificates and one not."""

    verifying: httpx.AsyncClient
    insecure: httpx.AsyncClient

    def for_monitor(self, *, verify_tls: bool) -> httpx.AsyncClient:
        """Return the client matching a monitor's TLS setting."""
        return self.verifying if verify_tls else self.insecure


class HttpChecker:
    """Checks that an HTTP endpoint answers as configured."""

    def __init__(self, clients: HttpClients) -> None:
        self._clients = clients

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        """Request the target and compare the response with the expectations."""
        options = monitor.http
        expected_status = list(options.expected_status) if options else [200]
        expected_body = options.expected_body if options else None
        method = options.method if options else "GET"
        headers = {"User-Agent": USER_AGENT, **(options.headers if options else {})}
        client = self._clients.for_monitor(verify_tls=options.verify_tls if options else True)

        start = time.perf_counter()
        try:
            request = client.build_request(
                method,
                monitor.target,
                headers=headers,
                timeout=httpx.Timeout(monitor.timeout),
            )
            response = await client.send(
                request,
                stream=True,
                follow_redirects=options.follow_redirects if options else False,
            )
            try:
                body = await _read_body(response) if expected_body is not None else ""
            finally:
                await response.aclose()
        except httpx.TimeoutException:
            return failure(f"timeout after {monitor.timeout}s")
        except httpx.TooManyRedirects:
            return failure("too many redirects")
        except httpx.ConnectError as exc:
            return failure(_connection_error(exc))
        except httpx.HTTPError as exc:
            return failure(f"request failed: {exc}")
        except Exception as exc:
            return failure(f"unexpected error: {type(exc).__name__}: {exc}")

        latency_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code not in expected_status:
            expected = ", ".join(str(code) for code in expected_status)
            return failure(
                f"HTTP {response.status_code}, expected {expected}",
                latency_ms=latency_ms,
                status_code=response.status_code,
            )

        if expected_body is not None and expected_body not in body:
            return failure(
                "body did not contain expected string",
                latency_ms=latency_ms,
                status_code=response.status_code,
            )

        return CheckOutcome(success=True, latency_ms=latency_ms, status_code=response.status_code)


async def _read_body(response: httpx.Response) -> str:
    """Read at most ``MAX_BODY_BYTES`` of the response body."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_BODY_BYTES:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


def _connection_error(exc: httpx.ConnectError) -> str:
    """Turn a connection error into a short phrase suited to a notification."""
    text = str(exc).lower()
    if "refused" in text:
        return "connection refused"
    if "name or service not known" in text or "nodename nor servname" in text:
        return "DNS lookup failed"
    if "certificate" in text or "ssl" in text:
        return "TLS certificate error"
    return "connection failed"
