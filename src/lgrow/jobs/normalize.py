"""Deduplication and the pure-Python prefilter.

This module is the reason the whole thing is cheap to run. A crawl pulls several
hundred postings; sending them all to Gemini would cost hundreds of calls a day.
Here we throw out everything obviously wrong using string work only — no AI —
and hand the model a few dozen genuine candidates.

The heuristic score is intentionally crude. Its only job is ranking the shortlist
so the AI budget goes to plausible jobs first; the real judgement happens in
`score.py`, which reads the actual description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import config
from . import geo
from .models import Job

# Aggregators are convenient, ATS boards are authoritative. When the same role
# appears on both, keep the employer's own listing.
_SOURCE_PRIORITY = ("greenhouse", "lever", "ashby", "himalayas", "remotive", "jobicy", "remoteok", "arbeitnow")

_SENIORITY_WORDS = {
    "junior": ("junior", "jr", "entry level", "entry-level", "graduate", "intern", "associate"),
    "mid": ("mid", "mid-level", "intermediate", "engineer ii", "engineer 2"),
    "senior": ("senior", "snr", "sr", "engineer iii", "engineer 3", "experienced"),
    "lead": ("lead", "principal", "head of", "manager", "architect"),
    "staff": ("staff", "principal", "distinguished"),
}

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "at", "with", "on",
    "remote", "hybrid", "full", "time", "part", "contract", "engineer2",
}


@dataclass
class FilterStats:
    """Where the funnel lost jobs — printed by `lgrow run crawl`."""

    fetched: int = 0
    after_dedupe: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    kept: int = 0
    shortlisted: int = 0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def render(self) -> str:
        lines = [
            f"  fetched            {self.fetched}",
            f"  after dedupe       {self.after_dedupe}",
        ]
        for reason, n in sorted(self.rejected.items(), key=lambda kv: -kv[1]):
            lines.append(f"    - {reason:<26} {n}")
        lines.append(f"  passed prefilter   {self.kept}")
        lines.append(f"  shortlisted for AI {self.shortlisted}")
        return "\n".join(lines)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1}


def _source_rank(source: str) -> int:
    base = source.split(":", 1)[0]
    try:
        return _SOURCE_PRIORITY.index(base)
    except ValueError:
        return len(_SOURCE_PRIORITY)


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same role appearing across multiple feeds.

    Keeps the highest-priority source, breaking ties by description length —
    a fuller description scores better and tailors better.
    """
    best: dict[str, Job] = {}
    for job in jobs:
        key = job.dedupe_key
        if not key.strip("|"):
            key = job.id  # unusable company/title — treat as unique
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = job
            continue
        challenger_wins = (
            _source_rank(job.source),
            -len(job.description),
        ) < (
            _source_rank(incumbent.source),
            -len(incumbent.description),
        )
        if challenger_wins:
            best[key] = job
    return list(best.values())


def _seniority_of(job: Job) -> set[str]:
    haystack = " ".join([job.title, " ".join(job.seniority_raw)]).lower()
    found = {level for level, words in _SENIORITY_WORDS.items()
             if any(w in haystack for w in words)}
    return found


def heuristic_score(job: Job, profile: config.Profile) -> int:
    """Cheap 0-100 relevance guess. String matching only."""
    targets = profile.targets
    title_tokens = _tokens(job.title)
    text = job.searchable()
    score = 0

    # Title vs target roles — the strongest cheap signal available.
    best_overlap = 0.0
    for role in targets.roles:
        role_tokens = _tokens(role)
        if not role_tokens:
            continue
        overlap = len(role_tokens & title_tokens) / len(role_tokens)
        best_overlap = max(best_overlap, overlap)
    score += int(best_overlap * 50)

    core_in_title = sum(1 for s in targets.skills_core if s.lower() in job.title.lower())
    score += min(core_in_title * 8, 24)
    core_in_body = sum(1 for s in targets.skills_core if s.lower() in text)
    score += min(core_in_body * 3, 15)
    secondary = sum(1 for s in targets.skills_secondary if s.lower() in text)
    score += min(secondary, 8)

    wanted = {s.lower() for s in targets.seniority}
    if wanted and (_seniority_of(job) & wanted):
        score += 8

    if _source_rank(job.source) < 3:  # an ATS board
        score += 5
    if job.salary_raw:
        score += 2
    if geo.WORLDWIDE in job.geo_tags:
        score += 3

    return max(0, min(100, score))


def _salary_floor_violated(job: Job, floor: int | None) -> bool:
    """Only reject when a number is present and clearly below the floor.

    An absent salary is never a rejection — most postings omit it, and dropping
    them would remove most of the market.
    """
    if not floor or not job.salary_raw:
        return False
    numbers = [
        int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{3,}", job.salary_raw)
    ]
    if not numbers:
        return False
    return max(numbers) < floor


def prefilter(
    jobs: list[Job],
    *,
    profile: config.Profile | None = None,
    max_age_days: int = 21,
    min_heuristic: int = 20,
    max_shortlist: int = 60,
    now: datetime | None = None,
) -> tuple[list[Job], FilterStats]:
    """Hard rejects, then rank by heuristic and take the top slice.

    Returns (shortlist, stats). The shortlist is what gets spent on AI scoring.
    """
    profile = profile or config.load_profile()
    now = now or datetime.now(timezone.utc)
    stats = FilterStats(fetched=len(jobs))

    unique = dedupe(jobs)
    stats.after_dedupe = len(unique)

    excluded = [k.lower() for k in profile.targets.exclude_keywords if k.strip()]
    allow = profile.geo.allow

    survivors: list[tuple[int, Job]] = []
    for job in unique:
        if not job.title or not job.company:
            stats.reject("incomplete record")
            continue

        age = job.age_days(now)
        if age is not None and age > max_age_days:
            stats.reject(f"older than {max_age_days}d")
            continue
        if age is not None and age < -1:
            stats.reject("posted in the future")
            continue

        title_low = job.title.lower()
        if any(kw in title_low for kw in excluded):
            stats.reject("excluded keyword in title")
            continue

        codes = set(job.geo_tags) or {geo.UNKNOWN}
        allowed, _reason = geo.is_allowed(codes, allow)
        if not allowed:
            stats.reject("geo not permitted")
            continue
        if not geo.english_market(codes):
            stats.reject("outside English-language market")
            continue

        if _salary_floor_violated(job, profile.targets.salary_floor_usd):
            stats.reject("below salary floor")
            continue

        if len(job.description) < 120:
            stats.reject("description too thin to judge")
            continue

        h = heuristic_score(job, profile)
        if h < min_heuristic:
            stats.reject(f"heuristic below {min_heuristic}")
            continue
        survivors.append((h, job))

    stats.kept = len(survivors)
    survivors.sort(key=lambda pair: (-pair[0], pair[1].company))
    shortlist = [job for _, job in survivors[:max_shortlist]]
    stats.shortlisted = len(shortlist)
    return shortlist, stats
