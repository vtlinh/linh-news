"""Render the latest assembled HTML snapshot via WeasyPrint and check
two failure modes inside the headline box and other text regions:

1. ``overflow``: a descendant whose ``position_y + height`` exceeds the
   parent region's border-box bottom — the bottom rows of text get clipped
   by ``overflow: hidden``.

2. ``occlusion``: a text-bearing descendant whose bounding rectangle
   intersects the floated ``.hero-figure``'s rectangle. Standard CSS
   floats push *inline* content out of the float's rectangle, but
   WeasyPrint's multicolumn implementation does not always honor floats
   that come from *outside* the multicol container — text in non-wrap
   columns can render right under the float and be visually hidden.

Prints PASS/FAIL per region. Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weasyprint import HTML  # noqa: E402

from app import pdf as _pdf  # noqa: F401, E402  — DLL registration


def _rect(box):
    """Return (top, left, bottom, right) of the box's margin/border edge."""
    top = float(getattr(box, "position_y", 0) or 0)
    left = float(getattr(box, "position_x", 0) or 0)
    h = float(getattr(box, "height", 0) or 0)
    w = float(getattr(box, "width", 0) or 0)
    pt = float(getattr(box, "padding_top", 0) or 0)
    pb = float(getattr(box, "padding_bottom", 0) or 0)
    pl = float(getattr(box, "padding_left", 0) or 0)
    pr = float(getattr(box, "padding_right", 0) or 0)
    bt = float(getattr(box, "border_top_width", 0) or 0)
    bb = float(getattr(box, "border_bottom_width", 0) or 0)
    bl = float(getattr(box, "border_left_width", 0) or 0)
    br = float(getattr(box, "border_right_width", 0) or 0)
    mt = float(getattr(box, "margin_top", 0) or 0)
    ml = float(getattr(box, "margin_left", 0) or 0)
    border_box_top = top + mt
    border_box_left = left + ml
    border_box_bottom = border_box_top + bt + pt + h + pb + bb
    border_box_right = border_box_left + bl + pl + w + pr + br
    return border_box_top, border_box_left, border_box_bottom, border_box_right


def _walk_text_boxes(box, out):
    """Collect line/text-bearing boxes for overlap testing. Text content
    in WeasyPrint sits inside ``LineBox`` (anonymous line boxes) or
    ``TextBox`` leaves."""
    tag = type(box).__name__
    if tag in ("LineBox", "TextBox", "InlineBox"):
        if float(getattr(box, "width", 0) or 0) > 0 and float(getattr(box, "height", 0) or 0) > 0:
            out.append(box)
    for c in getattr(box, "children", ()) or ():
        _walk_text_boxes(c, out)


def _deepest_descendant_bottom(box):
    deepest = 0.0

    def _walk(b):
        nonlocal deepest
        y = float(getattr(b, "position_y", 0) or 0)
        h = float(getattr(b, "height", 0) or 0)
        bot = y + h
        if bot > deepest:
            deepest = bot
        for c in getattr(b, "children", ()) or ():
            _walk(c)

    for c in getattr(box, "children", ()) or ():
        _walk(c)
    return deepest


def _find_by_class(box, cls):
    try:
        el = getattr(box, "element", None)
        classes = (el.get("class") if el is not None and hasattr(el, "get") else "") or ""
    except Exception:  # noqa: BLE001
        classes = ""
    if cls in classes.split():
        return box
    for c in getattr(box, "children", ()) or ():
        f = _find_by_class(c, cls)
        if f is not None:
            return f
    return None


def _rects_overlap(a, b, *, slack=0.5):
    """Return overlap area in px². Two rects (top,left,bottom,right)
    overlap when both axes' intervals overlap by > slack."""
    a_top, a_left, a_bot, a_right = a
    b_top, b_left, b_bot, b_right = b
    iw = min(a_right, b_right) - max(a_left, b_left)
    ih = min(a_bot, b_bot) - max(a_top, b_top)
    if iw > slack and ih > slack:
        return iw * ih
    return 0.0


def main():
    snap = Path(__file__).resolve().parent.parent / "logs" / "pdf-assembled-latest.html"
    if not snap.exists():
        print(f"Snapshot not found: {snap}")
        sys.exit(1)
    html = snap.read_text(encoding="utf-8")
    doc = HTML(string=html, base_url=str(snap.parent)).render()

    page = doc.pages[0]
    root = getattr(page, "_page_box", None)
    if root is None:
        print("No _page_box on first page")
        sys.exit(1)

    n_fail = 0

    # ── Figure-vs-hero-bottom overlap: catches the byline-overflowing-
    #    hero-bottom failure mode (the figcaption rendered past the
    #    hero-top's allotted height and into the body text below). ──
    figure = _find_by_class(root, "hero-figure")
    bottom = _find_by_class(root, "hero-bottom")
    if figure is not None and bottom is not None:
        fig_rect = _rect(figure)
        bot_rect = _rect(bottom)
        overlap = _rects_overlap(fig_rect, bot_rect)
        status = "PASS" if overlap <= 1.0 else "FAIL"
        if overlap > 1.0:
            n_fail += 1
        print(
            f"  {status:4} Figure vs hero-bottom overlap: figure_bottom={fig_rect[2]:.1f} "
            f"bottom_top={bot_rect[0]:.1f} overlap_area={overlap:.1f}px²"
        )

    # ── Wrap text deepest content vs hero-bottom top. Big gap = visible
    #    white space between the wrap text and the body continuation.
    #    Crucial: walk only descendant LineBox / TextBox boxes here.
    #    _deepest_descendant_bottom would include the anonymous column
    #    boxes that fill the entire wrap container height, masking the
    #    real text-end position. ──
    wrap = _find_by_class(root, "hero-wrap")
    if wrap is not None and bottom is not None:
        wrap_text_boxes: list = []
        _walk_text_boxes(wrap, wrap_text_boxes)
        if wrap_text_boxes:
            wrap_deepest = max(_rect(tb)[2] for tb in wrap_text_boxes)
        else:
            wrap_deepest = _rect(wrap)[0]  # empty wrap
        bot_top = _rect(bottom)[0]
        gap_px = bot_top - wrap_deepest
        # Allow up to 24pt = 32px of gap (hero-top margin-bottom + figure
        # byline area below image where wrap text legitimately can't reach).
        # Anything beyond that is wrap underfill = visible white band.
        status = "PASS" if gap_px <= 32.0 else "FAIL"
        if gap_px > 32.0:
            n_fail += 1
        print(
            f"  {status:4} Wrap text → hero-bottom gap: wrap_text_bottom={wrap_deepest:.1f} "
            f"bottom_top={bot_top:.1f} gap={gap_px:.1f}px"
        )

    # ── Bottom text fill: how much of hero-bottom is empty below text ──
    if bottom is not None:
        bot_text_boxes: list = []
        _walk_text_boxes(bottom, bot_text_boxes)
        if bot_text_boxes:
            bot_text_deepest = max(_rect(tb)[2] for tb in bot_text_boxes)
            bot_rect = _rect(bottom)
            bot_unfilled = bot_rect[2] - bot_text_deepest
            print(
                f"  INFO Hero-bottom unfilled at bottom: "
                f"bottom_bottom={bot_rect[2]:.1f} text_deepest={bot_text_deepest:.1f} "
                f"unfilled={bot_unfilled:.1f}px"
            )

    # ── Overflow check (each region's content fits its border-box) ──
    regions = [
        ("headline-box", "Headline box"),
        ("hero-body", "Headline body multicolumn"),
        ("upper-bottom", "Upper-bottom news pane"),
        ("upper-right", "Upper-right news pane"),
        ("news-l", "Lower band"),
        ("rail", "Side rail"),
    ]
    for cls, label in regions:
        box = _find_by_class(root, cls)
        if box is None:
            continue
        top, left, bottom, right = _rect(box)
        deepest = _deepest_descendant_bottom(box)
        overflow = deepest - bottom
        status = "PASS" if overflow <= 0.5 else "FAIL"
        if overflow > 0.5:
            n_fail += 1
        print(
            f"  {status:4} {label}: border_box_bottom={bottom:.1f} "
            f"deepest_inside={deepest:.1f} overflow={overflow:+.2f}px"
        )

    # ── Occlusion check: any body text under the floated hero figure ──
    figure = _find_by_class(root, "hero-figure")
    hero_body = _find_by_class(root, "hero-body")
    if figure is not None and hero_body is not None:
        fig_rect = _rect(figure)
        text_boxes: list = []
        _walk_text_boxes(hero_body, text_boxes)
        worst_overlap = 0.0
        worst_box = None
        for tb in text_boxes:
            tb_rect = _rect(tb)
            overlap = _rects_overlap(fig_rect, tb_rect)
            if overlap > worst_overlap:
                worst_overlap = overlap
                worst_box = tb_rect
        status = "PASS" if worst_overlap <= 1.0 else "FAIL"
        if worst_overlap > 1.0:
            n_fail += 1
        print(
            f"\n  {status:4} Figure / body occlusion: figure_rect={fig_rect}, "
            f"worst_overlap_area={worst_overlap:.1f}px²"
        )
        if worst_box is not None and worst_overlap > 1.0:
            print(f"         worst text box rect: {worst_box}")

    print(f"\n  Summary: {n_fail} failure(s)")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
