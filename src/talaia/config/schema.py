"""Pydantic models describing the contents of ``monitors.yaml``."""

import ipaddress
import re
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_PATTERN = r"^[a-z0-9-]+$"
HOSTNAME_PATTERN = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)

MIN_INTERVAL_SECONDS = 10
MIN_PORT = 1
MAX_PORT = 65535


class MonitorType(StrEnum):
    """The kind of check a monitor performs."""

    HTTP = "http"
    ICMP = "icmp"
    TCP = "tcp"


class StrictModel(BaseModel):
    """Base model that rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class HttpOptions(StrictModel):
    """The ``http:`` block of an HTTP monitor."""

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] = "GET"
    expected_status: Annotated[list[int], Field(min_length=1)] = [200]
    expected_body: str | None = None
    follow_redirects: bool = False
    verify_tls: bool = True
    headers: dict[str, str] = {}


class Defaults(StrictModel):
    """Values applied to any monitor that does not override them."""

    interval: int = Field(default=60, ge=MIN_INTERVAL_SECONDS)
    timeout: int = Field(default=10, ge=1)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_threshold: int = Field(default=2, ge=1)


class MonitorSpec(StrictModel):
    """One entry of the ``monitors:`` list, exactly as written in the file."""

    name: str = Field(max_length=64, pattern=NAME_PATTERN)
    type: MonitorType
    target: str
    group: str | None = Field(default=None, max_length=64)
    description: str | None = None
    interval: int | None = Field(default=None, ge=MIN_INTERVAL_SECONDS)
    timeout: int | None = Field(default=None, ge=1)
    failure_threshold: int | None = Field(default=None, ge=1)
    recovery_threshold: int | None = Field(default=None, ge=1)
    enabled: bool = True
    http: HttpOptions | None = None

    @model_validator(mode="after")
    def _check_target_matches_type(self) -> Self:
        """Require the target to have the shape implied by the monitor type."""
        validators = {
            MonitorType.HTTP: _validate_http_target,
            MonitorType.ICMP: _validate_icmp_target,
            MonitorType.TCP: _validate_tcp_target,
        }
        error = validators[self.type](self.target)
        if error is not None:
            raise ValueError(f"monitor {self.name!r}: {error}")
        return self

    @model_validator(mode="after")
    def _check_type_specific_block(self) -> Self:
        """Allow a type-specific block only on a monitor of that type."""
        if self.http is not None and self.type is not MonitorType.HTTP:
            raise ValueError(
                f"monitor {self.name!r}: an 'http:' block is only valid for type 'http', "
                f"not {self.type.value!r}"
            )
        return self


class MonitorConfig(StrictModel):
    """A monitor with the file defaults already applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: MonitorType
    target: str
    group: str | None
    description: str | None
    interval: int
    timeout: int
    failure_threshold: int
    recovery_threshold: int
    enabled: bool
    http: HttpOptions | None


class MonitorsFile(StrictModel):
    """The whole configuration file."""

    defaults: Defaults = Defaults()
    monitors: Annotated[list[MonitorSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_names_are_unique(self) -> Self:
        """Reject a name used by more than one monitor."""
        seen: set[str] = set()
        for spec in self.monitors:
            if spec.name in seen:
                raise ValueError(f"duplicate monitor name: {spec.name!r}")
            seen.add(spec.name)
        return self

    @model_validator(mode="after")
    def _check_timeout_below_interval(self) -> Self:
        """Reject a timeout that is not shorter than the interval it runs within."""
        for monitor in self.resolve():
            if monitor.timeout >= monitor.interval:
                raise ValueError(
                    f"monitor {monitor.name!r}: timeout ({monitor.timeout}s) must be less "
                    f"than interval ({monitor.interval}s)"
                )
        return self

    def resolve(self) -> list[MonitorConfig]:
        """Return the monitors with the file defaults applied."""
        return [_apply_defaults(spec, self.defaults) for spec in self.monitors]


def _apply_defaults(spec: MonitorSpec, defaults: Defaults) -> MonitorConfig:
    """Merge a monitor's overrides over the file defaults."""
    return MonitorConfig(
        name=spec.name,
        type=spec.type,
        target=spec.target,
        group=spec.group,
        description=spec.description,
        interval=spec.interval if spec.interval is not None else defaults.interval,
        timeout=spec.timeout if spec.timeout is not None else defaults.timeout,
        failure_threshold=(
            spec.failure_threshold
            if spec.failure_threshold is not None
            else defaults.failure_threshold
        ),
        recovery_threshold=(
            spec.recovery_threshold
            if spec.recovery_threshold is not None
            else defaults.recovery_threshold
        ),
        enabled=spec.enabled,
        http=spec.http,
    )


def _validate_http_target(target: str) -> str | None:
    """Return an error message if the target is not an absolute HTTP(S) URL."""
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        return f"type 'http' needs a target starting with http:// or https://, got {target!r}"
    if not parsed.netloc:
        return f"target {target!r} has no host"
    return None


def _validate_icmp_target(target: str) -> str | None:
    """Return an error message if the target is not a bare hostname or IP address."""
    if "://" in target or "/" in target:
        return f"type 'icmp' needs a bare hostname or IP address, got {target!r}"
    if ":" in target and not _is_ip_address(target):
        return f"type 'icmp' does not take a port, got {target!r}"
    if _is_ip_address(target) or HOSTNAME_PATTERN.match(target):
        return None
    return f"target {target!r} is not a valid hostname or IP address"


def _validate_tcp_target(target: str) -> str | None:
    """Return an error message if the target is not ``host:port``."""
    host, separator, port = target.rpartition(":")
    if not separator or not host:
        return f"type 'tcp' needs a target of the form host:port, got {target!r}"
    if not port.isdigit() or not MIN_PORT <= int(port) <= MAX_PORT:
        return f"target {target!r} has an invalid port; expected {MIN_PORT}-{MAX_PORT}"
    host = host.strip("[]")
    if _is_ip_address(host) or HOSTNAME_PATTERN.match(host):
        return None
    return f"target {target!r} has an invalid host"


def _is_ip_address(value: str) -> bool:
    """Whether the value parses as an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True
