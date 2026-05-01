"""Add watchlist_stocks table.

Revision ID: 0005_watchlist_stocks
Revises: 0004_kv_cache
Create Date: 2026-04-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_watchlist_stocks"
down_revision = "0004_kv_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist_stocks",
        sa.Column("symbol", sa.String(), primary_key=True),
    )
    # Seed with the previously-hardcoded watchlist so existing behaviour is
    # preserved.
    op.bulk_insert(
        sa.table("watchlist_stocks", sa.column("symbol", sa.String())),
        [{"symbol": s} for s in ("GOOG", "SMCI", "NVDA", "TSLA")],
    )


def downgrade() -> None:
    op.drop_table("watchlist_stocks")
