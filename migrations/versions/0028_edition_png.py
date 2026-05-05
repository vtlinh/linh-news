"""Add ``editions.png`` — PNG raster of the broadsheet PDF.

Generated alongside the PDF and served by the new ``/png/{day}`` and
``/png/{day}/{name}`` endpoints. Nullable: existing rows keep NULL until
their next regeneration, and a PNG render failure leaves the column NULL
without breaking the PDF upsert.

Revision ID: 0028_edition_png
Revises: 0027_user_id
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_edition_png"
down_revision = "0027_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editions",
        sa.Column("png", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("editions", "png")
