"""SQLite-backed job records.

Job state has to outlive both a browser refresh and a server restart, so the
authoritative copy lives on disk. `progress`/`stage` are written back throttled
(see `Store.progress`) — losing a second of progress on a crash is fine, losing
the fact that a translation happened is not.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("PDF2ZH_WEBAPP_DATA")
                or Path(__file__).parent / "data").expanduser().resolve()
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "jobs.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,      -- original filename stem, for display
    src_name    TEXT NOT NULL DEFAULT '',  -- uploaded file name, kept for resume
    model       TEXT NOT NULL,
    lang_in     TEXT NOT NULL,
    lang_out    TEXT NOT NULL,
    pages       TEXT NOT NULL DEFAULT '',
    output      TEXT NOT NULL,      -- mono | dual | both
    effort      TEXT NOT NULL DEFAULT 'high',  -- off | low | high | max
    whiteout    INTEGER NOT NULL DEFAULT 0,    -- erase scanned original text
    status      TEXT NOT NULL,      -- queued | running | done | error | interrupted
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    kinds       TEXT NOT NULL DEFAULT '[]',   -- JSON list of available downloads
    tokens_in_hit  INTEGER NOT NULL DEFAULT 0,
    tokens_in_miss INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    calls          INTEGER NOT NULL DEFAULT 0,
    -- Cost is accumulated per API call at the rate in force at that moment, so
    -- it stays correct across peak/off-peak boundaries and price changes.
    cost           REAL NOT NULL DEFAULT 0,
    priced         INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

_LIVE_STATES = ("queued", "running")

# Columns added to `jobs` after the first release, applied on open.
_ADDED_COLUMNS = {
    "src_name": "TEXT NOT NULL DEFAULT ''",
    "effort": "TEXT NOT NULL DEFAULT 'high'",
    "whiteout": "INTEGER NOT NULL DEFAULT 0",
    "tokens_in_hit": "INTEGER NOT NULL DEFAULT 0",
    "tokens_in_miss": "INTEGER NOT NULL DEFAULT 0",
    "tokens_out": "INTEGER NOT NULL DEFAULT 0",
    "calls": "INTEGER NOT NULL DEFAULT 0",
    "cost": "REAL NOT NULL DEFAULT 0",
    "priced": "INTEGER NOT NULL DEFAULT 1",
}


class Store:
    def __init__(self) -> None:
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + an explicit lock: the worker pool and the
        # request handlers both touch this connection.
        self._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()
        self._last_write: dict = {}

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in _ADDED_COLUMNS.items():
            if col not in have:
                self._db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    # -- writes ---------------------------------------------------------------

    def create(self, job_id: str, **fields) -> None:
        now = time.time()
        row = {"id": job_id, "status": "queued", "progress": 0.0,
               "stage": "Queued", "error": "", "kinds": "[]",
               "created_at": now, "updated_at": now, **fields}
        cols = ",".join(row)
        self._exec(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(row))})",
                   tuple(row.values()))

    def update(self, job_id: str, **fields) -> None:
        if "kinds" in fields:
            fields["kinds"] = json.dumps(fields["kinds"])
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?",
                   (*fields.values(), job_id))

    def add_usage(self, job_id: str, **deltas) -> None:
        """Accumulate token/cost counters — a resumed job spends on top of what
        its earlier run already spent, so these must add, not overwrite."""
        sets = ",".join(f"{k}={k}+?" for k in deltas)
        self._exec(f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
                   (*deltas.values(), time.time(), job_id))

    def progress(self, job_id: str, progress: float, stage: str) -> None:
        """Throttled progress write — the tqdm callback fires far too often."""
        prev = self._last_write.get(job_id)
        if prev and progress - prev[0] < 0.01 and stage == prev[1]:
            return
        self._last_write[job_id] = (progress, stage)
        # `None` means "leave the stage alone": the caller set a stage of its
        # own and only the bar is moving.
        fields = {"stage": stage} if stage is not None else {}
        self.update(job_id, progress=progress, status="running", **fields)

    def reap_stale(self) -> int:
        """A restart kills any in-flight translation; nothing can resume it."""
        cur = self._exec(
            "UPDATE jobs SET status='interrupted', error='err_interrupted' "
            f"WHERE status IN ({','.join('?' * len(_LIVE_STATES))})", _LIVE_STATES)
        return cur.rowcount

    def delete(self, job_id: str) -> None:
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    # -- reads ----------------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        cur = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
        return _to_dict(row) if row else None

    def list(self, limit: int = 50) -> list:
        cur = self._exec("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                         (limit,))
        return [_to_dict(r) for r in cur.fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["kinds"] = json.loads(d["kinds"])
    return d


def job_dir(job_id: str) -> Path:
    return FILES_DIR / job_id
