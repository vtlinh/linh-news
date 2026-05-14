"""Hourly NWS forecast cache + grid lookup + severe-overnight phrases.

Adds two tables:

* ``weather_grid`` — caches the ``/points/{lat,lon}`` → ``(gridId, gridX,
  gridY)`` resolution per coords, so subsequent runs skip the lookup and
  hit ``/gridpoints/{id}/{x},{y}/forecast/hourly`` directly.
* ``weather_hourly`` — one row per (coords, start_at) hour. Each
  generation upserts ~156 future hours and prunes rows older than 14
  days. This rolling cache lets the strip renderer reconstruct any day
  within roughly ±7 days from the DB alone.

Also seeds 40 ``period="night"`` rows into ``weather_phrases`` (20 each
for the ``thunderstorm`` and ``snow`` buckets), used as the severe-
overnight clause appended to the today / tomorrow prose paragraph.

Revision ID: 0033_weather_hourly
Revises: 0032_temp_unit_default_f
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.weather_prose import night_seed_rows

revision = "0033_weather_hourly"
down_revision = "0032_temp_unit_default_f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather_grid",
        sa.Column("coords", sa.String(), primary_key=True),
        sa.Column("grid_id", sa.String(), nullable=False),
        sa.Column("grid_x", sa.Integer(), nullable=False),
        sa.Column("grid_y", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "weather_hourly",
        sa.Column("coords", sa.String(), primary_key=True),
        sa.Column("start_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("temp_c", sa.Integer(), nullable=False),
        sa.Column("short_forecast", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )

    weather_phrases = sa.table(
        "weather_phrases",
        sa.column("period", sa.String()),
        sa.column("bucket", sa.String()),
        sa.column("text", sa.Text()),
    )
    op.bulk_insert(weather_phrases, night_seed_rows())


def downgrade() -> None:
    op.execute("DELETE FROM weather_phrases WHERE period = 'night'")
    op.drop_table("weather_hourly")
    op.drop_table("weather_grid")
