"""Add favorite_movies table.

Revision ID: 0010_favorite_movies
Revises: 0009_movies
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_favorite_movies"
down_revision = "0009_movies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "favorite_movies",
        sa.Column("title", sa.String(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("favorite_movies")
