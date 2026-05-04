from __future__ import annotations

from app.auth import is_admin, is_allowed, load_allowlist


def test_load_allowlist_parses_csv_and_normalizes(users_csv):
    emails = load_allowlist(users_csv)
    assert emails == {"vtlinh87@gmail.com", "friend@example.com"}


def test_load_allowlist_skips_blanks():
    assert load_allowlist("a@x.com, ,b@y.com,,") == {"a@x.com", "b@y.com"}


def test_is_allowed_case_insensitive(users_csv):
    assert is_allowed("VTLinh87@Gmail.com", users_csv)
    assert is_allowed("friend@EXAMPLE.com", users_csv)
    assert not is_allowed("stranger@example.com", users_csv)


def test_is_admin_only_linh():
    assert is_admin("vtlinh87@gmail.com")
    assert is_admin("VTLINH87@GMAIL.COM")
    assert not is_admin("friend@example.com")
