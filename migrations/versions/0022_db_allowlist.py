"""Move the authorized-user list from AUTHORIZED_USERS env CSV into the
``user_settings`` table. A row in ``user_settings`` now means
"this email may sign in". The admin's email is also auto-included server-side,
so this migration just seeds the existing CSV so nobody loses access on deploy.

Revision ID: 0022_db_allowlist
Revises: 0021_per_user_scope
Create Date: 2026-05-04
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.settings import get_settings

revision = "0022_db_allowlist"
down_revision = "0021_per_user_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    settings = get_settings()
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    existing = {
        e.lower()
        for e in bind.execute(sa.text("SELECT email FROM user_settings")).scalars()
    }
    raw = settings.authorized_users or ""
    csv_emails = {p.strip().lower() for p in raw.split(",") if p.strip()}
    csv_emails.add(settings.admin_email.lower())

    now = datetime.now(UTC)
    json_lit = "'[]'::json" if is_pg else "'[]'"
    for em in sorted(csv_emails - existing):
        op.execute(
            sa.text(
                f"INSERT INTO user_settings "
                "(email, display_name, address, weather_coords, "
                f"sections_json, children_json, updated_at) "
                f"VALUES (:e, NULL, NULL, NULL, {json_lit}, {json_lit}, :ts)"
            ).bindparams(e=em, ts=now)
        )


def downgrade() -> None:
    raise NotImplementedError(
        "0022_db_allowlist is one-way: deleting the rows would lock users out."
    )
