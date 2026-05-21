"""Probe whether the upper-bottom fit measurement diverges from the
final assembly's rendered height.

Renders the same content twice:
  1. The way `_try_fit` does — a single-region page sized exactly to
     (W=box_w_in, H=bottom_h_in).
  2. The way assemble_final_html does — inside a flex column container
     mirroring .upper-left / .headline-box / .upper-bottom.

For each, walks the box tree, prints the deepest descendant bottom of
the upper-bottom equivalent. If (2) > (1), the assembly is rendering
the same content TALLER than the fit step measured, which would explain
the visual cut-off under `overflow: hidden`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import order matters — app.pdf loads the WeasyPrint native libs via
# os.add_dll_directory at import time.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import date  # noqa: E402

from weasyprint import HTML  # noqa: E402

from app import pdf as _pdf  # noqa: F401, E402  — import for DLL registration
from app.db import Edition, session_factory  # noqa: E402
from app.pdf_renderer import (  # noqa: E402
    build_pdf_parts,
    build_single_region_html,
    news_region_css,
)


def _find_box_by_class(root, class_name):
    el_classes = []
    try:
        el = root.element if hasattr(root, "element") else None
        if el is not None and hasattr(el, "get"):
            el_classes = (el.get("class") or "").split()
    except Exception:
        pass
    if class_name in el_classes:
        return root
    for child in getattr(root, "children", ()) or ():
        found = _find_box_by_class(child, class_name)
        if found is not None:
            return found
    return None


def _deepest(box):
    deepest = 0.0
    try:
        y = float(getattr(box, "position_y", 0) or 0)
        h = float(getattr(box, "height", 0) or 0)
        deepest = y + h
    except (TypeError, ValueError):
        pass

    def _walk(b):
        nonlocal deepest
        try:
            y = float(getattr(b, "position_y", 0) or 0)
            h = float(getattr(b, "height", 0) or 0)
            bottom = y + h
            if bottom > deepest:
                deepest = bottom
        except (TypeError, ValueError):
            pass
        for c in getattr(b, "children", ()) or ():
            _walk(c)

    for c in getattr(box, "children", ()) or ():
        _walk(c)
    return deepest


def main():
    Maker = session_factory()
    with Maker() as s:
        row = s.get(Edition, (date(2026, 5, 14), "vtlinh87@gmail.com"))
        linhnews = dict(row.content_json or {})

    parts = build_pdf_parts(
        linhnews,
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        weather_prose_html="",
        today=date(2026, 5, 14),
        masthead_name="Linh",
    )
    upper_bottom_html = ""
    # We need the actual upper-bottom slice that pdf.html_to_pdf_ex would
    # build. That requires the same below-news split logic. Approximate by
    # taking the first N sections from upper_sections that fit by area —
    # but the easier path: render the whole assembly via pdf.html_to_pdf_ex
    # and pull the rendered HTML from the trace? It's not exposed.
    #
    # Simpler approach: query the edition's persisted PDF rail-style cache
    # for the upper-bottom HTML if available; otherwise just dump the first
    # upper_sections as a representative payload at 10.1pt and compare.
    if not upper_bottom_html:
        from app.pdf_renderer import _render_news_band

        upper_sections = parts.upper_sections or []
        # The actual split happens in pdf.html_to_pdf_ex; emulate "take all
        # upper_sections" for now — overestimates, but tells us whether the
        # measurement vs. assembly diverges on the SAME content.
        upper_bottom_html = _render_news_band(upper_sections, image_bytes_by_id={})

    # ── Sizes from the actual recent run log ──
    # box_w_in = 8.88, bottom_h_in = 8.67, upper_bottom_font_pt = 10.1.
    box_w = 8.88
    bottom_h = 8.67
    font_pt = 10.1
    headline_h = 7.0  # approx — anything; doesn't affect width measurement

    below_css = news_region_css().replace("column-count: 4", "column-count: 3")

    # ─────────────────── Render 1: fit-step style ───────────────────
    fit_html = build_single_region_html(
        upper_bottom_html,
        width_in=box_w,
        height_in=bottom_h,
        font_pt=font_pt,
        region_css=below_css,
        font_face_css=parts.font_face_css,
    )
    fit_doc = HTML(string=fit_html).render()
    fit_page = fit_doc.pages[0]
    fit_page_h_px = float(getattr(fit_page, "height", 0) or 0)
    fit_root = getattr(fit_page, "_page_box", None)
    fit_deepest = _deepest(fit_root) if fit_root else 0.0
    print(f"FIT step:    page_h_px={fit_page_h_px:.2f} deepest={fit_deepest:.2f}")
    print(f"             (page H = {bottom_h:.3f}in * 96 dpi = {bottom_h * 96:.1f}px)")

    # ─────────────────── Render 2: flex-assembly style ───────────────────
    # Reproduce just .upper-left containing .headline-box (dummy) +
    # .upper-bottom (real content). One CSS bundle covers @page, body,
    # .upper-left flex container, .headline-box stub, .upper-bottom rules.
    headline_box_h = headline_h
    bottom_h_assembly = bottom_h  # mirrors the height the assembly uses
    upper_h = headline_h + bottom_h_assembly + 6 / 72
    css_extra = f"""
    @page {{ size: {box_w:.3f}in {upper_h:.3f}in; margin: 0; }}
    html, body {{ margin: 0; padding: 0;
                  font-family: "Times New Roman", Georgia, serif; }}
    body {{ line-height: 1.15; }}
    .upper-left {{ display: flex; flex-direction: column;
                   width: {box_w:.3f}in;
                   height: {upper_h:.3f}in;
                   max-height: {upper_h:.3f}in;
                   min-width: 0;
                   overflow: hidden;
                   box-sizing: border-box; }}
    .headline-box {{ flex: 0 0 {(headline_box_h - 13 / 72):.3f}in;
                     height: {(headline_box_h - 13 / 72):.3f}in;
                     width: {box_w:.3f}in;
                     border: 1pt solid #000;
                     padding: 8pt 8pt 3pt 8pt;
                     margin-bottom: 6pt;
                     overflow: hidden;
                     box-sizing: border-box; }}
    .upper-bottom {{ flex: 1 1 auto;
                     width: {box_w:.3f}in;
                     height: {bottom_h_assembly:.3f}in;
                     max-height: {bottom_h_assembly:.3f}in;
                     min-width: 0;
                     font-size: {font_pt:.2f}pt;
                     line-height: 1.15;
                     overflow: hidden;
                     box-sizing: border-box; }}
    """
    scoped_below = below_css.replace(".region", ".upper-bottom")

    assembly_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        + parts.font_face_css
        + css_extra
        + scoped_below
        + "</style></head><body>"
        + '<div class="upper-left">'
        + '<div class="headline-box">HEADLINE STUB</div>'
        + f'<div class="upper-bottom">{upper_bottom_html}</div>'
        + "</div></body></html>"
    )
    asm_doc = HTML(string=assembly_html).render()
    asm_page = asm_doc.pages[0]
    asm_page_h_px = float(getattr(asm_page, "height", 0) or 0)
    asm_root = getattr(asm_page, "_page_box", None)

    # Walk the box tree to find the .upper-bottom box specifically and
    # measure its content deepest, vs the page deepest.
    def _find_upper_bottom(box):
        try:
            el = getattr(box, "element_tag", None)
        except Exception:
            el = None
        try:
            elem = getattr(box, "element", None)
            classes = (elem.get("class") if elem is not None and hasattr(elem, "get") else "") or ""
        except Exception:
            classes = ""
        if el == "div" and "upper-bottom" in classes.split():
            return box
        for c in getattr(box, "children", ()) or ():
            found = _find_upper_bottom(c)
            if found is not None:
                return found
        return None

    ub_box = _find_upper_bottom(asm_root) if asm_root else None
    asm_page_deepest = _deepest(asm_root) if asm_root else 0.0
    ub_deepest = _deepest(ub_box) if ub_box is not None else 0.0
    ub_top = float(getattr(ub_box, "position_y", 0) or 0) if ub_box else 0.0
    ub_height = float(getattr(ub_box, "height", 0) or 0) if ub_box else 0.0

    print()
    print(f"ASM (flex):  page_h_px={asm_page_h_px:.2f}  page_deepest={asm_page_deepest:.2f}")
    if ub_box is not None:
        print(
            f"             .upper-bottom: top={ub_top:.2f} h={ub_height:.2f} "
            f"deepest_inside={ub_deepest:.2f}  overflow_by={ub_deepest - (ub_top + ub_height):.2f}"
        )
    else:
        print("             .upper-bottom box not found")

    # Find headline-box for diagnostics.
    def _find_class(box, cls):
        try:
            elem = getattr(box, "element", None)
            classes = (elem.get("class") if elem is not None and hasattr(elem, "get") else "") or ""
        except Exception:
            classes = ""
        if cls in classes.split():
            return box
        for c in getattr(box, "children", ()) or ():
            f = _find_class(c, cls)
            if f is not None:
                return f
        return None

    hb = _find_class(asm_root, "headline-box") if asm_root else None
    ul = _find_class(asm_root, "upper-left") if asm_root else None
    if ul is not None:
        ul_top = float(getattr(ul, "position_y", 0) or 0)
        ul_h = float(getattr(ul, "height", 0) or 0)
        print(
            f"             .upper-left: top={ul_top:.2f} h={ul_h:.2f} "
            f"requested={(7.0 + 8.67 + 6 / 72) * 96:.2f}px"
        )
    if hb is not None:
        hb_top = float(getattr(hb, "position_y", 0) or 0)
        hb_h = float(getattr(hb, "height", 0) or 0)
        hb_mt = float(getattr(hb, "margin_top", 0) or 0)
        hb_mb = float(getattr(hb, "margin_bottom", 0) or 0)
        hb_pt = float(getattr(hb, "padding_top", 0) or 0)
        hb_pb = float(getattr(hb, "padding_bottom", 0) or 0)
        hb_bt = float(getattr(hb, "border_top_width", 0) or 0)
        hb_bb = float(getattr(hb, "border_bottom_width", 0) or 0)
        print(
            f"             .headline-box: top={hb_top:.2f} h={hb_h:.2f} "
            f"margin=({hb_mt:.1f},{hb_mb:.1f}) padding=({hb_pt:.1f},{hb_pb:.1f}) "
            f"border=({hb_bt:.1f},{hb_bb:.1f})"
        )
        outer_bottom = hb_top + hb_mt + hb_bt + hb_pt + hb_h + hb_pb + hb_bb + hb_mb
        print(
            f"             headline-box margin-box bottom={outer_bottom:.2f}px"
            f"   |  .upper-bottom top={ub_top:.2f}px  |  gap={ub_top - outer_bottom:.2f}px"
        )
    if ub_box is not None:
        ub_mt = float(getattr(ub_box, "margin_top", 0) or 0)
        ub_pt = float(getattr(ub_box, "padding_top", 0) or 0)
        ub_bt = float(getattr(ub_box, "border_top_width", 0) or 0)
        print(
            f"             upper-bottom margin_top={ub_mt:.1f} padding_top={ub_pt:.1f} "
            f"border_top={ub_bt:.1f}"
        )


if __name__ == "__main__":
    main()
