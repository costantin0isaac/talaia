"""The login throttle, driven by an injected clock so nothing sleeps."""

from datetime import UTC, datetime, timedelta

import pytest

from talaia.auth.throttle import WINDOW, LoginThrottle

START = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
CLIENT = "10.0.0.5"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def throttle(clock: Clock, *, max_attempts: int = 3, lockout: int = 60) -> LoginThrottle:
    return LoginThrottle(
        max_attempts=max_attempts,
        lockout_seconds=lockout,
        max_lockout_seconds=900,
        clock=clock,
    )


class TestBudget:
    def test_an_unknown_client_is_allowed(self, clock: Clock) -> None:
        assert throttle(clock).check(CLIENT).allowed is True

    def test_failures_below_the_budget_are_allowed(self, clock: Clock) -> None:
        limiter = throttle(clock)

        for _ in range(2):
            limiter.record_failure(CLIENT)

        assert limiter.check(CLIENT).allowed is True

    def test_the_budget_is_spent_at_the_limit(self, clock: Clock) -> None:
        limiter = throttle(clock)

        for _ in range(3):
            limiter.record_failure(CLIENT)

        decision = limiter.check(CLIENT)
        assert decision.allowed is False
        assert decision.retry_after == 60

    def test_a_success_forgets_the_failures(self, clock: Clock) -> None:
        """A real user who mistypes twice and then succeeds starts clean."""
        limiter = throttle(clock)
        for _ in range(2):
            limiter.record_failure(CLIENT)

        limiter.record_success(CLIENT)
        for _ in range(2):
            limiter.record_failure(CLIENT)

        assert limiter.check(CLIENT).allowed is True

    def test_clients_are_counted_separately(self, clock: Clock) -> None:
        """One attacker must not lock everyone else out."""
        limiter = throttle(clock)
        for _ in range(5):
            limiter.record_failure("10.0.0.99")

        assert limiter.check(CLIENT).allowed is True


class TestLockout:
    def test_the_lockout_expires(self, clock: Clock) -> None:
        limiter = throttle(clock)
        for _ in range(3):
            limiter.record_failure(CLIENT)

        clock.advance(61)

        assert limiter.check(CLIENT).allowed is True

    def test_retry_after_counts_down(self, clock: Clock) -> None:
        limiter = throttle(clock)
        for _ in range(3):
            limiter.record_failure(CLIENT)

        clock.advance(20)

        assert limiter.check(CLIENT).retry_after == 40

    def test_each_further_failure_doubles_the_wait(self, clock: Clock) -> None:
        limiter = throttle(clock)
        for _ in range(3):
            limiter.record_failure(CLIENT)
        assert limiter.check(CLIENT).retry_after == 60

        limiter.record_failure(CLIENT)
        assert limiter.check(CLIENT).retry_after == 120

        limiter.record_failure(CLIENT)
        assert limiter.check(CLIENT).retry_after == 240

    def test_the_wait_is_capped(self, clock: Clock) -> None:
        """Doubling forever would lock an address out for years after a bad afternoon."""
        limiter = throttle(clock)
        for _ in range(20):
            limiter.record_failure(CLIENT)

        assert limiter.check(CLIENT).retry_after == 900

    def test_retry_after_is_never_zero_while_locked(self, clock: Clock) -> None:
        limiter = throttle(clock)
        for _ in range(3):
            limiter.record_failure(CLIENT)

        clock.advance(59.9)

        assert limiter.check(CLIENT).retry_after >= 1


class TestForgetting:
    def test_a_stale_client_is_dropped(self, clock: Clock) -> None:
        """The map must not grow for every address that ever mistyped once."""
        limiter = throttle(clock)
        limiter.record_failure(CLIENT)

        clock.advance(WINDOW.total_seconds() + 901)
        limiter.check("someone-else")

        assert CLIENT not in limiter._clients

    def test_a_locked_client_is_not_dropped_early(self, clock: Clock) -> None:
        limiter = throttle(clock)
        for _ in range(3):
            limiter.record_failure(CLIENT)

        clock.advance(30)
        limiter.check("someone-else")

        assert limiter.check(CLIENT).allowed is False
