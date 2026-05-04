"""Copy all data from Fly Postgres (via local proxy) into the local SQLite DB.

Prereqs:
    fly proxy 15432:5432 -a linh-news-db &      # in another terminal
    uv run alembic upgrade head                  # creates SQLite schema
    uv run python scripts/copy_pg_to_sqlite.py

Idempotent: clears each target table before inserting. Uses the same
SQLAlchemy models for both sides, so any schema drift will fail loudly.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.db import Base

PG_URL = os.environ.get(
    "PG_SOURCE_URL",
    "postgresql+psycopg://linh_news:Js4W98SMyePWLsD@localhost:15432/linh_news?sslmode=disable",
)


def main() -> int:
    sqlite_engine = db_module.engine()
    if "sqlite" not in str(sqlite_engine.url):
        print(f"Refusing: target DB is not SQLite (got {sqlite_engine.url}).", file=sys.stderr)
        return 2

    pg_engine = create_engine(PG_URL, connect_args={"connect_timeout": 5})
    PgSession = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    SqliteSession = db_module.session_factory()

    # Order matters when there are FKs. Editions before subsection_images.
    # Tables that don't exist on the prod (Fly Postgres) DB yet — skip them.
    skip = {"alembic_version", "user_settings"}
    tables_in_order = [t for t in Base.metadata.sorted_tables if t.name not in skip]

    with PgSession() as ps, SqliteSession() as ss:
        # Clear in reverse order to satisfy FKs.
        for t in reversed(tables_in_order):
            ss.execute(delete(t))
        ss.commit()

        for t in tables_in_order:
            rows = ps.execute(select(t)).mappings().all()
            if not rows:
                print(f"  {t.name}: 0 rows")
                continue
            ss.execute(t.insert(), [dict(r) for r in rows])
            ss.commit()
            print(f"  {t.name}: {len(rows)} rows")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
