"""Move ``personalized_enabled`` from ``google_oauth`` to ``user_settings``.

The flag is a per-user preference the admin sets on the Users page; it has
nothing to do with OAuth credentials. Putting it on user_settings lets the
admin toggle it before the user has ever signed in.

Revision ID: 0023_personalized_to_user_settings
Revises: 0022_db_allowlist
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.settings import get_settings

revision = "0023_personalized_to_user_settings"
down_revision = "0022_db_allowlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "user_settings",
        sa.Column(
            "personalized_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Copy existing flags from google_oauth where rows already line up.
    op.execute(
        sa.text(
            "UPDATE user_settings AS us SET personalized_enabled = go.personalized_enabled "
            "FROM google_oauth AS go WHERE go.email = us.email"
        )
        if bind.dialect.name == "postgresql"
        else sa.text(
            "UPDATE user_settings SET personalized_enabled = "
            "(SELECT personalized_enabled FROM google_oauth "
            "WHERE google_oauth.email = user_settings.email) "
            "WHERE EXISTS (SELECT 1 FROM google_oauth "
            "WHERE google_oauth.email = user_settings.email)"
        )
    )
    # Admin is always personalized.
    admin = get_settings().admin_email.lower()
    op.execute(
        sa.text(
            "UPDATE user_settings SET personalized_enabled = TRUE WHERE email = :e"
        ).bindparams(e=admin)
    )
    op.drop_column("google_oauth", "personalized_enabled")


def downgrade() -> None:
    raise NotImplementedError(
        "0023 is one-way: re-adding the column would lose admin toggles "
        "made after this migration."
    )
