"""Per-user PDF access token.

Each ``user_settings`` row gets a secret ``pdf_token`` used as the bearer
secret for unauthenticated PDF access (home-screen shortcuts / iOS
Shortcuts). The admin's row is backfilled with the legacy
``PDF_LATEST_TOKEN`` env value so existing bookmarks keep working; every
other user gets a freshly minted token. The column is NOT NULL after
backfill.

Revision ID: 0026_user_pdf_token
Revises: 0025_debug_editions
Create Date: 2026-05-05
"""

from __future__ import annotations

import os
import secrets

import sqlalchemy as sa
from alembic import op

revision = "0026_user_pdf_token"
down_revision = "0025_debug_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("pdf_token", sa.String(), nullable=True),
    )
    # Backfill — admin gets the legacy PDF_LATEST_TOKEN, everyone else a fresh
    # secret. Done in Python so each row gets its own random value.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT email FROM user_settings")).fetchall()
    admin_email = (os.environ.get("ADMIN_EMAIL") or "vtlinh87@gmail.com").lower()
    legacy = os.environ.get("PDF_LATEST_TOKEN", "").strip()
    for (email,) in rows:
        is_admin = (email or "").lower() == admin_email
        token = legacy if (is_admin and legacy) else secrets.token_urlsafe(36)
        bind.execute(
            sa.text("UPDATE user_settings SET pdf_token = :t WHERE email = :e"),
            {"t": token, "e": email},
        )
    op.alter_column("user_settings", "pdf_token", nullable=False)
    op.create_index(
        "ix_user_settings_pdf_token", "user_settings", ["pdf_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_user_settings_pdf_token", table_name="user_settings")
    op.drop_column("user_settings", "pdf_token")
