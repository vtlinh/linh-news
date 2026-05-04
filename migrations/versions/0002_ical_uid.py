"""Add ical_uid to important_events.

Revision ID: 0002_ical_uid
Revises: 0001_initial
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_ical_uid"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("important_events") as batch:
        batch.add_column(sa.Column("ical_uid", sa.String(), nullable=True))
        batch.create_index("ix_important_events_ical_uid", ["ical_uid"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("important_events") as batch:
        batch.drop_index("ix_important_events_ical_uid")
        batch.drop_column("ical_uid")
