"""Tests for the monitors.yaml schema.

A configuration bug silently breaks monitoring, so these tests cover the boundaries
rather than the happy path alone.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from talaia.config.schema import (
    HttpOptions,
    MonitorsFile,
    MonitorSpec,
    MonitorType,
    TlsOptions,
    split_tls_target,
)


def parse(document: dict[str, Any]) -> MonitorsFile:
    return MonitorsFile.model_validate(document)


class TestDefaults:
    def test_minimal_config_applies_all_defaults(self, minimal_config: dict[str, Any]) -> None:
        monitor = parse(minimal_config).resolve()[0]

        assert monitor.interval == 60
        assert monitor.timeout == 10
        assert monitor.failure_threshold == 3
        assert monitor.recovery_threshold == 2
        assert monitor.enabled is True
        assert monitor.group is None
        assert monitor.http is None

    def test_file_defaults_override_built_in_defaults(self) -> None:
        monitor = parse(
            {
                "defaults": {"interval": 120, "timeout": 30, "failure_threshold": 5},
                "monitors": [{"name": "web", "type": "http", "target": "http://10.0.0.1"}],
            }
        ).resolve()[0]

        assert monitor.interval == 120
        assert monitor.timeout == 30
        assert monitor.failure_threshold == 5
        assert monitor.recovery_threshold == 2

    def test_monitor_overrides_file_defaults(self) -> None:
        monitor = parse(
            {
                "defaults": {"interval": 120, "timeout": 30},
                "monitors": [
                    {
                        "name": "web",
                        "type": "http",
                        "target": "http://10.0.0.1",
                        "interval": 30,
                        "timeout": 5,
                    }
                ],
            }
        ).resolve()[0]

        assert monitor.interval == 30
        assert monitor.timeout == 5

    def test_http_block_defaults(self) -> None:
        monitor = parse(
            {"monitors": [{"name": "w", "type": "http", "target": "http://h", "http": {}}]}
        ).resolve()[0]

        assert monitor.http is not None
        assert monitor.http.method == "GET"
        assert monitor.http.expected_status == [200]
        assert monitor.http.expected_body is None
        assert monitor.http.follow_redirects is False
        assert monitor.http.verify_tls is True
        assert monitor.http.headers == {}

    def test_resolved_monitors_are_immutable(self, minimal_config: dict[str, Any]) -> None:
        monitor = parse(minimal_config).resolve()[0]

        with pytest.raises(ValidationError):
            monitor.target = "http://elsewhere"  # type: ignore[misc]


class TestUnknownKeys:
    def test_typo_in_monitor_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="intervall"):
            parse(
                {
                    "monitors": [
                        {
                            "name": "web",
                            "type": "http",
                            "target": "http://10.0.0.1",
                            "intervall": 30,
                        }
                    ]
                }
            )

    def test_typo_in_defaults_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timeoutt"):
            parse({"defaults": {"timeoutt": 5}, "monitors": []})

    def test_typo_in_http_block_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expected_statuses"):
            parse(
                {
                    "monitors": [
                        {
                            "name": "w",
                            "type": "http",
                            "target": "http://h",
                            "http": {"expected_statuses": [200]},
                        }
                    ]
                }
            )

    def test_unknown_top_level_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="notifications"):
            parse({"notifications": {}, "monitors": []})


class TestNames:
    def test_duplicate_name_is_rejected_and_names_the_collision(self) -> None:
        with pytest.raises(ValidationError, match="duplicate monitor name: 'web'"):
            parse(
                {
                    "monitors": [
                        {"name": "web", "type": "http", "target": "http://10.0.0.1"},
                        {"name": "web", "type": "icmp", "target": "10.0.0.2"},
                    ]
                }
            )

    @pytest.mark.parametrize("name", ["Web", "web_app", "web.app", "web app", "wéb", ""])
    def test_invalid_name_is_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": name, "type": "icmp", "target": "10.0.0.1"}]})

    def test_name_longer_than_64_characters_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": "a" * 65, "type": "icmp", "target": "10.0.0.1"}]})

    @pytest.mark.parametrize("name", ["web", "web-app", "a", "a1-b2-c3", "a" * 64])
    def test_valid_name_is_accepted(self, name: str) -> None:
        assert parse({"monitors": [{"name": name, "type": "icmp", "target": "h"}]}) is not None


class TestTargetMatchesType:
    @pytest.mark.parametrize(
        "target",
        ["http://10.0.0.1", "https://example.com/health", "http://host:8080/x?y=1"],
    )
    def test_valid_http_target(self, target: str) -> None:
        assert parse({"monitors": [{"name": "w", "type": "http", "target": target}]})

    @pytest.mark.parametrize("target", ["10.0.0.1", "example.com", "ftp://example.com", "//x"])
    def test_http_target_without_scheme_is_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError, match="http"):
            parse({"monitors": [{"name": "w", "type": "http", "target": target}]})

    @pytest.mark.parametrize("target", ["10.0.0.2", "example.com", "host", "::1"])
    def test_valid_icmp_target(self, target: str) -> None:
        assert parse({"monitors": [{"name": "w", "type": "icmp", "target": target}]})

    @pytest.mark.parametrize("target", ["http://10.0.0.2", "10.0.0.2:22", "host/path"])
    def test_icmp_target_with_scheme_or_port_is_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": "w", "type": "icmp", "target": target}]})

    @pytest.mark.parametrize("target", ["10.0.0.20:22", "example.com:443", "[::1]:22"])
    def test_valid_tcp_target(self, target: str) -> None:
        assert parse({"monitors": [{"name": "w", "type": "tcp", "target": target}]})

    @pytest.mark.parametrize(
        "target", ["10.0.0.20", "10.0.0.20:", "10.0.0.20:0", "10.0.0.20:70000", "10.0.0.20:ssh"]
    )
    def test_tcp_target_without_valid_port_is_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": "w", "type": "tcp", "target": target}]})

    def test_http_target_with_scheme_but_no_host_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no host"):
            parse({"monitors": [{"name": "w", "type": "http", "target": "http:///path"}]})

    @pytest.mark.parametrize("target", ["-bad-", "host..name", "a b"])
    def test_icmp_target_that_is_not_a_hostname_is_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError, match="not a valid hostname"):
            parse({"monitors": [{"name": "w", "type": "icmp", "target": target}]})

    @pytest.mark.parametrize("target", ["-bad-:22", "host..name:22"])
    def test_tcp_target_with_invalid_host_is_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError, match="invalid host"):
            parse({"monitors": [{"name": "w", "type": "tcp", "target": target}]})

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": "w", "type": "dns", "target": "example.com"}]})


class TestTypeSpecificBlocks:
    @pytest.mark.parametrize("monitor_type", ["icmp", "tcp"])
    def test_http_block_on_non_http_monitor_is_rejected(self, monitor_type: str) -> None:
        target = "10.0.0.1" if monitor_type == "icmp" else "10.0.0.1:22"
        with pytest.raises(ValidationError, match="only valid for type 'http'"):
            parse(
                {
                    "monitors": [
                        {
                            "name": "w",
                            "type": monitor_type,
                            "target": target,
                            "http": {"method": "GET"},
                        }
                    ]
                }
            )

    def test_http_block_on_http_monitor_is_accepted(self) -> None:
        monitor = parse(
            {
                "monitors": [
                    {
                        "name": "w",
                        "type": "http",
                        "target": "http://h",
                        "http": {"method": "HEAD", "expected_status": [200, 204]},
                    }
                ]
            }
        ).resolve()[0]

        assert monitor.type is MonitorType.HTTP
        assert monitor.http is not None
        assert monitor.http.method == "HEAD"
        assert monitor.http.expected_status == [200, 204]


class TestIntervalAndTimeout:
    @pytest.mark.parametrize(("interval", "timeout"), [(10, 10), (10, 20), (60, 60)])
    def test_timeout_not_less_than_interval_is_rejected(self, interval: int, timeout: int) -> None:
        with pytest.raises(ValidationError, match="must be less than interval"):
            parse(
                {
                    "monitors": [
                        {
                            "name": "w",
                            "type": "icmp",
                            "target": "h",
                            "interval": interval,
                            "timeout": timeout,
                        }
                    ]
                }
            )

    def test_timeout_from_defaults_is_checked_against_overridden_interval(self) -> None:
        with pytest.raises(ValidationError, match="must be less than interval"):
            parse(
                {
                    "defaults": {"timeout": 30},
                    "monitors": [
                        {"name": "w", "type": "icmp", "target": "h", "interval": 20},
                    ],
                }
            )

    def test_interval_below_ten_seconds_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": [{"name": "w", "type": "icmp", "target": "h", "interval": 9}]})

    def test_empty_monitor_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse({"monitors": []})


class TestTlsMonitors:
    def test_a_bare_host_defaults_to_443(self) -> None:
        assert split_tls_target("example.com") == ("example.com", 443)

    def test_an_explicit_port_is_kept(self) -> None:
        assert split_tls_target("example.com:8443") == ("example.com", 8443)

    def test_a_bare_ipv4_address_defaults_to_443(self) -> None:
        assert split_tls_target("10.0.0.1") == ("10.0.0.1", 443)

    def test_a_bracketed_ipv6_address_with_a_port(self) -> None:
        assert split_tls_target("[::1]:8443") == ("::1", 8443)

    @pytest.mark.parametrize("target", ["example.com", "example.com:8443", "10.0.0.1"])
    def test_valid_targets_are_accepted(self, target: str) -> None:
        spec = MonitorSpec(name="cert", type=MonitorType.TLS, target=target)

        assert spec.target == target

    @pytest.mark.parametrize(
        "target",
        ["https://example.com", "example.com/path", "example.com:0", "example.com:70000"],
    )
    def test_invalid_targets_are_rejected(self, target: str) -> None:
        with pytest.raises(ValidationError):
            MonitorSpec(name="cert", type=MonitorType.TLS, target=target)

    def test_a_tls_block_is_accepted_on_a_tls_monitor(self) -> None:
        spec = MonitorSpec(
            name="cert",
            type=MonitorType.TLS,
            target="example.com",
            tls=TlsOptions(warn_days=30),
        )

        assert spec.tls is not None
        assert spec.tls.warn_days == 30

    def test_the_warning_window_defaults_to_a_fortnight(self) -> None:
        assert TlsOptions().warn_days == 14

    @pytest.mark.parametrize("warn_days", [0, -1])
    def test_a_nonsense_warning_window_is_rejected(self, warn_days: int) -> None:
        with pytest.raises(ValidationError):
            TlsOptions(warn_days=warn_days)

    def test_a_tls_block_on_another_type_is_rejected(self) -> None:
        """The same rule that already protects the http block."""
        with pytest.raises(ValidationError, match="only valid for type 'tls'"):
            MonitorSpec(name="ping", type=MonitorType.ICMP, target="10.0.0.1", tls=TlsOptions())

    def test_an_http_block_on_a_tls_monitor_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only valid for type 'http'"):
            MonitorSpec(
                name="cert",
                type=MonitorType.TLS,
                target="example.com",
                http=HttpOptions(),
            )

    def test_the_block_survives_default_resolution(self) -> None:
        config = MonitorsFile(
            monitors=[
                MonitorSpec(
                    name="cert",
                    type=MonitorType.TLS,
                    target="example.com",
                    tls=TlsOptions(warn_days=21),
                )
            ]
        ).resolve()[0]

        assert config.tls is not None
        assert config.tls.warn_days == 21
