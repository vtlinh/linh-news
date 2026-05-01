"""Add event_emojis table.

Revision ID: 0008_event_emojis
Revises: 0007_edition_pdf_html
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_event_emojis"
down_revision = "0007_edition_pdf_html"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_emojis",
        sa.Column("title_norm", sa.String(), primary_key=True),
        sa.Column("emoji", sa.String(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("event_emojis")
