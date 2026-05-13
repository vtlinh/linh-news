"""Build the daily-edition prompt for Claude.

This module replaces the old static ``news.pr`` file. The section table is
no longer hardcoded — it's pulled from the user's ``user_settings`` row at
generation time. ``display_name`` (also from settings) replaces every
"Linh"-branded reference. ``children`` (also from settings) drives the
school grade-filter rule.
"""

from __future__ import annotations

from datetime import date

from app import kids, prompts


def _render_sections_table(sections: list[dict]) -> str:
    """Render the user's section list as a numbered, ordered list. The
    LLM gets stable ``key``s so the server can detect missing sections
    and re-roll them."""
    lines = []
    for i, sec in enumerate(sections, 1):
        key = sec.get("key", "")
        title = (sec.get("title") or "").strip()
        desc = (sec.get("description") or "").strip()
        n = int(sec.get("subsection_count", 5))
        prefs = [p for p in (sec.get("preferred_sources") or []) if p]
        use_global = bool(sec.get("use_global_sources", True))
        lines.append(f"### {i}. `{key}` — {title}")
        lines.append(f"Minimum: {n} distinct fresh subsections (more is fine).")
        if desc:
            lines.append(f"Topic: {desc}")
        if prefs:
            lines.append(
                "Always check these preferred sources on every run, in addition "
                "to any other relevant sources you find: " + ", ".join(prefs) + "."
            )
            if not use_global:
                lines.append(
                    "Use ONLY these preferred sources for this section — do NOT "
                    "browse outside them."
                )
        lines.append("")
    return "\n".join(lines)


def _render_headline_block(eligible_titles: list[str]) -> str:
    """Return the headline-instruction block for the daily prompt, or an
    empty string when no section is marked ``can_be_headline``."""
    if not eligible_titles:
        return ""
    bullets = "\n".join(f"  - {t}" for t in eligible_titles)
    return (
        "## Headline (top-of-front-page deep dive)\n\n"
        "In addition to the section content, return ONE `headline` object "
        "— a focused, long-form treatment of a SINGLE story drawn from one "
        "of these topics:\n"
        f"{bullets}\n\n"
        "Rules:\n"
        "  - The body covers ONE story only. NO 'in other news', NO "
        "'meanwhile', NO 'besides this', NO 'elsewhere'. Stay on that "
        "one story.\n"
        "  - Body length must be at least 2x a normal subsection body.\n"
        "  - Tell us: what happened, who is involved, why it matters, "
        "and what to watch next.\n"
        "  - `sources`: >=3 strong, direct sources (Tier 1 outlets preferred).\n"
        "  - Do NOT supply image URLs anywhere — the server fetches the "
        "image from your sources.\n"
        "  - Do NOT also include this same story as a subsection inside its "
        "source section. Pick a different angle or a different story for "
        "that section's subsections so the headline does not appear twice "
        "on the page."
    )


def _render_children_block(children_json: list[dict], today: date) -> str:
    parsed = kids.parse_children(children_json)
    if not parsed:
        return "(no children configured)"
    out = []
    for c in parsed:
        g = kids.grade_for(c["birthday"], today)
        a = kids.age_on(c["birthday"], today)
        out.append(f"- {c['name']}: age {a}, {kids.grade_label(g)}")
    return "\n".join(out)


def build_prompt(
    *,
    display_name: str,
    today: date,
    sections: list[dict],
    children: list[dict],
    watchlist_stocks: list[str],
    dorchester_events: str,
) -> str:
    """Return the full system prompt sent to Claude. All `{{...}}` style
    placeholders that used to live in news.pr are now substituted in
    Python; the LLM receives a complete, ready-to-read prompt."""

    name = display_name.strip() or "the reader"
    headline_titles = [
        (s.get("title") or "").strip()
        for s in sections
        if s.get("can_be_headline") and (s.get("title") or "").strip()
    ]
    return prompts.render(
        "edition_system",
        name=name,
        today_iso=today.isoformat(),
        sections_table=_render_sections_table(sections),
        headline_block=_render_headline_block(headline_titles),
        children_block=_render_children_block(children, today),
        grade_label=kids.grades_label(children, today) or "(none)",
        watchlist_repr=", ".join(watchlist_stocks) if watchlist_stocks else "(empty)",
        dorchester_events=dorchester_events,
    )
