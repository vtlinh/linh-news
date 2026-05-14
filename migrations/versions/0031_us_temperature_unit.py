"""Add ``user_settings.temperature_unit`` (C / F).

Per-user preference for weather temperature display in both HTML and PDF
editions. NWS data is fetched in Celsius regardless; the renderer
converts to °F at display time when the user has chosen "F".

Revision ID: 0031_us_temperature_unit
Revises: 0030_google_oauth_revoked_at
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_us_temperature_unit"
down_revision = "0030_google_oauth_revoked_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "temperature_unit",
            sa.String(),
            nullable=False,
            server_default="C",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "temperature_unit")
