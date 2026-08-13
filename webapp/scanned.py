"""Detecting scanned PDFs, and cleaning up the output for them.

A scanned page is a picture of text plus an invisible OCR layer. pdf2zh
replaces the OCR layer with the translation, but the English the reader
actually sees lives in the image and cannot be removed by editing content
streams — so the translation lands on top of it.

`is_scanned()` spots those files before any money is spent. `whiteout()` is the
opt-in repair: it erases the *pixels* of the original text (using the source
document's own text boxes to know where they were) and leaves everything else,
including figures, untouched.
"""

from pathlib import Path
from typing import Optional

import pymupdf

SAMPLE_PAGES = 5
COVERAGE_THRESHOLD = 0.8    # image area / page area, averaged over the sample
PAD = 1.5                   # grow each box slightly; scans are never pixel-exact


def is_scanned(path: Path) -> dict:
    """Heuristic: nearly the whole page is imagery, yet text can be extracted."""
    doc = pymupdf.open(path)
    try:
        n = min(SAMPLE_PAGES, doc.page_count)
        if not n:
            return {"scanned": False, "coverage": 0.0, "chars": 0}
        coverage = chars = 0
        for i in range(n):
            page = doc[i]
            area = page.rect.get_area()
            if area:
                coverage += sum(pymupdf.Rect(b["bbox"]).get_area()
                                for b in page.get_image_info()) / area
            chars += len(page.get_text().strip())
        coverage, chars = coverage / n, chars // n
        return {"scanned": coverage >= COVERAGE_THRESHOLD and chars > 50,
                "coverage": round(coverage, 2), "chars": chars}
    finally:
        doc.close()


def _text_rects(page) -> list:
    """Paragraph boxes of the source page, including its invisible OCR layer.

    Deliberately paragraph-level rather than per-span: an OCR layer's spans do
    not tile the paragraph, so span boxes leave slivers of the scan showing
    between lines. Block boxes only ever cover text, so figures survive.
    """
    rects = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:          # 0 = text, 1 = image
            continue
        rect = pymupdf.Rect(block["bbox"])
        if rect.is_empty or rect.is_infinite or rect.get_area() <= 0:
            continue
        rects.append(rect + (-PAD, -PAD, PAD, PAD))
    return rects


def whiteout(source: Path, target: Path, page_for: Optional[callable] = None) -> int:
    """Erase the scanned original text from `target`, in place.

    `page_for(i)` maps a source page index to the page in `target` holding its
    translation — identity for mono output, 2i+1 for the dual layout.
    """
    page_for = page_for or (lambda i: i)
    src = pymupdf.open(source)
    out = pymupdf.open(target)
    try:
        cleaned = 0
        for i in range(src.page_count):
            j = page_for(i)
            if j >= out.page_count:
                break
            page = out[j]
            rects = _text_rects(src[i])
            if not rects:
                continue
            for rect in rects:
                page.add_redact_annot(rect)
            # Blank the image pixels inside those boxes, but keep the text we
            # put there and leave line art (figures, rules) alone.
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
            cleaned += 1
        tmp = target.with_suffix(".cleaned.pdf")
        out.save(tmp, deflate=True, garbage=3)
    finally:
        src.close()
        out.close()
    tmp.replace(target)
    return cleaned


def dual_page_for(i: int) -> int:
    """In pdf2zh's dual output the translated pages sit at the odd indices."""
    return i * 2 + 1
