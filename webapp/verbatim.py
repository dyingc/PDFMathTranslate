"""Keep verbatim blocks out of the surrounding paragraph.

pdf2zh's unit of layout is the region the layout model reports, and that model
happily wraps prose, a code block, more prose and another code block into one
"plain text" region. Everything in a region becomes one paragraph, and a
paragraph that starts with prose is re-flowed — so the code blocks get inlined
into running text and land on top of it.

The layout model only sees a picture, but the PDF knows exactly where its
monospaced blocks are. This module hands those rectangles back to the model's
output as `isolate_formula` regions, which `translate_patch` paints as
"preserve" areas. Each block then becomes its own paragraph, anchored at its
original position, and the prose around it is split at the right places.
"""

import threading
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pymupdf
from pdf2zh.doclayout import YoloBox

# Font families used for code and other verbatim material.
MONO_FONTS = ("txtt", "cmtt", "Courier", "Mono", "Consol", "Inconsolata",
              "Menlo", "SFMono", "Typewriter")
MIN_LINES = 2        # a single monospaced word inside a sentence is not a block
LINE_GAP = 8.0       # points; larger gaps mean separate blocks
PAD = 1.0

_state = threading.local()


def _is_mono(span) -> bool:
    font = (span.get("font") or "").split("+")[-1]
    return any(m in font for m in MONO_FONTS)


def verbatim_blocks(path: Path) -> dict:
    """Monospaced blocks per page, as rectangles in PDF coordinates."""
    doc = pymupdf.open(path)
    try:
        found = {}
        for i, page in enumerate(doc):
            mono, prose = [], []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    spans = line.get("spans") or []
                    if not spans:
                        continue
                    rect = pymupdf.Rect(line["bbox"])
                    (mono if all(_is_mono(s) for s in spans) else prose).append(rect)
            if not mono:
                continue
            mono.sort(key=lambda r: r.y0)
            clusters = []
            for rect in mono:
                if clusters and rect.y0 - clusters[-1][-1].y1 < LINE_GAP:
                    clusters[-1].append(rect)
                else:
                    clusters.append([rect])
            rects = []
            for cluster in clusters:
                if len(cluster) < MIN_LINES:
                    continue
                box = pymupdf.Rect(cluster[0])
                for rect in cluster[1:]:
                    box |= rect
                # A band shared with prose is a paragraph, not a block; marking
                # it would cut the paragraph in half.
                if any(box.y0 < p.y1 and p.y0 < box.y1 for p in prose):
                    continue
                rects.append(box + (-PAD, -PAD, PAD, PAD))
            if rects:
                found[i] = rects
        return found
    finally:
        doc.close()


def text_lines(path: Path) -> dict:
    """Every text line per page, as rectangles in PDF coordinates."""
    doc = pymupdf.open(path)
    try:
        found = {}
        for i, page in enumerate(doc):
            rects = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    rect = pymupdf.Rect(line["bbox"])
                    if rect.width > 20 and not rect.is_empty:
                        rects.append(rect)
            if rects:
                found[i] = rects
        return found
    finally:
        doc.close()


# Layout classes pdf2zh preserves rather than reflows; widening one of those
# would be a different operation entirely.
VCLS = ("abandon", "figure", "table", "isolate_formula", "formula_caption")
COVERS = 0.5      # of the line's height, before a region counts as "on" the line
MARGIN = 2.0      # points of slack at the line's ends


def heal_cuts(result, lines) -> int:
    """Widen regions that cut a text line, so the line stays one paragraph.

    pdf2zh gives every layout region its own paragraph id and assigns each
    character the id of the region it falls in. A region that covers the middle
    of a line but not its ends therefore splits that line into three paragraphs
    — in this book, mid-word: "More ex" / "ample programs ... implementatio" /
    "n of TIP.". The offenders are low-confidence detections that overlap real
    text; on SPA.pdf one of 0.47 confidence cut a line at exactly x=195 and
    x=442.

    Widening, rather than dropping, keeps whatever the model was right about:
    the region still exists and still starts and ends where it did vertically.
    Only its horizontal extent grows, and only far enough to contain lines it
    was already sitting on — so in a two-column layout it cannot reach the
    other column, because no line spans both.
    """
    healed = 0
    for i, box in enumerate(result.boxes):
        if result.names[int(box.cls)] in VCLS:
            continue
        x0, y0, x1, y1 = (float(v) for v in np.array(box.xyxy).squeeze())
        wide0, wide1 = x0, x1
        for line in lines:
            if min(y1, line.y1) - max(y0, line.y0) <= COVERS * line.height:
                continue                      # barely touching, not sitting on it
            if x1 <= line.x0 or x0 >= line.x1:
                continue                      # elsewhere on the page
            if x0 <= line.x0 + MARGIN and x1 >= line.x1 - MARGIN:
                continue                      # already holds the whole line
            wide0, wide1 = min(wide0, line.x0), max(wide1, line.x1)
        if wide0 < x0 or wide1 > x1:
            result.boxes[i] = YoloBox(data=np.array(
                [wide0, y0, wide1, y1, float(box.conf), float(box.cls)]))
            healed += 1
    return healed


def install(model) -> None:
    """Wrap the layout model once so marked pages get extra regions."""
    if getattr(model, "_verbatim_wrapped", False):
        return
    names = model._names
    pairs = names.items() if isinstance(names, dict) else enumerate(names)
    cls_id = next((k for k, v in pairs if v == "isolate_formula"), None)
    if cls_id is None:
        return
    predict = model.predict

    def wrapped(image, *args, **kwargs):
        results = predict(image, *args, **kwargs)
        pages = getattr(_state, "pages", None)
        if pages and results:
            index = _state.cursor
            _state.cursor += 1
            # translate_patch calls predict exactly once per page, in the order
            # of the page list we passed it — see the smoke test.
            if index < len(pages):
                page = pages[index]
                for rect in _state.blocks.get(page, ()):
                    results[0].boxes.append(YoloBox(data=np.array(
                        [rect.x0, rect.y0, rect.x1, rect.y1, 0.99, cls_id])))
                # After ours are in, so a block we added is healed too.
                lines = getattr(_state, "lines", {}).get(page, ())
                if lines:
                    heal_cuts(results[0], lines)
        return results

    model.predict = wrapped
    model._verbatim_wrapped = True


@contextmanager
def marking(pages, blocks, lines=None):
    """Apply `blocks` and line healing to this thread's pages, in order."""
    _state.pages, _state.blocks, _state.cursor = list(pages), blocks, 0
    _state.lines = lines or {}
    try:
        yield
    finally:
        _state.pages, _state.blocks, _state.cursor = None, {}, 0
        _state.lines = {}
