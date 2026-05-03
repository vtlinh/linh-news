"""Drop summary_html and event_fingerprint from calendar_day_summaries.

Calendar HTML is now rendered inline at view time from events_json + the
event_emojis map; the cached HTML and its fingerprint are no longer used.

Revision ID: 0012_drop_calendar_summary_html
Revises: 0011_weather_native
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_drop_calendar_summary_html"
down_revision = "0011_weather_native"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("calendar_day_summaries", "summary_html")
    op.drop_column("calendar_day_summaries", "event_fingerprint")


def downgrade() -> None:
    op.add_column(
        "calendar_day_summaries",
        sa.Column("event_fingerprint", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "calendar_day_summaries",
        sa.Column("summary_html", sa.Text(), nullable=False, server_default=""),
    )
