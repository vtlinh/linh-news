"""Make ``google_calendars`` per-user.

Until now the cache was a single global snapshot. With per-user editions any
two signed-in users would clobber each other's calendar list. Re-key on
``(email, id)`` and drop the existing rows — they re-populate on the next
admin/calendars visit.

Revision ID: 0024_per_user_cals
Revises: 0023_personalized_to_us
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_per_user_cals"
down_revision = "0023_personalized_to_us"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cache contents are disposable — drop & recreate is simpler than
    # back-filling email for unattributed rows.
    op.drop_table("google_calendars")
    op.create_table(
        "google_calendars",
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("google_calendars")
    op.create_table(
        "google_calendars",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
