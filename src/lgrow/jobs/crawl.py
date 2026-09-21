"""The crawl pipeline: fetch -> dedupe -> prefilter -> score -> enqueue."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .. import config, db, llm
from . import normalize, score as scoring, sources, store
from .fetcher import FetchError, Fetcher
from .models import Job, ScoredJob


@dataclass
class CrawlReport:
    per_source: dict[str, int] = field(default_factory=dict)
    source_errors: dict[str, str] = field(default_factory=dict)
    stats: normalize.FilterStats | None = None
    scored: list[ScoredJob] = field(default_factory=list)
    queued: list[ScoredJob] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    tailored: int = 0

    def render(self) -> str:
        lines = ["Sources:"]
        for name, n in sorted(self.per_source.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name:<22} {n:>5}")
        for name, err in self.source_errors.items():
            lines.append(f"  {name:<22} FAILED  {err[:70]}")

        if self.stats:
            lines.append("")
            lines.append("Funnel:")
            lines.append(self.stats.render())

        lines.append("")
        lines.append(f"Stored: {self.inserted} new, {self.updated} refreshed")
        if self.scored:
            top = self.scored[0].score
            good = sum(1 for s in self.scored if s.score >= 70)
            blocked = sum(1 for s in self.scored if not s.geo_ok)
            lines.append(
                f"Scored: {len(self.scored)} · best {top} · {good} at 70+ · "
                f"{blocked} geo-blocked"
            )
        lines.append(f"Added to your queue: {len(self.queued)}")
        if self.tailored:
            lines.append(f"Application materials prepared: {self.tailored}")
        return "\n".join(lines)


def _aggregator_opts(name: str, sources_cfg: config.Sources) -> dict:
    """Per-source knobs from sources.yaml (limit / pages / count)."""
    toggle = sources_cfg.aggregators.get(name)
    if toggle is None:
        return {}
    extras = toggle.model_dump(exclude={"enabled"})
    return {k: v for k, v in extras.items() if v is not None}


def fetch_all(
    sources_cfg: config.Sources, report: CrawlReport, say: Callable[[str], None]
) -> list[Job]:
    """Pull from every enabled source. One failure never stops the others."""
    queries = [q for q in sources_cfg.queries if q and not config.contains_placeholder(q)]
    collected: list[Job] = []

    with Fetcher(sources_cfg.http) as fetcher:
        for name in sources_cfg.enabled_aggregators():
            if name not in sources.AGGREGATORS:
                report.source_errors[name] = "unknown source name"
                continue
            try:
                jobs = sources.fetch_aggregator(
                    name, fetcher, queries=queries, **_aggregator_opts(name, sources_cfg)
                )
            except FetchError as exc:
                report.source_errors[name] = str(exc)
                say(f"  ! {name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 — a bad feed must not kill the crawl
                report.source_errors[name] = f"{type(exc).__name__}: {exc}"
                say(f"  ! {name}: {type(exc).__name__}: {exc}")
                continue
            report.per_source[name] = len(jobs)
            collected.extend(jobs)
            say(f"  {name}: {len(jobs)}")

        if sources_cfg.ats.enabled and sources_cfg.ats.total_companies():
            def on_error(key: str, exc: Exception) -> None:
                report.source_errors[key] = str(exc)
                say(f"  ! {key}: {exc}")

            ats_jobs = sources.ats.fetch_all(
                fetcher,
                greenhouse=sources_cfg.ats.greenhouse,
                lever=sources_cfg.ats.lever,
                ashby=sources_cfg.ats.ashby,
                on_error=on_error,
            )
            by_board: dict[str, int] = {}
            for job in ats_jobs:
                by_board[job.source] = by_board.get(job.source, 0) + 1
            report.per_source.update(by_board)
            for board, n in by_board.items():
                say(f"  {board}: {n}")
            collected.extend(ats_jobs)

    return collected


def crawl(
    *,
    progress: Callable[[str], None] | None = None,
    max_shortlist: int = 60,
    skip_scoring: bool = False,
) -> CrawlReport:
    say = progress or (lambda _m: None)
    profile = config.load_profile()
    sources_cfg = config.load_sources()
    report = CrawlReport()

    say("Fetching…")
    raw = fetch_all(sources_cfg, report, say)
    if not raw:
        say("No jobs fetched — every source failed or returned nothing.")
        return report

    say("")
    say("Filtering…")
    shortlist, stats = normalize.prefilter(
        raw,
        profile=profile,
        max_age_days=sources_cfg.max_age_days,
        max_shortlist=max_shortlist,
        min_heuristic=profile.scoring.min_heuristic,
    )
    report.stats = stats

    with db.session() as conn:
        # Persist the shortlist only. Storing all several-hundred raw postings
        # would bloat the database with rows nothing ever reads.
        report.inserted, report.updated = store.upsert_jobs(conn, shortlist)

    if skip_scoring or not shortlist:
        return report

    say("")
    say("Scoring with Gemini…")
    scored = scoring.score_jobs(shortlist, profile=profile, progress=say)
    scored = scoring.apply_geo_sanity(scored, profile)
    report.scored = scored

    with db.session() as conn:
        report.queued = store.enqueue(
            conn,
            scored,
            min_score=profile.scoring.min_score,
            queue_size=profile.scoring.daily_queue_size,
        )

    if report.queued:
        say("")
        say("Preparing application materials…")
        report.tailored = tailor_queued(report.queued, profile=profile, progress=say)

    return report


def tailor_queued(
    queued: list[ScoredJob],
    *,
    profile: config.Profile | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
) -> int:
    """Generate resume/cover letter/answers for the strongest new matches.

    During an automatic crawl only jobs at or above `tailor_threshold` qualify —
    it costs several Gemini calls per job, and a 66-scoring match rarely
    justifies the spend. `force=True` is for `lgrow jobs tailor`, where you asked
    for a specific job and your judgement outranks the threshold.
    """
    from . import tailor as tailor_mod

    say = progress or (lambda _m: None)
    profile = profile or config.load_profile()
    threshold = profile.scoring.tailor_threshold

    eligible = queued if force else [i for i in queued if i.score >= threshold]
    if not eligible:
        say(f"  none of the new matches reached the tailoring threshold "
            f"({threshold}); materials skipped")
        say(f"  to prepare one anyway: lgrow jobs tailor <job-id>")
        return 0

    done = 0
    for item in eligible:
        say(f"  {item.job.title} @ {item.job.company} (score {item.score})")
        try:
            output = tailor_mod.tailor_for_job(item.job, profile=profile, progress=say)
        except llm.LLMBudgetError as exc:
            say(f"  ! {exc}")
            break
        except Exception as exc:  # noqa: BLE001 — never lose the queue over this
            say(f"  ! failed: {type(exc).__name__}: {exc}")
            continue
        say(output.render())
        with db.session() as conn:
            store.set_artifacts_dir(conn, item.job.id, str(output.directory))
        done += 1
    return done
