"""Notice when a document is already in the language it is being translated to.

Uploading last week's output instead of the original is an easy mistake — the
files sit next to each other and differ by a suffix — and an expensive one: a
dual document holds both languages, so the bill roughly doubles and half of it
is spent translating Chinese into Chinese. It happened here, on a 24-page paper:
390 paragraphs instead of 203, 201 of them already Chinese, ¥0.59 instead of
¥0.18.

The check only claims what a script can actually tell. Chinese, Japanese,
Korean and Russian are written in scripts a Latin-script source never uses, so
counting characters answers the question outright. French, German, Spanish and
English share an alphabet, and separating them needs word lists this module has
no business carrying — for those targets it stays quiet rather than guessing.
"""

from pathlib import Path

import pymupdf

# Ranges that a Latin-script document does not use. Han is shared between
# Chinese and Japanese, which is why the Japanese test also accepts kana: a
# Chinese document should not be mistaken for a Japanese one.
_HAN = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))
_KANA = ((0x3040, 0x30FF),)
_HANGUL = ((0xAC00, 0xD7AF), (0x1100, 0x11FF))
_CYRILLIC = ((0x0400, 0x04FF),)

SCRIPTS = {
    "zh": _HAN,
    "zh-TW": _HAN,
    "ja": _HAN + _KANA,
    "ko": _HANGUL,
    "ru": _CYRILLIC,
}

# Counted by page, not by character. Chinese is far denser than English — the
# same content is roughly 800 characters against 3000 — so a dual document,
# half of it Chinese, measures only 19% by character and slips under any
# sensible bar. By page it measures 50%, which is what it actually is.
THRESHOLD = 0.25         # of the pages that carry text
PAGE_SHARE = 0.5         # of a page's letters, before the page counts as translated
MIN_PAGE_CHARS = 50      # too little text on the page to judge it by


def _in(ranges, code: int) -> bool:
    return any(lo <= code <= hi for lo, hi in ranges)


def already_translated(path: Path, lang_out: str) -> dict:
    """The share of pages already written in the target language."""
    ranges = SCRIPTS.get(lang_out)
    if not ranges:
        return {"hit": False, "ratio": 0.0}
    doc = pymupdf.open(path)
    try:
        pages = hits = 0
        for page in doc:
            target = latin = 0
            for ch in page.get_text():
                code = ord(ch)
                if _in(ranges, code):
                    target += 1
                elif ch.isalpha() and code < 0x0250:
                    latin += 1
            if target + latin < MIN_PAGE_CHARS:
                continue
            pages += 1
            if target / (target + latin) >= PAGE_SHARE:
                hits += 1
    finally:
        doc.close()
    if not pages:
        return {"hit": False, "ratio": 0.0}
    ratio = hits / pages
    return {"hit": ratio >= THRESHOLD, "ratio": ratio}
