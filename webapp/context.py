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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# Big enough that a paragraph sees real context, small enough that the model
# does not start summarising — long outputs drift.
CHUNK_CHARS = 2000
PROFILE_CHARS = 6000     # sampled across the document, not just the front
CLIP = 600           # per paragraph, so one long one cannot eat the budget

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
    '"title", "field", "summary" and "terms". "title" is the document\'s '
    'title, "field" its academic or technical field, "summary" at most 25 '
    "words on what it is about; use the language of the document itself for "
    'these three. "terms" is a list of at most {terms} objects {"source", '
    '"target", "forms"}: the document\'s key technical terms, proper nouns '
    'and acronyms, with the translation to use for each in {lang_out}. Give '
    "the same string as the translation for anything that should keep its "
    'original form. "forms" lists the other spellings of the same term — '
    'inflections, plurals, hyphenations, "sound" and "unsound" for '
    '"soundness" — exactly as they are written in the text.\n\n'
    "Propose a term ONLY if it appears in the excerpts, where you can see how "
    "it is used; a translation guessed from a bare word would be wrong for the "
    "whole document. The frequency list is for judging which of those terms "
    'matter and for collecting their other spellings into "forms" — a spelling '
    "needs no context, only recognition."
)


# Words too common to be terms, and too common to be worth listing.
_STOP = frozenset("""a an the and or but if then else of in on at to for from by
with without into over under about as is are was were be been being do does did
this that these those it its we our you your they their he she his her not no
than such can may might must shall should will would have has had also more most
other some any each which what when where who whom whose there here how why all
both few many much only own same so too very just now new one two three first
second next last other another between during before after above below up down
out off again further once because while until although however therefore thus
hence e.g i.e et al fig figure section chapter table example note see let us""".split())

_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{1,}")


def vocabulary(paragraphs: list, unigrams: int = 120, bigrams: int = 60) -> str:
    """The document's most frequent content words and pairs, with counts.

    Sampling text can only ever show the model part of the document, and a term
    it never sees is a term it cannot pin. Counting is different: it runs over
    *everything*, costs nothing, and ranks by exactly the property that decides
    whether a term matters — how often it appears.

    It is also how the variants arrive. "sound", "soundness" and "unsound" all
    show up in the list as separate frequent words, so the model can see they
    belong together and say so, rather than us guessing with suffix rules.
    """
    words = Counter()
    pairs = Counter()
    for para in paragraphs:
        found = [m.group(0).lower() for m in _WORD.finditer(para)]
        for word in found:
            if len(word) > 2 and word not in _STOP:
                words[word] += 1
        for left, right in zip(found, found[1:]):
            if left not in _STOP and right not in _STOP \
                    and len(left) > 2 and len(right) > 2:
                pairs[f"{left} {right}"] += 1
    lines = [f"{w} ({n})" for w, n in words.most_common(unigrams) if n > 2]
    lines += [f"{p} ({n})" for p, n in pairs.most_common(bigrams) if n > 2]
    return ", ".join(lines)


def sizing(paragraphs: list) -> tuple:
    """How much text to show, and how many terms to ask for.

    Input is nearly free — 20k characters of excerpt is about ¥0.01 — while the
    number of terms is what actually costs, being output. So the excerpt scales
    generously with the document and the term count carefully: a book yields
    far more terminology than a paper, but a list long enough to swamp the
    prompt would be paid for on every chunk that follows.
    """
    total = sum(len(p) for p in paragraphs)
    excerpt = min(max(3000, total // 20), 20000)
    terms = min(max(25, total // 4000), 80)
    return excerpt, terms


def sample(paragraphs: list, budget: int = PROFILE_CHARS) -> str:
    """Excerpts spread over the whole document, not just its opening pages.

    Core terminology often appears first in a methods section halfway through;
    a prefix sample would miss exactly the words that most need pinning down.

    Spending the whole budget matters: an earlier version guessed the stride
    from a constant and systematically undershot, taking 1604 characters where
    3000 were allowed. Whatever it left unspent was coverage given away for
    nothing, since the excerpt is the cheap half of this call.
    """
    usable = [p[:CLIP] for p in paragraphs if not is_trivial(p)]
    if not usable:
        return ""
    total = sum(len(p) for p in usable)
    if total <= budget:
        return "\n\n".join(usable)

    # Walk the document at a stride, then walk it again offset by one, until
    # the budget is full. Spread first, density second.
    stride = max(2, round(total / budget))
    picked, taken, used = [None] * len(usable), 0, 0
    for offset in range(stride):
        for i in range(offset, len(usable), stride):
            if picked[i] is not None:
                continue
            if used + len(usable[i]) > budget:
                continue
            picked[i] = usable[i]
            used += len(usable[i])
            taken += 1
        if used >= budget * 0.98:
            break
    return "\n\n".join(p for p in picked if p is not None)


def _grounded(triples: list, excerpt: str) -> list:
    """Drop terms the excerpt never shows, so none is translated from a bare word.

    A term counts as visible if the excerpt contains its name or any of the
    spellings reported for it — the reported name may be a tidied-up form of
    what the text actually writes.
    """
    flat = Glossary._flat(excerpt).lower()
    kept, dropped = [], []
    for source, target, forms in triples:
        shown = [source, *(forms or ())]
        if any(Glossary._flat(str(f)).lower() in flat for f in shown if f):
            kept.append((source, target, forms))
        else:
            dropped.append(source)
    if dropped:
        logger.info("glossary: %d proposed terms not visible in the excerpt, "
                    "left for chunk extraction: %s", len(dropped), dropped[:8])
    return kept


def describe(tr, excerpt: str, title: str = "", words: str = "",
             terms: int = 25) -> dict:
    """One call, or none: a title/field/summary block for every later prompt."""
    if not excerpt:
        return {}
    prompt = (_PROFILE_PROMPT.replace("{lang_out}", tr.lang_out)
                             .replace("{terms}", str(terms)))
    body = f"Excerpts:\n{excerpt}"
    if words:
        body = f"Most frequent words in the whole document:\n{words}\n\n{body}"
    try:
        content = _ask(tr, [{"role": "user", "content": f"{prompt}\n\n{body}"}])
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
    except Exception as exc:      # noqa: BLE001 - context is an optimisation
        logger.warning("document profile failed: %s", exc)
        return {}
    if title and not data.get("title"):
        data["title"] = title
    profile = {k: str(v)[:300] for k, v in data.items()
               if k in ("title", "field", "summary") and v}
    # Seeding the glossary from the same call is what keeps the early chunks
    # from each coining their own translation for the document's core terms.
    #
    # A term is only accepted if it is visible in the excerpt. The frequency
    # list names words with no context at all, and a translation guessed from a
    # bare word gets pinned for the whole document by first-writer-wins — a
    # systematic error, which is far worse than the drift it is meant to cure.
    # Terms that appear only outside the excerpt are not lost: chunk-by-chunk
    # extraction picks them up later, where the context is complete by
    # construction.
    seed = Glossary()
    seed.add(_grounded(_pairs(data), excerpt))
    if seed.terms():
        profile["terms"] = seed.terms()
        profile["forms"] = seed.forms()
    return profile


# ---------------------------------------------------------------------------
# The glossary: named entities and technical terms, agreed once per document.

MAX_INJECTED = 40        # terms per chunk, so the prompt cannot be swamped


class Glossary:
    """Terms and their agreed translations, growing as the document is read.

    An entry whose translation equals its source *is* the do-not-translate
    list — "NSA" rendered as "NSA". There is no need for a second mechanism:
    a term that must stay in its original form is just a term that translates
    to itself.

    First writer wins, permanently. Consistency is the entire point, so a
    uniformly second-best rendering beats one that is locally optimal and
    drifts across the document.
    """

    def __init__(self, initial: dict = None, variants: dict = None) -> None:
        self._lock = threading.Lock()
        self._terms = {}
        self._forms = {}         # a form as written -> the term it belongs to
        self._losers = {}        # rejected translation -> the one that won
        seen = variants or {}
        self.add((source, target, seen.get(source))
                 for source, target in (initial or {}).items())

    def _learn(self, term: str, form: str) -> None:
        form = self._flat(str(form)).strip()
        if not form or len(form) > 80:
            return
        # Case is only meaningful for acronyms, where it is the sole thing
        # separating TIP from tip.
        self._forms.setdefault(
            form.lower() if len(form) >= self.CASEFOLD_FROM else form, term)

    def add(self, entries) -> None:
        """Record (source, target) or (source, target, forms) triples.

        Shortest first, so a general decision is on record before the phrases
        built on it are judged against it — otherwise whether a contradiction
        is caught would depend on the order the model happened to list them in.
        """
        with self._lock:
            for entry in sorted(entries, key=lambda e: len(str(e[0]))):
                source, target, forms = (tuple(entry) + (None,))[:3]
                source, target = str(source).strip(), str(target).strip()
                if not source or not target or len(source) > 80:
                    continue
                # A term is identified by any form of it that we have seen, so
                # "Soundness" and "sound" are one entry, not two contradictory
                # ones.
                key = self._flat(source)
                key = key.lower() if len(key) >= self.CASEFOLD_FROM else key
                term = self._forms.get(key, source)
                clash = self._contradicts(term, target)
                if clash:
                    logger.info("glossary: %r -> %r contradicts %r, dropped",
                                term, target, clash)
                    continue
                winner = self._terms.setdefault(term, target)
                if winner != target:
                    self._losers[target] = winner
                self._learn(term, source)
                for form in forms or ():
                    self._learn(term, form)

    def _contradicts(self, source: str, target: str):
        """A shorter term this entry contains but does not honour, if any.

        A glossary can disagree with itself across lengths, and it did: NSA's
        held both `token -> token` and `hierarchical token modeling ->
        层次化标记建模`. The longer entry silently overrode the shorter one
        wherever it applied, and 标记 spread from it through the abstract.

        A longer term is a phrase built from shorter ones, so its translation
        should keep something of theirs. When it keeps nothing at all, the
        longer entry is the one to drop: the shorter term is the more general
        decision and the one already in use elsewhere.

        Both tests are deliberately weak, because a strict one was tried and
        was wrong far more often than right. It has to be a whole word —
        "attention" is not a part of "FlashAttention", "native" is not a part
        of "alternative" — and sharing any character is enough, so that
        `sparse -> 稀疏的` does not condemn `稀疏注意力策略` over a 的. What
        survives is the case that matters: renderings with nothing whatsoever
        in common, like `token -> token` against `层次化标记建模`.
        """
        # An entry that translates to itself is a decision to leave something
        # alone, not a rendering that can disagree with anything. A Chinese
        # name never shares a character with its Latin original, so the test
        # below would condemn every one of them: `Henry Gordon Rice` kept as
        # written was rejected for "contradicting" `Rice -> 赖斯`, and
        # `Full Attention` as a baseline's name for contradicting
        # `attention -> 注意力`. Both were right as they stood.
        if target == source:
            return None
        words = self._flat(source).lower().split()
        for other, rendering in self._terms.items():
            small = self._flat(other).lower()
            parts = small.split()
            n = len(parts)
            if small == self._flat(source).lower() or len(small) < 3:
                continue
            if not any(words[i:i + n] == parts for i in range(len(words) - n + 1)):
                continue
            if set(rendering) & set(target):
                continue
            return f"{other} -> {rendering}"
        return None

    # Below this length a term is an acronym — "TIP", "CFG" — where case is the
    # only thing separating it from an ordinary word, so it must match exactly.
    CASEFOLD_FROM = 4

    @staticmethod
    def _flat(text: str) -> str:
        """Hyphens and whitespace made interchangeable.

        71% of the terms extracted from a real book are more than one word, and
        English writes the same compound both ways depending on where it sits:
        "context sensitivity" in the noun position, "context-sensitive" in the
        adjective one. This is typography, not morphology — worth doing in code
        because it is the same in every language that uses the Latin script.
        """
        return " ".join(text.replace("-", " ").split())

    def matching(self, text: str) -> list:
        """The terms that actually occur in this chunk, longest first.

        Sending the whole glossary would push the real work down the prompt and
        cost tokens on every call; sending what appears is enough to pin it.

        A term is looked for under every form of it that has been reported,
        because a term named "Soundness" is written "sound", "unsound" and
        "soundly" in the body. Pinning one form and leaving the others free is
        worse than having no glossary at all: on SPA.pdf "Soundness" was pinned
        to 可靠性 while "sound" drifted to 健全, and consistency for that term
        fell from 83% without a glossary to 64% with one.

        Which strings are forms of which term is asked of the model rather than
        derived here. Suffix rules would be English-only — `lang_in` is a
        setting — and would still miss analysis/analyses or index/indices,
        exactly the words a technical document leans on.
        """
        flat = self._flat(text)
        lowered = flat.lower()
        with self._lock:
            found = {self._forms[form] for form in self._forms
                     if (form in lowered if len(form) >= self.CASEFOLD_FROM
                         else form in flat)}
            hits = [(s, self._terms[s]) for s in found if s in self._terms]
        hits.sort(key=lambda p: len(p[0]), reverse=True)
        return hits[:MAX_INJECTED]

    def terms(self) -> dict:
        with self._lock:
            return dict(self._terms)

    def forms(self) -> dict:
        """The spellings seen for each term, so a later run starts knowing them."""
        out = {}
        with self._lock:
            for form, term in self._forms.items():
                out.setdefault(term, []).append(form)
        return out

    def fixups(self) -> dict:
        """Rejected renderings to rewrite, and the ones that replace them.

        Concurrency means the first few chunks leave before any of them has
        seen the others' terms, so they can each coin a different translation
        for the same term. The winner is used from then on, but those early
        translations still carry the rejected wording.

        Pairs where one string contains the other are dropped: rewriting "切片"
        to "程序切片" inside a text that already says "程序切片" would produce
        "程序程序切片".
        """
        with self._lock:
            return {lose: win for lose, win in self._losers.items()
                    if lose and win and lose != win
                    and lose not in win and win not in lose}


def apply_fixups(text: str, fixups: dict) -> str:
    for lose, win in fixups.items():
        text = text.replace(lose, win)
    return text


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
        "- Where a glossary is given, use exactly those translations. An entry "
        "whose translation equals its source must be left in its original "
        "form.\n"
        "- A glossary entry is given in one grammatical form but covers all of "
        "them. When you meet another form of a listed term, keep the root of "
        "the given translation and inflect it to fit — do not switch to a "
        "different word. Given \"soundness -> 可靠性\", write \"sound\" as "
        "\"可靠的\" and \"unsound\" as \"不可靠的\", never as 健全 or 合理.\n"
        "- Output the translation only, with no commentary.\n\n"
        'Reply with a JSON object {"segments": [{"id": <int>, "text": '
        "<translation>}], \"terms\": [{\"source\": <term>, \"target\": "
        "<translation>}]} . \"segments\" "
        "holds exactly one entry per input segment, with the same ids. "
        "\"terms\" holds the technical terms, proper nouns and acronyms you "
        "translated that are NOT already in the glossary, spelled exactly as "
        "in the source; give the same string as the translation for anything "
        "that should stay in its original form."
    )


def _preamble(tr, profile: dict) -> str:
    """The part of the prompt that never changes, so it stays cache-hot.

    DeepSeek prices a cached prompt prefix at a fiftieth of a fresh one, but
    only matches from the first token — so everything constant has to come
    first and the per-chunk text last.
    """
    # Only the description belongs here. The glossary is deliberately left out:
    # it grows while the job runs, and a preamble that changes would defeat the
    # prefix cache it sits in front of.
    lines = [f"{k.capitalize()}: {profile[k]}"
             for k in ("title", "field", "summary") if profile.get(k)]
    block = "This document:\n" + "\n".join(lines) + "\n\n" if lines else ""
    return f"{_rules(tr)}\n\n{block}"


def _ask(tr, messages: list) -> str:
    response = tr.client.chat.completions.create(
        model=tr.model,
        messages=messages,
        response_format={"type": "json_object"},
        **tr.options,
    )
    return response.choices[0].message.content


def _pairs(data) -> list:
    """(source, target, forms) triples from a reply's "terms".

    The glossary is an optimisation; a model that gets its shape wrong must
    cost us the terms, never the translations that came with them — so a
    malformed entry, or a malformed "forms", is dropped rather than raised.
    """
    out = []
    for item in data.get("terms") or ():
        try:
            forms = item.get("forms") or ()
            forms = [f for f in forms if isinstance(f, str)] \
                if isinstance(forms, (list, tuple)) else ()
            out.append((item["source"], item["target"], forms))
        except (TypeError, KeyError, AttributeError):
            continue
    return out


def _translate_chunk(tr, preamble: str, chunk: list, glossary=None) -> dict:
    """{index: translation} for one chunk, or {} if the reply cannot be trusted.

    Ids make a mismatch loud and local: a model that merges two segments returns
    a set of ids that no longer matches, so the chunk is dropped rather than
    silently shifting every later paragraph onto the wrong piece of the page.
    """
    payload = {"segments": [{"id": i, "text": text} for i, text in chunk]}
    body = json.dumps(payload, ensure_ascii=False)

    # The glossary goes here, not in the preamble: it changes from chunk to
    # chunk, and anything variable placed before the constant part would stop
    # the prefix cache matching for the whole job.
    known = glossary.matching(" ".join(t for _, t in chunk)) if glossary else []
    if known:
        listing = "\n".join(f"{s} -> {t}" for s, t in known)
        body = f"Glossary already agreed for this document:\n{listing}\n\n{body}"

    messages = [
        {"role": "system", "content": preamble},
        {"role": "user", "content": body},
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
        if glossary:
            glossary.add(_pairs(data))
        return got
    return {}


def prepare(tr, paragraphs: list, profile: dict = None, progress=None,
            threads: int = 1, limit: int = CHUNK_CHARS) -> tuple:
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
    profile = profile or {}
    preamble = _preamble(tr, profile)
    glossary = Glossary(profile.get("terms"), profile.get("forms"))

    groups = chunks(unique, limit)
    done = finished = 0
    lock = threading.Lock()
    written = []

    def run(group):
        nonlocal done, finished
        texts = dict(group)
        result = _translate_chunk(tr, preamble, group, glossary)
        with lock:
            for index, translation in result.items():
                key = cache_key(tr.job_id, texts[index])
                if key is not None:
                    tr.cache.set(key, translation)
                    written.append((key, translation))
                done += 1
            finished += 1
            if progress:
                progress(finished / len(groups))

    # The first chunk goes alone, to warm DeepSeek's prefix cache. Measured on a
    # 33-call job that fanned out immediately, only 23% of input tokens were
    # cached: the whole first wave of parallel calls left before the preamble
    # had been seen once, and a cache miss costs fifty times a hit. This is not
    # a wasted probe — it is real work, just not concurrent. It also gives the
    # glossary a first draft before anything runs in parallel.
    run(groups[0])
    # The rest are independent by construction, each carrying its own context,
    # so they fan out over the same thread budget the layout pass would use.
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        list(pool.map(run, groups[1:]))

    # Chunks are cached as they arrive, so a crash never throws away work that
    # was already paid for. The rewrite therefore has to happen against the
    # cache rather than before it — entries replace on conflict, so setting the
    # corrected text again is enough.
    fixups = glossary.fixups()
    if fixups:
        repaired = 0
        for key, translation in written:
            fixed = apply_fixups(translation, fixups)
            if fixed != translation:
                tr.cache.set(key, fixed)
                repaired += 1
        logger.info("glossary: %d conflicting renderings rewritten in %d "
                    "paragraphs", len(fixups), repaired)
    profile["terms"] = glossary.terms()
    profile["forms"] = glossary.forms()
    return done, len(unique)


# ---------------------------------------------------------------------------
# Remembering what a document is, between runs.

def profile_key(excerpt: str) -> str:
    """Identify a description by the text it was inferred from.

    Not by the file: re-saving a PDF changes every byte of it while changing
    nothing that the description depends on, and a document rewritten around
    its metadata would wrongly reuse the old one. Keying on the excerpt states
    the invariant exactly — same input to the same extraction, same result —
    and costs nothing, because collecting the paragraphs is free and already
    happens first.

    It also means a two-page run and a whole-book run get different keys, which
    is right: the description drawn from the whole book is the better one and
    should not be displaced by the narrower one.
    """
    import hashlib
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:32]


def _profile_path(key: str) -> Path:
    from webapp.store import DATA_DIR
    folder = DATA_DIR / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.json"


def load_profile(key: str):
    """The description already inferred from this exact text, if any.

    Describing a document costs a call, and the field it yields is part of the
    cache key — so without this, a rerun could not even tell whether its
    paragraphs were cached until it had paid to ask what the document was
    about again.
    """
    try:
        return json.loads(_profile_path(key).read_text())
    except (OSError, ValueError):
        return None


def save_profile(key: str, profile: dict) -> None:
    if not profile:
        return          # a failed description must not be remembered as final
    try:
        _profile_path(key).write_text(json.dumps(profile, ensure_ascii=False))
    except OSError as exc:      # noqa: BLE001 - a cache, not a requirement
        logger.warning("could not save document profile: %s", exc)


def document_key(path: Path) -> str:
    """Identify a document by the text it contains.

    Not by its bytes: re-saving a PDF rewrites every one of them and changes
    nothing that a translation depends on. Not by the paragraphs collected for
    this job either, since those follow the page selection — keyed that way,
    translating pages 1-10 and then the whole book would share nothing.
    """
    import hashlib
    import pymupdf
    h = hashlib.sha256()
    doc = pymupdf.open(path)
    try:
        for page in doc:
            h.update(page.get_text().encode("utf-8", "replace"))
    finally:
        doc.close()
    return h.hexdigest()[:16]


def title_of(path: Path) -> str:
    """The document's own title, when it bothered to record one."""
    import pymupdf
    doc = pymupdf.open(path)
    try:
        return (doc.metadata or {}).get("title", "") or ""
    finally:
        doc.close()
