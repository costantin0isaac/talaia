import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_real_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _is_local(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        return str(address[0]) in _LOCAL_HOSTS
    return False


@pytest.fixture(autouse=True)
def block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any test that opens a non-local socket without mocking it."""
    if request.node.get_closest_marker("integration"):
        yield
        return

    def guard(target: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_local(address):
            return _real_connect(target, address, *args, **kwargs)
        pytest.fail(f"unmocked network access to {address!r}")

    def guard_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_local(address):
            return _real_create_connection(address, *args, **kwargs)
        pytest.fail(f"unmocked network access to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard_create_connection)
    yield
