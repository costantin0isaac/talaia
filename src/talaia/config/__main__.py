"""Command-line validation of monitor configuration files.

Usage: python -m talaia.config config/monitors.yaml [...]
"""

import sys
from pathlib import Path

from talaia.config.loader import ConfigError, load_config


def main(argv: list[str]) -> int:
    """Validate every path given, reporting each result. Return 1 on any failure."""
    if not argv:
        print("usage: python -m talaia.config <path> [<path> ...]", file=sys.stderr)
        return 2

    failed = False
    for argument in argv:
        path = Path(argument)
        try:
            config = load_config(path)
        except ConfigError as exc:
            print(f"INVALID  {path}\n{exc}", file=sys.stderr)
            failed = True
        else:
            print(f"ok       {path} ({len(config.resolve())} monitors)")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
