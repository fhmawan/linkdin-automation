"""Filesystem layout. Every path in the project resolves through here."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Root of the lgrow project.

    Override with LGROW_HOME when running from an installed package or when
    the working directory can't be relied on (cron, for instance).
    """
    env = os.environ.get("LGROW_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # src/lgrow/paths.py -> src/lgrow -> src -> <root>
    return Path(__file__).resolve().parents[2]


ROOT = project_root()

CONFIG_DIR = ROOT / "config"
PROFILE_YAML = CONFIG_DIR / "profile.yaml"
SOURCES_YAML = CONFIG_DIR / "sources.yaml"
VOICE_YAML = CONFIG_DIR / "voice.yaml"
AI_YAML = CONFIG_DIR / "ai.yaml"

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
JOURNAL_PATH = DATA_DIR / "journal.md"
TOKENS_PATH = DATA_DIR / ".tokens.json"
RESUME_BASE = DATA_DIR / "resume_base.docx"
APPLICATIONS_DIR = DATA_DIR / "applications"
LOGS_DIR = DATA_DIR / "logs"
LOCKS_DIR = DATA_DIR / "locks"

# Holds GEMINI_API_KEY and the LinkedIn app credentials.
ENV_PATH = ROOT / ".env"


def ensure_dirs() -> None:
    """Create the writable directories. Safe to call repeatedly."""
    for d in (DATA_DIR, APPLICATIONS_DIR, LOGS_DIR, LOCKS_DIR):
        d.mkdir(parents=True, exist_ok=True)
