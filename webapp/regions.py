"""Regions the reader marked by hand, so a block is carried over untouched.

`verbatim.py` finds blocks to preserve by their typeface, which only works for
code set in a typewriter font. Pseudo-code, rule tables and aligned equations
are set in the body face, and the reader marks those by hand because detecting
them automatically was tried and abandoned. What was tried, so it is not tried
again the same way:

* Typeface. In one textbook the unification pseudo-code is TeXGyrePagella, the
  same font as the prose three lines above it. There is nothing to key on.
* The layout model. It calls that block "plain text" with 0.97 confidence, and
  looking at it, that is not an unreasonable reading: body face, ragged lines,
  no rule or panel around it.
* Geometry, five ways — lines short of the right margin; the margin taken as a
  consensus rather than a maximum; runs split by vertical gap; runs of
  consecutive short lines; agreement with a neighbour's right edge. On ten
  documents the best of them still flagged 15-37% of all lines, ordinary
  paragraphs among them. Each version was derived from whichever page had last
  been looked at, which is the shape of the mistake more than any one rule was.

Detection may well be possible. It is not possible by inspection, which is the
only method that had been applied: it would need a labelled set built first and
candidate rules scored against it. Until someone does that, the reader points.

The marks are ordinary PDF annotations, drawn in whatever viewer is at hand and
read back here, which is why there is no drawing surface to build or maintain.
A convention keeps them apart from the reader's own notes: a rectangle, black,
dashed. Highlights, sticky notes and freehand are ignored outright, and a solid
or coloured rectangle is taken to be a note rather than an instruction.

Coordinates stay in PDF points, top-down, which is what `verbatim_blocks`
already hands to the layout model — no flip, unlike the path into pdfminer.
"""

import json
from pathlib import Path

import pymupdf

from webapp.store import DATA_DIR

# Black, within the slack a viewer's colour picker leaves.
BLACK = 0.25
# A viewer may round the width; the dashes are what carries the meaning, so the
# width is only checked loosely, to keep a thick dashed box from qualifying.
MAX_WIDTH = 2.0


def _is_mark(annot) -> bool:
    if annot.type[1] != "Square":
        return False
    border = annot.border or {}
    if not border.get("dashes"):
        return False
    if (border.get("width") or 0) > MAX_WIDTH:
        return False
    stroke = (annot.colors or {}).get("stroke") or []
    return bool(stroke) and all(c <= BLACK for c in stroke)


def marked_regions(path: Path) -> dict:
    """Marked rectangles per zero-based page index."""
    doc = pymupdf.open(path)
    try:
        found = {}
        for i, page in enumerate(doc):
            boxes = [tuple(annot.rect) for annot in page.annots()
                     if _is_mark(annot)]
            if boxes:
                found[i] = boxes
        return found
    finally:
        doc.close()


def strip(marked: Path, target: Path) -> None:
    """Write `marked` to `target` without its annotations.

    A marked copy is the original plus rectangles. Used as the source it would
    carry those rectangles into the translation, so they come off first — the
    text, and therefore the document's identity, is untouched either way.
    """
    doc = pymupdf.open(marked)
    try:
        for page in doc:
            for annot in reversed(list(page.annots())):
                page.delete_annot(annot)
        doc.save(target)
    finally:
        doc.close()


def _path(doc_key: str) -> Path:
    folder = DATA_DIR / "regions"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{doc_key}.json"


def save(doc_key: str, regions: dict) -> None:
    """Remember the regions for this document, keyed by its own text.

    Keyed by content rather than by job so that marking a book once carries to
    every later translation of it — a different model, a different page range,
    a re-uploaded copy of the same file.
    """
    _path(doc_key).write_text(json.dumps({str(k): v for k, v in regions.items()}))


def load(doc_key: str) -> dict:
    try:
        raw = json.loads(_path(doc_key).read_text())
    except (OSError, ValueError):
        return {}
    return {int(k): [pymupdf.Rect(*b) for b in v] for k, v in raw.items()}


def merge(blocks: dict, regions: dict) -> int:
    """Add the marked regions to what `verbatim_blocks` already found."""
    for page, rects in regions.items():
        blocks.setdefault(page, []).extend(rects)
    return sum(len(r) for r in regions.values())
