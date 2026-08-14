"""Leave tables of contents untranslated.

pdf2zh's unit of layout is the region the layout model detects, and a whole
table of contents is one such region: its entries get merged into a single
paragraph and re-wrapped, so "1.1 Applications ... 1" and the next entry run
together into flowing text.

Fixing that properly means splitting the layout map per line, deep inside
`high_level.translate_patch`. Skipping the page instead costs a dozen lines:
the page is copied through untouched, so the contents stay in the source
language but keep their alignment, dot leaders and — since the page numbers do
not change — their usefulness. Same trade as code blocks: a correct English
block beats a mangled Chinese one.
"""

import re
from pathlib import Path

import pymupdf

# Four or more dot leaders on a line, e.g. "The Syntax of TIP . . . . . 9".
_LEADER = re.compile(r"(?:[.·]\s?){4,}")
_MIN_LINES = 4          # below this it is prose that merely contains an ellipsis


def toc_pages(path: Path) -> set:
    """0-based indices of pages that look like a table of contents."""
    doc = pymupdf.open(path)
    try:
        found = set()
        for i, page in enumerate(doc):
            hits = 0
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"])
                    if _LEADER.search(text):
                        hits += 1
            if hits >= _MIN_LINES:
                found.add(i)
        return found
    finally:
        doc.close()


def without_toc(path: Path, pages) -> tuple:
    """Return (pages_to_translate, skipped) with contents pages removed.

    `pages` is the caller's selection, or None for "everything".
    """
    skip = toc_pages(path)
    if not skip:
        return pages, set()
    doc = pymupdf.open(path)
    try:
        wanted = list(pages) if pages is not None else list(range(doc.page_count))
    finally:
        doc.close()
    kept = [p for p in wanted if p not in skip]
    if not kept:                     # a document that is only contents
        return pages, set()
    return kept, skip & set(wanted)
