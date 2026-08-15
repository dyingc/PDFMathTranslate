"""Translate paragraphs in context instead of one at a time.

pdf2zh translates each paragraph with an isolated request: no document title, no
neighbouring text, no terminology. Measured on this app's own cache, 17% of the
units it sends are shorter than 40 characters — headings, section numbers, and
in one case the bare letter "n". A model given that much to work with can only
guess, and it guesses differently every time, so terminology drifts across a
document.

The obstacle is architectural: pdf2zh asks for a translation from inside its
layout loop, one paragraph at a time, and never has the next one to hand. So the
work is done *before* that loop instead:

 1. run the whole layout pass with translation disabled, which costs no API call
    and yields exactly the paragraphs pdf2zh will later ask for, in order;
 2. describe the document once, from a sample of those paragraphs;
 3. translate them in chunks of a few thousand characters, so a paragraph is
    surrounded by its neighbours and headings sit above the text they head;
 4. write each translation back into pdf2zh's cache under its own paragraph.

The real pass then finds everything cached and never calls the API. Because the
cache stays keyed per paragraph, entries remain reusable across documents.

Every step is best-effort: anything not cached here is simply translated the old
way, one paragraph at a time. A failure costs quality, never correctness.
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# Big enough that a paragraph sees real context, small enough that the model
# does not start summarising — long outputs drift.
CHUNK_CHARS = 2000
PROFILE_CHARS = 6000     # sampled across the document, not just the front
MAX_TERMS_SAMPLE = 40

# A paragraph with no letter in it (page numbers, "1.", stray symbols) or one or
# two characters carries nothing to translate, and sending it is how "n" becomes
# "TIP 的 n。".
_PLACEHOLDER = re.compile(r"\{v\d+\}|</?b\d+>")
MIN_CHARS = 3


def is_trivial(text: str) -> bool:
    """True if translating `text` could only invent something."""
    bare = _PLACEHOLDER.sub("", text).strip()
    if len(bare) < MIN_CHARS:
        return True
    return not any(ch.isalpha() for ch in bare)


# ---------------------------------------------------------------------------
# Step 1: collect the paragraphs pdf2zh will ask for.

_collected: dict = {}
_lock = threading.Lock()


def collect_into(job_id: str) -> list:
    """Register a sink for `job_id` and return it."""
    with _lock:
        sink = _collected[job_id] = []
    return sink


def record(job_id: str, text: str) -> None:
    with _lock:
        sink = _collected.get(job_id)
    if sink is not None:
        sink.append(text)


def drop(job_id: str) -> list:
    with _lock:
        return _collected.pop(job_id, [])


# ---------------------------------------------------------------------------
# Cache keys wide enough to be worth reusing.

# A paragraph is cached under itself only if it is long enough to identify what
# it means. "2.1 Overview" or "In this section we describe our approach." are
# translated differently depending on what follows, and as a key they would
# match any document in the world — so a short paragraph is keyed by itself plus
# as much of what follows as it takes to reach this many characters.
MIN_KEY_CHARS = 200


def build_keys(paragraphs: list) -> dict:
    """Cache key per paragraph text, widened until it is distinctive."""
    keys = {}
    for i, para in enumerate(paragraphs):
        if len(para) >= MIN_KEY_CHARS:
            keys.setdefault(para, para)
            continue
        parts, total, j = [para], len(para), i + 1
        while total < MIN_KEY_CHARS and j < len(paragraphs):
            parts.append(paragraphs[j])
            total += len(paragraphs[j])
            j += 1
        # First occurrence wins, so both passes agree on repeated headers.
        keys.setdefault(para, "\n".join(parts))
    return keys


_keys: dict = {}


def use_keys(job_id: str, paragraphs: list) -> None:
    with _lock:
        _keys[job_id] = build_keys(paragraphs)


def forget_keys(job_id: str) -> None:
    with _lock:
        _keys.pop(job_id, None)


def cache_key(job_id: str, text: str):
    """The key to cache `text` under, or None if it must not be cached.

    Without a key map — context disabled, or a paragraph the collecting pass
    never saw — a short paragraph has no context to widen it with. Caching it
    under its bare self would hand the same translation to every future document
    that happens to contain the same few words, so it is not cached at all.
    """
    if len(text) >= MIN_KEY_CHARS:
        return text
    with _lock:
        keys = _keys.get(job_id)
    return keys.get(text) if keys else None


# ---------------------------------------------------------------------------
# Step 2: describe the document.

_PROFILE_PROMPT = (
    "Below are excerpts from a document. Reply with a JSON object with keys "
    '"title", "field" and "summary". "title" is the document\'s title, "field" '
    'its academic or technical field, "summary" at most 25 words on what it is '
    "about. Use the language of the document itself."
)


def _sample(paragraphs: list, budget: int = PROFILE_CHARS) -> str:
    """Excerpts spread over the whole document, not just its opening pages.

    Core terminology often appears first in a methods section halfway through;
    a prefix sample would miss exactly the words that most need pinning down.
    """
    usable = [p for p in paragraphs if not is_trivial(p)]
    if not usable:
        return ""
    step = max(1, len(usable) * 200 // max(budget, 1))
    picked, total = [], 0
    for para in usable[::step]:
        para = para[:600]
        if total + len(para) > budget:
            break
        picked.append(para)
        total += len(para)
    return "\n\n".join(picked)


def describe(tr, paragraphs: list, title: str = "") -> dict:
    """One call, or none: a title/field/summary block for every later prompt."""
    excerpt = _sample(paragraphs)
    if not excerpt:
        return {}
    try:
        content = _ask(tr, [
            {"role": "user",
             "content": f"{_PROFILE_PROMPT}\n\n{excerpt}"},
        ])
        profile = json.loads(content)
        if not isinstance(profile, dict):
            return {}
    except Exception as exc:      # noqa: BLE001 - context is an optimisation
        logger.warning("document profile failed: %s", exc)
        return {}
    if title and not profile.get("title"):
        profile["title"] = title
    return {k: str(v)[:300] for k, v in profile.items()
            if k in ("title", "field", "summary") and v}


# ---------------------------------------------------------------------------
# Step 3: translate in chunks.

def chunks(items: list, limit: int = CHUNK_CHARS) -> list:
    """Group consecutive (index, text) pairs without ever splitting one."""
    out, current, size = [], [], 0
    for item in items:
        length = len(item[1])
        if current and size + length > limit:
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        out.append(current)
    return out


def _rules(tr) -> str:
    return (
        f"You are a professional translator working on one document. "
        f"You translate {tr.lang_in} into {tr.lang_out}.\n\n"
        "Rules:\n"
        "- Translate every segment faithfully. Never merge, split, reorder, "
        "summarise or omit a segment.\n"
        "- Copy placeholders such as {v0}, {v12} and tags such as <b3></b3> "
        "into the translation exactly as they appear, in the same order.\n"
        "- A segment may be a heading or a sentence fragment; translate it as "
        "what it is, using the surrounding segments to understand it.\n"
        "- Keep terminology identical throughout the whole document.\n"
        "- Output the translation only, with no commentary.\n\n"
        'Reply with a JSON object {"segments": [{"id": <int>, "text": '
        "<translation>}]} holding exactly one entry per input segment, with the "
        "same ids."
    )


def _preamble(tr, profile: dict) -> str:
    """The part of the prompt that never changes, so it stays cache-hot.

    DeepSeek prices a cached prompt prefix at a fiftieth of a fresh one, but
    only matches from the first token — so everything constant has to come
    first and the per-chunk text last.
    """
    block = ""
    if profile:
        lines = [f"{k.capitalize()}: {v}" for k, v in profile.items()]
        block = "This document:\n" + "\n".join(lines) + "\n\n"
    return f"{_rules(tr)}\n\n{block}"


def _ask(tr, messages: list) -> str:
    response = tr.client.chat.completions.create(
        model=tr.model,
        messages=messages,
        response_format={"type": "json_object"},
        **tr.options,
    )
    return response.choices[0].message.content


def _translate_chunk(tr, preamble: str, chunk: list) -> dict:
    """{index: translation} for one chunk, or {} if the reply cannot be trusted.

    Ids make a mismatch loud and local: a model that merges two segments returns
    a set of ids that no longer matches, so the chunk is dropped rather than
    silently shifting every later paragraph onto the wrong piece of the page.
    """
    payload = {"segments": [{"id": i, "text": text} for i, text in chunk]}
    messages = [
        {"role": "system", "content": preamble},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    want = {i for i, _ in chunk}
    for attempt in range(2):
        try:
            data = json.loads(_ask(tr, messages))
            segments = data["segments"]
            got = {int(s["id"]): str(s["text"]) for s in segments}
        except Exception as exc:      # noqa: BLE001
            logger.warning("chunk translation failed (try %d): %s", attempt + 1, exc)
            continue
        if set(got) != want or not all(t.strip() for t in got.values()):
            logger.warning("chunk translation misaligned (try %d): "
                           "missing %s, extra %s", attempt + 1,
                           sorted(want - set(got)), sorted(set(got) - want))
            continue
        return got
    return {}


def prepare(tr, paragraphs: list, profile: dict = None, progress=None,
            threads: int = 1) -> tuple:
    """Fill pdf2zh's cache with context-aware translations. Returns (done, total).

    Whatever is left out stays uncached and is translated paragraph-by-paragraph
    by the normal path, so this can fail in part or in whole without breaking
    the job.
    """
    todo = []
    for i, para in enumerate(paragraphs):
        if is_trivial(para):
            continue
        key = cache_key(tr.job_id, para)
        if key is not None and tr.cache.get(key) is not None:
            continue
        todo.append((i, para))
    # Duplicated paragraphs (running headers, repeated captions) only need one.
    seen, unique = set(), []
    for i, para in todo:
        if para in seen:
            continue
        seen.add(para)
        unique.append((i, para))

    if not unique:
        return 0, 0
    preamble = _preamble(tr, profile or {})

    groups = chunks(unique)
    done = finished = 0
    lock = threading.Lock()

    def run(group):
        nonlocal done, finished
        texts = dict(group)
        result = _translate_chunk(tr, preamble, group)
        with lock:
            for index, translation in result.items():
                key = cache_key(tr.job_id, texts[index])
                if key is not None:
                    tr.cache.set(key, translation)
                done += 1
            finished += 1
            if progress:
                progress(finished / len(groups))

    # The first chunk goes alone, to warm DeepSeek's prefix cache. Measured on a
    # 33-call job that fanned out immediately, only 23% of input tokens were
    # cached: the whole first wave of parallel calls left before the preamble
    # had been seen once, and a cache miss costs fifty times a hit. This is not
    # a wasted probe — it is real work, just not concurrent.
    run(groups[0])
    # The rest are independent by construction, each carrying its own context,
    # so they fan out over the same thread budget the layout pass would use.
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        list(pool.map(run, groups[1:]))
    return done, len(unique)


# ---------------------------------------------------------------------------
# Remembering what a document is, between runs.

def _digest(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:32]


def _profile_path(path: Path) -> Path:
    from webapp.store import DATA_DIR
    folder = DATA_DIR / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_digest(path)}.json"


def load_profile(path: Path):
    """The description already inferred for this exact file, if any.

    Describing a document costs a call, and the field it yields is part of the
    cache key — so without this, a rerun could not even tell whether its
    paragraphs were cached until it had paid to ask what the document was
    about again. Keyed by content, so an edited file gets described afresh.
    """
    try:
        return json.loads(_profile_path(path).read_text())
    except (OSError, ValueError):
        return None


def save_profile(path: Path, profile: dict) -> None:
    if not profile:
        return          # a failed description must not be remembered as final
    try:
        _profile_path(path).write_text(json.dumps(profile, ensure_ascii=False))
    except OSError as exc:      # noqa: BLE001 - a cache, not a requirement
        logger.warning("could not save document profile: %s", exc)


def title_of(path: Path) -> str:
    """The document's own title, when it bothered to record one."""
    import pymupdf
    doc = pymupdf.open(path)
    try:
        return (doc.metadata or {}).get("title", "") or ""
    finally:
        doc.close()
