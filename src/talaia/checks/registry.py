"""Mapping from monitor type to checker instance."""

from talaia.checks.base import Checker
from talaia.checks.http import HttpChecker, HttpClients
from talaia.checks.icmp import IcmpChecker
from talaia.checks.tcp import TcpChecker
from talaia.config.schema import MonitorType


class CheckerRegistry:
    """Holds the checker for each monitor type this build supports."""

    def __init__(self, checkers: dict[MonitorType, Checker]) -> None:
        self._checkers = checkers

    def get(self, monitor_type: MonitorType) -> Checker | None:
        """Return the checker for a type, or None if it is not implemented yet."""
        return self._checkers.get(monitor_type)

    @property
    def supported_types(self) -> frozenset[MonitorType]:
        """The monitor types this registry can check."""
        return frozenset(self._checkers)


def build_registry(clients: HttpClients) -> CheckerRegistry:
    """Build the registry covering every monitor type."""
    return CheckerRegistry(
        {
            MonitorType.HTTP: HttpChecker(clients),
            MonitorType.ICMP: IcmpChecker(),
            MonitorType.TCP: TcpChecker(),
        }
    )
