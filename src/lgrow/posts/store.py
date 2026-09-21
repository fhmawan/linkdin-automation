"""Post persistence and lifecycle transitions.

States: draft -> approved -> scheduled -> published
        (draft|approved|scheduled) -> skipped
        scheduled -> failed  (retryable; stays visible)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import db

DRAFT = "draft"
APPROVED = "approved"
SCHEDULED = "scheduled"
PUBLISHED = "published"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Post:
    id: int
    pillar: str
    draft_text: str
    final_text: str | None
    state: str
    critique: dict
    activity_ids: list[int]
    scheduled_for: str | None
    published_at: str | None
    post_urn: str | None
    visibility: str
    last_error: str | None
    created_at: str

    @property
    def text(self) -> str:
        """What would actually be published."""
        return (self.final_text or self.draft_text).strip()


def _to_post(row: sqlite3.Row) -> Post:
    return Post(
        id=int(row["id"]),
        pillar=row["pillar"],
        draft_text=row["draft_text"],
        final_text=row["final_text"],
        state=row["state"],
        critique=db.jload(row["critique"], {}) or {},
        activity_ids=db.jload(row["activity_ids"], []) or [],
        scheduled_for=row["scheduled_for"],
        published_at=row["published_at"],
        post_urn=row["post_urn"],
        visibility=row["visibility"] or "PUBLIC",
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


def create(
    conn: sqlite3.Connection,
    *,
    pillar: str,
    text: str,
    critique: dict | None = None,
    activity_ids: list[int] | None = None,
    model: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO posts (pillar, draft_text, state, critique, activity_ids, "
        "model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pillar,
            text,
            DRAFT,
            db.jdump(critique or {}),
            db.jdump(activity_ids or []),
            model,
            db.utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, post_id: int) -> Post | None:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return _to_post(row) if row else None


def by_state(conn: sqlite3.Connection, state: str, limit: int = 50) -> list[Post]:
    rows = conn.execute(
        "SELECT * FROM posts WHERE state = ? ORDER BY created_at DESC LIMIT ?",
        (state, limit),
    ).fetchall()
    return [_to_post(r) for r in rows]


def pending_review(conn: sqlite3.Connection) -> list[Post]:
    rows = conn.execute(
        "SELECT * FROM posts WHERE state = ? ORDER BY created_at", (DRAFT,)
    ).fetchall()
    return [_to_post(r) for r in rows]


def set_text(conn: sqlite3.Connection, post_id: int, text: str) -> None:
    conn.execute("UPDATE posts SET final_text = ? WHERE id = ?", (text, post_id))
    conn.commit()


def set_state(
    conn: sqlite3.Connection, post_id: int, state: str, *, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE posts SET state = ?, last_error = ? WHERE id = ?",
        (state, error, post_id),
    )
    conn.commit()


def schedule(
    conn: sqlite3.Connection, post_id: int, when: datetime, visibility: str = "PUBLIC"
) -> None:
    conn.execute(
        "UPDATE posts SET state = ?, scheduled_for = ?, visibility = ? WHERE id = ?",
        (SCHEDULED, when.astimezone(timezone.utc).isoformat(), visibility, post_id),
    )
    conn.commit()


def due(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Post]:
    """Scheduled posts whose slot has arrived, oldest first."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM posts WHERE state IN (?, ?) AND scheduled_for IS NOT NULL "
        "AND scheduled_for <= ? ORDER BY scheduled_for",
        (SCHEDULED, FAILED, now.astimezone(timezone.utc).isoformat()),
    ).fetchall()
    return [_to_post(r) for r in rows]


def scheduled_dates(conn: sqlite3.Connection) -> list[str]:
    """ISO timestamps already taken, so the slot picker doesn't double-book."""
    rows = conn.execute(
        "SELECT scheduled_for FROM posts WHERE scheduled_for IS NOT NULL "
        "AND state IN (?, ?, ?)",
        (SCHEDULED, PUBLISHED, FAILED),
    ).fetchall()
    return [r["scheduled_for"] for r in rows if r["scheduled_for"]]


def mark_published(
    conn: sqlite3.Connection, post_id: int, post_urn: str
) -> None:
    conn.execute(
        "UPDATE posts SET state = ?, published_at = ?, post_urn = ?, last_error = NULL "
        "WHERE id = ?",
        (PUBLISHED, db.utcnow(), post_urn, post_id),
    )
    conn.commit()


def recent_published_texts(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT draft_text, final_text FROM posts WHERE state = ? "
        "ORDER BY published_at DESC LIMIT ?",
        (PUBLISHED, limit),
    ).fetchall()
    return [(r["final_text"] or r["draft_text"]) for r in rows]


def all_recent_texts(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    """Published *and* pending texts — a draft shouldn't repeat another draft."""
    rows = conn.execute(
        "SELECT draft_text, final_text FROM posts WHERE state != ? "
        "ORDER BY created_at DESC LIMIT ?",
        (SKIPPED, limit),
    ).fetchall()
    return [(r["final_text"] or r["draft_text"]) for r in rows]


def pillar_history(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    rows = conn.execute(
        "SELECT pillar FROM posts WHERE state != ? ORDER BY created_at DESC LIMIT ?",
        (SKIPPED, limit),
    ).fetchall()
    return [r["pillar"] for r in rows]


def record_engagement(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    likes: int | None = None,
    comments: int | None = None,
    reposts: int | None = None,
    impressions: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO post_engagement (post_id, likes, comments, reposts, "
        "impressions, logged_at) VALUES (?, ?, ?, ?, ?, ?)",
        (post_id, likes, comments, reposts, impressions, db.utcnow()),
    )
    conn.commit()


def pillar_performance(conn: sqlite3.Connection) -> list[dict]:
    """Average engagement per pillar, from the numbers you logged by hand.

    LinkedIn's `r_member_social` scope is approval-gated, so this is the only
    feedback loop available — but it's enough to steer future drafts.
    """
    rows = conn.execute(
        "SELECT p.pillar, COUNT(DISTINCT p.id) AS posts, "
        "       AVG(e.likes) AS avg_likes, AVG(e.comments) AS avg_comments "
        "FROM posts p JOIN post_engagement e ON e.post_id = p.id "
        "GROUP BY p.pillar ORDER BY avg_likes DESC"
    ).fetchall()
    return [
        {
            "pillar": r["pillar"],
            "posts": int(r["posts"]),
            "avg_likes": round(r["avg_likes"] or 0, 1),
            "avg_comments": round(r["avg_comments"] or 0, 1),
        }
        for r in rows
    ]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) AS n FROM posts GROUP BY state").fetchall()
    return {r["state"]: int(r["n"]) for r in rows}
