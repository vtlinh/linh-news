from __future__ import annotations

import os
from collections.abc import Iterator

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
def users_csv() -> str:
    return "vtlinh87@gmail.com, friend@example.com"


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
def client(db_session, monkeypatch, users_csv):
    """FastAPI TestClient with the SQLite session factory installed.

    Seeds the DB allowlist with the two test users via the same
    ``user_settings`` table the production allowlist reads from.
    """
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from app.db import UserSettings, get_session, session_factory
    from app.main import app

    for em in ("vtlinh87@gmail.com", "friend@example.com"):
        if db_session.get(UserSettings, em) is None:
            db_session.add(
                UserSettings(
                    email=em,
                    display_name=None,
                    sections_json=[],
                    children_json=[],
                    updated_at=datetime.now(UTC),
                )
            )
    db_session.commit()

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
