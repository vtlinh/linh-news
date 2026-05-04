"""Compute current grade and age from a child's birthday.

US schooling norm assumed by this module:
- school year flips on **August 1** (anyone reading on/after Aug 1 is in the
  academic year that starts that month);
- a child enters Kindergarten the August on/after their 5th birthday;
- age = grade + 5 (K = 5, 1st = 6, 2nd = 7, …).

This file is the single source of truth used both by the prompt template
(grade-filter wording) and by ``calendar_oauth`` (grade-aware event filter).
Children come from ``UserSettings.children_json``; if a user has no
children, callers fall back to ``DEFAULT_GRADE``/``DEFAULT_AGE``.
"""

from __future__ import annotations

from datetime import date

# Used when a user has no children in their settings.
DEFAULT_GRADE = 0
DEFAULT_AGE = 5


def _school_year_start(today: date) -> int:
    """The calendar year of the August 1 that opens the *current* school
    year. For a date in Jan–Jul, that's last August; for Aug–Dec, this
    August."""
    return today.year if today.month >= 8 else today.year - 1


def grade_for(birthday: date, today: date) -> int:
    """Return the grade level a child of this birthday is in on ``today``.
    K = 0, 1st = 1, etc. May be negative for pre-K children — caller can
    clamp at 0 if it doesn't want to show them at all."""
    sy_start = _school_year_start(today)
    # Age the child reaches on or before this school year's Aug 1.
    sy_birthday_year = sy_start
    sy_age = sy_birthday_year - birthday.year
    if (birthday.month, birthday.day) > (8, 1):
        sy_age -= 1
    return sy_age - 5


def age_on(birthday: date, today: date) -> int:
    yrs = today.year - birthday.year
    if (today.month, today.day) < (birthday.month, birthday.day):
        yrs -= 1
    return yrs


def parse_children(children_json: list[dict] | None) -> list[dict]:
    """Filter to entries with a parseable birthday. Returns dicts with
    ``name``, ``birthday`` (date), preserving the input order."""
    out: list[dict] = []
    for c in children_json or []:
        b = (c.get("birthday") or "").strip()
        if not b:
            continue
        try:
            bd = date.fromisoformat(b)
        except ValueError:
            continue
        out.append({"name": (c.get("name") or "").strip(), "birthday": bd})
    return out


def grades_for_today(children_json: list[dict] | None, today: date) -> list[int]:
    """Sorted unique list of grades currently attended by the children.
    Grades < 0 (not yet in K) are dropped."""
    parsed = parse_children(children_json)
    grades = {grade_for(c["birthday"], today) for c in parsed}
    return sorted(g for g in grades if g >= 0)


def grade_label(g: int) -> str:
    if g <= 0:
        return "Kindergarten"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(g if g < 20 else g % 10, "th")
    return f"{g}{suffix} grade"


def grades_label(children_json: list[dict] | None, today: date) -> str:
    """Comma-separated human label for the list of grades currently
    attended (e.g. ``"Kindergarten, 2nd grade"``). Empty string if no
    children of school age."""
    return ", ".join(grade_label(g) for g in grades_for_today(children_json, today))
