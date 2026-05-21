"""Add ``user_settings.tts_prefs_json``.

Per-user TTS reader preferences (voice, engine, rate, pitch, volume) for
the in-page karaoke reader.

Revision ID: 0035_tts_prefs
Revises: 0034_weather_phrases_dropnight
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_tts_prefs"
down_revision = "0034_weather_phrases_dropnight"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "tts_prefs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "tts_prefs_json")
