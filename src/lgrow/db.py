"""SQLite persistence.

One file, `data/app.db`. Schema is created on demand and versioned through the
`user_version` pragma so future changes can migrate forward without a
migration framework.

State that must survive a reboot lives here — in particular the `runs` table,
which is what lets `lgrow run catchup` replay work missed while the laptop was
off.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

from . import paths

SCHEMA_VERSION = 1

_SCHEMA = """
-- Raw jobs as fetched, deduplicated by content identity.
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,     -- stable hash of (company, title, source_id)
    source          TEXT NOT NULL,        -- himalayas | remotive | greenhouse:stripe | ...
    source_id       TEXT,
    url             TEXT NOT NULL,
    apply_url       TEXT,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location_raw    TEXT,
    geo_tags        TEXT,                 -- JSON array of normalised region codes
    employment_type TEXT,
    salary_raw      TEXT,
    description     TEXT,
    description_hash TEXT,                -- drives the scoring cache
    posted_at       TEXT,                 -- ISO8601 UTC
    fetched_at      TEXT NOT NULL,
    dedupe_key      TEXT                  -- normalised company|title
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);

-- Gemini's verdict on a job, cached against the job text + profile revision so
-- a re-crawl never pays to score the same posting twice.
CREATE TABLE IF NOT EXISTS job_scores (
    job_id          TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    score           INTEGER NOT NULL,
    verdict         TEXT NOT NULL,        -- JSON: why_fit, gaps, red_flags, geo_ok
    model           TEXT,
    scored_at       TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_version),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

-- Your review queue. One row per job you were shown.
CREATE TABLE IF NOT EXISTS applications (
    job_id          TEXT PRIMARY KEY,
    state           TEXT NOT NULL,        -- queued | applied | skipped | expired
    artifacts_dir   TEXT,
    created_at      TEXT NOT NULL,
    applied_at      TEXT,
    followup_due    TEXT,
    followup_done   INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_apps_state ON applications(state);

-- Raw material for posts: journal notes, your commits, market signal.
CREATE TABLE IF NOT EXISTS activity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,        -- journal | git | market
    occurred_at     TEXT NOT NULL,
    text            TEXT NOT NULL,
    meta            TEXT,                 -- JSON
    fingerprint     TEXT UNIQUE,          -- prevents re-importing the same commit
    used_in_post    INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_used ON activity(used_in_post);

-- Post lifecycle: draft -> approved -> scheduled -> published.
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pillar          TEXT NOT NULL,
    draft_text      TEXT NOT NULL,
    final_text      TEXT,
    state           TEXT NOT NULL,        -- draft|approved|scheduled|published|failed|skipped
    critique        TEXT,                 -- JSON from the anti-slop pass
    activity_ids    TEXT,                 -- JSON array
    scheduled_for   TEXT,
    published_at    TEXT,
    post_urn        TEXT,
    visibility      TEXT DEFAULT 'PUBLIC',
    last_error      TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_state ON posts(state);
CREATE INDEX IF NOT EXISTS idx_posts_sched ON posts(scheduled_for);

-- Engagement is entered by hand: LinkedIn's r_member_social scope is
-- approval-gated, so we cannot read our own post stats back via the API.
CREATE TABLE IF NOT EXISTS post_engagement (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL,
    likes           INTEGER,
    comments        INTEGER,
    reposts         INTEGER,
    impressions     INTEGER,
    logged_at       TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

-- Gemini call ledger. Guards the daily allowance and makes cost visible.
CREATE TABLE IF NOT EXISTS api_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    day             TEXT NOT NULL,        -- UTC date, YYYY-MM-DD
    model           TEXT NOT NULL,
    purpose         TEXT,
    ok              INTEGER NOT NULL,
    duration_ms     INTEGER,
    prompt_chars    INTEGER,
    response_chars  INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON api_usage(day);

-- Scheduled-task bookkeeping; `catchup` reads this to find missed runs.
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,        -- running | ok | error
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task, started_at);

CREATE TABLE IF NOT EXISTS kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def utcnow() -> str:
    """ISO8601 UTC timestamp, second precision, always suffixed with Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utctoday() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def connect() -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(paths.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init(conn: sqlite3.Connection | None = None) -> None:
    """Create the schema if absent and stamp the version."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(_SCHEMA)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        if own:
            conn.close()


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Connection with schema guaranteed, committing on clean exit."""
    conn = connect()
    try:
        init(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── small helpers used across modules ───────────────────────────────────────


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def jload(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def record_usage(
    conn: sqlite3.Connection,
    *,
    model: str,
    purpose: str,
    ok: bool,
    duration_ms: int,
    prompt_chars: int,
    response_chars: int,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO api_usage "
        "(day, model, purpose, ok, duration_ms, prompt_chars, response_chars, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utctoday(),
            model,
            purpose,
            1 if ok else 0,
            duration_ms,
            prompt_chars,
            response_chars,
            error,
            utcnow(),
        ),
    )
    conn.commit()


def calls_today(conn: sqlite3.Connection, day: str | None = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM api_usage WHERE day = ?", (day or utctoday(),)
    ).fetchone()
    return int(row["n"])


def start_run(conn: sqlite3.Connection, task: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (task, started_at, status) VALUES (?, ?, 'running')",
        (task, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, detail: str | None = None
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (utcnow(), status, detail, run_id),
    )
    conn.commit()


def last_successful_run(conn: sqlite3.Connection, task: str) -> datetime | None:
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE task = ? AND status = 'ok' "
        "ORDER BY finished_at DESC LIMIT 1",
        (task,),
    ).fetchone()
    if not row or not row["finished_at"]:
        return None
    return datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))


__all__ = [
    "SCHEMA_VERSION",
    "calls_today",
    "connect",
    "date",
    "finish_run",
    "init",
    "jdump",
    "jload",
    "kv_get",
    "kv_set",
    "last_successful_run",
    "record_usage",
    "session",
    "start_run",
    "utcnow",
    "utctoday",
]
