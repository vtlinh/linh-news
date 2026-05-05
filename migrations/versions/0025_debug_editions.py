"""Debug copy of ``content_json`` retained when PDF generation fails.

A row is written here as soon as the LLM call returns a valid structured
``LinhNews`` dict. If the rest of the pipeline succeeds (``_upsert_edition``
runs to completion), the row is deleted. If anything between LLM-success and
upsert fails (PDF overflow → placeholder, WeasyPrint error, DB error), the
row stays so the failed run can be replayed without another LLM call.

``expires_at`` gives a 3-day TTL — the next generation purges anything past
its expiry, so the table never grows unbounded.

Revision ID: 0025_debug_editions
Revises: 0024_per_user_cals
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_debug_editions"
down_revision = "0024_per_user_cals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "debug_editions",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_debug_editions_expires_at",
        "debug_editions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_debug_editions_expires_at", table_name="debug_editions")
    op.drop_table("debug_editions")
