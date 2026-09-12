"""The contract every notifier implements, and the messages they carry."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from talaia.formatting import format_duration, format_timestamp

Priority = Literal["default", "high"]

DOWN_TAG = "rotating_light"
UP_TAG = "white_check_mark"


@dataclass(frozen=True, slots=True)
class Notification:
    """One rendered message, in terms a notifier can deliver."""

    title: str
    body: str
    priority: Priority
    tag: str
    link: str | None = None


class Notifier(Protocol):
    """Delivers notifications.

    Implementations never raise: a notification that cannot be delivered is logged and
    dropped, because a failing notifier must never disturb the checking it reports on.
    """

    async def send(self, notification: Notification) -> None:
        """Deliver one notification."""
        ...

    async def aclose(self) -> None:
        """Release whatever the notifier holds open."""
        ...


class NullNotifier:
    """The notifier used when no topic is configured."""

    async def send(self, notification: Notification) -> None:
        """Discard the notification."""
        return None

    async def aclose(self) -> None:
        """Nothing to close."""
        return None


def down_notification(
    *,
    monitor: str,
    target: str,
    error: str | None,
    at: datetime,
    link: str | None = None,
) -> Notification:
    """Build the message announcing that a monitor has gone down."""
    body = "\n".join(
        [
            f"Target: {target}",
            f"Error: {error or 'check failed'}",
            f"Since: {format_timestamp(at)}",
        ]
    )
    return Notification(
        title=f"🔴 {monitor} is DOWN",
        body=body,
        priority="high",
        tag=DOWN_TAG,
        link=link,
    )


def up_notification(
    *,
    monitor: str,
    downtime_seconds: int | None,
    at: datetime,
    link: str | None = None,
) -> Notification:
    """Build the message announcing that a monitor has recovered."""
    downtime = (
        f"Down for {format_duration(downtime_seconds)}"
        if downtime_seconds is not None
        else "Downtime unknown"
    )
    body = "\n".join([downtime, f"Recovered: {format_timestamp(at)}"])
    return Notification(
        title=f"🟢 {monitor} recovered",
        body=body,
        priority="default",
        tag=UP_TAG,
        link=link,
    )
