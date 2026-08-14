"""DeepSeek translator with thinking control and token metering.

pdf2zh's own `DeepseekTranslator` neither exposes DeepSeek's `thinking` /
`reasoning_effort` parameters nor reports token usage. Both are added here by
subclassing and wrapping the OpenAI client, so `do_translate()` — including its
retry and stream handling — stays untouched upstream.

`pdf2zh.converter` resolves translator classes from module globals at call
time, so `install()` can swap ours in by name.
"""

from pdf2zh import converter
from pdf2zh.translator import DeepseekTranslator

from webapp import context
from webapp.pricing import METER

# DeepSeek's documented values. "off" means thinking disabled entirely;
# the rest are reasoning_effort levels used with thinking enabled.
EFFORTS = ["off", "low", "high", "max"]
DEFAULT_EFFORT = "high"          # matches the API default, so behaviour is unchanged


class MeteredDeepseekTranslator(DeepseekTranslator):
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
    }

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None,
                 ignore_cache=False):
        super().__init__(lang_in, lang_out, model, envs=envs, prompt=prompt,
                         ignore_cache=ignore_cache)
        effort = (self.envs.get("DEEPSEEK_EFFORT") or DEFAULT_EFFORT).lower()
        if effort not in EFFORTS:
            effort = DEFAULT_EFFORT
        self.job_id = self.envs.get("DEEPSEEK_JOB_ID") or ""
        # Always read explicitly: pdf2zh keeps the last envs it saw for a
        # service, so a value left over from the collecting pass would silently
        # turn the real pass into another no-op.
        self.collect = self.envs.get("DEEPSEEK_COLLECT") or ""

        # `thinking` is not an OpenAI parameter, so it has to ride in extra_body.
        thinking = {"type": "disabled"} if effort == "off" else {"type": "enabled"}
        extra = {"thinking": thinking}
        if effort != "off":
            extra["reasoning_effort"] = effort
        self.options["extra_body"] = extra

        # Different thinking settings produce different translations, so they
        # must be part of the cache key — otherwise switching effort would
        # silently replay results produced at another setting.
        self.add_cache_impact_parameters("thinking", thinking["type"])
        self.add_cache_impact_parameters("reasoning_effort",
                                         effort if effort != "off" else "")

        self._meter_client()

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
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        translation = self.do_translate(text)
        self.cache.set(key, translation)
        return translation

    def _meter_client(self) -> None:
        """Wrap chat.completions.create so every response is priced."""
        create = self.client.chat.completions.create
        job_id, model = self.job_id, self.model

        def metered(**kwargs):
            if kwargs.get("stream"):
                # Usage only arrives in a final chunk if we ask for it.
                kwargs["stream_options"] = {"include_usage": True}
                stream = create(**kwargs)

                def relay():
                    for chunk in stream:
                        if getattr(chunk, "usage", None):
                            METER.record(job_id, model, chunk.usage)
                        yield chunk

                return relay()
            response = create(**kwargs)
            METER.record(job_id, model, getattr(response, "usage", None))
            return response

        self.client.chat.completions.create = metered


def install() -> None:
    """Make pdf2zh use our subclass for service name "deepseek"."""
    converter.DeepseekTranslator = MeteredDeepseekTranslator
