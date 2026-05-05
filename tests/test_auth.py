from __future__ import annotations

from datetime import UTC, datetime

from app.auth import is_admin, is_allowed, load_allowlist
from app.db import UserSettings


def _add(s, email: str) -> None:
    s.add(
        UserSettings(
            email=email,
            display_name=None,
            sections_json=[],
            children_json=[],
            updated_at=datetime.now(UTC),
        )
    )
    s.commit()


def test_load_allowlist_reads_user_settings(db_session):
    _add(db_session, "friend@example.com")
    emails = load_allowlist(db_session)
    # Admin email is always included even without an explicit row.
    assert "vtlinh87@gmail.com" in emails
    assert "friend@example.com" in emails


def test_load_allowlist_admin_always_present_when_table_empty(db_session):
    assert load_allowlist(db_session) == {"vtlinh87@gmail.com"}


def test_is_allowed_case_insensitive(db_session):
    _add(db_session, "friend@example.com")
    assert is_allowed("VTLinh87@Gmail.com", db_session)
    assert is_allowed("friend@EXAMPLE.com", db_session)
    assert not is_allowed("stranger@example.com", db_session)


def test_is_admin_only_linh():
    assert is_admin("vtlinh87@gmail.com")
    assert is_admin("VTLINH87@GMAIL.COM")
    assert not is_admin("friend@example.com")
