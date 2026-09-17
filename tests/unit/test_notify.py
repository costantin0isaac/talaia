"""Message shape and delivery behaviour of the notifiers."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from talaia.notify.base import (
    Notification,
    NullNotifier,
    down_notification,
    startup_notification,
    up_notification,
)
from talaia.notify.ntfy import NtfyNotifier

SERVER = "http://10.0.0.9:8080"
TOPIC = "talaia-alerts"
AT = datetime(2026, 3, 14, 9, 30, 5, tzinfo=UTC)

DOWN = Notification(
    title="🔴 web is DOWN",
    body="Target: http://10.0.0.1\nError: connection refused",
    priority="high",
    tag="rotating_light",
    link="http://talaia.lan/monitors/web",
)


@pytest.fixture
async def notifier() -> AsyncIterator[NtfyNotifier]:
    async with httpx.AsyncClient() as client:
        yield NtfyNotifier(url=SERVER, topic=TOPIC, client=client, backoff=(0.0, 0.0))


class TestMessages:
    def test_down_carries_target_error_and_time(self) -> None:
        notification = down_notification(
            monitor="web", target="http://10.0.0.1", error="connection refused", at=AT
        )

        assert notification.title == "🔴 web is DOWN"
        assert notification.priority == "high"
        assert notification.tag == "rotating_light"
        assert "Target: http://10.0.0.1" in notification.body
        assert "Error: connection refused" in notification.body
        assert "Since: 2026-03-14 09:30:05 UTC" in notification.body

    def test_down_without_an_error_still_says_something(self) -> None:
        notification = down_notification(monitor="web", target="http://10.0.0.1", error=None, at=AT)

        assert "Error: check failed" in notification.body

    def test_up_carries_the_downtime(self) -> None:
        notification = up_notification(monitor="web", downtime_seconds=372, at=AT)

        assert notification.title == "🟢 web recovered"
        assert notification.priority == "default"
        assert notification.tag == "white_check_mark"
        assert "Down for 6m 12s" in notification.body
        assert "Recovered: 2026-03-14 09:30:05 UTC" in notification.body

    def test_up_without_a_duration_does_not_invent_one(self) -> None:
        notification = up_notification(monitor="web", downtime_seconds=None, at=AT)

        assert "Downtime unknown" in notification.body

    def test_a_link_is_included_when_given(self) -> None:
        notification = down_notification(
            monitor="web",
            target="http://10.0.0.1",
            error=None,
            at=AT,
            link="http://talaia.lan/monitors/web",
        )

        assert notification.link == "http://talaia.lan/monitors/web"


class TestNullNotifier:
    async def test_discards_without_complaining(self) -> None:
        null = NullNotifier()

        await null.send(DOWN)
        await null.aclose()


class TestPayload:
    @respx.mock
    async def test_publishes_json_so_emoji_survive(self, notifier: NtfyNotifier) -> None:
        """Titles carry emoji, which the latin-1 header form of the API cannot express."""
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))

        await notifier.send(DOWN)

        payload = json.loads(route.calls.last.request.read())
        assert payload["topic"] == TOPIC
        assert payload["title"] == "🔴 web is DOWN"

    @respx.mock
    async def test_maps_the_priority_and_tag(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))

        await notifier.send(DOWN)

        payload = json.loads(route.calls.last.request.read())
        assert payload["priority"] == 4
        assert payload["tags"] == ["rotating_light"]
        assert payload["click"] == "http://talaia.lan/monitors/web"

    @respx.mock
    async def test_a_token_becomes_a_bearer_header(self) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            authenticated = NtfyNotifier(url=SERVER, topic=TOPIC, token="tk_secret", client=client)

            await authenticated.send(DOWN)

        assert route.calls.last.request.headers["Authorization"] == "Bearer tk_secret"

    @respx.mock
    async def test_no_token_means_no_authorization_header(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))

        await notifier.send(DOWN)

        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    async def test_a_trailing_slash_in_the_url_is_not_doubled(self) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            trailing = NtfyNotifier(url=f"{SERVER}/", topic=TOPIC, client=client)

            await trailing.send(DOWN)

        assert route.called


class TestDelivery:
    @respx.mock
    async def test_a_successful_post_is_not_repeated(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))

        await notifier.send(DOWN)

        assert route.call_count == 1

    @respx.mock
    async def test_a_transport_error_is_retried_twice(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(side_effect=httpx.ConnectError("refused"))

        await notifier.send(DOWN)

        assert route.call_count == 3

    @respx.mock
    async def test_a_server_error_is_retried_twice(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(503))

        await notifier.send(DOWN)

        assert route.call_count == 3

    @respx.mock
    async def test_a_retry_that_succeeds_stops_there(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(
            side_effect=[httpx.Response(502), httpx.Response(200)]
        )

        await notifier.send(DOWN)

        assert route.call_count == 2

    @respx.mock
    async def test_a_client_error_is_not_retried(self, notifier: NtfyNotifier) -> None:
        """A wrong topic, URL or token is permanent; retrying only delays the log line."""
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(403))

        await notifier.send(DOWN)

        assert route.call_count == 1

    @respx.mock
    async def test_giving_up_raises_nothing(self, notifier: NtfyNotifier) -> None:
        """A failing notifier must never disturb the checking it reports on."""
        respx.post(f"{SERVER}/").mock(side_effect=httpx.ConnectTimeout("timed out"))

        await notifier.send(DOWN)

    async def test_closing_leaves_a_borrowed_client_open(self) -> None:
        async with httpx.AsyncClient() as client:
            borrowed = NtfyNotifier(url=SERVER, topic=TOPIC, client=client)

            await borrowed.aclose()

            assert client.is_closed is False

    async def test_closing_closes_an_owned_client(self) -> None:
        owned = NtfyNotifier(url=SERVER, topic=TOPIC)

        await owned.aclose()

        assert owned._client.is_closed is True


class TestStartupNotification:
    def test_says_what_is_being_watched(self) -> None:
        notification = startup_notification(version="1.1.0", monitors=20, at=AT)

        assert notification.title == "👁 talaia is watching"
        assert "Watching 20 monitors" in notification.body
        assert "Version 1.1.0" in notification.body
        assert "Started: 2026-03-14 09:30:05 UTC" in notification.body

    def test_it_is_low_priority(self) -> None:
        """Nobody must act on it; it is a receipt, not an alarm."""
        assert startup_notification(version="1.1.0", monitors=1, at=AT).priority == "low"

    def test_one_monitor_is_not_plural(self) -> None:
        assert (
            "Watching 1 monitor\n" in startup_notification(version="1.1.0", monitors=1, at=AT).body
        )

    def test_it_can_carry_a_link(self) -> None:
        notification = startup_notification(
            version="1.1.0", monitors=3, at=AT, link="http://talaia.lan"
        )

        assert notification.link == "http://talaia.lan"


class TestLowPriorityMapping:
    @respx.mock
    async def test_low_maps_to_ntfy_priority_two(self, notifier: NtfyNotifier) -> None:
        route = respx.post(f"{SERVER}/").mock(return_value=httpx.Response(200))

        await notifier.send(startup_notification(version="1.1.0", monitors=2, at=AT))

        assert json.loads(route.calls.last.request.read())["priority"] == 2
