"""`lgrow` command line entrypoint."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import __version__, db, paths


def _print(msg: str = "") -> None:
    print(msg, flush=True)


# ─── doctor ──────────────────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    _print(f"lgrow {__version__}  ·  project root: {paths.ROOT}")
    _print()
    checks = doctor.run(probe=args.probe, network=args.network, progress=_print)
    _print(doctor.render(checks))
    oks, warns, fails = doctor.summary(checks)
    _print()
    _print(f"  {oks} ok · {warns} warning(s) · {fails} blocking")
    if fails:
        _print()
        _print("  Fix the ✗ items above, then re-run `lgrow doctor`.")
        if not args.probe:
            _print("  Add --probe to spend one Gemini call verifying auth for real.")
        return 1
    if warns:
        _print()
        _print("  Nothing blocking. Warnings reduce quality but the pipeline will run.")
    return 0


# ─── init ────────────────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    paths.ensure_dirs()
    with db.session() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    _print(f"✓ data directories under {paths.DATA_DIR}")
    _print(f"✓ database ready at {paths.DB_PATH} (schema v{version})")

    if not paths.JOURNAL_PATH.exists():
        paths.JOURNAL_PATH.write_text(
            "# Work journal\n"
            "#\n"
            "# One line per thing you learned, built, or broke. Raw and specific beats\n"
            "# polished — the specifics are what make posts sound like you.\n"
            "# Add entries with:  lgrow note \"...\"\n\n",
            encoding="utf-8",
        )
        _print(f"✓ created {paths.JOURNAL_PATH}")

    env_example = paths.ROOT / ".env.example"
    if env_example.exists() and not paths.ENV_PATH.exists():
        _print(f"! copy .env.example to .env and fill in your LinkedIn app creds")

    _print()
    _print("Next:  lgrow doctor")
    return 0


# ─── note ────────────────────────────────────────────────────────────────────


def cmd_note(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        _print("Nothing to record. Usage: lgrow note \"what you figured out\"")
        return 2

    paths.ensure_dirs()
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    if not paths.JOURNAL_PATH.exists():
        paths.JOURNAL_PATH.write_text("# Work journal\n\n", encoding="utf-8")
    with paths.JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"- [{stamp}] {text}\n")

    _print(f"✓ noted. {paths.JOURNAL_PATH.name} is the raw material for your posts.")
    return 0


# ─── usage ───────────────────────────────────────────────────────────────────


def cmd_usage(args: argparse.Namespace) -> int:
    from . import config, llm

    ai = config.load_ai()
    with db.session() as conn:
        today = db.utctoday()
        rows = conn.execute(
            "SELECT purpose, model, COUNT(*) AS n, SUM(ok) AS ok, "
            "       SUM(prompt_chars) AS pc, SUM(response_chars) AS rc, "
            "       AVG(duration_ms) AS ms "
            "FROM api_usage WHERE day = ? GROUP BY purpose, model ORDER BY n DESC",
            (today,),
        ).fetchall()
        total = db.calls_today(conn)
        history = conn.execute(
            "SELECT day, COUNT(*) AS n, SUM(ok) AS ok FROM api_usage "
            "WHERE day < ? GROUP BY day ORDER BY day DESC LIMIT 7",
            (today,),
        ).fetchall()

    _print(f"Model usage for {today} (UTC)")
    _print(f"  {total} call(s) used · {llm.remaining_budget()} left of "
           f"{ai.budget.max_calls_per_day} self-imposed cap")
    _print("  (Free tiers cap by the day and count each provider separately;")
    _print("   the cap above is only a runaway-loop guard. Normal use is ~15.)")
    if rows:
        _print()
        _print(f"  {'purpose':<24} {'model':<38} {'calls':>5} {'ok':>4} {'avg ms':>8}")
        for r in rows:
            _print(f"  {(r['purpose'] or '?'):<24} {(r['model'] or '?'):<38} "
                   f"{r['n']:>5} {int(r['ok'] or 0):>4} {int(r['ms'] or 0):>8}")
    else:
        _print("\n  No calls yet today.")
    if history:
        _print()
        _print("  Previous days:")
        for r in history:
            _print(f"    {r['day']}  {r['n']:>4} call(s), {int(r['ok'] or 0)} ok")
    return 0


# ─── linkedin ────────────────────────────────────────────────────────────────


def cmd_linkedin(args: argparse.Namespace) -> int:
    from .linkedin import auth as li_auth

    try:
        if args.action == "login":
            tokens = li_auth.login(open_browser=not args.no_browser)
            _print()
            _print(f"✓ authorised as {tokens.person_name or 'you'} ({tokens.person_urn})")
            _print(f"  scopes: {tokens.scope}")
            if tokens.access_expires_at:
                _print(f"  access token valid until {tokens.access_expires_at:%Y-%m-%d}")
            if tokens.refresh_expires_at:
                _print(f"  refresh token valid until {tokens.refresh_expires_at:%Y-%m-%d}")
            return 0

        if args.action == "refresh":
            tokens = li_auth.refresh()
            _print(f"✓ access token refreshed, valid until "
                   f"{tokens.access_expires_at:%Y-%m-%d}"
                   if tokens.access_expires_at else "✓ access token refreshed")
            return 0

        if args.action == "whoami":
            tokens = li_auth.load_tokens()
            if not tokens:
                _print("Not authorised. Run: lgrow linkedin login")
                return 1
            _print(f"person: {tokens.person_name or '?'}  {tokens.person_urn or '?'}")
            _print(f"scopes: {tokens.scope}")
            now = datetime.now(timezone.utc)
            if tokens.access_expires_at:
                _print(f"access token:  {(tokens.access_expires_at - now).days}d left")
            if tokens.refresh_expires_at:
                _print(f"refresh token: {(tokens.refresh_expires_at - now).days}d left")
            return 0
    except li_auth.LinkedInAuthError as exc:
        _print(f"✗ {exc}")
        return 1
    return 2


# ─── jobs ────────────────────────────────────────────────────────────────────


def cmd_crawl(args: argparse.Namespace) -> int:
    from .jobs import crawl as crawl_mod

    report = crawl_mod.crawl(
        progress=_print,
        max_shortlist=args.max_shortlist,
        skip_scoring=args.skip_scoring,
    )
    _print()
    _print(report.render())

    if report.queued:
        _print()
        _print("New in your queue:")
        for item in report.queued:
            _print(f"  [{item.score:>3}] {item.job.summary_line()}")
        _print()
        _print("Review them with:  lgrow jobs queue     (or: lgrow dashboard)")
    elif report.scored and not args.skip_scoring:
        _print()
        _print("Nothing new cleared your score threshold. Either the crawl found")
        _print("no good matches today, or your queue is already full — check")
        _print("`lgrow jobs queue` and clear some entries.")
    return 0


def _cmd_jobs_tailor(args: argparse.Namespace) -> int:
    """Generate application materials for queued jobs.

    The queue is read and released before tailoring starts: `tailor_queued`
    opens its own database sessions per job, and holding the outer one open
    across many minutes of Gemini calls would serialise writes for no reason.
    """
    from .jobs import crawl as crawl_mod, store
    from .jobs.models import ScoredJob

    with db.session() as conn:
        entries = store.queue(conn, limit=max(args.limit, 200 if args.job_id else args.limit))
    if args.job_id:
        entries = [e for e in entries if e.job.id.startswith(args.job_id)]
        if not entries:
            _print(f"✗ no queued job with an id starting {args.job_id!r}")
            _print("  list them with: lgrow jobs queue")
            return 1
    if not entries:
        _print("Queue is empty. Run: lgrow run crawl")
        return 0
    entries = entries[: args.limit] if not args.job_id else entries

    targets = [
        ScoredJob(
            job=e.job, score=e.score, why_fit=e.why_fit, gaps=e.gaps,
            red_flags=e.red_flags, geo_ok=e.geo_ok, geo_note=e.geo_note,
        )
        for e in entries
    ]
    _print(f"Preparing materials for {len(targets)} job(s)…")
    # Explicit request, so bypass the score threshold the crawl applies.
    count = crawl_mod.tailor_queued(targets, progress=_print, force=True)
    _print()
    _print(f"✓ prepared materials for {count} job(s), under {paths.APPLICATIONS_DIR}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    from .jobs import store

    if args.action == "tailor":
        return _cmd_jobs_tailor(args)

    with db.session() as conn:
        if args.action == "queue":
            entries = store.queue(conn, limit=args.limit)
            if not entries:
                _print("Queue is empty. Run: lgrow run crawl")
                return 0
            for i, e in enumerate(entries, start=1):
                _print(f"\n{i}. [{e.score:>3}] {e.job.title}")
                _print(f"   {e.job.company} · {e.job.location_raw or '?'} · {e.job.source}")
                _print(f"   {e.job.url}")
                if e.why_fit:
                    for w in e.why_fit:
                        _print(f"   + {w}")
                if e.gaps:
                    for g in e.gaps:
                        _print(f"   - {g}")
                if e.red_flags:
                    for r in e.red_flags:
                        _print(f"   ⚠ {r}")
                if not e.geo_ok and e.geo_note:
                    _print(f"   ⚠ location: {e.geo_note}")
                _print(f"   id: {e.job.id}")
            _print()
            _print(f"{len(entries)} in queue. Mark one with:")
            _print("  lgrow jobs applied <id>   |   lgrow jobs skip <id>")
            return 0

        if args.action in ("applied", "skip"):
            if not args.job_id:
                _print(f"Usage: lgrow jobs {args.action} <job-id>")
                return 2
            state = store.APPLIED if args.action == "applied" else store.SKIPPED
            if store.set_state(conn, args.job_id, state):
                _print(f"✓ marked {args.job_id} as {state}")
                if state == store.APPLIED:
                    _print(f"  follow-up reminder in {store.FOLLOWUP_DAYS} days")
                return 0
            _print(f"✗ no queue entry with id {args.job_id}")
            return 1

        if args.action == "stats":
            s = store.stats(conn)
            _print("Pipeline:")
            for key in ("jobs_known", "scored", store.QUEUED, store.APPLIED, store.SKIPPED):
                if key in s:
                    _print(f"  {key:<12} {s[key]}")
            due = store.followups_due(conn)
            if due:
                _print(f"\n{len(due)} follow-up(s) due:")
                for e in due:
                    _print(f"  {e.job.title} @ {e.job.company}  (applied {e.applied_at[:10]})")
            return 0
    return 2


# ─── posts ───────────────────────────────────────────────────────────────────


def _edit_in_editor(initial: str) -> str | None:
    """Open $EDITOR on the text. Returns None if unchanged or cancelled."""
    import os
    import subprocess
    import tempfile

    fallback = "notepad" if os.name == "nt" else "nano"
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or fallback
    with tempfile.NamedTemporaryFile(
        "w+", suffix=".md", encoding="utf-8", delete=False
    ) as fh:
        fh.write(initial)
        fh.write(
            "\n\n<!-- Edit above, save and close. Lines starting with "
            "<!-- are stripped. -->\n"
        )
        temp_path = fh.name
    try:
        subprocess.run([editor, temp_path], check=False)
        with open(temp_path, encoding="utf-8") as fh:
            edited = fh.read()
    except OSError as exc:
        _print(f"could not open {editor}: {exc}")
        return None
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    cleaned = "\n".join(
        line for line in edited.splitlines() if not line.strip().startswith("<!--")
    ).strip()
    return cleaned or None


def _show_post(post, report=None) -> None:
    from .posts import quality as quality_mod

    report = report or quality_mod.check(post.text, require_concrete=False)
    _print("─" * 68)
    _print(f"draft #{post.id}   pillar: {post.pillar}   state: {post.state}")
    _print("─" * 68)
    _print(post.text)
    _print("─" * 68)
    _print(report.render())
    critique = post.critique or {}
    if critique:
        _print(
            f"AI critique: specificity {critique.get('specificity', '?')}/100, "
            f"reads_as_ai={critique.get('reads_as_ai')}"
        )
        for problem in critique.get("problems") or []:
            _print(f"  · {problem}")
    hook_len = len(report.hook)
    _print(f"hook ({hook_len} chars, shown before “…see more”): {report.hook}")


def cmd_posts(args: argparse.Namespace) -> int:
    from .posts import generate, publish as publish_mod, schedule as sched, store as post_store

    if args.action == "draft":
        results = generate.draft_week(count=args.count, progress=_print)
        return 0 if results or args.count == 0 else 0

    if args.action == "list":
        with db.session() as conn:
            counts = post_store.counts(conn)
            _print("Posts by state: " + (
                ", ".join(f"{k}={v}" for k, v in counts.items()) or "none yet"))
            for state in (post_store.DRAFT, post_store.SCHEDULED,
                          post_store.PUBLISHED, post_store.FAILED):
                posts = post_store.by_state(conn, state, limit=args.limit)
                if not posts:
                    continue
                _print(f"\n{state}:")
                for p in posts:
                    when = p.scheduled_for or p.published_at or p.created_at
                    first = p.text.split("\n", 1)[0][:60]
                    extra = f"  {p.post_urn}" if p.post_urn else ""
                    _print(f"  #{p.id:<4} {(when or '')[:16]}  {p.pillar:<20} {first}{extra}")
                    if p.last_error:
                        _print(f"        error: {p.last_error[:100]}")
        return 0

    if args.action == "review":
        with db.session() as conn:
            pending = post_store.pending_review(conn)
        if not pending:
            _print("No drafts awaiting review. Create some with: lgrow posts draft")
            return 0

        _print(f"{len(pending)} draft(s) to review.\n")
        for post in pending:
            _show_post(post)
            _print()
            choice = ""
            while choice not in ("a", "e", "s", "r", "q"):
                choice = input(
                    "  [a]pprove & schedule  [e]dit  [r]egenerate  [s]kip  [q]uit: "
                ).strip().lower()

            if choice == "q":
                _print("stopped — remaining drafts stay pending")
                return 0
            if choice == "s":
                with db.session() as conn:
                    post_store.set_state(conn, post.id, post_store.SKIPPED)
                    from .posts import activity as activity_mod

                    activity_mod.release(conn, post.id)
                _print("  skipped; its source activity is free to reuse\n")
                continue
            if choice == "r":
                with db.session() as conn:
                    post_store.set_state(conn, post.id, post_store.SKIPPED)
                    from .posts import activity as activity_mod

                    activity_mod.release(conn, post.id)
                    _print("  regenerating…")
                    result = generate.draft_one(conn, progress=_print)
                if result:
                    with db.session() as conn:
                        fresh = post_store.get(conn, result.post_id)
                    if fresh:
                        _show_post(fresh, result.report)
                        if input("  approve this one? [y/N]: ").strip().lower() == "y":
                            publish_mod.approve_and_schedule(
                                fresh.id, visibility=args.visibility, progress=_print
                            )
                _print()
                continue

            text = post.text
            if choice == "e":
                edited = _edit_in_editor(text)
                if edited:
                    text = edited
                    from .posts import quality as quality_mod

                    _print()
                    _print(quality_mod.check(text, require_concrete=False).render())

            publish_mod.approve_and_schedule(
                post.id, text=text, visibility=args.visibility, progress=_print
            )
            _print()
        return 0

    if args.action == "schedule":
        with db.session() as conn:
            approved = post_store.by_state(conn, post_store.APPROVED, limit=20)
            taken = post_store.scheduled_dates(conn)
        if not approved:
            slots = sched.next_slots(3, taken=taken)
            _print("No approved posts waiting. Next free slots would be:")
            for slot in slots:
                _print(f"  {sched.describe(slot)}")
            return 0
        for post in approved:
            publish_mod.approve_and_schedule(
                post.id, visibility=args.visibility, progress=_print
            )
        return 0

    if args.action == "engagement":
        if not args.post_id:
            _print("Usage: lgrow posts engagement <post-id> --likes N --comments N")
            return 2
        with db.session() as conn:
            post = post_store.get(conn, int(args.post_id))
            if post is None:
                _print(f"no post #{args.post_id}")
                return 1
            post_store.record_engagement(
                conn,
                post.id,
                likes=args.likes,
                comments=args.comments,
                reposts=args.reposts,
                impressions=args.impressions,
            )
            _print(f"✓ recorded engagement for #{post.id}")
            perf = post_store.pillar_performance(conn)
        if perf:
            _print("\nAverage engagement by pillar:")
            for row in perf:
                _print(f"  {row['pillar']:<22} {row['posts']:>3} post(s)  "
                       f"{row['avg_likes']:>6} likes  {row['avg_comments']:>5} comments")
        return 0

    if args.action == "publish-now":
        if not args.post_id:
            _print("Usage: lgrow posts publish-now <post-id> [--dry-run]")
            return 2
        with db.session() as conn:
            post = post_store.get(conn, int(args.post_id))
        if post is None:
            _print(f"no post #{args.post_id}")
            return 1
        ok = publish_mod.publish_one(post, dry_run=args.dry_run, progress=_print)
        return 0 if ok else 1

    return 2


# ─── dashboard ───────────────────────────────────────────────────────────────


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import server

    server.serve(port=args.port, open_browser=not args.no_browser)
    return 0


# ─── run ─────────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    """Scheduled-task entrypoint, guarded by a lock and logged to the runs table."""
    from .runner import run_task

    return run_task(args.task, args, printer=_print)


# ─── parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lgrow",
        description=(
            "LinkedIn growth + remote job application engine. "
            "AI runs through the Gemini CLI (your Google AI Pro subscription); "
            "LinkedIn is touched only via its official API."
        ),
    )
    parser.add_argument("--version", action="version", version=f"lgrow {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="check that everything is configured")
    p_doctor.add_argument("--probe", action="store_true",
                          help="spend Gemini calls to verify auth and models for real")
    p_doctor.add_argument("--network", action="store_true",
                          help="also check that every job source is reachable")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init", help="create data dirs and the database")
    p_init.set_defaults(func=cmd_init)

    p_note = sub.add_parser("note", help="record something you learned (feeds post drafts)")
    p_note.add_argument("text", nargs="+")
    p_note.set_defaults(func=cmd_note)

    p_usage = sub.add_parser("usage", help="show Gemini call usage and remaining budget")
    p_usage.set_defaults(func=cmd_usage)

    p_crawl = sub.add_parser("crawl", help="fetch, score and queue jobs (alias of `run crawl`)")
    p_crawl.add_argument("--skip-scoring", action="store_true",
                         help="fetch and filter only — spends no Gemini calls")
    p_crawl.add_argument("--max-shortlist", type=int, default=60,
                         help="cap on jobs sent for AI scoring (default 60)")
    p_crawl.set_defaults(func=cmd_crawl)

    p_jobs = sub.add_parser("jobs", help="inspect and update your application queue")
    p_jobs.add_argument(
        "action", choices=["queue", "applied", "skip", "stats", "tailor"]
    )
    p_jobs.add_argument("job_id", nargs="?")
    p_jobs.add_argument("--limit", type=int, default=20)
    p_jobs.set_defaults(func=cmd_jobs)

    p_posts = sub.add_parser("posts", help="draft, review, schedule and publish posts")
    p_posts.add_argument(
        "action",
        choices=["draft", "review", "list", "schedule", "engagement", "publish-now"],
    )
    p_posts.add_argument("post_id", nargs="?")
    p_posts.add_argument("--count", type=int, default=None,
                         help="how many drafts to create (default: cadence.posts_per_week)")
    p_posts.add_argument("--limit", type=int, default=10)
    p_posts.add_argument("--visibility", choices=["PUBLIC", "CONNECTIONS"],
                         default="PUBLIC",
                         help="CONNECTIONS is worth using for your first live post")
    p_posts.add_argument("--dry-run", action="store_true",
                         help="print the API payload instead of publishing")
    p_posts.add_argument("--likes", type=int)
    p_posts.add_argument("--comments", type=int)
    p_posts.add_argument("--reposts", type=int)
    p_posts.add_argument("--impressions", type=int)
    p_posts.set_defaults(func=cmd_posts)

    p_dash = sub.add_parser("dashboard", help="local review dashboard in your browser")
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument("--no-browser", action="store_true")
    p_dash.set_defaults(func=cmd_dashboard)

    p_run = sub.add_parser("run", help="scheduled task entrypoint (used by cron)")
    p_run.add_argument("task", choices=["crawl", "publish-due", "weekly", "catchup"])
    p_run.add_argument("--dry-run", action="store_true",
                       help="publish tasks print the payload instead of sending it")
    p_run.add_argument("--skip-scoring", action="store_true")
    p_run.add_argument("--max-shortlist", type=int, default=60)
    p_run.set_defaults(func=cmd_run)

    p_li = sub.add_parser("linkedin", help="authorise and inspect LinkedIn access")
    p_li.add_argument("action", choices=["login", "refresh", "whoami"])
    p_li.add_argument("--no-browser", action="store_true",
                      help="print the URL instead of opening a browser")
    p_li.set_defaults(func=cmd_linkedin)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
