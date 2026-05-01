from __future__ import annotations

import json
import re

from anthropic import Anthropic

from app.settings import get_settings


def _client() -> Anthropic:
    return Anthropic(api_key=get_settings().anthropic_api_key)


def generate_edition(prompt_template: str, context: dict) -> dict:
    """Send the rendered prompt to Claude, return parsed {html, pdf_html} dict.

    The static instructions block is sent as a system message with
    cache_control so daily runs hit the prompt cache. The dynamic context
    (calendar JSON, hidden movies, etc.) is the user message.
    """
    settings = get_settings()
    system_block, user_block = _split_template(prompt_template, context)

    msg = _client().messages.create(
        model=settings.anthropic_model,
        max_tokens=16000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=[
            {
                "type": "text",
                "text": system_block,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_block}],
    )

    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    return _parse_json(text)


def _split_template(tmpl: str, ctx: dict) -> tuple[str, str]:
    """Split the rendered prompt: static instructions go in system (cached),
    dynamic context (DATE/SLOT/JSON blobs) goes in user message."""
    rendered_static = _substitute(tmpl, {k: f"{{{{{k}}}}}" for k in ctx})  # leave placeholders
    user_lines = ["Run for these inputs (substitute into the system prompt):"]
    for k, v in ctx.items():
        user_lines.append(f"- {k} = {_to_text(v)}")
    user_lines.append(
        "\nReturn ONLY a single JSON object {\"html\": ..., \"pdf_html\": ...}."
    )
    return rendered_static, "\n".join(user_lines)


def _substitute(tmpl: str, ctx: dict) -> str:
    out = tmpl
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", _to_text(v))
    return out


def _to_text(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json(text: str) -> dict:
    m = _JSON_FENCE.search(text)
    payload = m.group(1) if m else text
    # If still wrapped in prose, find first { ... last }
    if not payload.lstrip().startswith("{"):
        first = payload.find("{")
        last = payload.rfind("}")
        if first >= 0 and last > first:
            payload = payload[first : last + 1]
    data = json.loads(payload)
    if "html" not in data or "pdf_html" not in data:
        raise ValueError(f"Claude response missing html/pdf_html keys: {list(data)}")
    return data
