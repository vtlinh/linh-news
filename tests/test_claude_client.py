from __future__ import annotations

import pytest

from app.claude_client import _parse_json, _substitute


def test_substitute_handles_strings_and_objects():
    out = _substitute(
        "Hi {{NAME}}, hidden={{HIDDEN}}",
        {"NAME": "Linh", "HIDDEN": ["A", "B"]},
    )
    assert "Hi Linh" in out
    assert '["A", "B"]' in out


def test_parse_json_strips_code_fence():
    raw = 'Sure!\n```json\n{"html": "<p>x</p>", "pdf_html": "<p>y</p>"}\n```'
    out = _parse_json(raw)
    assert out == {"html": "<p>x</p>", "pdf_html": "<p>y</p>"}


def test_parse_json_extracts_when_prose_around():
    raw = 'Here you go: {"html": "a", "pdf_html": "b"} thanks'
    assert _parse_json(raw) == {"html": "a", "pdf_html": "b"}


def test_parse_json_rejects_missing_keys():
    with pytest.raises(ValueError):
        _parse_json('{"html": "only"}')
