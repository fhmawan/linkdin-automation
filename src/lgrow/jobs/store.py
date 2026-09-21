"""Persistence for jobs, verdicts and the application queue."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db
from .models import Job, ScoredJob

FOLLOWUP_DAYS = 7

# Queue states. A job leaves `queued` only by your decision.
QUEUED, APPLIED, SKIPPED, EXPIRED = "queued", "applied", "skipped", "expired"
TERMINAL = (APPLIED, SKIPPED, EXPIRED)


@dataclass
class QueueEntry:
    job: Job
    score: int
    why_fit: list[str]
    gaps: list[str]
    red_flags: list[str]
    geo_ok: bool
    geo_note: str
    state: str
    artifacts_dir: str | None
    created_at: str
    applied_at: str | None = None
    followup_due: str | None = None


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        source=row["source"],
        source_id=row["source_id"] or "",
        url=row["url"],
        apply_url=row["apply_url"],
        company=row["company"],
        title=row["title"],
        location_raw=row["location_raw"] or "",
        geo_tags=db.jload(row["geo_tags"], []) or [],
        employment_type=row["employment_type"],
        salary_raw=row["salary_raw"],
        description=row["description"] or "",
        posted_at=(
            datetime.fromisoformat(row["posted_at"].replace("Z", "+00:00"))
            if row["posted_at"]
            else None
        ),
    )


def upsert_jobs(conn: sqlite3.Connection, jobs: list[Job]) -> tuple[int, int]:
    """Insert or refresh job rows. Returns (inserted, updated)."""
    inserted = updated = 0
    now = db.utcnow()
    for job in jobs:
        existing = conn.execute("SELECT id FROM jobs WHERE id = ?", (job.id,)).fetchone()
        params = (
            job.source,
            job.source_id,
            job.url,
            job.apply_url,
            job.company,
            job.title,
            job.location_raw,
            db.jdump(job.geo_tags),
            job.employment_type,
            job.salary_raw,
            job.description,
            job.description_hash,
            job.posted_at.isoformat() if job.posted_at else None,
            now,
            job.dedupe_key,
        )
        if existing:
            conn.execute(
                "UPDATE jobs SET source=?, source_id=?, url=?, apply_url=?, company=?, "
                "title=?, location_raw=?, geo_tags=?, employment_type=?, salary_raw=?, "
                "description=?, description_hash=?, posted_at=?, fetched_at=?, dedupe_key=? "
                "WHERE id=?",
                (*params, job.id),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO jobs (source, source_id, url, apply_url, company, title, "
                "location_raw, geo_tags, employment_type, salary_raw, description, "
                "description_hash, posted_at, fetched_at, dedupe_key, id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*params, job.id),
            )
            inserted += 1
    conn.commit()
    return inserted, updated


def decided_job_ids(conn: sqlite3.Connection) -> set[str]:
    """Jobs you've already applied to or skipped — never show these again."""
    rows = conn.execute(
        f"SELECT job_id FROM applications WHERE state IN "
        f"({','.join('?' * len(TERMINAL))})",
        TERMINAL,
    ).fetchall()
    return {r["job_id"] for r in rows}


def decided_dedupe_keys(conn: sqlite3.Connection) -> set[str]:
    """Dedupe keys behind decided applications.

    Stops the same role re-entering the queue via a different feed after you've
    already applied to or rejected it.
    """
    rows = conn.execute(
        f"SELECT j.dedupe_key FROM applications a JOIN jobs j ON j.id = a.job_id "
        f"WHERE a.state IN ({','.join('?' * len(TERMINAL))})",
        TERMINAL,
    ).fetchall()
    return {r["dedupe_key"] for r in rows if r["dedupe_key"]}


def enqueue(
    conn: sqlite3.Connection,
    scored: list[ScoredJob],
    *,
    min_score: int,
    queue_size: int,
) -> list[ScoredJob]:
    """Add the best new jobs to your review queue.

    Respects prior decisions and the daily queue cap — the bottleneck is your
    review time, not job supply, so an unbounded queue just becomes noise.
    """
    already = decided_job_ids(conn)
    decided_keys = decided_dedupe_keys(conn)
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE state = ?", (QUEUED,)
    ).fetchone()["n"]

    room = max(0, queue_size - int(open_count))
    if room == 0:
        return []

    added: list[ScoredJob] = []
    for item in sorted(scored, key=lambda s: -s.score):
        if len(added) >= room:
            break
        if item.score < min_score or not item.geo_ok:
            continue
        if item.job.id in already or item.job.dedupe_key in decided_keys:
            continue
        exists = conn.execute(
            "SELECT 1 FROM applications WHERE job_id = ?", (item.job.id,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO applications (job_id, state, created_at) VALUES (?, ?, ?)",
            (item.job.id, QUEUED, db.utcnow()),
        )
        added.append(item)
    conn.commit()
    return added


def queue(
    conn: sqlite3.Connection, *, state: str = QUEUED, limit: int = 50
) -> list[QueueEntry]:
    rows = conn.execute(
        "SELECT a.state, a.artifacts_dir, a.created_at, a.applied_at, a.followup_due, "
        "       s.score, s.verdict, j.* "
        "FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN job_scores s ON s.job_id = a.job_id "
        "WHERE a.state = ? "
        "ORDER BY s.score DESC NULLS LAST, a.created_at DESC "
        "LIMIT ?",
        (state, limit),
    ).fetchall()

    entries: list[QueueEntry] = []
    for row in rows:
        verdict = db.jload(row["verdict"], {}) or {}
        entries.append(
            QueueEntry(
                job=_job_from_row(row),
                score=int(row["score"] or 0),
                why_fit=verdict.get("why_fit") or [],
                gaps=verdict.get("gaps") or [],
                red_flags=verdict.get("red_flags") or [],
                geo_ok=bool(verdict.get("geo_ok", True)),
                geo_note=verdict.get("geo_note") or "",
                state=row["state"],
                artifacts_dir=row["artifacts_dir"],
                created_at=row["created_at"],
                applied_at=row["applied_at"],
                followup_due=row["followup_due"],
            )
        )
    return entries


def set_state(conn: sqlite3.Connection, job_id: str, state: str) -> bool:
    now = db.utcnow()
    if state == APPLIED:
        due = (datetime.now(timezone.utc) + timedelta(days=FOLLOWUP_DAYS)).isoformat()
        cur = conn.execute(
            "UPDATE applications SET state = ?, applied_at = ?, followup_due = ? "
            "WHERE job_id = ?",
            (state, now, due, job_id),
        )
    else:
        cur = conn.execute(
            "UPDATE applications SET state = ? WHERE job_id = ?", (state, job_id)
        )
    conn.commit()
    return cur.rowcount > 0


def set_artifacts_dir(conn: sqlite3.Connection, job_id: str, path: str) -> None:
    conn.execute(
        "UPDATE applications SET artifacts_dir = ? WHERE job_id = ?", (path, job_id)
    )
    conn.commit()


def followups_due(conn: sqlite3.Connection) -> list[QueueEntry]:
    """Applications sent a week ago with no nudge logged yet."""
    rows = conn.execute(
        "SELECT a.state, a.artifacts_dir, a.created_at, a.applied_at, a.followup_due, "
        "       s.score, s.verdict, j.* "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN job_scores s ON s.job_id = a.job_id "
        "WHERE a.state = ? AND a.followup_done = 0 AND a.followup_due IS NOT NULL "
        "  AND a.followup_due <= ? "
        "ORDER BY a.followup_due",
        (APPLIED, datetime.now(timezone.utc).isoformat()),
    ).fetchall()
    out: list[QueueEntry] = []
    for row in rows:
        verdict = db.jload(row["verdict"], {}) or {}
        out.append(
            QueueEntry(
                job=_job_from_row(row),
                score=int(row["score"] or 0),
                why_fit=verdict.get("why_fit") or [],
                gaps=verdict.get("gaps") or [],
                red_flags=verdict.get("red_flags") or [],
                geo_ok=bool(verdict.get("geo_ok", True)),
                geo_note=verdict.get("geo_note") or "",
                state=row["state"],
                artifacts_dir=row["artifacts_dir"],
                created_at=row["created_at"],
                applied_at=row["applied_at"],
                followup_due=row["followup_due"],
            )
        )
    return out


def mark_followup_done(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute(
        "UPDATE applications SET followup_done = 1 WHERE job_id = ?", (job_id,)
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM applications GROUP BY state"
    ).fetchall()
    out = {r["state"]: int(r["n"]) for r in rows}
    out["jobs_known"] = int(
        conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    )
    out["scored"] = int(
        conn.execute("SELECT COUNT(*) AS n FROM job_scores").fetchone()["n"]
    )
    return out
