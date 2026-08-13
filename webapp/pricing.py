"""Cost accounting for DeepSeek calls.

Two things make this less trivial than tokens × price:

* DeepSeek changes its price list (a peak/off-peak scheme takes effect
  2026-08-16 16:00 UTC), and
* under that scheme the rate depends on the hour the call is made.

So the price is resolved *per API call, at the moment of the call*, and the
resulting cost is accumulated. A job that starts off-peak and finishes during
peak hours — or that straddles a price change — is billed correctly, the same
way the provider bills it.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PRICING_PATH = Path(__file__).parent / "pricing.json"
_MILLION = 1_000_000


def _parse(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class PriceTable:
    def __init__(self, path: Path = PRICING_PATH) -> None:
        data = json.loads(path.read_text())
        self.currency = data.get("currency", "USD")
        self.source = data.get("source", "")
        self.checked_at = data.get("checked_at", "")
        self.regimes = sorted(
            ({**r, "_from": _parse(r["effective_from"])} for r in data["regimes"]),
            key=lambda r: r["_from"],
        )

    def regime_at(self, when: float) -> dict:
        current = self.regimes[0]
        for regime in self.regimes:
            if regime["_from"] <= when:
                current = regime
            else:
                break
        return current

    @staticmethod
    def period(regime: dict, when: float) -> str:
        hour = datetime.fromtimestamp(when, timezone.utc).hour
        for start, end in regime.get("peak_hours_utc", []):
            if start <= hour < end:
                return "peak"
        return "off_peak"

    def rate(self, model: str, when: float) -> Optional[dict]:
        """Price per 1M tokens in effect for `model` at `when`, or None."""
        regime = self.regime_at(when)
        rates = regime["rates"].get(model)
        if not rates:
            return None
        period = self.period(regime, when)
        return rates.get(period) or rates.get("off_peak")

    def cost(self, model: str, when: float,
             cache_hit: int, cache_miss: int, output: int) -> Optional[float]:
        rate = self.rate(model, when)
        if rate is None:
            return None
        return (cache_hit * rate["cache_hit"]
                + cache_miss * rate["cache_miss"]
                + output * rate["output"]) / _MILLION


TABLE = PriceTable()


class Meter:
    """Per-job token/cost accumulator, written to by the translator threads.

    pdf2zh fans a job out over its own worker threads, so the job id travels
    through the translator's envs and the accumulators live here, keyed by id.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict = {}

    def start(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id] = {"tokens_in_hit": 0, "tokens_in_miss": 0,
                                  "tokens_out": 0, "cost": 0.0, "calls": 0,
                                  "priced": True}

    def record(self, job_id: str, model: str, usage) -> None:
        """Price one API response at the rate in effect right now."""
        if usage is None:
            return
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        if hit is None and miss is None:
            # Provider did not break the prompt down; treat it all as a miss,
            # which over- rather than under-states the cost.
            hit, miss = 0, prompt
        hit, miss = hit or 0, miss or 0
        out = getattr(usage, "completion_tokens", 0) or 0

        cost = TABLE.cost(model, datetime.now(timezone.utc).timestamp(),
                          hit, miss, out)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["tokens_in_hit"] += hit
            job["tokens_in_miss"] += miss
            job["tokens_out"] += out
            job["calls"] += 1
            if cost is None:
                job["priced"] = False      # unknown model: tokens still counted
            else:
                job["cost"] += cost

    def pop(self, job_id: str) -> dict:
        with self._lock:
            return self._jobs.pop(job_id, None) or {
                "tokens_in_hit": 0, "tokens_in_miss": 0, "tokens_out": 0,
                "cost": 0.0, "calls": 0, "priced": True}


METER = Meter()
