"""Add structured-content storage: editions.content_json + subsection_images.

The LLM now returns a structured ``LinhNews`` object instead of HTML; the
server renders the page from it. ``content_json`` holds that structure so
re-renders don't require a fresh Claude call. ``subsection_images`` stores
the bytes of one downloaded-and-resized image per news subsection.

Revision ID: 0014_subsection_images_content
Revises: 0013_movie_backdrops
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_subsection_images_content"
down_revision = "0013_movie_backdrops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editions",
        sa.Column("content_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "subsection_images",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "edition_date",
            sa.Date(),
            sa.ForeignKey("editions.date", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("section_key", sa.String(), nullable=False),
        sa.Column("subsection_idx", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subsection_images")
    op.drop_column("editions", "content_json")
