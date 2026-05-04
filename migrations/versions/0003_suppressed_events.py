"""Add suppressed_events table.

Revision ID: 0003_suppressed_events
Revises: 0002_ical_uid
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_suppressed_events"
down_revision = "0002_ical_uid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppressed_events",
        sa.Column("ical_uid", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("suppressed_events")
