"""Add calendar_day_summaries table.

Revision ID: 0006_calendar_day_summaries
Revises: 0005_watchlist_stocks
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_calendar_day_summaries"
down_revision = "0005_watchlist_stocks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_day_summaries",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("summary_html", sa.Text(), nullable=False),
        sa.Column("event_fingerprint", sa.String(64), nullable=False),
        sa.Column("events_json", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("calendar_day_summaries")
