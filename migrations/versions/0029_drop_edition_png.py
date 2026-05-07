"""Drop ``editions.png`` — PNG generation has been removed.

The ``/png/{day}`` and ``/png/{day}/{name}`` endpoints, ``app.pdf.pdf_to_png``,
and the in-pipeline rasterization step are all gone; this migration drops
the now-unused column so the schema matches the model.

Revision ID: 0029_drop_edition_png
Revises: 0028_edition_png
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_drop_edition_png"
down_revision = "0028_edition_png"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("editions", "png")


def downgrade() -> None:
    op.add_column(
        "editions",
        sa.Column("png", sa.LargeBinary(), nullable=True),
    )
