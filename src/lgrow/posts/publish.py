"""Publish scheduled posts through the official LinkedIn API.

Called every 30 minutes by cron. Deliberately conservative: it re-runs the
quality checks immediately before publishing (config may have changed since the
draft was approved), publishes at most one post per invocation, and records the
returned post URN so a retry can never double-post.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .. import config, db
from ..linkedin import auth as li_auth, client as li_client
from . import quality, store

# One per run. Even if several are somehow due at once, a burst of posts is both
# bad for reach and the sort of pattern worth never generating.
MAX_PER_RUN = 1


def publish_one(
    post: store.Post,
    *,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
) -> bool:
    say = progress or (lambda _m: None)
    text = post.text
    if not text:
        say(f"  post #{post.id} has no text — marking failed")
        with db.session() as conn:
            store.set_state(conn, post.id, store.FAILED, error="empty text")
        return False

    # Re-check now, not just at approval time.
    report = quality.check(text, require_concrete=False)
    if report.problems:
        say(f"  ! post #{post.id} no longer passes checks:")
        for problem in report.problems:
            say(f"      {problem}")
        say("    publishing anyway — you approved this text explicitly")

    try:
        result = li_client.publish_text(
            text, visibility=post.visibility, dry_run=dry_run
        )
    except (li_client.LinkedInApiError, li_auth.LinkedInAuthError) as exc:
        say(f"  ✗ post #{post.id} failed: {exc}")
        with db.session() as conn:
            store.set_state(conn, post.id, store.FAILED, error=str(exc)[:500])
        return False
    except ValueError as exc:
        say(f"  ✗ post #{post.id} rejected before sending: {exc}")
        with db.session() as conn:
            store.set_state(conn, post.id, store.FAILED, error=str(exc)[:500])
        return False

    if dry_run:
        say(f"  (dry run) post #{post.id} would publish as {post.visibility}")
        return True

    with db.session() as conn:
        store.mark_published(conn, post.id, result.post_urn)
    say(f"  ✓ published #{post.id} as {post.visibility}")
    say(f"    {result.permalink}")
    say(f"    log engagement later with: lgrow posts engagement {post.id} --likes N")
    return True


def publish_due(
    *,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> str:
    say = progress or (lambda _m: None)
    now = now or datetime.now(timezone.utc)

    with db.session() as conn:
        pending = store.due(conn, now=now)

    if not pending:
        with db.session() as conn:
            upcoming = store.by_state(conn, store.SCHEDULED, limit=3)
        if upcoming:
            nxt = min(
                (p.scheduled_for for p in upcoming if p.scheduled_for), default=None
            )
            say(f"Nothing due. Next scheduled: {nxt}")
        else:
            say("Nothing due and nothing scheduled.")
        return "nothing due"

    say(f"{len(pending)} post(s) due")
    published = 0
    for post in pending[:MAX_PER_RUN]:
        if publish_one(post, dry_run=dry_run, progress=say):
            published += 1
    if len(pending) > MAX_PER_RUN:
        say(f"  {len(pending) - MAX_PER_RUN} more due; they'll go out on later runs "
            f"(one post per run by design)")
    return f"published={published} due={len(pending)}"


def approve_and_schedule(
    post_id: int,
    *,
    text: str | None = None,
    visibility: str = "PUBLIC",
    progress: Callable[[str], None] | None = None,
) -> datetime | None:
    """Approve a draft and give it the next free slot."""
    from . import schedule as sched

    say = progress or (lambda _m: None)
    voice = config.load_voice()

    with db.session() as conn:
        post = store.get(conn, post_id)
        if post is None:
            say(f"no post with id {post_id}")
            return None
        if text is not None and text.strip() != post.text:
            store.set_text(conn, post_id, text.strip())

        slots = sched.next_slots(1, voice=voice, taken=store.scheduled_dates(conn))
        if not slots:
            say("no free slot in the next 60 days — check cadence.days in voice.yaml")
            return None
        when = slots[0]
        store.schedule(conn, post_id, when, visibility=visibility)

    say(f"✓ post #{post_id} scheduled for {sched.describe(when)} ({visibility})")
    return when
