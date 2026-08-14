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
                for rect in _state.blocks.get(pages[index], ()):
                    results[0].boxes.append(YoloBox(data=np.array(
                        [rect.x0, rect.y0, rect.x1, rect.y1, 0.99, cls_id])))
        return results

    model.predict = wrapped
    model._verbatim_wrapped = True


@contextmanager
def marking(pages, blocks):
    """Apply `blocks` to the pages translated in this thread, in order."""
    _state.pages, _state.blocks, _state.cursor = list(pages), blocks, 0
    try:
        yield
    finally:
        _state.pages, _state.blocks, _state.cursor = None, {}, 0
