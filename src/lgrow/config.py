"""Typed configuration loaded from config/*.yaml.

Values the user must supply are marked FILL_ME in the YAML rather than guessed.
`unfilled()` walks a loaded config and reports what's still a placeholder, which
is what `lgrow doctor` uses to tell the user exactly what to go and edit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from . import paths

PLACEHOLDER = "FILL_ME"


def load_env() -> None:
    """Pull `.env` into the process environment. Safe to call repeatedly.

    Both secrets the project needs live there — the LinkedIn app credentials and
    `GEMINI_API_KEY`. Reading it here rather than relying on the shell matters
    for scheduled runs: cron does not source a login profile, so a key exported
    from `.bashrc` works interactively and then fails on every crawl at 07:07.

    `override=False` keeps a value already exported in the environment winning
    over the file, which is what makes one-off overrides on the command line work.
    """
    if paths.ENV_PATH.exists():
        load_dotenv(paths.ENV_PATH, override=False)


# ─── profile.yaml ────────────────────────────────────────────────────────────


class Identity(BaseModel):
    full_name: str = PLACEHOLDER
    headline: str = PLACEHOLDER
    location: str = PLACEHOLDER
    email: str = PLACEHOLDER
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    # Optional, like the other contact fields: nothing in the codebase reads it,
    # so a missing GitHub account should not block `lgrow doctor`.
    github_username: str = ""


class GitConfig(BaseModel):
    """Commit identities used to harvest post material.

    Deliberately never inferred from the environment — the shell's git config or
    a session email is not necessarily who the user commits as.
    """

    authors: list[str] = Field(default_factory=list)
    repo_paths: list[str] = Field(default_factory=list)
    lookback_days: int = 7


class Targets(BaseModel):
    roles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=lambda: ["mid", "senior"])
    skills_core: list[str] = Field(default_factory=list)
    skills_secondary: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    salary_floor_usd: int | None = None
    # Seniority words in a title are a poor proxy for the "N+ years required"
    # line that actually gates an application. These two make the intent
    # explicit to the scorer: what you have, and how far above it you'll apply.
    years_experience: float | None = None
    max_years_required: float | None = None


class Geo(BaseModel):
    allow: list[str] = Field(
        default_factory=lambda: ["worldwide", "US", "CA", "AU", "GB", "IE", "NZ"]
    )
    my_timezone: str = PLACEHOLDER


class WorkAuth(BaseModel):
    authorized_countries: list[str] = Field(default_factory=list)
    needs_sponsorship: bool = True
    notice_period: str = ""
    salary_expectation: str = ""


class Scoring(BaseModel):
    min_score: int = 65
    daily_queue_size: int = 10
    tailor_threshold: int = 75
    # Prefilter cutoff, before any AI spend. A title sharing one word with a
    # target role ("COSTING ENGINEER" vs "Full Stack Engineer") scores ~25 on
    # its own, so a cutoff below that shortlists the whole *Engineer long tail.
    min_heuristic: int = 20


class Profile(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    git: GitConfig = Field(default_factory=GitConfig)
    targets: Targets = Field(default_factory=Targets)
    geo: Geo = Field(default_factory=Geo)
    work_auth: WorkAuth = Field(default_factory=WorkAuth)
    scoring: Scoring = Field(default_factory=Scoring)


# ─── sources.yaml ────────────────────────────────────────────────────────────


class HttpConfig(BaseModel):
    user_agent: str = "lgrow/0.1 (personal job search)"
    timeout_seconds: float = 25.0
    max_retries: int = 3
    delay_between_requests: float = 1.0


class SourceToggle(BaseModel):
    """Per-source switch. Extra keys (limit, pages, count) are kept as-is."""

    model_config = {"extra": "allow"}
    enabled: bool = True


class AtsConfig(BaseModel):
    enabled: bool = True
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)

    def total_companies(self) -> int:
        return len(self.greenhouse) + len(self.lever) + len(self.ashby)


class Sources(BaseModel):
    http: HttpConfig = Field(default_factory=HttpConfig)
    aggregators: dict[str, SourceToggle] = Field(default_factory=dict)
    queries: list[str] = Field(default_factory=list)
    ats: AtsConfig = Field(default_factory=AtsConfig)
    max_age_days: int = 21

    def enabled_aggregators(self) -> list[str]:
        return [name for name, cfg in self.aggregators.items() if cfg.enabled]


# ─── ai.yaml ─────────────────────────────────────────────────────────────────


class Models(BaseModel):
    """Which model answers each tier, and what to try when it will not.

    An entry is `provider:model`, split on the first colon only — OpenRouter IDs
    contain their own colon (`openrouter:z-ai/glm-5.2:free`). A bare name means
    Gemini, so `gemini-3.5-flash` and `gemini:gemini-3.5-flash` are the same
    thing.

    Fallbacks are per-tier rather than shared, because the tiers now differ in
    more than cost: `cheap` may route to a third party that `quality` must not.
    """

    quality: str = "gemini-3.5-flash"
    cheap: str = "gemini-3.5-flash-lite"
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)

    def chain(self, tier: str) -> list[str]:
        """Preferred model for a tier, then its fallbacks, de-duplicated."""
        primary = self.cheap if tier == "cheap" else self.quality
        out: list[str] = [primary] if primary else []
        for entry in self.fallbacks.get(tier, []):
            if entry and entry not in out:
                out.append(entry)
        return out


class Budget(BaseModel):
    max_calls_per_day: int = 300
    reserve_calls: int = 20


class Runtime(BaseModel):
    timeout_seconds: float = 300.0
    max_retries: int = 3
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0


class SchemaRetry(BaseModel):
    max_attempts: int = 3


class Ai(BaseModel):
    models: Models = Field(default_factory=Models)
    budget: Budget = Field(default_factory=Budget)
    runtime: Runtime = Field(default_factory=Runtime)
    schema_retry: SchemaRetry = Field(default_factory=SchemaRetry)


# ─── voice.yaml ──────────────────────────────────────────────────────────────


class Cadence(BaseModel):
    posts_per_week: int = 3
    days: list[str] = Field(default_factory=lambda: ["tuesday", "wednesday", "thursday"])
    slots: list[str] = Field(default_factory=lambda: ["08:15", "12:30"])
    jitter_minutes: int = 20
    max_per_day: int = 1


class Pillar(BaseModel):
    name: str
    weight: int = 25
    brief: str = ""


class Style(BaseModel):
    hook_max_chars: int = 140
    min_chars: int = 700
    max_chars: int = 1300
    max_hashtags: int = 3
    line_break_every_sentences: int = 2
    require_closing_question: bool = True
    require_concrete_detail: bool = True
    first_person: bool = True


class Voice(BaseModel):
    cadence: Cadence = Field(default_factory=Cadence)
    pillars: list[Pillar] = Field(default_factory=list)
    style: Style = Field(default_factory=Style)
    tone: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    banned_chars: list[str] = Field(default_factory=list)
    max_similarity_to_recent: float = 0.55


# ─── loading ─────────────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file: {path}\n"
            "Expected the config/ directory from the repo — was it deleted?"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
    return data


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    return Profile.model_validate(_read_yaml(paths.PROFILE_YAML))


@lru_cache(maxsize=1)
def load_sources() -> Sources:
    return Sources.model_validate(_read_yaml(paths.SOURCES_YAML))


@lru_cache(maxsize=1)
def load_voice() -> Voice:
    return Voice.model_validate(_read_yaml(paths.VOICE_YAML))


@lru_cache(maxsize=1)
def load_ai() -> Ai:
    return Ai.model_validate(_read_yaml(paths.AI_YAML))


def reset_cache() -> None:
    """Drop cached configs — used by tests and by the long-lived dashboard."""
    load_profile.cache_clear()
    load_sources.cache_clear()
    load_voice.cache_clear()
    load_ai.cache_clear()


# ─── placeholder detection ───────────────────────────────────────────────────


def unfilled(model: BaseModel, prefix: str = "") -> list[str]:
    """Dotted paths of every field still holding the FILL_ME placeholder.

    Recurses into nested models and into lists of strings, so
    `targets.roles: [FILL_ME]` is reported as `targets.roles[0]`.
    """
    found: list[str] = []
    for name, value in model.__dict__.items():
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            found.extend(unfilled(value, prefix=f"{path}."))
        elif isinstance(value, str):
            if value.strip() == PLACEHOLDER:
                found.append(path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, str) and item.strip() == PLACEHOLDER:
                    found.append(f"{path}[{i}]")
                elif isinstance(item, BaseModel):
                    found.extend(unfilled(item, prefix=f"{path}[{i}]."))
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and item.strip() == PLACEHOLDER:
                    found.append(f"{path}.{key}")
                elif isinstance(item, BaseModel):
                    found.extend(unfilled(item, prefix=f"{path}.{key}."))
    return found


def contains_placeholder(text: str) -> bool:
    return PLACEHOLDER in text
