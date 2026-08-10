"""Minimal web app: translate PDFs with DeepSeek while keeping formulas intact.

Reuses pdf2zh's translation pipeline (`pdf2zh.high_level.translate`) and its
built-in DeepSeek translator. This layer only adds HTTP, a job store, and
in-memory-only API key handling.
"""

import os
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

from webapp.store import DATA_DIR, Store, job_dir  # noqa: E402

BASE_DIR = Path(__file__).parent

MODELS = {
    "deepseek-v4-flash": "DeepSeek V4 Flash (fast / cheap)",
    "deepseek-v4-pro": "DeepSeek V4 Pro (higher quality)",
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

OUTPUTS = {
    "both": "两者",
    "mono": "纯译文",
    "dual": "原文/译文对照",
}

# The API key is the one thing that stays memory-only, by design.
SESSIONS: Dict[str, str] = {}
STORE = Store()
POOL = ThreadPoolExecutor(max_workers=2)

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
    n = STORE.reap_stale()
    print(f"[webapp] data dir: {DATA_DIR}"
          + (f" | marked {n} interrupted job(s)" if n else ""))


@app.on_event("shutdown")
def _shutdown() -> None:
    SESSIONS.clear()


def _require_key(sid: Optional[str]) -> str:
    key = SESSIONS.get(sid or "")
    if not key:
        raise HTTPException(status_code=401, detail="API key not set for this session")
    return key


@app.get("/api/config")
def get_config(sid: Optional[str] = Cookie(None)):
    return {
        "models": MODELS,
        "languages": LANGUAGES,
        "outputs": OUTPUTS,
        "data_dir": str(DATA_DIR),
        "has_key": bool(SESSIONS.get(sid or "")),
    }


@app.post("/api/session")
def set_key(response: Response, api_key: str = Form(...)):
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Empty API key")
    try:
        OpenAI(api_key=api_key, base_url="https://api.deepseek.com").models.list()
    except Exception as exc:  # noqa: BLE001 - surface the provider error verbatim
        raise HTTPException(status_code=400, detail=f"Key rejected by DeepSeek: {exc}")

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


def _run_job(job_id: str, src: Path, api_key: str, model: str, lang_in: str,
             lang_out: str, pages: Optional[list], output: str) -> None:
    def on_progress(t) -> None:
        total = getattr(t, "total", 0) or 0
        STORE.progress(job_id,
                       round(t.n / total, 3) if total else 0.0,
                       getattr(t, "desc", "") or "Translating")

    try:
        STORE.update(job_id, status="running", stage="Translating")
        out_dir = src.parent
        translate(
            files=[str(src)],
            output=str(out_dir),
            pages=pages,
            lang_in=lang_in,
            lang_out=lang_out,
            service=f"deepseek:{model}",
            thread=4,
            callback=on_progress,
            model=ModelInstance.value,
            envs={"DEEPSEEK_API_KEY": api_key, "DEEPSEEK_MODEL": model},
        )
        # pdf2zh always writes both variants; keep only what was asked for.
        kinds = ["mono", "dual"] if output == "both" else [output]
        for unwanted in {"mono", "dual"} - set(kinds):
            (out_dir / f"{src.stem}-{unwanted}.pdf").unlink(missing_ok=True)
        src.unlink(missing_ok=True)  # the upload is reproducible; the output is not
        STORE.update(job_id, status="done", progress=1.0, stage="Completed",
                     kinds=kinds)
    except Exception as exc:  # noqa: BLE001
        STORE.update(job_id, status="error", error=str(exc), stage="Failed")


@app.post("/api/translate")
async def start_translate(
    file: UploadFile = File(...),
    model: str = Form("deepseek-v4-flash"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh"),
    pages: str = Form(""),
    output: str = Form("both"),
    sid: Optional[str] = Cookie(None),
):
    api_key = _require_key(sid)
    if model not in MODELS:
        raise HTTPException(status_code=400, detail="Unknown model")
    if output not in OUTPUTS:
        raise HTTPException(status_code=400, detail="Unknown output type")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    job_id = uuid.uuid4().hex
    d = job_dir(job_id)
    d.mkdir(parents=True)
    src = d / os.path.basename(file.filename)
    src.write_bytes(await file.read())

    STORE.create(job_id, name=src.stem, model=model, lang_in=lang_in,
                 lang_out=lang_out, pages=pages, output=output)
    POOL.submit(_run_job, job_id, src, api_key, model, lang_in, lang_out,
                _parse_pages(pages), output)
    return {"job_id": job_id}


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
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="任务进行中，无法删除")
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    STORE.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = STORE.get(job_id)
    if not job or kind not in job["kinds"]:
        raise HTTPException(status_code=404, detail="Not available")
    path = job_dir(job_id) / f"{job['name']}-{kind}.pdf"
    if not path.exists():
        raise HTTPException(status_code=410, detail="文件已被删除")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
