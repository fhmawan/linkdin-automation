"""The normalised job record every source adapter produces."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from . import textutil


class Job(BaseModel):
    """One posting, normalised across sources.

    `id` is derived from (source, source_id) so re-fetching is idempotent, while
    `dedupe_key` is derived from (company, title) so the same role arriving from
    four different aggregators collapses to one queue entry.
    """

    source: str
    source_id: str
    url: str
    apply_url: str | None = None
    company: str
    title: str
    location_raw: str = ""
    geo_tags: list[str] = Field(default_factory=list)
    employment_type: str | None = None
    salary_raw: str | None = None
    description: str = ""
    posted_at: datetime | None = None
    seniority_raw: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return textutil.stable_id(self.source, self.source_id)

    @property
    def dedupe_key(self) -> str:
        return textutil.dedupe_key(self.company, self.title)

    @property
    def description_hash(self) -> str:
        return textutil.content_hash(self.description)

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.posted_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (now - posted).total_seconds() / 86400.0

    def searchable(self) -> str:
        """Everything a keyword prefilter should look at."""
        return " ".join(
            [self.title, self.company, self.location_raw, " ".join(self.tags), self.description]
        ).lower()

    def summary_line(self) -> str:
        loc = self.location_raw or ",".join(self.geo_tags) or "?"
        return f"{self.title} @ {self.company} [{loc}] ({self.source})"


class ScoredJob(BaseModel):
    """A job plus Gemini's verdict on it."""

    job: Job
    score: int
    why_fit: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    geo_ok: bool = True
    geo_note: str = ""
    model: str = ""


def epoch_to_dt(value: object) -> datetime | None:
    """Epoch seconds (or milliseconds) to an aware UTC datetime."""
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    # Lever returns milliseconds; everything else seconds.
    if num > 1e11:
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso_to_dt(value: object) -> datetime | None:
    """Tolerant ISO8601 parse — feeds vary on timezone suffixes."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
