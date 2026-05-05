"""Per-user scope: add email column + composite PKs to overlays/edition tables.

Every existing row is backfilled with the admin email so the existing
single-user install keeps working byte-for-byte. New ``shared_editions``
table caches the date-global LLM output so we can run the LLM once per
date and assemble per-user editions on top.

Layered on top of the Data tab feature (revisions 0018–0020) which added
``user_settings`` (per-user sections / weather coords / children) and
``google_calendars`` (cached calendar list).

Revision ID: 0021_per_user_scope
Revises: 0020_google_calendars
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.settings import get_settings

revision = "0021_per_user_scope"
down_revision = "0020_google_calendars"
branch_labels = None
depends_on = None


def _admin_email() -> str:
    return get_settings().admin_email.lower()


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    admin = _admin_email()

    # ── google_oauth ──────────────────────────────────────────────────────
    # Add per-user identity + admin toggle. The legacy id=1 row becomes the
    # admin's row with personalization on.
    with op.batch_alter_table("google_oauth") as bop:
        bop.add_column(sa.Column("email", sa.Text(), nullable=True))
        bop.add_column(
            sa.Column(
                "personalized_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        bop.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        bop.add_column(
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        sa.text("UPDATE google_oauth SET email = :e WHERE email IS NULL").bindparams(e=admin)
    )
    op.execute(
        sa.text(
            "UPDATE google_oauth SET personalized_enabled = :t WHERE email = :e"
        ).bindparams(t=True, e=admin)
    )
    with op.batch_alter_table("google_oauth") as bop:
        bop.alter_column("email", existing_type=sa.Text(), nullable=False)
        bop.create_unique_constraint("uq_google_oauth_email", ["email"])

    # ── editions + subsection_images PK/FK swap ───────────────────────────
    # Order matters: the FK on subsection_images.edition_date depends on
    # editions.editions_pkey, so we must drop the FK first, then swap the
    # PK, then recreate the FK against the new composite PK.
    with op.batch_alter_table("editions") as bop:
        bop.add_column(sa.Column("email", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE editions SET email = :e WHERE email IS NULL").bindparams(e=admin))
    with op.batch_alter_table("subsection_images") as bop:
        bop.add_column(sa.Column("edition_email", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE subsection_images SET edition_email = :e WHERE edition_email IS NULL"
        ).bindparams(e=admin)
    )
    if is_sqlite:
        # batch_alter_table recreates the table; declare the new PK / FK
        # here so the recreated schema is correct in one go.
        with op.batch_alter_table("editions", recreate="always") as bop:
            bop.alter_column("email", existing_type=sa.Text(), nullable=False)
            bop.drop_constraint("editions_pkey", type_="primary")
            bop.create_primary_key("editions_pkey", ["date", "email"])
        with op.batch_alter_table("subsection_images", recreate="always") as bop:
            bop.alter_column("edition_email", existing_type=sa.Text(), nullable=False)
            bop.drop_constraint("subsection_images_edition_date_fkey", type_="foreignkey")
            bop.create_foreign_key(
                "subsection_images_edition_fkey",
                "editions",
                ["edition_date", "edition_email"],
                ["date", "email"],
                ondelete="CASCADE",
            )
    else:
        with op.batch_alter_table("editions") as bop:
            bop.alter_column("email", existing_type=sa.Text(), nullable=False)
        with op.batch_alter_table("subsection_images") as bop:
            bop.alter_column("edition_email", existing_type=sa.Text(), nullable=False)
        # 1) Drop the old FK so the editions PK can be swapped.
        op.execute(
            "ALTER TABLE subsection_images "
            "DROP CONSTRAINT IF EXISTS subsection_images_edition_date_fkey"
        )
        # 2) Swap the editions PK.
        op.execute("ALTER TABLE editions DROP CONSTRAINT editions_pkey")
        op.execute("ALTER TABLE editions ADD PRIMARY KEY (date, email)")
        # 3) Recreate the FK against the new composite PK.
        op.execute(
            "ALTER TABLE subsection_images "
            "ADD CONSTRAINT subsection_images_edition_fkey "
            "FOREIGN KEY (edition_date, edition_email) "
            "REFERENCES editions (date, email) ON DELETE CASCADE"
        )

    # ── per-user PK additions ────────────────────────────────────────────
    _add_email_pk(
        "hidden_calendars",
        existing_pk_cols=["calendar_id"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="hidden_calendars_pkey",
    )
    _add_email_pk(
        "hidden_movies",
        existing_pk_cols=["title"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="hidden_movies_pkey",
    )
    _add_email_pk(
        "favorite_movies",
        existing_pk_cols=["title"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="favorite_movies_pkey",
    )
    _add_email_pk(
        "calendar_day_summaries",
        existing_pk_cols=["day"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="calendar_day_summaries_pkey",
    )
    _add_email_pk(
        "watchlist_stocks",
        existing_pk_cols=["symbol"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="watchlist_stocks_pkey",
    )
    _add_email_pk(
        "suppressed_events",
        existing_pk_cols=["ical_uid"],
        admin=admin,
        is_sqlite=is_sqlite,
        existing_pk_name="suppressed_events_pkey",
    )

    # important_events keeps its surrogate id PK; add email column + a
    # unique (email, ical_uid) constraint replacing the old single-column one.
    with op.batch_alter_table("important_events") as bop:
        bop.add_column(sa.Column("email", sa.Text(), nullable=True))
    op.execute(
        sa.text("UPDATE important_events SET email = :e WHERE email IS NULL").bindparams(e=admin)
    )
    with op.batch_alter_table("important_events") as bop:
        bop.alter_column("email", existing_type=sa.Text(), nullable=False)
    # Replace the legacy unique(ical_uid) with unique(email, ical_uid).
    # The legacy constraint may not exist on every install (older test DBs
    # never created it), so guard with IF EXISTS on Postgres; on SQLite the
    # batch recreate above already swapped the schema.
    if not is_sqlite:
        op.execute(
            "ALTER TABLE important_events "
            "DROP CONSTRAINT IF EXISTS important_events_ical_uid_key"
        )
    op.create_unique_constraint(
        "uq_important_events_email_ical_uid", "important_events", ["email", "ical_uid"]
    )

    # ── shared_editions: cache for Phase A LLM output ────────────────────
    op.create_table(
        "shared_editions",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def _add_email_pk(
    table: str,
    *,
    existing_pk_cols: list[str],
    admin: str,
    is_sqlite: bool,
    existing_pk_name: str,
) -> None:
    """Add an ``email`` column to ``table``, backfill the admin email, and
    promote the PK to ``(email, *existing_pk_cols)``."""
    with op.batch_alter_table(table) as bop:
        bop.add_column(sa.Column("email", sa.Text(), nullable=True))
    op.execute(
        sa.text(f"UPDATE {table} SET email = :e WHERE email IS NULL").bindparams(e=admin)
    )
    new_pk = ["email", *existing_pk_cols]
    if is_sqlite:
        with op.batch_alter_table(table, recreate="always") as bop:
            bop.alter_column("email", existing_type=sa.Text(), nullable=False)
            bop.drop_constraint(existing_pk_name, type_="primary")
            bop.create_primary_key(existing_pk_name, new_pk)
    else:
        with op.batch_alter_table(table) as bop:
            bop.alter_column("email", existing_type=sa.Text(), nullable=False)
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {existing_pk_name}")
        cols = ", ".join(new_pk)
        op.execute(f"ALTER TABLE {table} ADD PRIMARY KEY ({cols})")


def downgrade() -> None:
    # One-way migration: restoring single-user PKs after multiple users have
    # signed in would silently drop everyone but the admin. Refuse rather
    # than corrupt data.
    raise NotImplementedError(
        "0018_per_user_scope is one-way: rolling back would drop per-user data."
    )
