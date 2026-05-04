from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from app.llm_schema import EDITION_SCHEMA
from app.settings import get_settings

# ── Default tool: web search (server-side, no client handling needed) ────
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


def _client() -> Anthropic:
    return Anthropic(api_key=get_settings().anthropic_api_key)


def call_with_schema(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str,
    schema_description: str,
    extra_tools: list[dict] | None = None,
    max_tokens: int = 32000,
    model: str | None = None,
) -> dict[str, Any]:
    """Default pattern for Claude API calls in this project.

    Forces a structured response by exposing a single client tool
    (``schema_name``) whose ``input_schema`` is the caller-supplied JSON
    schema. The model may also use any ``extra_tools`` (e.g. server-side
    web search) before emitting the final structured payload.

    Returns the dict the model passed as that tool's input.
    """
    settings = get_settings()
    return_tool = {
        "name": schema_name,
        "description": schema_description,
        "input_schema": schema,
    }
    tools = [return_tool] + list(extra_tools or [])
    client = _client()
    model_name = model or settings.anthropic_model
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

    # Streaming is required for long requests (>10 min) per the Anthropic SDK.
    # We don't surface partial deltas to callers — we just consume the stream
    # and read the final message. If the model returns ``stop_reason="pause_turn"``
    # (long tool-use turn was suspended to avoid hitting the per-turn limit),
    # Anthropic's API tells us to append the assistant content and call again.
    # We do that up to a small cap so we don't spin forever.
    MAX_PAUSE_RESUMES = 4
    last_msg = None
    for _ in range(MAX_PAUSE_RESUMES + 1):
        with client.messages.stream(
            model=model_name,
            max_tokens=max_tokens,
            tools=tools,
            system=system_blocks,
            messages=messages,
        ) as stream:
            msg = stream.get_final_message()
        last_msg = msg

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == schema_name:
                return dict(block.input)

        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        break

    msg = last_msg
    text_blocks = [
        b.text for b in (msg.content if msg else []) if getattr(b, "type", None) == "text"
    ]
    raise ValueError(
        f"Claude did not invoke {schema_name!r}. "
        f"stop_reason={getattr(msg, 'stop_reason', None)}. "
        f"Text content: {' '.join(text_blocks)[:500]}"
    )


def generate_edition(rendered_prompt: str) -> dict:
    """Send the fully-rendered prompt to Claude, return the parsed
    NewsEdition dict (see :data:`app.llm_schema.EDITION_SCHEMA`).
    """
    return call_with_schema(
        system=rendered_prompt,
        user=(
            "Run all web_search queries needed for this edition, then call "
            "the return_edition tool exactly once with the structured "
            "NewsEdition payload (sections + stocks). Do NOT return HTML."
        ),
        schema=EDITION_SCHEMA,
        schema_name="return_edition",
        schema_description=(
            "Return the structured daily edition. Call this exactly once "
            "after all web searches are complete."
        ),
        extra_tools=[WEB_SEARCH_TOOL],
    )
