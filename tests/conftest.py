from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Force test settings BEFORE any app import.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret")


@pytest.fixture()
def tmp_users_file(tmp_path: Path) -> Path:
    p = tmp_path / "users.txt"
    p.write_text("# comment\nvtlinh87@gmail.com\nfriend@example.com\n", encoding="utf-8")
    return p


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """In-process SQLite session, schema created from SQLAlchemy metadata."""
    from app.db import Base, set_session_factory

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    set_session_factory(Maker)
    with Maker() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def client(db_session, monkeypatch, tmp_users_file):
    """FastAPI TestClient with the SQLite session factory installed."""
    from fastapi.testclient import TestClient

    from app import auth as auth_module
    from app.db import get_session, session_factory
    from app.main import app

    # Wire allowlist file
    monkeypatch.setattr(auth_module, "load_allowlist", lambda path=None: {
        "vtlinh87@gmail.com",
        "friend@example.com",
    })

    def override_session():
        Maker = session_factory()
        with Maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def login_as(client, db_session):
    """Returns a function (email) -> sets session cookie on the client."""
    from app.auth import SESSION_COOKIE, create_session

    def _login(email: str) -> None:
        sid = create_session(db_session, email)
        client.cookies.set(SESSION_COOKIE, sid)

    return _login
