"""Rewrite ``weather_phrases`` today/tomorrow rows to drop "overnight" /
"tonight" wording.

The ``{l}`` slot in those rows is now the *daytime* low (min of the
7 AM–10 PM hourly window), not the overnight low — so prose that says
"low of {l}°C overnight" is factually wrong. This migration rewrites
the seed library in place, replacing the existing 360 ``period IN
('today','tomorrow')`` rows with the current contents of
:data:`app.weather_prose.PHRASES`.

The ``period='night'`` rows added in 0033 are untouched.

Revision ID: 0034_weather_phrases_dropnight
Revises: 0033_weather_hourly
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.weather_prose import all_seed_rows

revision = "0034_weather_phrases_dropnight"
down_revision = "0033_weather_hourly"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Wipe the day/tomorrow rows and reseed from the current PHRASES table.
    op.execute("DELETE FROM weather_phrases WHERE period IN ('today', 'tomorrow')")
    weather_phrases = sa.table(
        "weather_phrases",
        sa.column("period", sa.String()),
        sa.column("bucket", sa.String()),
        sa.column("text", sa.Text()),
    )
    op.bulk_insert(weather_phrases, all_seed_rows())


def downgrade() -> None:
    # Re-seed is a one-way operation — there is no record of the pre-0034
    # wording in this module after the upgrade lands. Leave the data as-is
    # on downgrade; the renderer still functions, just without the old
    # "overnight" phrasing.
    pass
