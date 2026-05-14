"""Flip ``user_settings.temperature_unit`` default to ``F``.

The column was added in 0031 with a server-default of ``"C"``, which
backfilled every existing row to Celsius. We've since decided that
Fahrenheit is the better default for this app's audience. This migration
changes the server-default to ``"F"`` for future inserts and updates
every row that's still on the original ``"C"`` backfill — users who
explicitly chose Celsius via the UI saved before this migration are
indistinguishable from the backfilled rows, so this is a one-time bulk
flip rather than a per-user opt-in.

Revision ID: 0032_temp_unit_default_f
Revises: 0031_us_temperature_unit
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_temp_unit_default_f"
down_revision = "0031_us_temperature_unit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_settings",
        "temperature_unit",
        server_default="F",
        existing_type=sa.String(),
        existing_nullable=False,
    )
    op.execute("UPDATE user_settings SET temperature_unit = 'F' WHERE temperature_unit = 'C'")


def downgrade() -> None:
    op.alter_column(
        "user_settings",
        "temperature_unit",
        server_default="C",
        existing_type=sa.String(),
        existing_nullable=False,
    )
