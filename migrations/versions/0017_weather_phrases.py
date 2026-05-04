"""Hand-written weather prose library: 9 buckets x 2 periods x 20 variants.

Revision ID: 0017_weather_phrases
Revises: 0016_edition_pdf_rail
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.weather_prose import all_seed_rows

revision = "0017_weather_phrases"
down_revision = "0016_edition_pdf_rail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    weather_phrases = op.create_table(
        "weather_phrases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_weather_phrases_period_bucket",
        "weather_phrases",
        ["period", "bucket"],
    )
    op.bulk_insert(weather_phrases, all_seed_rows())


def downgrade() -> None:
    op.drop_index("ix_weather_phrases_period_bucket", table_name="weather_phrases")
    op.drop_table("weather_phrases")
