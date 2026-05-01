"""Add editions.pdf_html column for debug/replay.

Revision ID: 0007_edition_pdf_html
Revises: 0006_calendar_day_summaries
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_edition_pdf_html"
down_revision = "0006_calendar_day_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editions", sa.Column("pdf_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("editions", "pdf_html")
