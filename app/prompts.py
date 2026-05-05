"""Mako-backed loader for LLM prompt templates stored in ``prompts/``.

Every prompt sent to Claude lives as a ``.mako`` file at the repo root so
the prose can be edited without touching Python. Use ``render(name, **vars)``
for templates with substitutions; raw text files render fine with no vars.
"""

from __future__ import annotations

from pathlib import Path

from mako.lookup import TemplateLookup

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_lookup = TemplateLookup(
    directories=[str(_PROMPTS_DIR)],
    input_encoding="utf-8",
    output_encoding=None,
    default_filters=[],
)


def render(name: str, /, **kwargs: object) -> str:
    """Render ``prompts/<name>.mako`` with the given Mako variables."""
    tmpl = _lookup.get_template(f"{name}.mako")
    return tmpl.render(**kwargs).strip()
