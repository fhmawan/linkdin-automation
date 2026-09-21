"""Draft posts from real activity, then attack the draft for AI tells.

Two ideas do the heavy lifting:

1. **Nothing is written from nothing.** Every draft must be grounded in specific
   items harvested by `activity.py`. A post with no concrete detail is worse for
   your reputation than not posting, so the prompt is given evidence and told to
   cite it.

2. **The model reviews its own output as a hostile reader.** A first draft from
   any model drifts toward LinkedIn-generic. A second pass scored on specificity
   and authenticity, combined with the deterministic checks in `quality.py`,
   removes most of that.

Drafting uses the `quality` model tier — this is text going out under your name.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .. import config, db, llm
from . import activity as activity_mod, quality, store

MAX_STRUCTURE_RETRIES = 2


class Draft(BaseModel):
    """What the model returns for a single post."""

    hook: str = Field(description="First line. Must stand alone and fit ~140 chars.")
    body: str = Field(description="The rest of the post, after the hook.")
    hashtags: list[str] = Field(default_factory=list, max_length=3)
    concrete_detail_used: str = Field(
        default="", description="The specific fact from the evidence you built this on"
    )

    def assemble(self) -> str:
        text = f"{self.hook.strip()}\n\n{self.body.strip()}"
        tags = [f"#{t.lstrip('#')}" for t in self.hashtags if t.strip()]
        if tags:
            text += "\n\n" + " ".join(tags)
        return text.strip()


class Critique(BaseModel):
    reads_as_ai: bool = Field(description="Would a careful reader think a bot wrote this?")
    specificity: int = Field(description="0-100: how concrete and evidence-grounded")
    problems: list[str] = Field(default_factory=list, max_length=5)
    improved_text: str = Field(
        default="", description="Full rewritten post if changes are needed, else empty"
    )


@dataclass
class DraftResult:
    post_id: int
    pillar: str
    text: str
    report: quality.Report
    critique: dict


# ─── pillar rotation ─────────────────────────────────────────────────────────


def pick_pillar(voice: config.Voice, history: list[str]) -> config.Pillar:
    """Choose the pillar furthest below its target share.

    Deterministic rather than random: over a few weeks this converges on the
    configured weights, and it never posts the same pillar three times running
    the way random selection sometimes does.
    """
    pillars = voice.pillars
    if not pillars:
        raise ValueError("no content pillars configured in config/voice.yaml")

    total_weight = sum(max(0, p.weight) for p in pillars) or 1
    window = history[: max(len(pillars) * 3, 6)]

    best: tuple[float, int, config.Pillar] | None = None
    for index, pillar in enumerate(pillars):
        target = max(0, pillar.weight) / total_weight
        actual = (window.count(pillar.name) / len(window)) if window else 0.0
        deficit = target - actual
        # Tie-break away from the most recent pillar.
        recency_penalty = 0 if not history or history[0] != pillar.name else 1
        candidate = (-deficit, recency_penalty, pillar)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    assert best is not None
    return best[2]


# ─── evidence selection ──────────────────────────────────────────────────────


def select_evidence(
    rows: list[sqlite3.Row], pillar: config.Pillar, *, want: int = 6
) -> list[sqlite3.Row]:
    """Pick activity items suited to the chosen pillar."""
    if pillar.name == "market_observation":
        market = [r for r in rows if r["source"] == activity_mod.MARKET]
        if market:
            return market[:1]
        return []  # this pillar has nothing honest to say without market data

    preferred = [r for r in rows if r["source"] in (activity_mod.JOURNAL, activity_mod.GIT)]
    return preferred[:want]


def _evidence_block(rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in rows:
        meta = db.jload(row["meta"], {}) or {}
        tag = row["source"]
        if meta.get("repo"):
            tag = f"git:{meta['repo']}"
        lines.append(f"- ({tag}, {row['occurred_at'][:10]}) {row['text']}")
    return "\n".join(lines)


# ─── prompts ─────────────────────────────────────────────────────────────────


def build_draft_prompt(
    *,
    pillar: config.Pillar,
    evidence: list[sqlite3.Row],
    profile: config.Profile,
    voice: config.Voice,
    recent: list[str],
    fix_note: str = "",
) -> str:
    style = voice.style
    tone = "\n".join(f"- {t}" for t in voice.tone)
    banned = ", ".join(f"{p!r}" for p in voice.banned_phrases)
    banned_chars = ", ".join(f"{c!r}" for c in voice.banned_chars)
    recent_block = (
        "\n\n".join(f"---\n{t[:400]}" for t in recent[:4])
        if recent
        else "(none yet)"
    )

    prompt = f"""Write one LinkedIn post for this person, in their voice, as if they \
wrote it themselves. You are ghost-writing, not commentating.

## Who is posting
{profile.identity.full_name} — {profile.identity.headline}
Based in {profile.identity.location}.

## The angle for this post: {pillar.name}
{pillar.brief.strip()}

## Evidence you must build on
These are real things this person actually did or observed. The post must be \
grounded in at least one of them, and must reuse its specifics — the actual tool \
name, number, error, or outcome. Do not generalise the specifics away.

{_evidence_block(evidence)}

## Voice
{tone}

## Hard requirements
- First line ("hook") must work alone and be under {style.hook_max_chars} characters. \
LinkedIn hides everything after it behind "…see more", so it decides whether \
anyone reads the post.
- Total length between {style.min_chars} and {style.max_chars} characters.
- A line break every {style.line_break_every_sentences} sentences at most. Dense \
paragraphs get skipped in the feed.
- At most {style.max_hashtags} hashtags, and only genuinely relevant ones.
- End with a real question — one a practitioner would actually answer, not a \
rhetorical prompt.
- Include at least one concrete, checkable detail from the evidence.
- First person throughout.

## Never do these
- Never use these phrases: {banned}
- Never use these characters: {banned_chars}
- No opening with a one-word sentence for drama.
- No "unpopular opinion", no fake-humility bragging, no listicle of platitudes.
- Do not claim results, metrics, job titles or credentials that are not in the \
evidence above. Inventing achievements is the single worst failure here.
- Do not describe the process of learning in the abstract. Show the specific thing.

## The person's recent posts, to avoid repeating yourself
{recent_block}
"""
    if fix_note:
        prompt += f"\n## Corrections required\n{fix_note}\n"
    return prompt


def build_critique_prompt(text: str, voice: config.Voice) -> str:
    return f"""Read this draft LinkedIn post as a sceptical senior engineer who \
scrolls past ninety percent of their feed. Your job is to catch it sounding \
AI-written before it publishes under someone's real name.

## The draft
---
{text}
---

## Judge it on
1. **Does it read as machine-written?** Tells: even, rhythmic sentence lengths; \
abstract nouns doing the work; a tidy three-part structure; enthusiasm with no \
object; transitions like "moreover" or "in conclusion"; any sentence that could \
appear in anyone's post about anything.
2. **Specificity (0-100).** Could only this person have written this, about this \
week? A post full of real particulars scores high. One that could be posted by \
ten thousand people scores below 30.
3. **Does the hook earn the click** without being clickbait?
4. **Is the closing question answerable** by a practitioner, or is it filler?

## If it needs work
Put a complete rewrite in `improved_text` — the whole post, ready to publish, \
keeping every real detail from the original and inventing nothing new. Preserve \
the author's claims exactly; you may only change how they are expressed.
If it genuinely needs no change, leave `improved_text` empty.

Be harsh. A false pass costs the author more than a false fail.
"""


# ─── generation ──────────────────────────────────────────────────────────────


def draft_one(
    conn: sqlite3.Connection,
    *,
    pillar: config.Pillar | None = None,
    profile: config.Profile | None = None,
    voice: config.Voice | None = None,
    progress: Callable[[str], None] | None = None,
) -> DraftResult | None:
    """Produce one reviewed draft, or None if there's nothing to write about."""
    say = progress or (lambda _m: None)
    profile = profile or config.load_profile()
    voice = voice or config.load_voice()

    history = store.pillar_history(conn)
    pillar = pillar or pick_pillar(voice, history)

    pool = activity_mod.unused(conn)
    evidence = select_evidence(pool, pillar)
    if not evidence:
        say(f"  – {pillar.name}: no unused activity to build on; skipping")
        return None

    recent = store.all_recent_texts(conn)
    say(f"  drafting '{pillar.name}' from {len(evidence)} activity item(s)")

    text = ""
    report = quality.Report()
    fix_note = ""

    for attempt in range(1, MAX_STRUCTURE_RETRIES + 2):
        prompt = build_draft_prompt(
            pillar=pillar,
            evidence=evidence,
            profile=profile,
            voice=voice,
            recent=recent,
            fix_note=fix_note,
        )
        draft = llm.generate_structured(
            prompt, Draft, purpose="draft_post", tier="quality"
        )
        text = draft.assemble()
        report = quality.check(text, voice=voice, recent=recent)
        if report.ok:
            break
        say(f"  attempt {attempt}: {len(report.problems)} rule violation(s)")
        for problem in report.problems:
            say(f"    ✗ {problem}")
        fix_note = quality.fix_instructions(report)
        if attempt > MAX_STRUCTURE_RETRIES:
            say("  ! keeping the draft despite violations — flagged for your review")

    # ─ anti-slop pass ─
    critique_dict: dict = {}
    try:
        critique = llm.generate_structured(
            build_critique_prompt(text, voice),
            Critique,
            purpose="critique_post",
            tier="quality",
        )
    except llm.LLMError as exc:
        say(f"  ! critique pass unavailable ({exc}); keeping the first draft")
    else:
        critique_dict = critique.model_dump()
        say(f"  critique: specificity {critique.specificity}/100, "
            f"reads_as_ai={critique.reads_as_ai}")
        for problem in critique.problems:
            say(f"    · {problem}")

        should_replace = (
            critique.improved_text.strip()
            and (critique.reads_as_ai or critique.specificity < 60)
        )
        if should_replace:
            candidate = critique.improved_text.strip()
            candidate_report = quality.check(candidate, voice=voice, recent=recent)
            # Only accept the rewrite if it doesn't break the hard rules.
            if len(candidate_report.problems) <= len(report.problems):
                say("  applied the rewrite from the critique pass")
                text, report = candidate, candidate_report
            else:
                say("  rejected the rewrite — it broke more rules than the original")

    post_id = store.create(
        conn,
        pillar=pillar.name,
        text=text,
        critique=critique_dict,
        activity_ids=[int(r["id"]) for r in evidence],
        model="quality",
    )
    activity_mod.mark_used(conn, [int(r["id"]) for r in evidence], post_id)
    return DraftResult(
        post_id=post_id, pillar=pillar.name, text=text, report=report, critique=critique_dict
    )


def draft_week(
    *,
    count: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[DraftResult]:
    """Draft the week's posts, refreshing the activity pool first."""
    say = progress or (lambda _m: None)
    profile = config.load_profile()
    voice = config.load_voice()
    target = count or voice.cadence.posts_per_week

    results: list[DraftResult] = []
    with db.session() as conn:
        added = activity_mod.sync(conn, profile=profile)
        say(f"  activity synced: " + ", ".join(f"{k}+{v}" for k, v in added.items()))
        say(f"  unused pool: {activity_mod.pool_summary(conn)}")

        existing = len(store.pending_review(conn)) + len(store.by_state(conn, store.APPROVED))
        if existing >= target:
            say(f"  {existing} draft(s) already awaiting review — nothing new needed")
            return []

        for _ in range(target - existing):
            try:
                result = draft_one(conn, profile=profile, voice=voice, progress=say)
            except llm.LLMBudgetError as exc:
                say(f"  ! {exc}")
                break
            except llm.LLMError as exc:
                say(f"  ! drafting failed: {exc}")
                break
            if result is None:
                break
            results.append(result)
            say(f"  ✓ draft #{result.post_id} ({result.pillar}), {result.report.chars} chars")

    if results:
        say("")
        say(f"Review them with:  lgrow posts review")
    return results
