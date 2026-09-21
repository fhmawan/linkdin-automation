"""Harvest raw material for posts.

The hardest part of posting consistently is not writing — it's having something
real to say. Generic posts are worse than no posts, so drafting is only ever
allowed to work from concrete evidence collected here:

  journal  what you typed into `lgrow note`
  git      your actual commits, from the identities YOU configure
  market   aggregate signal from the jobs crawled this week

On git: the author identities come from `config/profile.yaml` and are never
inferred from the shell's git config or the environment. Guessing them would
silently harvest the wrong person's commits, or none at all.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, db, paths
from ..jobs import textutil

JOURNAL = "journal"
GIT = "git"
MARKET = "market"

_JOURNAL_LINE = re.compile(r"^\s*[-*]\s*\[(?P<ts>[^\]]+)\]\s*(?P<text>.+?)\s*$")
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


@dataclass
class Item:
    source: str
    occurred_at: datetime
    text: str
    meta: dict
    fingerprint: str


def _parse_journal_timestamp(raw: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def harvest_journal(path: Path | None = None) -> list[Item]:
    path = path or paths.JOURNAL_PATH
    if not path.exists():
        return []

    items: list[Item] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _JOURNAL_LINE.match(line)
        if match:
            when = _parse_journal_timestamp(match.group("ts"))
            text = match.group("text")
        else:
            # Tolerate hand-written lines with no timestamp.
            when = datetime.now(timezone.utc)
            text = line.lstrip("-*").strip()
        if len(text) < 8:
            continue
        items.append(
            Item(
                source=JOURNAL,
                occurred_at=when,
                text=text,
                meta={},
                fingerprint=textutil.content_hash(f"journal:{text}"),
            )
        )
    return items


def harvest_git(profile: config.Profile | None = None) -> list[Item]:
    """Your commits from the configured repos, over the lookback window."""
    profile = profile or config.load_profile()
    git_cfg = profile.git
    if not git_cfg.authors or not git_cfg.repo_paths:
        return []

    since = (
        datetime.now(timezone.utc) - timedelta(days=git_cfg.lookback_days)
    ).strftime("%Y-%m-%d")
    items: list[Item] = []

    for repo_raw in git_cfg.repo_paths:
        repo = Path(repo_raw).expanduser()
        if not (repo / ".git").exists() and not repo.is_dir():
            continue
        cmd = [
            "git", "-C", str(repo), "log",
            f"--since={since}",
            f"--pretty=format:%H{_FIELD_SEP}%aI{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}",
            "--no-merges",
        ]
        # Multiple --author flags are OR-ed by git.
        for author in git_cfg.authors:
            cmd.append(f"--author={author}")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            continue
        if proc.returncode != 0:
            continue

        for record in proc.stdout.split(_RECORD_SEP):
            record = record.strip()
            if not record:
                continue
            fields = record.split(_FIELD_SEP)
            if len(fields) < 3:
                continue
            sha, iso, subject = fields[0], fields[1], fields[2]
            body = fields[3] if len(fields) > 3 else ""
            if not subject.strip():
                continue
            try:
                when = datetime.fromisoformat(iso)
            except ValueError:
                when = datetime.now(timezone.utc)
            text = subject.strip()
            if body.strip():
                text += f"\n{body.strip()[:400]}"
            items.append(
                Item(
                    source=GIT,
                    occurred_at=when if when.tzinfo else when.replace(tzinfo=timezone.utc),
                    text=text,
                    meta={"repo": repo.name, "sha": sha[:10]},
                    fingerprint=textutil.content_hash(f"git:{sha}"),
                )
            )
    return items


_SKILL_WORDS = re.compile(
    r"\b(python|typescript|javascript|golang|go|rust|java|kotlin|swift|ruby|php|"
    r"c\+\+|c#|scala|elixir|react|vue|svelte|angular|next\.js|node|django|flask|"
    r"fastapi|rails|spring|graphql|grpc|rest|postgres(?:ql)?|mysql|mongodb|redis|"
    r"kafka|rabbitmq|elasticsearch|snowflake|dbt|spark|airflow|docker|kubernetes|"
    r"terraform|ansible|aws|gcp|azure|ci/cd|github actions|pytorch|tensorflow|"
    r"llm|rag|langchain|openai|prometheus|grafana|datadog)\b",
    re.IGNORECASE,
)


def harvest_market(conn: sqlite3.Connection, *, days: int = 7) -> list[Item]:
    """One aggregate observation about what employers asked for this week.

    Deliberately a single item, not one per job: the interesting post is the
    pattern across postings, and flooding the pool with per-job noise would
    crowd out your actual work.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT title, description FROM jobs WHERE fetched_at >= ?", (cutoff,)
    ).fetchall()
    if len(rows) < 10:
        return []

    counts: Counter[str] = Counter()
    for row in rows:
        blob = f"{row['title']}\n{row['description'] or ''}"
        # Count each skill once per posting, not once per mention.
        for skill in {m.group(0).lower() for m in _SKILL_WORDS.finditer(blob)}:
            counts[skill] += 1

    if not counts:
        return []
    top = counts.most_common(10)
    total = len(rows)
    summary = ", ".join(f"{skill} ({n}/{total} postings)" for skill, n in top)
    text = (
        f"Across {total} remote postings reviewed in the last {days} days, the "
        f"most requested skills were: {summary}."
    )
    return [
        Item(
            source=MARKET,
            occurred_at=datetime.now(timezone.utc),
            text=text,
            meta={"postings": total, "top": dict(top)},
            # Date-scoped so a re-run the same day doesn't duplicate it.
            fingerprint=textutil.content_hash(
                f"market:{datetime.now(timezone.utc):%Y-%m-%d}"
            ),
        )
    ]


def sync(conn: sqlite3.Connection, *, profile: config.Profile | None = None) -> dict[str, int]:
    """Import all sources into the activity table. Returns per-source new counts."""
    profile = profile or config.load_profile()
    batches = {
        JOURNAL: harvest_journal(),
        GIT: harvest_git(profile),
        MARKET: harvest_market(conn),
    }

    added: dict[str, int] = {}
    now = db.utcnow()
    for source, items in batches.items():
        n = 0
        for item in items:
            cur = conn.execute(
                "INSERT OR IGNORE INTO activity "
                "(source, occurred_at, text, meta, fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.source,
                    item.occurred_at.isoformat(),
                    item.text,
                    db.jdump(item.meta),
                    item.fingerprint,
                    now,
                ),
            )
            n += cur.rowcount
        added[source] = n
    conn.commit()
    return added


def unused(
    conn: sqlite3.Connection, *, days: int = 21, limit: int = 40
) -> list[sqlite3.Row]:
    """Activity not yet used in a post, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT * FROM activity WHERE used_in_post IS NULL AND occurred_at >= ? "
        "ORDER BY occurred_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()


def mark_used(conn: sqlite3.Connection, activity_ids: list[int], post_id: int) -> None:
    if not activity_ids:
        return
    conn.executemany(
        "UPDATE activity SET used_in_post = ? WHERE id = ?",
        [(post_id, aid) for aid in activity_ids],
    )
    conn.commit()


def release(conn: sqlite3.Connection, post_id: int) -> None:
    """Free a post's activity for reuse — called when a draft is discarded."""
    conn.execute("UPDATE activity SET used_in_post = NULL WHERE used_in_post = ?", (post_id,))
    conn.commit()


def pool_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM activity WHERE used_in_post IS NULL "
        "GROUP BY source"
    ).fetchall()
    if not rows:
        return "no unused activity"
    return ", ".join(f"{r['source']}: {r['n']}" for r in rows)
