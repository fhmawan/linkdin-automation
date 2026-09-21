"""AI match scoring — the one place jobs meet the model.

Cost control is the whole design here. A crawl shortlists a few dozen jobs;
scoring them one-per-call would be a few dozen Gemini calls a day. Instead we
batch ~12 jobs per prompt and cache every verdict against
(job description, profile revision), so a re-crawl of an unchanged posting costs
nothing. Typical result: 40 jobs scored in 3-4 calls.

Scoring runs on the `cheap` tier. It's bulk classification, and the model only
has to rank — the expensive `quality` tier is reserved for text you'll put your
name on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from pydantic import BaseModel, Field, field_validator

from .. import config, db, llm
from . import geo, textutil
from .models import Job, ScoredJob

# Per-job description budget inside a batch prompt. Enough to judge a role;
# small enough that 12 of them plus instructions stay comfortable.
DESC_BUDGET = 3200
DEFAULT_BATCH_SIZE = 12


class JobVerdict(BaseModel):
    """The model's judgement on one job."""

    ref: int = Field(description="The job's number from the input list")
    score: int = Field(description="0-100 fit against the candidate profile")
    why_fit: list[str] = Field(default_factory=list, max_length=4)
    gaps: list[str] = Field(default_factory=list, max_length=4)
    red_flags: list[str] = Field(default_factory=list, max_length=4)
    geo_ok: bool = True
    geo_note: str = ""

    @field_validator("score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))


class BatchVerdict(BaseModel):
    verdicts: list[JobVerdict]


def profile_version(profile: config.Profile | None = None) -> str:
    """Revision key for the scoring cache.

    Only the fields that actually change a verdict are included, so editing your
    phone number doesn't invalidate every cached score.
    """
    profile = profile or config.load_profile()
    t = profile.targets
    material = "|".join(
        [
            ",".join(sorted(t.roles)),
            ",".join(sorted(t.seniority)),
            ",".join(sorted(t.skills_core)),
            ",".join(sorted(t.skills_secondary)),
            ",".join(sorted(profile.geo.allow)),
            ",".join(sorted(profile.work_auth.authorized_countries)),
            str(profile.work_auth.needs_sponsorship),
            str(t.salary_floor_usd),
            str(t.years_experience),
            str(t.max_years_required),
        ]
    )
    return textutil.content_hash(material)


def _years(value: float) -> str:
    """Render 2.0 as '2' but keep 2.5 as '2.5' — '2.0 years' reads like a bug."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _experience_rule(profile: config.Profile) -> str:
    """The seniority instruction, sharpened when experience bounds are set.

    Without these, a posting asking "3+ years" is judged only against fuzzy
    seniority words in its title, and the model is free to read any shortfall as
    a hard mismatch. That is the wrong call for a candidate deliberately
    applying one notch above their current years.
    """
    t = profile.targets
    if t.years_experience is None or t.max_years_required is None:
        return "3. **Seniority mismatch** in either direction."
    have, ceiling = _years(t.years_experience), _years(t.max_years_required)
    return (
        f"3. **Years of experience.** The candidate has {have} years and has "
        f"decided to apply for roles asking up to {ceiling} years. A posting "
        f"requiring at most {ceiling} years is therefore NOT a seniority "
        f"mismatch: note the shortfall in `gaps` if you like, but do not cut "
        f"the score for it, and never put it in `red_flags`. Only a "
        f"requirement above {ceiling} years is a real mismatch. Being clearly "
        f"over-qualified is also a mismatch."
    )


def _profile_brief(profile: config.Profile) -> str:
    t = profile.targets
    wa = profile.work_auth
    lines = [
        f"Headline: {profile.identity.headline}",
        f"Based in: {profile.identity.location} (timezone {profile.geo.my_timezone})",
        f"Target roles: {', '.join(t.roles) or 'unspecified'}",
        f"Seniority: {', '.join(t.seniority) or 'any'}",
    ]
    if t.years_experience is not None:
        lines.append(
            f"Professional experience: {_years(t.years_experience)} years"
        )
    if t.max_years_required is not None:
        lines.append(
            f"Highest 'years required' they will apply to: "
            f"{_years(t.max_years_required)} years"
        )
    lines += [
        f"Core skills: {', '.join(t.skills_core) or 'unspecified'}",
        f"Secondary skills: {', '.join(t.skills_secondary) or 'none'}",
        f"Can work remotely for employers in: {', '.join(profile.geo.allow)}",
        f"Already authorised to work in: {', '.join(wa.authorized_countries) or 'nowhere stated'}",
        f"Needs visa sponsorship: {'yes' if wa.needs_sponsorship else 'no'}",
    ]
    if t.salary_floor_usd:
        lines.append(f"Minimum acceptable salary: USD {t.salary_floor_usd:,}")
    return "\n".join(lines)


def _job_block(index: int, job: Job) -> str:
    return "\n".join(
        [
            f"### Job {index}",
            f"Title: {job.title}",
            f"Company: {job.company}",
            f"Stated location: {job.location_raw or 'not stated'}",
            f"Regions parsed from metadata: {', '.join(job.geo_tags) or 'none'}",
            f"Employment type: {job.employment_type or 'not stated'}",
            f"Salary: {job.salary_raw or 'not stated'}",
            f"Description:\n{textutil.truncate(job.description, DESC_BUDGET)}",
        ]
    )


def build_prompt(jobs: list[Job], profile: config.Profile) -> str:
    blocks = "\n\n".join(_job_block(i, job) for i, job in enumerate(jobs))
    return f"""You are screening remote job postings for one candidate. Be a strict, \
calibrated screener: your output decides which jobs are worth their limited \
application time, so inflated scores are actively harmful.

## Candidate
{_profile_brief(profile)}

## Scoring guide
- 90-100: strong match. Core skills and seniority line up, remote-eligible for \
this candidate, nothing disqualifying.
- 70-89: good match with a manageable gap or two.
- 50-69: plausible but a real stretch — wrong seniority, or several core skills missing.
- 25-49: weak. Different discipline, or a hard requirement they don't meet.
- 0-24: not applicable at all.

## What to check carefully
1. **Hidden location restrictions.** The metadata often says "remote" while the \
description says "must be based in the US", "requires overlap with EST", or \
"eligible to work in the UK without sponsorship". Read the text, and set \
`geo_ok` false with a short `geo_note` when the candidate is excluded. This is \
the most common reason a promising posting is actually a dead end.
2. **Sponsorship and authorisation.** If the role requires existing work \
authorisation the candidate lacks, that is a red flag, not a gap.
{_experience_rule(profile)}
4. **Vagueness.** A posting with no concrete requirements deserves a low score, \
not a generous one.

## Rules for your output
- Return exactly one verdict per job, and echo its number in `ref`.
- `why_fit`: up to 3 short, specific reasons grounded in the posting. No filler.
- `gaps`: what the candidate is missing. Be honest.
- `red_flags`: unpaid, commission-only, obvious scam, undisclosed agency, \
impossible timezone, sponsorship refused. Empty list if genuinely none.
- Every string must be under 140 characters.

## Jobs to score
{blocks}
"""


def _cached(
    conn: sqlite3.Connection, job: Job, version: str
) -> tuple[int, dict] | None:
    row = conn.execute(
        "SELECT score, verdict FROM job_scores WHERE job_id = ? AND profile_version = ?",
        (job.id, version),
    ).fetchone()
    if not row:
        return None
    return int(row["score"]), db.jload(row["verdict"], {}) or {}


def _persist(
    conn: sqlite3.Connection, scored: ScoredJob, version: str
) -> None:
    conn.execute(
        "INSERT INTO job_scores (job_id, profile_version, score, verdict, model, scored_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id, profile_version) DO UPDATE SET "
        "  score = excluded.score, verdict = excluded.verdict, "
        "  model = excluded.model, scored_at = excluded.scored_at",
        (
            scored.job.id,
            version,
            scored.score,
            db.jdump(
                {
                    "why_fit": scored.why_fit,
                    "gaps": scored.gaps,
                    "red_flags": scored.red_flags,
                    "geo_ok": scored.geo_ok,
                    "geo_note": scored.geo_note,
                }
            ),
            scored.model,
            db.utcnow(),
        ),
    )


def _verdict_to_scored(job: Job, v: JobVerdict, model: str) -> ScoredJob:
    score = v.score
    # A geo exclusion the model found in the description outranks any metadata
    # optimism — cap the score so it can't lead the queue.
    if not v.geo_ok:
        score = min(score, 25)
    return ScoredJob(
        job=job,
        score=score,
        why_fit=[s[:140] for s in v.why_fit],
        gaps=[s[:140] for s in v.gaps],
        red_flags=[s[:140] for s in v.red_flags],
        geo_ok=v.geo_ok,
        geo_note=v.geo_note[:200],
        model=model,
    )


def score_jobs(
    jobs: list[Job],
    *,
    profile: config.Profile | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[ScoredJob]:
    """Score every job, using the cache and batching the rest.

    Batches that fail are skipped with a warning rather than aborting the crawl:
    a partial queue is far more useful than none.
    """
    if not jobs:
        return []
    profile = profile or config.load_profile()
    version = profile_version(profile)
    say = progress or (lambda _msg: None)

    results: list[ScoredJob] = []
    pending: list[Job] = []

    with db.session() as conn:
        if use_cache:
            for job in jobs:
                hit = _cached(conn, job, version)
                if hit is None:
                    pending.append(job)
                    continue
                score, verdict = hit
                results.append(
                    ScoredJob(
                        job=job,
                        score=score,
                        why_fit=verdict.get("why_fit") or [],
                        gaps=verdict.get("gaps") or [],
                        red_flags=verdict.get("red_flags") or [],
                        geo_ok=bool(verdict.get("geo_ok", True)),
                        geo_note=verdict.get("geo_note") or "",
                        model=verdict.get("model") or "cached",
                    )
                )
        else:
            pending = list(jobs)

        if results:
            say(f"  {len(results)} verdict(s) reused from cache (0 calls)")

        batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
        if batches:
            say(f"  scoring {len(pending)} job(s) in {len(batches)} Gemini call(s)")

        model_label = config.load_ai().models.cheap or "auto"
        for n, batch in enumerate(batches, start=1):
            prompt = build_prompt(batch, profile)
            try:
                verdict = llm.generate_structured(
                    prompt,
                    BatchVerdict,
                    purpose="score_jobs",
                    tier="cheap",
                )
            except llm.LLMBudgetError:
                say(f"  ! budget exhausted at batch {n}/{len(batches)} — "
                    f"{len(pending) - (n - 1) * batch_size} job(s) left unscored")
                break
            except llm.LLMError as exc:
                say(f"  ! batch {n}/{len(batches)} failed ({exc}); skipping those "
                    f"{len(batch)} job(s)")
                continue

            by_ref = {v.ref: v for v in verdict.verdicts}
            missing = 0
            for i, job in enumerate(batch):
                v = by_ref.get(i)
                if v is None:
                    missing += 1
                    continue
                scored = _verdict_to_scored(job, v, model_label)
                results.append(scored)
                _persist(conn, scored, version)
            if missing:
                say(f"  ! batch {n}: model returned no verdict for {missing} job(s)")
            say(f"  batch {n}/{len(batches)} done")

    results.sort(key=lambda s: -s.score)
    return results


def apply_geo_sanity(scored: list[ScoredJob], profile: config.Profile) -> list[ScoredJob]:
    """Belt-and-braces metadata check after the model has spoken.

    The model reads prose; this catches the case where structured metadata is
    unambiguous and the model was too generous anyway.
    """
    for item in scored:
        codes = set(item.job.geo_tags)
        allowed, reason = geo.is_allowed(codes, profile.geo.allow)
        if not allowed and item.geo_ok:
            item.geo_ok = False
            item.geo_note = f"metadata says {reason}"
            item.score = min(item.score, 25)
    return scored
