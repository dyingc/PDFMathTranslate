"""Minimal web app: translate PDFs with DeepSeek while keeping formulas intact.

Reuses pdf2zh's translation pipeline (`pdf2zh.high_level.translate`) and its
built-in DeepSeek translator. This layer only adds HTTP, a job store, and
in-memory-only API key handling.
"""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional

# --- Hard requirement: the API key must never touch the disk. -----------------
# pdf2zh's BaseTranslator.set_envs() writes the envs it receives (including
# DEEPSEEK_API_KEY) into ~/.config/PDFMathTranslate/config.json. Disable the
# persistence layer before anything imports/instantiates a translator, so the
# config stays read-only for this process.
from pdf2zh.config import ConfigManager  # noqa: E402

ConfigManager._save_config = lambda self: None  # type: ignore[assignment]

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from openai import OpenAI  # noqa: E402

from pdf2zh.doclayout import ModelInstance, OnnxModel  # noqa: E402
from pdf2zh.high_level import translate  # noqa: E402

from webapp import context  # noqa: E402
from webapp.pricing import METER, TABLE  # noqa: E402
from webapp.deghost import deghost  # noqa: E402
from webapp.links import restore_links  # noqa: E402
from webapp.scanned import dual_page_for, is_scanned, whiteout  # noqa: E402
from webapp.toc import without_toc  # noqa: E402
from webapp.verbatim import (  # noqa: E402
    install as install_verbatim, marking, text_lines, verbatim_blocks,
)


def page_count(path: Path) -> int:
    import pymupdf
    doc = pymupdf.open(path)
    try:
        return doc.page_count
    finally:
        doc.close()
from webapp.store import DATA_DIR, Store, job_dir  # noqa: E402
from webapp.translator import DEFAULT_EFFORT, EFFORTS, install as install_translator  # noqa: E402

# Route pdf2zh's "deepseek" service to our metered subclass.
install_translator()

logger = logging.getLogger(__name__)

# uvicorn configures its own loggers and leaves the root one at WARNING, which
# would hide what this app reports about each job — the document profile it
# inferred, how much of the document it managed to translate in context.
_webapp_log = logging.getLogger("webapp")
if not _webapp_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[webapp] %(message)s"))
    _webapp_log.addHandler(_handler)
    _webapp_log.setLevel(logging.INFO)
    _webapp_log.propagate = False

BASE_DIR = Path(__file__).parent

# `hint` is an i18n key resolved by the client; only the brand name is literal.
MODELS = {
    "deepseek-v4-flash": {"label": "DeepSeek V4 Flash", "hint": "model_hint_fast"},
    "deepseek-v4-pro": {"label": "DeepSeek V4 Pro", "hint": "model_hint_quality"},
}

# Target languages, mirroring pdf2zh's own GUI language map.
LANGUAGES = {
    "zh": "简体中文",
    "zh-TW": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "ru": "Русский",
    "es": "Español",
    "it": "Italiano",
}

# Values are i18n keys, not display text — the UI language is chosen client-side.
OUTPUTS = {"both": "out_both", "mono": "out_mono", "dual": "out_dual"}

# Fonts whose text pdf2zh should carry over verbatim instead of translating and
# re-flowing. Without the typewriter families a code block is treated as prose:
# its lines are merged into one paragraph and re-wrapped, destroying the layout.
# Passing vfont *replaces* pdf2zh's built-in list, so the first half of this
# pattern reproduces that list — the smoke test checks it stays a superset.
VFONT = (r"(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy"
         r"|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math"
         r"|cmtt|txtt|.*Typewriter|.*Courier|Courier|.*Consol|Inconsolata"
         r"|Menlo|SFMono"
         # Computer Modern Roman at 5-7pt is a sub/superscript, never prose.
         # Left as text it turns a verbatim block into a mixed paragraph, and
         # pdf2zh then re-flows the block by the *paragraph's* font size — the
         # tiny script size — collapsing the line spacing until lines collide.
         # CMR8 and up can be real footnote text, so they stay translatable.
         r"|CMR[567])")

# Interface languages == the languages we can translate into.
UI_LANGS = list(LANGUAGES)
DEFAULT_UI_LANG = "zh"


# Everything except the API key is durable: settings you pass once stick around
# for the next start, so `start.sh` needs no flags after the first time.
SETTINGS_PATH = DATA_DIR / "settings.json"


def _settings_int(key: str, env: str, default: int) -> int:
    """Resolve a setting as: env var (and remember it) > saved value > default."""
    def to_int(candidate, fallback):
        try:
            return max(1, int(candidate))
        except (TypeError, ValueError):
            return fallback

    # A typo in the env var falls back to the last good value, not to the
    # default — silently undoing a deliberate setting would be worse.
    previous = to_int(_SETTINGS.get(key), default)
    raw = os.environ.get(env, "").strip()
    value = to_int(raw, previous) if raw else previous
    _SETTINGS[key] = value
    return value


DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    _SETTINGS: dict = json.loads(SETTINGS_PATH.read_text())
except (OSError, ValueError):
    _SETTINGS = {}

# How many PDFs translate at once. Each job additionally fans out LLM_THREADS
# concurrent requests to DeepSeek, so the real request concurrency is the
# product of the two — raise with the provider's rate limit in mind.
WORKERS = _settings_int("workers", "WEBAPP_WORKERS", 2)
LLM_THREADS = _settings_int("llm_threads", "WEBAPP_LLM_THREADS", 4)

# Translate paragraphs in chunks with the document described up front, instead
# of one isolated paragraph per request. Costs one extra (API-free) layout pass;
# set WEBAPP_CONTEXT=0 to fall back to pdf2zh's own behaviour.
CONTEXT = os.environ.get("WEBAPP_CONTEXT", "1").strip() not in ("0", "false", "no")

# The interface language is a saved preference like any other; it is what the
# very first screen (API key entry) renders in, so it must be known before the
# UI draws anything.
UI_LANG = _SETTINGS.get("ui_lang") or DEFAULT_UI_LANG
if UI_LANG not in UI_LANGS:
    UI_LANG = DEFAULT_UI_LANG
_SETTINGS["ui_lang"] = UI_LANG


def _save_settings() -> None:
    SETTINGS_PATH.write_text(json.dumps(_SETTINGS, indent=2, ensure_ascii=False))


_save_settings()

# Bounds for the values the UI is allowed to set.
LIMITS = {"workers": (1, 16), "llm_threads": (1, 32)}


class Gate:
    """A semaphore whose capacity can change while jobs are in flight.

    A fixed-size thread pool cannot be resized, and swapping pools would strand
    queued jobs. Gating execution instead means raising the limit releases
    waiting jobs immediately, and lowering it simply stops new ones from
    starting as the running ones finish.
    """

    def __init__(self, limit: int) -> None:
        self._cv = threading.Condition()
        self._limit = limit
        self._active = 0

    def set_limit(self, limit: int) -> None:
        with self._cv:
            self._limit = limit
            self._cv.notify_all()

    @contextmanager
    def slot(self):
        with self._cv:
            self._cv.wait_for(lambda: self._active < self._limit)
            self._active += 1
        try:
            yield
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify_all()


# The API key is the one thing that stays memory-only, by design.
SESSIONS: Dict[str, str] = {}
STORE = Store()
GATE = Gate(WORKERS)
# Sized to the maximum the UI allows; the gate, not the pool, sets concurrency.
POOL = ThreadPoolExecutor(max_workers=LIMITS["workers"][1])

app = FastAPI(title="PDF Translator (DeepSeek)")


@app.middleware("http")
async def _no_cache(request, call_next):
    """The single-page UI is edited in place; never let a browser pin an old copy."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.on_event("startup")
def _startup() -> None:
    # The layout model is what lets pdf2zh keep formulas/figures in place.
    if ModelInstance.value is None:
        ModelInstance.value = OnnxModel.load_available()
    install_verbatim(ModelInstance.value)
    n = STORE.reap_stale()
    print(f"[webapp] data dir: {DATA_DIR} | "
          f"workers={WORKERS} llm_threads={LLM_THREADS}"
          + (f" | marked {n} interrupted job(s)" if n else ""))


@app.on_event("shutdown")
def _shutdown() -> None:
    SESSIONS.clear()


def _require_key(sid: Optional[str]) -> str:
    key = SESSIONS.get(sid or "")
    if not key:
        raise HTTPException(status_code=401, detail="err_no_key")
    return key


@app.get("/api/config")
def get_config(sid: Optional[str] = Cookie(None)):
    return {
        "models": MODELS,
        "languages": LANGUAGES,
        "outputs": OUTPUTS,
        "data_dir": str(DATA_DIR),
        "efforts": EFFORTS,
        "resumable": list(RESUMABLE),
        "default_effort": DEFAULT_EFFORT,
        "pricing": _pricing_now(),
        "ui_langs": UI_LANGS,
        "ui_lang": UI_LANG,
        "workers": WORKERS,
        "llm_threads": LLM_THREADS,
        "limits": LIMITS,
        "has_key": bool(SESSIONS.get(sid or "")),
    }


@app.post("/api/settings")
def set_settings(workers: int = Form(...), llm_threads: int = Form(...)):
    global WORKERS, LLM_THREADS
    for name, value in (("workers", workers), ("llm_threads", llm_threads)):
        low, high = LIMITS[name]
        if not low <= value <= high:
            raise HTTPException(status_code=400, detail="err_out_of_range")
    WORKERS, LLM_THREADS = workers, llm_threads
    _SETTINGS.update(workers=workers, llm_threads=llm_threads)
    _save_settings()
    # Takes effect immediately: waiting jobs start, or new ones stop starting.
    GATE.set_limit(workers)
    return {"ok": True}


@app.post("/api/ui-lang")
def set_ui_lang(lang: str = Form(...)):
    global UI_LANG
    if lang not in UI_LANGS:
        raise HTTPException(status_code=400, detail="err_unknown_lang")
    UI_LANG = _SETTINGS["ui_lang"] = lang
    _save_settings()
    return {"ok": True}


@app.post("/api/session")
def set_key(response: Response, api_key: str = Form(...)):
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="err_empty_key")
    try:
        OpenAI(api_key=api_key, base_url="https://api.deepseek.com").models.list()
    except Exception as exc:  # noqa: BLE001 - surface the provider error verbatim
        # Code for the UI to translate, plus the provider's own words verbatim.
        raise HTTPException(status_code=400,
                            detail={"code": "err_key_rejected", "raw": str(exc)})

    sid = uuid.uuid4().hex
    SESSIONS[sid] = api_key
    # Session cookie: survives a page refresh, dies with the browser session.
    # The key itself lives only in this process's memory.
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return {"ok": True}


@app.delete("/api/session")
def clear_key(response: Response, sid: Optional[str] = Cookie(None)):
    SESSIONS.pop(sid or "", None)
    response.delete_cookie("sid")
    return {"ok": True}


def _pricing_now() -> dict:
    """Rates in force right now, for the UI to show before a job is started."""
    now = time.time()
    regime = TABLE.regime_at(now)
    return {
        "currency": TABLE.currency,
        "symbol": getattr(TABLE, "symbol", "¥"),
        "source": TABLE.source,
        "checked_at": TABLE.checked_at,
        "period": TABLE.period(regime, now),
        "rates": {m: TABLE.rate(m, now) for m in MODELS},
    }


def _envs(api_key: str, model: str, effort: str, job_id: str,
          collect: str = "", field: str = "") -> dict:
    return {"DEEPSEEK_API_KEY": api_key, "DEEPSEEK_MODEL": model,
            "DEEPSEEK_EFFORT": effort, "DEEPSEEK_JOB_ID": job_id,
            "DEEPSEEK_COLLECT": collect, "DEEPSEEK_FIELD": field}


def _with_context(job_id: str, src: Path, api_key: str, model: str,
                  lang_in: str, lang_out: str, pages, effort: str,
                  blocks: dict, lines: dict, order: list, report) -> str:
    """Pre-translate the document's paragraphs with their neighbours in view.

    Returns the field it inferred for the document, which the real pass needs
    in order to look cache entries up under the same key.

    Best-effort throughout: anything this leaves uncached is translated the
    ordinary way, one isolated paragraph at a time.
    """
    from webapp.translator import MeteredDeepseekTranslator

    STORE.update(job_id, stage="Reading document")
    sink = context.collect_into(job_id)
    try:
        with marking(order, blocks, lines):
            translate(files=[str(src)], output=str(src.parent), pages=pages,
                      lang_in=lang_in, lang_out=lang_out,
                      service=f"deepseek:{model}",
                      thread=1,          # keeps the paragraphs in reading order
                      vfont=VFONT, callback=lambda t: report(0, t),
                      model=ModelInstance.value,
                      envs=_envs(api_key, model, effort, job_id, collect=job_id))
        paragraphs = list(sink)
    finally:
        context.drop(job_id)
    if not paragraphs:
        return ""

    # Both this pass and the real one must agree on how a paragraph is keyed,
    # so the key map is registered for the whole job, not just for prepare().
    context.use_keys(job_id, paragraphs)
    STORE.update(job_id, stage="Translating in context")

    # Describe the document before anything is cached: the field it yields is
    # part of the cache key, so it has to be known before the first lookup.
    profile = context.load_profile(src)
    if profile is None:
        probe = MeteredDeepseekTranslator(
            lang_in, lang_out, model,
            envs=_envs(api_key, model, effort, job_id))
        profile = context.describe(probe, paragraphs, context.title_of(src))
        context.save_profile(src, profile)
        logger.info("job %s: document profile: %s", job_id, profile or "none")
    else:
        logger.info("job %s: reusing document profile: %s", job_id, profile)
    field = profile.get("field", "")

    tr = MeteredDeepseekTranslator(lang_in, lang_out, model,
                                   envs=_envs(api_key, model, effort, job_id,
                                              field=field))
    done, total = context.prepare(tr, paragraphs, profile,
                                  progress=lambda f: report(1, f),
                                  threads=LLM_THREADS)
    logger.info("job %s: %d/%d paragraphs translated with context",
                job_id, done, total)
    return field


def _run_job(job_id: str, src: Path, api_key: str, model: str, lang_in: str,
             lang_out: str, pages: Optional[list], output: str,
             effort: str, do_whiteout: bool = False) -> None:
    # Three passes share one progress bar: reading the document, translating it
    # in context, then laying it out.
    spans = [(0.0, 0.15), (0.15, 0.60), (0.60, 1.0)]

    def report(phase: int, value) -> None:
        if not isinstance(value, float):     # a tqdm object from pdf2zh
            total = getattr(value, "total", 0) or 0
            value = value.n / total if total else 0.0
        lo, hi = spans[phase]
        STORE.progress(job_id, round(lo + (hi - lo) * value, 3), None)

    def on_progress(t) -> None:
        report(2, t)

    METER.start(job_id)
    try:
        # Wait here, not in the pool: the job stays visibly "queued" until a
        # concurrency slot frees up, and the limit can change while it waits.
        with GATE.slot():
            STORE.update(job_id, status="running", stage="Translating")
            out_dir = src.parent
            pages, skipped = without_toc(src, pages)
            if skipped:
                logger.info("job %s: leaving contents pages %s untranslated",
                            job_id, sorted(p + 1 for p in skipped))
            blocks = verbatim_blocks(src)
            # A layout region that covers the middle of a line but not its ends
            # splits that line into three paragraphs, sometimes mid-word.
            lines = text_lines(src)
            order = pages if pages is not None else list(range(page_count(src)))
            field = ""
            if CONTEXT:
                try:
                    field = _with_context(job_id, src, api_key, model, lang_in,
                                          lang_out, pages, effort, blocks,
                                          lines, order, report)
                except Exception as exc:      # noqa: BLE001 - quality, not correctness
                    logger.warning("context pass failed for %s: %s", job_id, exc)
            STORE.update(job_id, status="running", stage="Translating")
            with marking(order, blocks, lines):
                translate(
                    files=[str(src)],
                    output=str(out_dir),
                    pages=pages,
                    lang_in=lang_in,
                    lang_out=lang_out,
                    service=f"deepseek:{model}",
                    thread=LLM_THREADS,      # read now, so changes apply to new jobs
                    vfont=VFONT,
                    callback=on_progress,
                    model=ModelInstance.value,
                    envs=_envs(api_key, model, effort, job_id, field=field),
                )
        # pdf2zh always writes both variants; keep only what was asked for.
        kinds = ["mono", "dual"] if output == "both" else [output]
        for unwanted in {"mono", "dual"} - set(kinds):
            (out_dir / f"{src.stem}-{unwanted}.pdf").unlink(missing_ok=True)
        # Both repairs need the upload, so they run before it is removed.
        if do_whiteout:
            STORE.update(job_id, stage="Cleaning up scan")
            for kind in kinds:
                whiteout(src, out_dir / f"{src.stem}-{kind}.pdf",
                         dual_page_for if kind == "dual" else None)
        for kind in kinds:
            path = out_dir / f"{src.stem}-{kind}.pdf"
            mapping = dual_page_for if kind == "dual" else None
            # Cheap no-op unless the source really hid text under an opaque fill.
            try:
                deghost(src, path, mapping)
            except Exception as exc:      # noqa: BLE001 - cosmetic, never fatal
                logger.warning("deghost failed for %s: %s", job_id, exc)
            try:
                restore_links(src, path, mapping)
            except Exception as exc:      # noqa: BLE001 - cosmetic, never fatal
                logger.warning("link restore failed for %s: %s", job_id, exc)
        src.unlink(missing_ok=True)  # the upload is reproducible; the output is not
        STORE.update(job_id, status="done", progress=1.0, stage="Completed",
                     kinds=kinds)
    except Exception as exc:  # noqa: BLE001
        STORE.update(job_id, status="error", error=str(exc), stage="Failed")
    finally:
        # A failed job still burned tokens; record what it spent either way.
        context.forget_keys(job_id)
        usage = METER.pop(job_id)
        STORE.add_usage(job_id, tokens_in_hit=usage["tokens_in_hit"],
                        tokens_in_miss=usage["tokens_in_miss"],
                        tokens_out=usage["tokens_out"], calls=usage["calls"],
                        cost=round(usage["cost"], 6))
        if not usage["priced"]:
            STORE.update(job_id, priced=0)


@app.post("/api/translate")
async def start_translate(
    file: UploadFile = File(...),
    model: str = Form("deepseek-v4-flash"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh"),
    pages: str = Form(""),
    output: str = Form("both"),
    effort: str = Form(DEFAULT_EFFORT),
    confirm_scanned: str = Form(""),
    sid: Optional[str] = Cookie(None),
):
    api_key = _require_key(sid)
    if model not in MODELS:
        raise HTTPException(status_code=400, detail="err_unknown_model")
    if output not in OUTPUTS:
        raise HTTPException(status_code=400, detail="err_unknown_output")
    if effort not in EFFORTS:
        raise HTTPException(status_code=400, detail="err_unknown_effort")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="err_pdf_only")

    job_id = uuid.uuid4().hex
    d = job_dir(job_id)
    d.mkdir(parents=True)
    src = d / os.path.basename(file.filename)
    src.write_bytes(await file.read())

    # A scan needs the user's decision before anything is spent on it.
    scan = is_scanned(src)
    if scan["scanned"] and not confirm_scanned:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=409,
                            detail={"code": "err_scanned", "raw": ""})

    STORE.create(job_id, name=src.stem, src_name=src.name, model=model,
                 lang_in=lang_in, lang_out=lang_out, pages=pages, output=output,
                 effort=effort, whiteout=int(bool(scan["scanned"])))
    _submit(STORE.get(job_id), src, api_key)
    return {"job_id": job_id}


def _source_of(job: dict) -> Optional[Path]:
    """The uploaded PDF, if it is still on disk."""
    d = job_dir(job["id"])
    if job.get("src_name"):
        path = d / job["src_name"]
        return path if path.exists() else None
    # Jobs created before src_name existed: the upload is the only PDF that is
    # not one of our two outputs.
    outputs = {f"{job['name']}-mono.pdf", f"{job['name']}-dual.pdf"}
    found = [f for f in d.glob("*.pdf") if f.name not in outputs] if d.exists() else []
    return found[0] if len(found) == 1 else None


def _submit(job: dict, src: Path, api_key: str) -> None:
    POOL.submit(_run_job, job["id"], src, api_key, job["model"], job["lang_in"],
                job["lang_out"], _parse_pages(job["pages"]), job["output"],
                job["effort"], bool(job["whiteout"]))


RESUMABLE = ("interrupted", "error")


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, sid: Optional[str] = Cookie(None)):
    """Re-run an interrupted or failed job.

    Not a byte-level resume — pdf2zh has no such concept. It re-runs the same
    job, and pdf2zh's paragraph cache means everything already translated comes
    back for free, so in practice only the unfinished part costs anything.
    """
    api_key = _require_key(sid)
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="err_unknown_job")
    if job["status"] not in RESUMABLE:
        raise HTTPException(status_code=409, detail="err_not_resumable")
    src = _source_of(job)
    if src is None:
        raise HTTPException(status_code=410, detail="err_no_source")

    STORE.update(job_id, status="queued", stage="Queued", error="", progress=0.0,
                 src_name=src.name)
    _submit(job, src, api_key)
    return {"ok": True}


def _parse_pages(spec: str) -> Optional[list]:
    """Parse a 1-based page spec like "1,3-5" into 0-based indices."""
    spec = (spec or "").strip()
    if not spec:
        return None
    out: list = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            out.extend(range(int(start) - 1, int(end)))
        else:
            out.append(int(part) - 1)
    return out or None


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": STORE.list()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="err_unknown_job")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="err_unknown_job")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="err_job_running")
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    STORE.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = STORE.get(job_id)
    if not job or kind not in job["kinds"]:
        raise HTTPException(status_code=404, detail="err_unknown_job")
    path = job_dir(job_id) / f"{job['name']}-{kind}.pdf"
    if not path.exists():
        raise HTTPException(status_code=410, detail="err_file_gone")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
