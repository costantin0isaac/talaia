"""The TLS monitor type, and the remaining validity its checker reports.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12 21:12:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES = ("http", "icmp", "tcp", "tls")
PREVIOUS_TYPES = ("http", "icmp", "tcp")

# The bare name; the metadata naming convention expands it to ck_monitors_monitortype.
CONSTRAINT = "monitortype"


def _type_check(values: tuple[str, ...]) -> str:
    """Render the IN list that the VARCHAR-backed enum is constrained by."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"type IN ({allowed})"


def upgrade() -> None:
    op.add_column(
        "monitor_states",
        sa.Column("last_expires_in_days", sa.Integer(), nullable=True),
    )
    # The monitor type is a VARCHAR with a CHECK, not a native enum, so admitting a new
    # value means rewriting the constraint rather than altering a type.
    op.drop_constraint(CONSTRAINT, "monitors", type_="check")
    op.create_check_constraint(CONSTRAINT, "monitors", sa.text(_type_check(TYPES)))


def downgrade() -> None:
    op.drop_column("monitor_states", "last_expires_in_days")
    op.execute("DELETE FROM monitors WHERE type = 'tls'")
    op.drop_constraint(CONSTRAINT, "monitors", type_="check")
    op.create_check_constraint(CONSTRAINT, "monitors", sa.text(_type_check(PREVIOUS_TYPES)))
