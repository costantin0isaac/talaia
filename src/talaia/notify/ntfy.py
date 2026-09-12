"""ntfy notifier."""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from talaia import __version__
from talaia.logging import get_logger
from talaia.notify.base import Notification, Priority

log = get_logger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 3.0)
TIMEOUT_SECONDS = 10.0
USER_AGENT = f"talaia/{__version__}"

PRIORITIES: dict[Priority, int] = {"default": 3, "high": 4}


@dataclass(frozen=True, slots=True)
class _Failure:
    """Why one delivery attempt did not succeed."""

    reason: str
    retriable: bool


class NtfyNotifier:
    """Publishes notifications to an ntfy topic."""

    def __init__(
        self,
        *,
        url: str,
        topic: str,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
    ) -> None:
        self._endpoint = f"{url.rstrip('/')}/"
        self._topic = topic
        self._backoff = backoff
        self._headers = {"User-Agent": USER_AGENT}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        self._owns_client = client is None

    async def send(self, notification: Notification) -> None:
        """Publish to the topic, retrying transient failures. Never raises."""
        payload = self._payload(notification)
        failure = _Failure(reason="not attempted", retriable=True)

        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._backoff[attempt - 1])

            result = await self._attempt(payload)
            if result is None:
                log.info("notification sent", title=notification.title, attempts=attempt + 1)
                return

            failure = result
            if not failure.retriable:
                break

        log.warning(
            "notification not delivered",
            title=notification.title,
            reason=failure.reason,
            retriable=failure.retriable,
        )

    async def aclose(self) -> None:
        """Close the HTTP client, unless it was supplied by the caller."""
        if self._owns_client:
            await self._client.aclose()

    async def _attempt(self, payload: dict[str, Any]) -> _Failure | None:
        """Post once. Returns ``None`` on success, or why it failed."""
        try:
            response = await self._client.post(self._endpoint, json=payload, headers=self._headers)
        except httpx.RequestError as error:
            return _Failure(reason=f"{type(error).__name__}: {error}", retriable=True)

        if response.is_success:
            return None
        # 4xx means a wrong topic, URL or token; retrying cannot fix it.
        return _Failure(reason=f"HTTP {response.status_code}", retriable=response.is_server_error)

    def _payload(self, notification: Notification) -> dict[str, Any]:
        """Render the notification as an ntfy JSON message.

        JSON rather than the header form, whose headers are latin-1 and cannot carry the
        emoji in the titles.
        """
        payload: dict[str, Any] = {
            "topic": self._topic,
            "title": notification.title,
            "message": notification.body,
            "priority": PRIORITIES[notification.priority],
            "tags": [notification.tag],
        }
        if notification.link:
            payload["click"] = notification.link
        return payload
