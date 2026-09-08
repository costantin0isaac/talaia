"""Reading and validating the monitor configuration file."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from talaia.config.schema import MonitorsFile


class ConfigError(Exception):
    """Raised when the configuration file cannot be read, parsed or validated."""


def load_config(path: Path) -> MonitorsFile:
    """Read and validate the configuration file at ``path``.

    Raises:
        ConfigError: The file is missing, unreadable, not valid YAML, or does not
            satisfy the schema. The message is safe to log and to return to a caller.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if document is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    try:
        return MonitorsFile.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_format_errors(exc)}") from exc


def _format_errors(exc: ValidationError) -> str:
    """Render a validation error as one indented line per problem."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)
