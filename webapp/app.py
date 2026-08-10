"""Minimal web app: translate PDFs with DeepSeek while keeping formulas intact.

Reuses pdf2zh's translation pipeline (`pdf2zh.high_level.translate`) and its
built-in DeepSeek translator. The only thing this app adds is a small HTTP layer
plus in-memory-only API key storage.
"""

import os
import shutil
import tempfile
import threading
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
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from openai import OpenAI  # noqa: E402

from pdf2zh.doclayout import ModelInstance, OnnxModel  # noqa: E402
from pdf2zh.high_level import translate  # noqa: E402

BASE_DIR = Path(__file__).parent
WORK_DIR = Path(tempfile.mkdtemp(prefix="pdf2zh-webapp-"))

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

# In-memory only. Wiped on restart, which is exactly the desired behaviour.
SESSIONS: Dict[str, str] = {}
JOBS: Dict[str, dict] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2)

app = FastAPI(title="PDF Translator (DeepSeek)")


@app.on_event("startup")
def _load_layout_model() -> None:
    # The layout model is what lets pdf2zh keep formulas/figures in place.
    if ModelInstance.value is None:
        ModelInstance.value = OnnxModel.load_available()


@app.on_event("shutdown")
def _cleanup() -> None:
    SESSIONS.clear()
    shutil.rmtree(WORK_DIR, ignore_errors=True)


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
             lang_out: str, pages: Optional[list]) -> None:
    job = JOBS[job_id]

    def on_progress(t) -> None:
        total = getattr(t, "total", 0) or 0
        if total:
            job["progress"] = round(t.n / total, 3)
        job["stage"] = getattr(t, "desc", "") or "Translating"

    try:
        job["status"] = "running"
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
        stem = src.stem
        job.update(
            status="done",
            progress=1.0,
            stage="Completed",
            mono=str(out_dir / f"{stem}-mono.pdf"),
            dual=str(out_dir / f"{stem}-dual.pdf"),
        )
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", error=str(exc))


@app.post("/api/translate")
async def start_translate(
    file: UploadFile = File(...),
    model: str = Form("deepseek-v4-flash"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh"),
    pages: str = Form(""),
    sid: Optional[str] = Cookie(None),
):
    api_key = _require_key(sid)
    if model not in MODELS:
        raise HTTPException(status_code=400, detail="Unknown model")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True)
    src = job_dir / os.path.basename(file.filename)
    src.write_bytes(await file.read())

    JOBS[job_id] = {"status": "queued", "progress": 0.0, "stage": "Queued",
                    "name": src.stem}
    POOL.submit(_run_job, job_id, src, api_key, model, lang_in, lang_out,
                _parse_pages(pages))
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


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    return JSONResponse({k: v for k, v in job.items() if k not in ("mono", "dual")}
                        | {"has_output": job.get("status") == "done"})


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or kind not in ("mono", "dual"):
        raise HTTPException(status_code=404, detail="Not available")
    path = job[kind]
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{job['name']}-{kind}.pdf")


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
