"""The contract every checker implements."""

from dataclasses import dataclass
from typing import Protocol

from talaia.config.schema import MonitorConfig

MAX_ERROR_LENGTH = 200


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The result of a single check."""

    success: bool
    latency_ms: int | None
    status_code: int | None = None
    error: str | None = None
    expires_in_days: int | None = None


class Checker(Protocol):
    """Performs one check against a monitor's target.

    Implementations never raise: any exception becomes an unsuccessful outcome. An
    exception escaping here would kill the monitor's scheduler task and silently stop
    checking that target.
    """

    async def check(self, monitor: MonitorConfig) -> CheckOutcome:
        """Check ``monitor`` and return the outcome."""
        ...


def failure(
    error: str, latency_ms: int | None = None, status_code: int | None = None
) -> CheckOutcome:
    """Build an unsuccessful outcome with a short, human-readable error."""
    return CheckOutcome(
        success=False,
        latency_ms=latency_ms,
        status_code=status_code,
        error=error[:MAX_ERROR_LENGTH],
    )
