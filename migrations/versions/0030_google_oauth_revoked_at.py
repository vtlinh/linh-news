"""Add ``google_oauth.revoked_at`` so we can surface dead refresh tokens.

When Google rejects the stored refresh token with ``invalid_grant`` (user
revoked access, password change, 6-month inactivity), we set this column
instead of deleting the row. ``require_viewer`` treats a revoked row as
missing and routes the user through OAuth re-consent; the admin Users page
shows a "Disconnected" badge so the state is visible.

Revision ID: 0030_google_oauth_revoked_at
Revises: 0029_drop_edition_png
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_google_oauth_revoked_at"
down_revision = "0029_drop_edition_png"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "google_oauth",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("google_oauth", "revoked_at")
