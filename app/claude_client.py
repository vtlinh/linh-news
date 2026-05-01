from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from app.settings import get_settings

# ── Default tool: web search (server-side, no client handling needed) ────
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# Schema for the daily edition. Defined once and reused as the structured
# output schema for both cron and refresh runs.
EDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "html": {
            "type": "string",
            "description": (
                "Full HTML page (mobile-first responsive) with all the "
                "sections requested in the system prompt. Includes Sources "
                "tooltips on every news/finance/AI/movie/weather/school item."
            ),
        },
        "pdf_html": {
            "type": "string",
            "description": (
                "Print-styled one-page HTML for WeasyPrint, NYT-front-page "
                "aesthetic. NO source citations, NO interactive buttons. "
                "Must fit on exactly one US Letter page."
            ),
        },
    },
    "required": ["html", "pdf_html"],
    "additionalProperties": False,
}


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

    # Streaming is required for long requests (>10 min) per the Anthropic SDK.
    # We don't surface partial deltas to callers — we just consume the stream
    # and read the final message.
    with _client().messages.stream(
        model=model or settings.anthropic_model,
        max_tokens=max_tokens,
        tools=tools,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == schema_name:
            return dict(block.input)

    # The model failed to call the structured-output tool.
    text_blocks = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    raise ValueError(
        f"Claude did not invoke {schema_name!r}. stop_reason={msg.stop_reason}. "
        f"Text content: {' '.join(text_blocks)[:500]}"
    )


def generate_edition(prompt_template: str, context: dict) -> dict:
    """Send the rendered prompt to Claude, return parsed {html, pdf_html} dict."""
    system_block = prompt_template
    user_lines = ["Run for these inputs (substitute into the system prompt):"]
    for k, v in context.items():
        user_lines.append(f"- {k} = {_to_text(v)}")
    user_lines.append(
        "\nWhen done, call the return_edition tool with the final HTML and pdf_html."
    )
    return call_with_schema(
        system=system_block,
        user="\n".join(user_lines),
        schema=EDITION_SCHEMA,
        schema_name="return_edition",
        schema_description=(
            "Return the final daily edition. Call this exactly once after all "
            "web searches are complete."
        ),
        extra_tools=[WEB_SEARCH_TOOL],
    )


def _to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)
