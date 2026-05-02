"""Native weather rendering: cache 'Now' observation + persist forecast/alerts on edition.

Revision ID: 0011_weather_native
Revises: 0010_favorite_movies
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_weather_native"
down_revision = "0010_favorite_movies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather_now",
        sa.Column("coords", sa.String(), primary_key=True),
        sa.Column("now_text", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "editions",
        sa.Column("weather_forecast_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "editions",
        sa.Column("weather_alerts_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("editions", "weather_alerts_json")
    op.drop_column("editions", "weather_forecast_json")
    op.drop_table("weather_now")
