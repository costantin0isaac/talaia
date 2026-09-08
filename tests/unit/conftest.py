from typing import Any

import pytest


@pytest.fixture
def minimal_config() -> dict[str, Any]:
    """Return the smallest configuration that validates."""
    return {"monitors": [{"name": "web", "type": "http", "target": "http://10.0.0.1"}]}
