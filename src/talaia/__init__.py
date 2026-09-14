"""Talaia — a self-hosted uptime monitor for a homelab."""

from importlib.metadata import version

# Read from the installed package metadata rather than repeated here, so pyproject.toml is
# the only place the number is written and the two cannot drift apart.
__version__ = version("talaia")
