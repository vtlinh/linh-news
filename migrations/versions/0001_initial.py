"""Initial schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editions",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "hidden_movies",
        sa.Column("title", sa.String(), primary_key=True),
        sa.Column("hidden_until", sa.Date(), nullable=False),
    )
    op.create_table(
        "hidden_calendars",
        sa.Column("calendar_id", sa.String(), primary_key=True),
        sa.Column("calendar_name", sa.String(), nullable=False),
    )
    op.create_table(
        "important_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_table(
        "google_oauth",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for t in (
        "sessions",
        "google_oauth",
        "important_events",
        "hidden_calendars",
        "hidden_movies",
        "editions",
    ):
        op.drop_table(t)
