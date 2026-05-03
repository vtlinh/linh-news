"""Add backdrops JSON column to movies.

Stores landscape (16:9) still URLs fetched from TMDB. The daily edition's
HTML and PDF render one at random above each movie title.

Revision ID: 0013_movie_backdrops
Revises: 0012_drop_calendar_summary_html
Create Date: 2026-05-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_movie_backdrops"
down_revision = "0012_drop_calendar_summary_html"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "movies",
        sa.Column(
            "backdrops",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("movies", "backdrops")
