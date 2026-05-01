from __future__ import annotations

from app.auth import is_admin, is_allowed, load_allowlist


def test_load_allowlist_skips_comments_and_blanks(tmp_users_file):
    emails = load_allowlist(tmp_users_file)
    assert emails == {"vtlinh87@gmail.com", "friend@example.com"}


def test_is_allowed_case_insensitive(tmp_users_file):
    assert is_allowed("VTLinh87@Gmail.com", tmp_users_file)
    assert is_allowed("friend@EXAMPLE.com", tmp_users_file)
    assert not is_allowed("stranger@example.com", tmp_users_file)


def test_is_admin_only_linh():
    assert is_admin("vtlinh87@gmail.com")
    assert is_admin("VTLINH87@GMAIL.COM")
    assert not is_admin("friend@example.com")
