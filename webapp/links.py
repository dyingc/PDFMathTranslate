"""Put the source document's hyperlinks back into the translated output.

pdf2zh's mono output keeps every link, but assembling the dual document goes
through PyMuPDF's `insert_file`, which cannot carry *named* destinations — the
form LaTeX uses for every cross-reference and table-of-contents entry. On this
book that silently drops 618 of 624 links.

They are rebuilt here as explicit page destinations, which survive any further
copying, and remapped so an internal reference lands on the translated page
rather than the original one.

Rectangles cannot simply be reused: the translation reflows the paragraph, so
"Chapter 12" or "[LSS+15]" ends up somewhere else on the page. Each link is
re-anchored by looking for its anchor text in the output — which works because
the parts that carry links (section numbers, citation keys) are exactly the
parts a translator leaves alone.
"""

import re
from pathlib import Path
from typing import Optional

import pymupdf

_INTERNAL = (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)
# A number, a section number, or a citation key: the tokens that survive
# translation verbatim and can therefore be searched for.
_TOKEN = re.compile(r"[0-9][\w.+\-]*|[A-Z][\w.+\-]{1,}")


def _reanchor(src_page, out_page, rect: pymupdf.Rect) -> Optional[pymupdf.Rect]:
    """Find where the text under `rect` ended up on the translated page."""
    anchor = (src_page.get_textbox(rect) or "").strip()
    if not anchor:
        return None
    for needle in (anchor, *(m.group(0) for m in _TOKEN.finditer(anchor))):
        hits = out_page.search_for(needle, quads=False)
        if not hits:
            continue
        # Layout is roughly preserved, so the nearest hit to the original spot
        # is the right one; an exact single hit needs no tie-breaking at all.
        if len(hits) == 1:
            return hits[0]
        centre = (rect.tl + rect.br) / 2
        return min(hits, key=lambda r: abs((r.tl + r.br) / 2 - centre))
    return None


def _key(link: dict) -> tuple:
    r = pymupdf.Rect(link["from"])
    return (link.get("kind"), round(r.x0, 1), round(r.y0, 1),
            round(r.x1, 1), round(r.y1, 1))


def restore_links(source: Path, target: Path,
                  page_for: Optional[callable] = None) -> int:
    """Rebuild `target`'s links from `source`, in place. Returns links added.

    Links already on a translated page are discarded first: pdf2zh's mono
    output inherits the originals at their original rectangles, which no longer
    line up with the reflowed translation. Only pages this call touches are
    affected, so the English half of a dual document keeps its own links.
    """
    page_for = page_for or (lambda i: i)
    src = pymupdf.open(source)
    out = pymupdf.open(target)
    added = 0
    try:
        for i in range(src.page_count):
            j = page_for(i)
            if j >= out.page_count:
                break
            page = out[j]
            for stale in page.get_links():
                page.delete_link(stale)
            for link in src[i].get_links():
                rect = _reanchor(src[i], page, pymupdf.Rect(link["from"]))
                if rect is None:
                    continue          # anchor text vanished; a misplaced link
                                      # is worse than no link
                new = dict(link)
                new["from"] = rect
                if link.get("kind") in _INTERNAL:
                    dest = link.get("page", -1)
                    if dest < 0 or dest >= src.page_count:
                        continue          # unresolvable, better dropped
                    # Resolve to a plain page destination and point it at the
                    # translated page, so following a link stays in Chinese.
                    new = {"kind": pymupdf.LINK_GOTO, "from": rect,
                           "page": page_for(dest),
                           "to": link.get("to") or pymupdf.Point(0, 0)}
                elif link.get("kind") != pymupdf.LINK_URI:
                    continue              # launch/remote actions are not ours
                try:
                    page.insert_link(new)
                    added += 1
                except Exception:         # noqa: BLE001 - one bad link is not fatal
                    continue
        if added:
            tmp = target.with_suffix(".links.pdf")
            out.save(tmp, deflate=True, garbage=3)
    finally:
        src.close()
        out.close()
    if added:
        tmp.replace(target)
    return added
