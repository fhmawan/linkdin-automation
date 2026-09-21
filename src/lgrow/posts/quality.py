"""Deterministic post checks.

Prompts are instructions, not guarantees — a model told "no em-dashes" will still
produce em-dashes sometimes. So every rule in voice.yaml is enforced twice: in
the prompt, and here in Python where it cannot be ignored. Anything this module
rejects is fed back to the model as a concrete fix list.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .. import config

_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")
_WORD_RE = re.compile(r"[a-z']+")


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hook: str = ""
    chars: int = 0
    hashtags: list[str] = field(default_factory=list)
    similarity: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [f"length {self.chars} chars · {len(self.hashtags)} hashtag(s)"]
        if self.similarity:
            lines.append(f"similarity to recent posts: {self.similarity:.0%}")
        for p in self.problems:
            lines.append(f"  ✗ {p}")
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


def hook_of(text: str) -> str:
    """The part LinkedIn shows before "…see more"."""
    first_line = text.strip().split("\n", 1)[0].strip()
    return first_line


def normalise_for_similarity(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def similarity(text: str, others: list[str]) -> float:
    """Highest similarity ratio against any previous post."""
    if not others:
        return 0.0
    target = normalise_for_similarity(text)
    if not target:
        return 0.0
    best = 0.0
    for other in others:
        ratio = difflib.SequenceMatcher(
            None, target, normalise_for_similarity(other)
        ).ratio()
        best = max(best, ratio)
    return best


def check(
    text: str,
    *,
    voice: config.Voice | None = None,
    recent: list[str] | None = None,
    require_concrete: bool = True,
) -> Report:
    voice = voice or config.load_voice()
    style = voice.style
    recent = recent or []

    body = text.strip()
    report = Report(chars=len(body), hook=hook_of(body))

    if not body:
        report.problems.append("empty post")
        return report

    # ─ length ─
    if len(body) < style.min_chars:
        report.problems.append(
            f"too short: {len(body)} chars, minimum {style.min_chars}"
        )
    if len(body) > style.max_chars:
        report.problems.append(
            f"too long: {len(body)} chars, maximum {style.max_chars}"
        )

    # ─ hook ─
    if len(report.hook) > style.hook_max_chars:
        report.problems.append(
            f"first line is {len(report.hook)} chars; LinkedIn truncates around "
            f"{style.hook_max_chars}, so the hook must fit in one line"
        )
    if not report.hook:
        report.problems.append("no opening line")

    # ─ hashtags ─
    report.hashtags = _HASHTAG_RE.findall(body)
    if len(report.hashtags) > style.max_hashtags:
        report.problems.append(
            f"{len(report.hashtags)} hashtags, maximum {style.max_hashtags}"
        )

    # ─ banned phrases and characters ─
    low = body.lower()
    for phrase in voice.banned_phrases:
        if phrase.lower() in low:
            report.problems.append(f"banned phrase: {phrase!r}")
    for ch in voice.banned_chars:
        if ch in body:
            report.problems.append(
                f"banned character {ch!r} — it reads as machine-written"
            )

    # ─ structure ─
    paragraphs = [p for p in body.split("\n") if p.strip()]
    if len(paragraphs) < 3:
        report.warnings.append(
            "few line breaks — dense blocks perform badly in the feed"
        )
    longest = max((len(p) for p in paragraphs), default=0)
    if longest > 400:
        report.warnings.append(f"one paragraph is {longest} chars; break it up")

    if style.require_closing_question and "?" not in body[-260:]:
        report.problems.append("no question near the end to invite replies")

    if style.first_person and not re.search(r"\b(I|I'm|I've|my|me)\b", body):
        report.warnings.append("no first-person voice — reads like a press release")

    # ─ concreteness ─
    if require_concrete and style.require_concrete_detail:
        has_number = bool(re.search(r"\d", body))
        has_code_ish = bool(re.search(r"[a-z]+[._][a-z]+|`[^`]+`|[A-Za-z]+\(\)", body))
        if not (has_number or has_code_ish):
            report.warnings.append(
                "no concrete detail found (no number, identifier or code "
                "reference); specifics are what make a post credible"
            )

    # ─ repetition ─
    report.similarity = similarity(body, recent)
    if report.similarity > voice.max_similarity_to_recent:
        report.problems.append(
            f"{report.similarity:.0%} similar to a recent post (limit "
            f"{voice.max_similarity_to_recent:.0%}) — say something new"
        )

    return report


def fix_instructions(report: Report) -> str:
    """Problems rewritten as an instruction the model can act on."""
    if report.ok:
        return ""
    return "Fix all of the following, changing nothing else:\n" + "\n".join(
        f"- {p}" for p in report.problems
    )
