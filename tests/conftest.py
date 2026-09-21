"""Test isolation.

`lgrow.paths` resolves its constants at import time, so setting `LGROW_HOME`
inside a test is too late — the module already computed `DATA_DIR`, `DB_PATH` and
friends. Without the fixture below, anything that opens a database session
(which includes every Gemini call, via the usage ledger) writes into the user's
real `data/app.db`.

That is exactly what happened during development: 14 fake `api_usage` rows
landed in the live database. This fixture redirects every writable path to a
per-test temp directory.

`CONFIG_DIR` is deliberately left pointing at the real `config/`, so tests that
load the shipped YAML still exercise the real files.
"""

from __future__ import annotations

import pytest

from lgrow import config, paths

# Every path constant that gets written to.
_WRITABLE = (
    "DATA_DIR",
    "DB_PATH",
    "JOURNAL_PATH",
    "TOKENS_PATH",
    "RESUME_BASE",
    "APPLICATIONS_DIR",
    "LOGS_DIR",
    "LOCKS_DIR",
    "ENV_PATH",
)


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Point all writable paths at a temp directory for the duration of a test."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)

    replacements = {
        "DATA_DIR": data,
        "DB_PATH": data / "app.db",
        "JOURNAL_PATH": data / "journal.md",
        "TOKENS_PATH": data / ".tokens.json",
        "RESUME_BASE": data / "resume_base.docx",
        "APPLICATIONS_DIR": data / "applications",
        "LOGS_DIR": data / "logs",
        "LOCKS_DIR": data / "locks",
        "ENV_PATH": tmp_path / ".env",
    }
    for name in _WRITABLE:
        monkeypatch.setattr(paths, name, replacements[name], raising=True)

    monkeypatch.setenv("LGROW_HOME", str(tmp_path))
    # Cached config objects must not leak between tests.
    config.reset_cache()
    yield tmp_path
    config.reset_cache()


@pytest.fixture(autouse=True)
def no_real_linkedin_credentials(monkeypatch):
    """Ensure no test can accidentally authenticate against LinkedIn."""
    for var in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
