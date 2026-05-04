"""Add editions.pdf_rail_json to cache the rendered PDF side rail.

Stores the calendar + movies HTML strings (already containing inlined
backdrop image data URIs) plus a render-logic version. Re-runs for the
same date reuse the cached rail when the version still matches, skipping
movie-backdrop fetches and the calendar/movies HTML build.

Revision ID: 0016_edition_pdf_rail
Revises: 0015_subsection_image_hash
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_edition_pdf_rail"
down_revision = "0015_subsection_image_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editions", sa.Column("pdf_rail_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("editions", "pdf_rail_json")
