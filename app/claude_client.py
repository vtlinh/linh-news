from __future__ import annotations

import logging
from typing import Any

from anthropic import Anthropic

from app import prompts
from app.llm_schema import EDITION_SCHEMA
from app.settings import get_settings

log = logging.getLogger(__name__)

# ── Default tool: web search (server-side, no client handling needed) ────
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


# ── API cost tracking ───────────────────────────────────────────────────
#
# Per-million-token prices (USD), as of late 2025, for input / output.
# Cache writes are billed at 1.25x input, cache reads at 0.1x input.
# Web search costs $10 per 1000 server-side searches.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICE = (15.0, 75.0)  # unknown model → fall back to opus pricing
_WEB_SEARCH_USD_PER_REQUEST = 0.01

# Session-local usage log. Each entry is ``{"model", "cost_usd",
# "input_tokens", "output_tokens", ...}``. The pipeline resets this at
# the start of a run and reads the total after all LLM calls so it can
# be rendered on the PDF dateline.
_session_usage: list[dict[str, Any]] = []


def reset_usage_log() -> None:
    """Clear the session usage accumulator. Call at the start of each
    generation run."""
    _session_usage.clear()


def get_session_cost_usd() -> float:
    """Sum of all API costs since the last :func:`reset_usage_log`."""
    return sum(entry.get("cost_usd", 0.0) for entry in _session_usage)


def get_session_usage_summary() -> str:
    """One-line summary of accumulated usage, suitable for logging."""
    if not _session_usage:
        return "no LLM calls"
    total = get_session_cost_usd()
    return (
        f"{len(_session_usage)} LLM calls, total ${total:.4f}: "
        + ", ".join(
            f"{e['model']}=${e['cost_usd']:.4f} "
            f"(in={e.get('input_tokens', 0)}, out={e.get('output_tokens', 0)})"
            for e in _session_usage
        )
    )


def _record_usage(model: str, usage_obj: Any) -> dict[str, Any]:
    """Compute the cost of one API call from its ``usage`` block and
    append to the session log. Returns the recorded entry."""
    input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
    output_tokens = getattr(usage_obj, "output_tokens", 0) or 0
    cache_w = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
    server_tool = getattr(usage_obj, "server_tool_use", None)
    web_search_requests = 0
    if server_tool is not None:
        web_search_requests = getattr(server_tool, "web_search_requests", 0) or 0

    rate_in, rate_out = _PRICE_PER_MTOK.get(model, _DEFAULT_PRICE)
    cost = 0.0
    cost += input_tokens / 1_000_000 * rate_in
    cost += output_tokens / 1_000_000 * rate_out
    cost += cache_w / 1_000_000 * rate_in * 1.25
    cost += cache_r / 1_000_000 * rate_in * 0.10
    cost += web_search_requests * _WEB_SEARCH_USD_PER_REQUEST

    entry = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": cache_w,
        "cache_read_tokens": cache_r,
        "web_search_requests": web_search_requests,
        "cost_usd": cost,
    }
    _session_usage.append(entry)
    log.info(
        "Anthropic API: model=%s in=%d out=%d cache_w=%d cache_r=%d "
        "websearch=%d cost=$%.4f",
        model, input_tokens, output_tokens, cache_w, cache_r,
        web_search_requests, cost,
    )
    return entry


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
        # Record usage + cost for this API call (each pause-resume turn
        # is a separate billed call).
        if getattr(msg, "usage", None) is not None:
            _record_usage(model_name, msg.usage)

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
        user=prompts.render("edition_user"),
        schema=EDITION_SCHEMA,
        schema_name="return_edition",
        schema_description=(
            "Return the structured daily edition. Call this exactly once "
            "after all web searches are complete."
        ),
        extra_tools=[WEB_SEARCH_TOOL],
    )
