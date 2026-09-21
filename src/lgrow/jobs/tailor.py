"""Tailor your resume and write application materials for one job.

The governing rule is that **nothing is invented**. The model may only re-word
and re-emphasise experience already in your base resume, so that the wording
mirrors the job description's vocabulary and survives ATS keyword matching. It
may not add employers, technologies, metrics or dates.

That rule is enforced structurally rather than trusted:

- Edits are returned as (original paragraph, replacement) pairs and applied only
  where the original matches exactly, so the model cannot append new sections.
- `suspicious_numbers()` flags any figure in a replacement that does not appear
  in the source paragraph — fabricated metrics are the most damaging and most
  likely invention, and they get surfaced to you rather than silently shipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from .. import config, llm, paths
from . import textutil
from .models import Job

JD_BUDGET = 6000


class Rewrite(BaseModel):
    original: str = Field(description="The paragraph's exact original text")
    replacement: str = Field(description="Rewritten version, same facts")
    reason: str = Field(default="", description="Which JD requirement this targets")


class TailoredResume(BaseModel):
    summary: str = Field(
        default="", description="Replacement professional summary, or empty to keep"
    )
    rewrites: list[Rewrite] = Field(default_factory=list, max_length=14)
    keywords_targeted: list[str] = Field(default_factory=list, max_length=20)
    honest_gaps: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Requirements the resume genuinely cannot support",
    )


class CoverLetter(BaseModel):
    body: str = Field(description="The letter, no greeting or sign-off")
    specifics_cited: list[str] = Field(
        default_factory=list, max_length=4,
        description="Details taken from this specific posting",
    )


class ScreenerAnswers(BaseModel):
    why_this_company: str = ""
    why_you: str = ""
    salary_expectation: str = ""
    work_authorization: str = ""
    notice_period: str = ""
    biggest_relevant_project: str = ""


@dataclass
class TailorOutput:
    directory: Path
    resume_docx: Path | None = None
    resume_pdf: Path | None = None
    cover_letter: Path | None = None
    answers: Path | None = None
    applied_edits: int = 0
    skipped_edits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"  files in {self.directory}"]
        for label, path in (
            ("resume (docx)", self.resume_docx),
            ("resume (pdf)", self.resume_pdf),
            ("cover letter", self.cover_letter),
            ("screener answers", self.answers),
        ):
            if path:
                lines.append(f"    {label:<18} {path.name}")
        lines.append(f"  {self.applied_edits} resume edit(s) applied")
        if self.skipped_edits:
            lines.append(f"  {len(self.skipped_edits)} edit(s) skipped (no exact match)")
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


# ─── resume reading and writing ──────────────────────────────────────────────


def read_resume_paragraphs(path: Path | None = None) -> list[str]:
    """Non-empty paragraph texts from the base resume, in order."""
    import docx

    path = path or paths.RESUME_BASE
    if not path.exists():
        raise FileNotFoundError(
            f"No base resume at {path}.\n"
            "Save your master resume there as .docx (not PDF — it has to be edited)."
        )
    document = docx.Document(str(path))
    texts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    # Table cells often hold skills sections.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                text = cell.text.strip()
                if text:
                    texts.append(text)
    return texts


_NUMBER_RE = re.compile(r"\d[\d,.]*\s*%?|\b\d+\s*(?:x|k|m|bn)\b", re.IGNORECASE)


def suspicious_numbers(original: str, replacement: str) -> list[str]:
    """Figures in the replacement that never appeared in the original.

    A fabricated "improved throughput by 40%" is the worst thing this pipeline
    could do to you in an interview, so any new number is reported.
    """
    def norm(values: list[str]) -> set[str]:
        return {v.strip().lower().rstrip(".") for v in values}

    before = norm(_NUMBER_RE.findall(original))
    after = norm(_NUMBER_RE.findall(replacement))
    return sorted(after - before)


def apply_rewrites(
    source: Path, dest: Path, tailored: TailoredResume
) -> tuple[int, list[str], list[str]]:
    """Copy the resume and apply replacements in place.

    Returns (applied, skipped_originals, warnings). Formatting survives because
    we write into the existing runs rather than rebuilding paragraphs.
    """
    import docx

    shutil.copy2(source, dest)
    document = docx.Document(str(dest))

    wanted = {r.original.strip(): r for r in tailored.rewrites if r.original.strip()}
    applied = 0
    warnings: list[str] = []
    matched: set[str] = set()

    def rewrite_paragraph(paragraph) -> bool:
        nonlocal applied
        key = paragraph.text.strip()
        rule = wanted.get(key)
        if rule is None or key in matched:
            return False
        new_text = rule.replacement.strip()
        if not new_text:
            return False

        invented = suspicious_numbers(key, new_text)
        if invented:
            warnings.append(
                f"skipped an edit that introduced figures not in your resume "
                f"({', '.join(invented)}): {new_text[:80]}…"
            )
            matched.add(key)
            return False

        if paragraph.runs:
            paragraph.runs[0].text = new_text
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.text = new_text
        applied += 1
        matched.add(key)
        return True

    for paragraph in document.paragraphs:
        rewrite_paragraph(paragraph)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    rewrite_paragraph(paragraph)

    document.save(str(dest))
    skipped = [orig for orig in wanted if orig not in matched]
    return applied, skipped, warnings


def to_pdf(docx_path: Path) -> Path | None:
    """Convert via LibreOffice if available. Absence is not an error."""
    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--headless", "--convert-to", "pdf", "--outdir",
             str(docx_path.parent), str(docx_path)],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    candidate = docx_path.with_suffix(".pdf")
    return candidate if candidate.exists() else None


# ─── prompts ─────────────────────────────────────────────────────────────────


def _job_context(job: Job) -> str:
    return "\n".join(
        [
            f"Title: {job.title}",
            f"Company: {job.company}",
            f"Location: {job.location_raw or 'not stated'}",
            f"Employment type: {job.employment_type or 'not stated'}",
            f"Salary: {job.salary_raw or 'not stated'}",
            "",
            "Description:",
            textutil.truncate(job.description, JD_BUDGET),
        ]
    )


def build_resume_prompt(job: Job, paragraphs: list[str], profile: config.Profile) -> str:
    numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))
    return f"""Re-word an existing resume so it matches one job posting more \
closely. You are editing wording and emphasis only.

## ABSOLUTE RULE
Invent nothing. You may not add an employer, job title, technology, \
certification, date, or metric that is not already present in the resume below. \
If the posting wants something the candidate does not have, say so in \
`honest_gaps` — do not quietly write it into their experience. A resume that \
wins a screen and collapses in the interview is worse than no application.

## What you may do
- Rewrite a bullet to lead with the aspect this employer cares about.
- Substitute the posting's vocabulary for an equivalent term the candidate \
already used (for example "REST APIs" where the resume says "web services", \
only if they genuinely mean the same thing here).
- Reorder the content within a single bullet for emphasis.
- Replace the professional summary with one targeted at this role, using only \
facts already in the resume.

## The job
{_job_context(job)}

## The candidate's current resume, paragraph by paragraph
{numbered}

## How to reply
- `rewrites`: for each paragraph you want changed, give its **exact** original \
text in `original` (copied character for character, without the `[n]` prefix) \
and the new wording in `replacement`. Only include paragraphs you are actually \
changing. Leave contact details, dates, employer names and section headings alone.
- `summary`: the replacement professional summary, or empty to keep the current one.
- `keywords_targeted`: terms from the posting you aligned the wording to.
- `honest_gaps`: what this posting asks for that the resume genuinely cannot support.
- Keep every replacement a similar length to its original so the layout holds.
"""


def build_cover_letter_prompt(job: Job, paragraphs: list[str], profile: config.Profile) -> str:
    resume_text = textutil.truncate("\n".join(paragraphs), 5000)
    return f"""Write the body of a short cover letter for this application.

## Rules
- 150 to 220 words. Nobody reads more.
- Reference at least two *specific* things from this posting — a named \
technology, a stated problem, a product detail. Generic enthusiasm is worthless \
and obvious.
- Every claim about the candidate must be supported by their resume below. \
Invent nothing.
- Plain and direct. No "I am writing to express my interest", no "I believe I \
would be a great fit", no restating the job title back at them.
- Lead with the strongest genuine match, not with a self-introduction.
- No greeting line and no sign-off — body paragraphs only.

## The job
{_job_context(job)}

## The candidate's resume
{resume_text}

## Candidate details
Name: {profile.identity.full_name}
Based in: {profile.identity.location}
Headline: {profile.identity.headline}
"""


def build_answers_prompt(job: Job, paragraphs: list[str], profile: config.Profile) -> str:
    wa = profile.work_auth
    resume_text = textutil.truncate("\n".join(paragraphs), 4000)
    return f"""Draft the candidate's answers to the screening questions this \
application will almost certainly ask. These are drafts the candidate will edit, \
so be concrete and usable, not diplomatic filler.

## Rules
- Each answer 2 to 4 sentences, first person, plain language.
- Ground every claim in the resume below. Invent nothing.
- For salary and authorisation, use the candidate's stated facts verbatim in \
substance; do not soften or invent a number they did not give.

## The job
{_job_context(job)}

## The candidate
Name: {profile.identity.full_name}
Headline: {profile.identity.headline}
Based in: {profile.identity.location}
Authorised to work in: {', '.join(wa.authorized_countries) or 'not stated'}
Needs sponsorship: {'yes' if wa.needs_sponsorship else 'no'}
Notice period: {wa.notice_period or 'not stated'}
Salary expectation: {wa.salary_expectation or 'not stated'}

## Resume
{resume_text}
"""


# ─── driver ──────────────────────────────────────────────────────────────────


def tailor_for_job(
    job: Job,
    *,
    profile: config.Profile | None = None,
    include_resume: bool = True,
    progress=None,
) -> TailorOutput:
    """Generate every application artifact for one job."""
    say = progress or (lambda _m: None)
    profile = profile or config.load_profile()

    out_dir = paths.APPLICATIONS_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    output = TailorOutput(directory=out_dir)

    # A readable record of what was applied to, alongside the artifacts.
    (out_dir / "job.md").write_text(
        f"# {job.title}\n\n"
        f"**Company:** {job.company}\n\n"
        f"**Location:** {job.location_raw or 'not stated'}\n\n"
        f"**Source:** {job.source}\n\n"
        f"**Posting:** {job.url}\n\n"
        f"**Apply:** {job.apply_url or job.url}\n\n"
        f"---\n\n{job.description}\n",
        encoding="utf-8",
    )

    paragraphs: list[str] = []
    if include_resume:
        try:
            paragraphs = read_resume_paragraphs()
        except FileNotFoundError as exc:
            output.warnings.append(str(exc).splitlines()[0])
            include_resume = False

    if include_resume and paragraphs:
        say("  tailoring resume…")
        try:
            tailored = llm.generate_structured(
                build_resume_prompt(job, paragraphs, profile),
                TailoredResume,
                purpose="tailor_resume",
                tier="quality",
            )
        except llm.LLMError as exc:
            output.warnings.append(f"resume tailoring failed: {exc}")
        else:
            if tailored.summary.strip():
                # Treat the summary as a rewrite of the longest early paragraph,
                # which is where a professional summary conventionally sits.
                candidates = [p for p in paragraphs[:6] if len(p) > 120]
                if candidates:
                    tailored.rewrites.append(
                        Rewrite(
                            original=candidates[0],
                            replacement=tailored.summary,
                            reason="targeted professional summary",
                        )
                    )
            dest = out_dir / "resume.docx"
            applied, skipped, warnings = apply_rewrites(paths.RESUME_BASE, dest, tailored)
            output.resume_docx = dest
            output.applied_edits = applied
            output.skipped_edits = skipped
            output.warnings.extend(warnings)
            say(f"    {applied} edit(s) applied, {len(skipped)} unmatched")

            pdf = to_pdf(dest)
            if pdf:
                output.resume_pdf = pdf
            else:
                output.warnings.append("PDF conversion unavailable (install libreoffice)")

            if tailored.honest_gaps:
                (out_dir / "gaps.md").write_text(
                    "# Gaps this posting exposes\n\n"
                    "Worth preparing an answer for each of these before you apply.\n\n"
                    + "\n".join(f"- {g}" for g in tailored.honest_gaps)
                    + "\n\n## Keywords targeted in the rewrite\n\n"
                    + "\n".join(f"- {k}" for k in tailored.keywords_targeted)
                    + "\n",
                    encoding="utf-8",
                )

    say("  writing cover letter…")
    try:
        letter = llm.generate_structured(
            build_cover_letter_prompt(job, paragraphs, profile),
            CoverLetter,
            purpose="cover_letter",
            tier="quality",
        )
    except llm.LLMError as exc:
        output.warnings.append(f"cover letter failed: {exc}")
    else:
        path = out_dir / "cover_letter.md"
        cited = "\n".join(f"- {s}" for s in letter.specifics_cited)
        path.write_text(
            f"# Cover letter — {job.title} at {job.company}\n\n"
            f"{letter.body.strip()}\n\n"
            f"---\n\n_Specifics cited from the posting:_\n{cited}\n",
            encoding="utf-8",
        )
        output.cover_letter = path

    say("  drafting screener answers…")
    try:
        answers = llm.generate_structured(
            build_answers_prompt(job, paragraphs, profile),
            ScreenerAnswers,
            purpose="screener_answers",
            tier="quality",
        )
    except llm.LLMError as exc:
        output.warnings.append(f"screener answers failed: {exc}")
    else:
        path = out_dir / "answers.md"
        sections = [
            ("Why this company?", answers.why_this_company),
            ("Why you?", answers.why_you),
            ("Biggest relevant project", answers.biggest_relevant_project),
            ("Salary expectation", answers.salary_expectation),
            ("Work authorisation", answers.work_authorization),
            ("Notice period", answers.notice_period),
        ]
        body = "\n\n".join(
            f"## {title}\n\n{text.strip()}" for title, text in sections if text.strip()
        )
        path.write_text(
            f"# Screener answers — {job.title} at {job.company}\n\n"
            f"Drafts. Read them before pasting; they are grounded in your resume "
            f"but the phrasing should sound like you.\n\n{body}\n",
            encoding="utf-8",
        )
        output.answers = path

    return output
