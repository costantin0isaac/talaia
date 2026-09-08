"""The UP/DOWN state machine.

Pure functions over a snapshot of a monitor's state. Nothing here touches the database,
the clock or the network, so every rule is directly testable.
"""

from dataclasses import dataclass
from enum import StrEnum

from talaia.db.models import MonitorStatus


class Transition(StrEnum):
    """The kind of status change a check produced."""

    NONE = "none"
    TO_UP = "to_up"
    TO_DOWN = "to_down"


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """A monitor's status and its consecutive-result counters."""

    status: MonitorStatus = MonitorStatus.UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0


@dataclass(frozen=True, slots=True)
class StateChange:
    """The result of applying one check outcome to a snapshot."""

    previous: StateSnapshot
    current: StateSnapshot
    transition: Transition

    @property
    def opens_incident(self) -> bool:
        """Whether this change starts a new incident."""
        return self.transition is Transition.TO_DOWN

    @property
    def resolves_incident(self) -> bool:
        """Whether this change closes the open incident."""
        return self.transition is Transition.TO_UP and self.previous.status is MonitorStatus.DOWN

    @property
    def notifies(self) -> bool:
        """Whether this change should produce a notification.

        Coming up from ``unknown`` is startup, not an event, so it is silent. Going down
        from ``unknown`` is a real event and is announced.
        """
        return self.opens_incident or self.resolves_incident


def evaluate(
    snapshot: StateSnapshot,
    *,
    success: bool,
    failure_threshold: int,
    recovery_threshold: int,
) -> StateChange:
    """Apply one check outcome to ``snapshot`` and return the resulting change."""
    if success:
        current = StateSnapshot(
            status=snapshot.status,
            consecutive_failures=0,
            consecutive_successes=snapshot.consecutive_successes + 1,
        )
        crossed = current.consecutive_successes >= recovery_threshold
        if snapshot.status is not MonitorStatus.UP and crossed:
            return StateChange(
                previous=snapshot,
                current=StateSnapshot(
                    status=MonitorStatus.UP,
                    consecutive_failures=0,
                    consecutive_successes=current.consecutive_successes,
                ),
                transition=Transition.TO_UP,
            )
        return StateChange(previous=snapshot, current=current, transition=Transition.NONE)

    current = StateSnapshot(
        status=snapshot.status,
        consecutive_failures=snapshot.consecutive_failures + 1,
        consecutive_successes=0,
    )
    crossed = current.consecutive_failures >= failure_threshold
    if snapshot.status is not MonitorStatus.DOWN and crossed:
        return StateChange(
            previous=snapshot,
            current=StateSnapshot(
                status=MonitorStatus.DOWN,
                consecutive_failures=current.consecutive_failures,
                consecutive_successes=0,
            ),
            transition=Transition.TO_DOWN,
        )
    return StateChange(previous=snapshot, current=current, transition=Transition.NONE)
