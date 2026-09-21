"""Scheduled-task dispatch.

Everything cron invokes comes through here so that all runs share three
properties: only one instance of a task at a time (a laptop waking from sleep
can otherwise fire overlapping jobs), every run recorded in the `runs` table,
and output copied to a dated log file.

The `catchup` task is what makes a laptop schedule trustworthy — it replays
whatever was missed while the machine was off.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import db, paths

# How stale a task may be before `catchup` re-runs it.
CATCHUP_GRACE = {
    "crawl": timedelta(hours=20),
    "weekly": timedelta(days=7),
    "publish-due": timedelta(hours=1),
}


class Lock:
    """Cooperative lock via O_EXCL, with staleness recovery.

    A crashed run must not wedge the schedule permanently, so a lock older than
    `max_age` is treated as abandoned.
    """

    def __init__(self, name: str, max_age: timedelta = timedelta(hours=3)) -> None:
        paths.ensure_dirs()
        self.path = paths.LOCKS_DIR / f"{name}.lock"
        self.max_age = max_age
        self.acquired = False

    def __enter__(self) -> "Lock":
        if self.path.exists():
            age = datetime.now(timezone.utc).timestamp() - self.path.stat().st_mtime
            if age > self.max_age.total_seconds():
                self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            self.acquired = False
            return self
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()}\n{db.utcnow()}\n")
        self.acquired = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


class Tee:
    """Print to the terminal and append to a dated log file."""

    def __init__(self, task: str, printer: Callable[[str], None]) -> None:
        paths.ensure_dirs()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.log_path: Path = paths.LOGS_DIR / f"{day}.log"
        self.task = task
        self.printer = printer
        self._fh = self.log_path.open("a", encoding="utf-8")
        self._fh.write(f"\n{'=' * 70}\n{db.utcnow()}  task={task}\n{'=' * 70}\n")

    def __call__(self, msg: str = "") -> None:
        self.printer(msg)
        self._fh.write(msg + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ─── tasks ───────────────────────────────────────────────────────────────────


def task_crawl(args: Any, say: Callable[[str], None]) -> str:
    from .jobs import crawl as crawl_mod

    report = crawl_mod.crawl(
        progress=say,
        max_shortlist=getattr(args, "max_shortlist", 60),
        skip_scoring=getattr(args, "skip_scoring", False),
    )
    say("")
    say(report.render())
    return f"queued={len(report.queued)} scored={len(report.scored)}"


def task_publish_due(args: Any, say: Callable[[str], None]) -> str:
    from .posts import publish

    return publish.publish_due(progress=say, dry_run=getattr(args, "dry_run", False))


def task_weekly(args: Any, say: Callable[[str], None]) -> str:
    from .jobs import store as job_store
    from .posts import generate

    parts: list[str] = []

    say("Drafting next week's posts…")
    try:
        drafts = generate.draft_week(progress=say)
        parts.append(f"drafts={len(drafts)}")
    except Exception as exc:  # noqa: BLE001 — one failure shouldn't skip the rest
        say(f"  ! drafting failed: {type(exc).__name__}: {exc}")
        parts.append("drafts=failed")

    say("")
    say("Refreshing LinkedIn token…")
    try:
        from .linkedin import auth as li_auth

        tokens = li_auth.load_tokens()
        if tokens and tokens.refresh_token:
            fresh = li_auth.refresh(tokens)
            if fresh.access_expires_at:
                say(f"  ✓ valid until {fresh.access_expires_at:%Y-%m-%d}")
            parts.append("token=refreshed")
        else:
            say("  – not authorised yet; skipping")
            parts.append("token=absent")
    except Exception as exc:  # noqa: BLE001
        say(f"  ! {exc}")
        parts.append("token=failed")

    say("")
    with db.session() as conn:
        due = job_store.followups_due(conn)
    if due:
        say(f"{len(due)} application(s) worth a follow-up:")
        for entry in due:
            say(f"  {entry.job.title} @ {entry.job.company} "
                f"(applied {(entry.applied_at or '')[:10]})")
            say(f"    {entry.job.url}")
    else:
        say("No follow-ups due.")
    parts.append(f"followups={len(due)}")

    return " ".join(parts)


def task_catchup(args: Any, say: Callable[[str], None]) -> str:
    """Replay tasks whose last success is older than their grace window."""
    now = datetime.now(timezone.utc)
    ran: list[str] = []

    with db.session() as conn:
        last = {task: db.last_successful_run(conn, task) for task in CATCHUP_GRACE}

    for task, grace in CATCHUP_GRACE.items():
        if task == "publish-due":
            continue  # its own 30-minute cron picks this up promptly
        previous = last.get(task)
        overdue = previous is None or (now - previous) > grace
        if not overdue:
            age = now - previous  # type: ignore[operator]
            say(f"  {task}: ran {age.total_seconds() / 3600:.1f}h ago — skipping")
            continue
        say(f"  {task}: overdue (last success: "
            f"{previous.isoformat() if previous else 'never'}) — running now")
        code = run_task(task, args, printer=say, nested=True)
        ran.append(f"{task}={'ok' if code == 0 else 'failed'}")

    # Anything already scheduled and past due should go out too.
    say("  publish-due: checking for posts past their slot")
    try:
        code = run_task("publish-due", args, printer=say, nested=True)
        ran.append(f"publish-due={'ok' if code == 0 else 'failed'}")
    except Exception as exc:  # noqa: BLE001
        say(f"  ! {exc}")

    return ", ".join(ran) or "nothing overdue"


TASKS: dict[str, Callable[[Any, Callable[[str], None]], str]] = {
    "crawl": task_crawl,
    "publish-due": task_publish_due,
    "weekly": task_weekly,
    "catchup": task_catchup,
}


def run_task(
    task: str,
    args: Any = None,
    *,
    printer: Callable[[str], None] | None = None,
    nested: bool = False,
) -> int:
    """Execute one task under lock, recording the outcome."""
    if task not in TASKS:
        (printer or print)(f"unknown task: {task}")
        return 2
    args = args or SimpleNamespace()
    printer = printer or print

    if nested:
        # Already inside a locked, logged parent run.
        return _execute(task, args, printer)

    with Lock(task) as lock:
        if not lock.acquired:
            printer(f"another `{task}` run is in progress ({lock.path}) — skipping")
            return 0
        say = Tee(task, printer)
        try:
            return _execute(task, args, say)
        finally:
            say.close()


def _execute(task: str, args: Any, say: Callable[[str], None]) -> int:
    with db.session() as conn:
        run_id = db.start_run(conn, task)
    try:
        detail = TASKS[task](args, say)
    except Exception as exc:  # noqa: BLE001 — cron needs a clean exit code
        trace = traceback.format_exc(limit=6)
        say(f"\n✗ {task} failed: {type(exc).__name__}: {exc}")
        say(trace)
        with db.session() as conn:
            db.finish_run(conn, run_id, "error", f"{type(exc).__name__}: {exc}"[:500])
        return 1
    with db.session() as conn:
        db.finish_run(conn, run_id, "ok", detail[:500] if detail else None)
    return 0


def main() -> int:
    """`python -m lgrow.runner <task>` — a cron-friendly alternative entrypoint."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <{'|'.join(TASKS)}>")
        return 2
    return run_task(sys.argv[1], SimpleNamespace(dry_run=False))


if __name__ == "__main__":
    sys.exit(main())
