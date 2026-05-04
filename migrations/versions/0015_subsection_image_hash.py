"""Add subsection_images.image_hash for cross-day dedup.

Stores the SHA-256 of the raw downloaded image bytes so the generation
pipeline can reject an og:image that already appeared in an earlier
edition — typically a generic site banner being served as og:image on
section / homepage URLs.

Revision ID: 0015_subsection_image_hash
Revises: 0014_subsection_images_content
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_subsection_image_hash"
down_revision = "0014_subsection_images_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subsection_images",
        sa.Column("image_hash", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_subsection_images_image_hash",
        "subsection_images",
        ["image_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subsection_images_image_hash",
        table_name="subsection_images",
    )
    op.drop_column("subsection_images", "image_hash")
