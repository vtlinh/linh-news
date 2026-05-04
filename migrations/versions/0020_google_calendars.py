"""Cache table for the user's Google Calendar list.

Revision ID: 0020_google_calendars
Revises: 0019_user_children
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_google_calendars"
down_revision = "0019_user_children"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendars",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("google_calendars")
