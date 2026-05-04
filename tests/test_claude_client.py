from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.claude_client import EDITION_SCHEMA, call_with_schema, generate_edition


def _fake_response(tool_name: str, tool_input: dict) -> MagicMock:
    msg = MagicMock()
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    msg.content = [block]
    msg.stop_reason = "tool_use"
    return msg


def _patch_stream(final_message: MagicMock):
    """Patch _client() so .messages.stream(...) yields a context manager that
    returns ``final_message`` from ``get_final_message()``."""
    client_patch = patch("app.claude_client._client")
    cm = client_patch.start()
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = stream_cm
    stream_cm.get_final_message.return_value = final_message
    cm.return_value.messages.stream.return_value = stream_cm
    return client_patch


def test_call_with_schema_returns_tool_input():
    fake = _fake_response("my_tool", {"a": 1, "b": "x"})
    p = _patch_stream(fake)
    try:
        out = call_with_schema(
            system="sys",
            user="u",
            schema={"type": "object"},
            schema_name="my_tool",
            schema_description="desc",
        )
    finally:
        p.stop()
    assert out == {"a": 1, "b": "x"}


def test_call_with_schema_raises_when_tool_not_invoked():
    msg = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "I cannot."
    msg.content = [text_block]
    msg.stop_reason = "end_turn"
    p = _patch_stream(msg)
    try:
        with pytest.raises(ValueError, match="did not invoke"):
            call_with_schema(
                system="sys",
                user="u",
                schema={"type": "object"},
                schema_name="my_tool",
                schema_description="desc",
            )
    finally:
        p.stop()


def test_generate_edition_returns_structured_dict():
    payload = {
        "sections": [
            {
                "key": "us",
                "title": "🇺🇸 Top US Political News",
                "subsections": [
                    {
                        "title": "h",
                        "text": "body",
                        "sources": [{"url": "https://x", "title": "T"}],
                    }
                ],
            }
        ],
        "stocks": [],
    }
    fake = _fake_response("return_edition", payload)
    p = _patch_stream(fake)
    try:
        out = generate_edition("rendered prompt for 2026-04-30")
    finally:
        p.stop()
    assert out == payload


def test_edition_schema_has_required_keys():
    assert set(EDITION_SCHEMA["required"]) == {"sections", "stocks"}
