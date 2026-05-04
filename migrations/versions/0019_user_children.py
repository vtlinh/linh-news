"""Add children_json to user_settings.

Revision ID: 0019_user_children
Revises: 0018_user_settings
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_user_children"
down_revision = "0018_user_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.add_column(sa.Column("children_json", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("children_json")
