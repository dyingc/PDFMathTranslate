"""Remove text that the source document kept hidden but pdf2zh made visible.

Some figures are drawn in two passes: a first pass (boxes + labels) and then a
second pass painted on top of it. In the source the first pass is completely
covered, so the reader sees one crisp label. pdf2zh rebuilds the page as
"all original graphics, then all text" (pdfinterp.py, `q {ops_base}Q ... {ops_new}`,
so translated text is never buried under an image) — which lifts that hidden
first pass above the covering box and yields a ghosted, doubled label.

The repair only touches text this module can *prove* was invisible in the
source: a span fully covered by an opaque fill painted after it. Everything
else is left exactly as pdf2zh produced it.
"""

from pathlib import Path
from typing import Optional

import pymupdf

PAD = 0.3


def _hidden_regions(page) -> list:
    """((font, size), rect) for text an opaque fill hides on this source page.

    Deliberately per-region: the same font and size is often used both inside a
    figure's hidden pass and elsewhere on the page in plain sight, so a
    style-wide verdict would delete labels that were never ghosts.
    """
    fills = [(d.get("seqno", 0), pymupdf.Rect(d["rect"]))
             for d in page.get_drawings()
             if d.get("fill") is not None and d.get("fill_opacity", 1) == 1]
    hidden = []
    for span in page.get_texttrace():
        if span.get("type") == 3 or not span.get("chars"):
            continue
        box, seq = pymupdf.Rect(span["bbox"]), span.get("seqno", 0)
        if box.is_empty or box.is_infinite:
            continue
        if any(fseq > seq and frect.contains(box) for fseq, frect in fills):
            hidden.append(((span.get("font"), round(span.get("size", 0), 2)),
                           box + (-1, -1, 1, 1)))
    return hidden


def _font_handles(doc, page) -> dict:
    """Reusable names for the page's embedded fonts, keyed by base name."""
    handles = {}
    for i, info in enumerate(page.get_fonts(full=True)):
        xref, basefont = info[0], info[3]
        name = basefont.split("+")[-1]
        try:
            buffer = doc.extract_font(xref)[3]
            if not buffer:
                continue
            handle = f"dg{i}"
            page.insert_font(fontname=handle, fontbuffer=buffer)
            handles[name] = handle
        except Exception:      # noqa: BLE001 - a font we cannot re-embed
            continue
    return handles


def deghost(source: Path, target: Path, page_for: Optional[callable] = None) -> int:
    """Drop the resurfaced copies in `target`, in place. Returns pages changed."""
    page_for = page_for or (lambda i: i)
    src = pymupdf.open(source)
    out = pymupdf.open(target)
    changed = 0
    try:
        for i in range(src.page_count):
            j = page_for(i)
            if j >= out.page_count:
                break
            hidden = _hidden_regions(src[i])
            if not hidden:
                continue
            page = out[j]

            chars = []
            for span in page.get_texttrace():
                if span.get("type") == 3 or not span.get("chars"):
                    continue
                style = (span.get("font"), round(span.get("size", 0), 2))
                for ch in span["chars"]:
                    rect = pymupdf.Rect(ch[3])
                    if rect.is_empty or rect.is_infinite:
                        continue
                    chars.append((rect, chr(ch[0]), ch[2], span, style))

            def is_covered(entry):
                rect, _, _, _, style = entry
                return any(hstyle == style and hrect.contains(rect)
                           for hstyle, hrect in hidden)

            covered = [e for e in chars if is_covered(e)]
            survivors = [e for e in chars if not is_covered(e)]

            # Only drop a hidden-styled character when another character still
            # occupies that spot. The two passes are often different revisions
            # of the same figure (COND vs PRED here), so the survivor need not
            # be identical — but if nothing survives, the label would vanish,
            # and a ghost is better than a hole.
            ghosts, keep = [], list(survivors)
            for entry in covered:
                rect = entry[0]
                if any((rect & other[0]).get_area() > 0.4 * rect.get_area()
                       for other in survivors):
                    ghosts.append(entry[:4])
                else:
                    keep.append(entry)
            keep = [e[:4] for e in keep]
            if not ghosts:
                continue

            # Redaction works on areas, so it takes the surviving copy with it.
            # Note what has to be put back before erasing anything.
            areas = [r + (-PAD, -PAD, PAD, PAD) for r, _, _, _ in ghosts]
            restore = [e for e in keep if any(e[0].intersects(a) for a in areas)]

            for area in areas:
                page.add_redact_annot(area)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)

            handles = _font_handles(out, page)
            for _, char, origin, span in restore:
                handle = handles.get((span.get("font") or "").split("+")[-1])
                if not handle:
                    continue
                page.insert_text(origin, char, fontname=handle,
                                 fontsize=span.get("size", 10),
                                 color=span.get("color", (0, 0, 0)))
            changed += 1
        if changed:
            tmp = target.with_suffix(".deghost.pdf")
            out.save(tmp, deflate=True, garbage=3)
    finally:
        src.close()
        out.close()
    if changed:
        tmp.replace(target)
    return changed
