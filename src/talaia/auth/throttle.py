"""Slowing down repeated failed logins.

argon2 already makes each guess cost about 50 ms, which is a speed bump rather than a
wall: left alone, an attacker on the LAN can still try a thousand passwords an hour. This
adds a per-client budget of failures, after which attempts are refused outright for a
window that doubles with each further failure.

Held in memory, not in the database. A single process owns every login, the state is worth
nothing after a restart, and a table would turn each attempt into a write — which is
exactly the amplification an attacker would be aiming for.

Keyed on the client address, so it does not slow the real user down when someone else is
guessing, and does not let one attacker lock a username out of their own account. The
trade-off is that guesses spread across many addresses are not slowed; defending that
needs something that can see the whole network, not one application.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]

# Attempts older than this are forgotten, so an occasional typo never accumulates into a
# lockout over days.
WINDOW = timedelta(minutes=15)


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


@dataclass
class _Attempts:
    """One client's recent failures."""

    failures: int = 0
    last_failure: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether an attempt may proceed, and how long until it may."""

    allowed: bool
    retry_after: int = 0


class LoginThrottle:
    """Counts failed logins per client and refuses attempts once the budget is spent."""

    def __init__(
        self,
        *,
        max_attempts: int,
        lockout_seconds: int,
        max_lockout_seconds: int,
        clock: Clock = utc_now,
    ) -> None:
        self._max_attempts = max_attempts
        self._lockout = lockout_seconds
        self._max_lockout = max_lockout_seconds
        self._clock = clock
        self._clients: dict[str, _Attempts] = {}

    def check(self, client: str) -> Decision:
        """Say whether this client may attempt a login right now."""
        self._forget_stale()
        record = self._clients.get(client)
        if record is None or record.failures < self._max_attempts:
            return Decision(allowed=True)

        elapsed = (self._clock() - record.last_failure).total_seconds()
        remaining = self._lockout_for(record.failures) - elapsed
        if remaining <= 0:
            return Decision(allowed=True)
        return Decision(allowed=False, retry_after=max(1, int(remaining)))

    def record_failure(self, client: str) -> None:
        """Note a failed attempt."""
        record = self._clients.setdefault(client, _Attempts(failures=0))
        record.failures += 1
        record.last_failure = self._clock()

    def record_success(self, client: str) -> None:
        """Forget a client's failures; they have proved who they are."""
        self._clients.pop(client, None)

    def _lockout_for(self, failures: int) -> float:
        """Return the lockout length after this many failures, doubling then capped."""
        over = failures - self._max_attempts
        return float(min(self._lockout * (2**over), self._max_lockout))

    def _forget_stale(self) -> None:
        """Drop clients whose lockout has expired, so the map cannot grow without bound."""
        now = self._clock()
        expired = [
            client
            for client, record in self._clients.items()
            if now - record.last_failure > max(WINDOW, timedelta(seconds=self._max_lockout))
        ]
        for client in expired:
            del self._clients[client]
