"""Translators with thinking control, token metering and a shared cache.

pdf2zh's own DeepSeek and OpenAI translators neither expose the parameter that
turns thinking off nor report token usage. Both are added here by subclassing
and wrapping the OpenAI client, so `do_translate()` — including its retry and
stream handling — stays untouched upstream.

`pdf2zh.converter` resolves translator classes from module globals at call
time, so `install()` can swap ours in by name.

The two providers share one cache. See `_shared_cache`.
"""

import logging

import openai

from pdf2zh import converter
from pdf2zh.cache import TranslationCache, _TranslationCache
from pdf2zh.translator import DeepseekTranslator, OpenAITranslator

from webapp import context
from webapp.pricing import METER
from webapp.vendors import EFFORTS, VENDORS

logger = logging.getLogger("webapp")

DEFAULT_EFFORT = "high"          # matches the API default, so behaviour is unchanged


class QuotaExhausted(RuntimeError):
    """The account cannot pay for this call, however long we wait."""


# What the provider says when more waiting will not help.
_EXHAUSTED = ("insufficient_quota", "exceeded your current quota",
              "billing_hard_limit_reached", "account_deactivated")


def _is_exhausted(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    code = (body or {}).get("code") if isinstance(body, dict) else None
    text = f"{code or ''} {exc}".lower()
    return any(marker in text for marker in _EXHAUSTED)


# One bucket for every provider and every setting. Upstream keys a translation
# by engine, model, prompt, temperature and thinking level, on the theory that
# each produces a different translation — true, but not the question being
# asked. What is wanted here is the sentence in Chinese, and a sentence already
# paid for should not be paid for again because the model that produced it has
# since been swapped out.
#
# The cost is that two models can no longer be compared on one document: the
# second run replays the first. That is what `cache_read` is for.
SHARED_ENGINE = "shared"


def _shared_cache(lang_in: str, lang_out: str, doc: str) -> TranslationCache:
    """The cache every provider reads and writes.

    `doc` is the document's own text, hashed. Translations are produced under a
    description of the document and a glossary agreed for it, so a cached one
    belongs to that document; and unlike the inferred field it used to be keyed
    by, the text is stable across runs.
    """
    return TranslationCache(SHARED_ENGINE, {"lang_in": lang_in,
                                            "lang_out": lang_out,
                                            "doc": doc})


def _replace(cache: TranslationCache, original: str, translation: str) -> None:
    """Write through, overwriting whatever was there.

    Upstream's `set` inserts and swallows the unique-constraint failure, so a
    key that already exists keeps its old value — which would make a re-run
    with reading disabled do all the work and then quietly discard it.
    """
    _TranslationCache.replace(
        translate_engine=cache.translate_engine,
        translate_engine_params=cache.translate_engine_params,
        original_text=original,
        translation=translation,
    ).execute()


class _Metered:
    """What our two translators share, on top of whichever upstream class."""

    def _setup(self, envs: dict, prefix: str, official: str) -> None:
        self.job_id = envs.get(f"{prefix}_JOB_ID") or ""
        # Always read explicitly: pdf2zh keeps the last envs it saw for a
        # service, so a value left over from the collecting pass would silently
        # turn the real pass into another no-op.
        self.collect = envs.get(f"{prefix}_COLLECT") or ""
        # Off by default: a re-run should cost nothing for what is already
        # translated. On, the job ignores what earlier runs left behind — which
        # is the only way to see a second model's opinion of a document a first
        # model has already been through.
        self.cache_read = (envs.get(f"{prefix}_CACHE_READ") or "1") != "0"
        self.cache_write = (envs.get(f"{prefix}_CACHE_WRITE") or "1") != "0"
        self.cache = _shared_cache(self.lang_in, self.lang_out,
                                   envs.get(f"{prefix}_DOC") or "")
        # Where to send the requests, stated rather than inherited. Both
        # upstream classes settle this in their constructor from `self.envs`,
        # which pdf2zh has merged with whatever it last saw for this service
        # name — so a previous job's gateway address arrives in a translator
        # that was given none. Rebuilding from the caller's own dict is what
        # makes "no address" mean the official one again, and it must happen
        # before the meter wraps the client or the wrapper would be left
        # around the discarded one.
        self.client = openai.OpenAI(
            base_url=(envs.get(f"{prefix}_BASE_URL") or "").strip() or official,
            api_key=envs.get(f"{prefix}_API_KEY") or self.envs.get(f"{prefix}_API_KEY"),
        )
        self._meter_client()

    def prompt(self, text, prompt_template=None):
        """Carry the document's field, topic and terms into a single paragraph.

        This is the path a paragraph takes when the chunk holding it was
        rejected — a misaligned reply, a call that failed twice. Those
        paragraphs used to be the only text in the document translated with no
        context and no terminology whatsoever, which is exactly the state this
        whole layer exists to get out of. On one run there were 41 of them.
        """
        messages = super().prompt(text, prompt_template)
        # pdf2zh calls this during __init__, before our own fields exist.
        job = getattr(self, "job_id", "")
        if not job:
            return messages
        profile, glossary = context.brief_for(job)
        block = context.document_block(profile)
        terms = glossary.matching(text) if glossary else []
        if not block and not terms:
            return messages
        preface = [block] if block else []
        if terms:
            preface.append("Use these agreed translations where they apply: "
                           + "; ".join(f"{s} -> {t}" for s, t in terms))
        # Before the instruction, not after: the model should know what it is
        # reading before it is told what to do with it.
        messages[0]["content"] = "\n".join(preface) + "\n\n" + messages[0]["content"]
        return messages

    def translate(self, text: str, ignore_cache: bool = False) -> str:
        if self.collect:
            context.record(self.collect, text)
            return text
        # Not worth an API call, and worse: with nothing to go on the model
        # invents. This is deliberately not cached — it is a rule, not a result.
        if context.is_trivial(text):
            return text
        # A short paragraph is cached under itself *plus what follows it*, so a
        # heading translated here can never be replayed into an unrelated
        # document. `None` means there is no honest key: translate, don't cache.
        key = context.cache_key(self.job_id, text)
        if key is None:
            return self.do_translate(text)
        if not (self.ignore_cache or ignore_cache):
            # This run's own work first, and unconditionally: it is the only
            # thing both flags leave in place.
            hit = context.recall_fresh(self.job_id, key)
            if hit is None and self.cache_read:
                hit = self.cache.get(key)
            if hit is not None:
                return hit
        translation = self.do_translate(text)
        self.remember(key, translation)
        return translation

    def remember(self, key: str, translation: str) -> None:
        """Record a translation: always for this run, on disk if allowed."""
        context.remember_fresh(self.job_id, key, translation)
        if self.cache_write:
            _replace(self.cache, key, translation)

    def _meter_client(self) -> None:
        """Wrap chat.completions.create so every response is priced.

        Also the only place that sees the provider's own words. Upstream
        retries `RateLimitError` a hundred times at fifteen seconds apiece and
        logs nothing but the attempt number, which for a key with no credit is
        six hours of silence: OpenAI answers "you are out of quota" with the
        same 429 it uses for "you are going too fast", and the SDK raises the
        same class for both.
        """
        create = self.client.chat.completions.create
        job_id, model = self.job_id, self.model

        # The request's own arguments arrive as **kwargs and one of them is
        # `model`, so nothing here may take a parameter by that name — the
        # meter's identity rides in a closure instead of the signature.
        def meter(usage):
            METER.record(job_id, model, usage)

        def metered(**kwargs):
            try:
                return self._call(create, meter, **kwargs)
            except openai.RateLimitError as exc:
                logger.warning("%s rate-limited: %s", model, exc)
                if _is_exhausted(exc):
                    # Not a rate limit at all. Raised as something upstream's
                    # retry does not catch, so the job stops now and says why,
                    # instead of waiting out a hundred attempts per paragraph.
                    raise QuotaExhausted(str(exc)) from exc
                raise
            except openai.APIStatusError as exc:
                logger.warning("%s call failed: %s", model, exc)
                raise

        self.client.chat.completions.create = metered

    def _call(self, create, meter, **kwargs):
        """Make the call and price whatever comes back."""
        if kwargs.get("stream"):
            # Usage only arrives in a final chunk if we ask for it.
            kwargs["stream_options"] = {"include_usage": True}
            stream = create(**kwargs)

            def relay():
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        meter(chunk.usage)
                    yield chunk

            return relay()
        response = create(**kwargs)
        meter(getattr(response, "usage", None))
        return response


class MeteredDeepseekTranslator(_Metered, DeepseekTranslator):
    name = "deepseek"
    envs = {
        "DEEPSEEK_API_KEY": None,
        "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_EFFORT": DEFAULT_EFFORT,
        "DEEPSEEK_JOB_ID": "",
        # Set for the collecting pass: return every paragraph unchanged and
        # note it down, so a whole layout run costs nothing and reveals exactly
        # which paragraphs the real run will ask for.
        "DEEPSEEK_COLLECT": "",
        "DEEPSEEK_DOC": "",
        "DEEPSEEK_BASE_URL": VENDORS["deepseek"]["base_url"],
        "DEEPSEEK_CACHE_READ": "1",
        "DEEPSEEK_CACHE_WRITE": "1",
    }

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None,
                 ignore_cache=False):
        super().__init__(lang_in, lang_out, model, envs=envs, prompt=prompt,
                         ignore_cache=ignore_cache)
        effort = ((envs or {}).get("DEEPSEEK_EFFORT")
                  or DEFAULT_EFFORT).lower()
        if effort not in EFFORTS:
            effort = DEFAULT_EFFORT

        # `thinking` is not an OpenAI parameter, so it has to ride in extra_body.
        thinking = {"type": "disabled"} if effort == "off" else {"type": "enabled"}
        extra = {"thinking": thinking}
        if effort != "off":
            extra["reasoning_effort"] = effort
        self.options["extra_body"] = extra

        # The caller's dict, not self.envs: pdf2zh merges in whatever it last
        # saw for this service name, so a value from a previous job leaks into
        # the next one — which is how a translator built with no address at all
        # ended up pointing at the previous one's local gateway.
        self._setup(envs or {}, "DEEPSEEK", VENDORS["deepseek"]["base_url"])


class MeteredOpenAITranslator(_Metered, OpenAITranslator):
    name = "openai"
    envs = {
        "OPENAI_BASE_URL": VENDORS["openai"]["base_url"],
        "OPENAI_API_KEY": None,
        "OPENAI_MODEL": "gpt-5.6-luna",
        "OPENAI_STREAM": "true",
        "OPENAI_STOP_TOKENS": "",
        "OPENAI_MAX_TOKENS": -1,
        "OPENAI_JOB_ID": "",
        "OPENAI_COLLECT": "",
        "OPENAI_DOC": "",
        "OPENAI_CACHE_READ": "1",
        "OPENAI_CACHE_WRITE": "1",
    }

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None,
                 ignore_cache=False, **kwargs):
        super().__init__(lang_in, lang_out, model, envs=envs, prompt=prompt,
                         ignore_cache=ignore_cache, **kwargs)
        # gpt-5.6-luna reasons by default, and reasoning is billed as output —
        # the expensive half of the bill — for a task that is transcription
        # rather than deduction. The interface offers no other setting; this is
        # what makes that true of the request as well.
        self.options["extra_body"] = {"reasoning_effort": "none"}
        self._setup(envs or {}, "OPENAI", VENDORS["openai"]["base_url"])


def install() -> None:
    """Make pdf2zh resolve our subclasses for these service names."""
    converter.DeepseekTranslator = MeteredDeepseekTranslator
    converter.OpenAITranslator = MeteredOpenAITranslator
