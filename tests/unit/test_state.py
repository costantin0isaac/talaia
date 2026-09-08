"""Tests for the UP/DOWN state machine.

This is the logical heart of the application: it decides when an incident opens and when
a notification is sent. It is pure, so every rule is asserted directly.
"""

import pytest

from talaia.db.models import MonitorStatus
from talaia.engine.state import StateSnapshot, Transition, evaluate

FAILURE_THRESHOLD = 3
RECOVERY_THRESHOLD = 2


def run(
    outcomes: str,
    *,
    start: MonitorStatus = MonitorStatus.UNKNOWN,
    failure_threshold: int = FAILURE_THRESHOLD,
    recovery_threshold: int = RECOVERY_THRESHOLD,
) -> tuple[StateSnapshot, list[Transition]]:
    """Feed a string of 's'uccesses and 'f'ailures through the machine."""
    snapshot = StateSnapshot(status=start)
    transitions: list[Transition] = []
    for character in outcomes:
        change = evaluate(
            snapshot,
            success=character == "s",
            failure_threshold=failure_threshold,
            recovery_threshold=recovery_threshold,
        )
        snapshot = change.current
        if change.transition is not Transition.NONE:
            transitions.append(change.transition)
    return snapshot, transitions


class TestThresholds:
    @pytest.mark.parametrize(
        ("outcomes", "expected_status", "expected_transitions"),
        [
            ("", MonitorStatus.UNKNOWN, []),
            ("f", MonitorStatus.UNKNOWN, []),
            ("ff", MonitorStatus.UNKNOWN, []),
            ("fff", MonitorStatus.DOWN, [Transition.TO_DOWN]),
            ("ffff", MonitorStatus.DOWN, [Transition.TO_DOWN]),
            ("s", MonitorStatus.UNKNOWN, []),
            ("ss", MonitorStatus.UP, [Transition.TO_UP]),
            ("sss", MonitorStatus.UP, [Transition.TO_UP]),
            ("ffs", MonitorStatus.UNKNOWN, []),
            ("ffsff", MonitorStatus.UNKNOWN, []),
            ("ffsfff", MonitorStatus.DOWN, [Transition.TO_DOWN]),
            ("ssfff", MonitorStatus.DOWN, [Transition.TO_UP, Transition.TO_DOWN]),
            ("ssfffss", MonitorStatus.UP, [Transition.TO_UP, Transition.TO_DOWN, Transition.TO_UP]),
            ("fffs", MonitorStatus.DOWN, [Transition.TO_DOWN]),
            ("fffss", MonitorStatus.UP, [Transition.TO_DOWN, Transition.TO_UP]),
        ],
    )
    def test_transition_table(
        self,
        outcomes: str,
        expected_status: MonitorStatus,
        expected_transitions: list[Transition],
    ) -> None:
        snapshot, transitions = run(outcomes)

        assert snapshot.status is expected_status
        assert transitions == expected_transitions

    def test_two_failures_do_not_transition_but_the_third_does(self) -> None:
        assert run("ff")[1] == []
        assert run("fff")[1] == [Transition.TO_DOWN]

    def test_one_success_mid_streak_resets_the_failure_counter(self) -> None:
        snapshot, transitions = run("ffs")

        assert snapshot.consecutive_failures == 0
        assert snapshot.consecutive_successes == 1
        assert transitions == []

    def test_one_failure_mid_streak_resets_the_success_counter(self) -> None:
        snapshot, _ = run("sf")

        assert snapshot.consecutive_successes == 0
        assert snapshot.consecutive_failures == 1

    def test_recovery_requires_the_configured_number_of_successes(self) -> None:
        assert run("fffs")[0].status is MonitorStatus.DOWN
        assert run("fffss")[0].status is MonitorStatus.UP

    @pytest.mark.parametrize("threshold", [1, 2, 5])
    def test_failure_threshold_is_honoured(self, threshold: int) -> None:
        before = "f" * (threshold - 1)
        assert run(before, failure_threshold=threshold)[1] == []
        assert run(before + "f", failure_threshold=threshold)[1] == [Transition.TO_DOWN]

    @pytest.mark.parametrize("threshold", [1, 2, 5])
    def test_recovery_threshold_is_honoured(self, threshold: int) -> None:
        before = "s" * (threshold - 1)
        assert run(before, recovery_threshold=threshold)[1] == []
        assert run(before + "s", recovery_threshold=threshold)[1] == [Transition.TO_UP]


class TestIdempotence:
    def test_a_monitor_already_down_does_not_open_a_second_incident(self) -> None:
        snapshot = StateSnapshot(status=MonitorStatus.DOWN, consecutive_failures=9)

        change = evaluate(
            snapshot, success=False, failure_threshold=FAILURE_THRESHOLD, recovery_threshold=2
        )

        assert change.transition is Transition.NONE
        assert change.opens_incident is False
        assert change.notifies is False
        assert change.current.consecutive_failures == 10

    def test_a_monitor_already_up_does_not_transition_again(self) -> None:
        snapshot = StateSnapshot(status=MonitorStatus.UP, consecutive_successes=9)

        change = evaluate(
            snapshot, success=True, failure_threshold=3, recovery_threshold=RECOVERY_THRESHOLD
        )

        assert change.transition is Transition.NONE
        assert change.notifies is False

    def test_six_hours_down_produces_exactly_one_incident(self) -> None:
        """A long outage must not produce a stream of notifications."""
        _, transitions = run("f" * 360)

        assert transitions == [Transition.TO_DOWN]


class TestNotificationRules:
    def test_unknown_to_up_is_silent(self) -> None:
        change = evaluate(
            StateSnapshot(status=MonitorStatus.UNKNOWN, consecutive_successes=1),
            success=True,
            failure_threshold=3,
            recovery_threshold=2,
        )

        assert change.transition is Transition.TO_UP
        assert change.notifies is False
        assert change.resolves_incident is False

    def test_unknown_to_down_notifies(self) -> None:
        change = evaluate(
            StateSnapshot(status=MonitorStatus.UNKNOWN, consecutive_failures=2),
            success=False,
            failure_threshold=3,
            recovery_threshold=2,
        )

        assert change.transition is Transition.TO_DOWN
        assert change.notifies is True
        assert change.opens_incident is True

    def test_down_to_up_notifies_and_resolves(self) -> None:
        change = evaluate(
            StateSnapshot(status=MonitorStatus.DOWN, consecutive_successes=1),
            success=True,
            failure_threshold=3,
            recovery_threshold=2,
        )

        assert change.transition is Transition.TO_UP
        assert change.resolves_incident is True
        assert change.notifies is True

    def test_up_to_down_notifies_and_opens(self) -> None:
        change = evaluate(
            StateSnapshot(status=MonitorStatus.UP, consecutive_failures=2),
            success=False,
            failure_threshold=3,
            recovery_threshold=2,
        )

        assert change.opens_incident is True
        assert change.notifies is True


class TestPurity:
    def test_the_input_snapshot_is_never_mutated(self) -> None:
        snapshot = StateSnapshot(status=MonitorStatus.UP, consecutive_successes=4)

        evaluate(snapshot, success=False, failure_threshold=3, recovery_threshold=2)

        assert snapshot == StateSnapshot(status=MonitorStatus.UP, consecutive_successes=4)

    def test_evaluate_is_deterministic(self) -> None:
        snapshot = StateSnapshot(status=MonitorStatus.UP, consecutive_failures=2)
        kwargs = {"success": False, "failure_threshold": 3, "recovery_threshold": 2}

        assert evaluate(snapshot, **kwargs) == evaluate(snapshot, **kwargs)  # type: ignore[arg-type]
