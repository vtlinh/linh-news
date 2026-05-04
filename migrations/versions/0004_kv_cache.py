"""Add kv_cache table.

Revision ID: 0004_kv_cache
Revises: 0003_suppressed_events
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_kv_cache"
down_revision = "0003_suppressed_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kv_cache",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kv_cache")
