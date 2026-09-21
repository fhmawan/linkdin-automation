"""Guard the guard: prove the test suite cannot touch real user data.

Without this, a regression in conftest.py would silently start writing to the
user's live database again, and nothing would fail.
"""

from __future__ import annotations

from lgrow import db, paths


def test_paths_are_redirected_to_tmp(isolate_paths):
    assert str(paths.DB_PATH).startswith(str(isolate_paths))
    assert str(paths.DATA_DIR).startswith(str(isolate_paths))
    assert "Linkdin" not in str(paths.DB_PATH)


def test_database_writes_land_in_tmp(isolate_paths):
    with db.session() as conn:
        db.record_usage(
            conn, model="fake", purpose="isolation-check", ok=True,
            duration_ms=1, prompt_chars=1, response_chars=1,
        )
        assert db.calls_today(conn) == 1
    assert paths.DB_PATH.exists()
    assert str(paths.DB_PATH.parent).startswith(str(isolate_paths))


def test_each_test_gets_a_fresh_database():
    """No state carries over from the previous test's writes."""
    with db.session() as conn:
        assert db.calls_today(conn) == 0


def test_config_dir_still_points_at_the_real_yaml():
    # Tests should exercise the shipped config files, not fixtures of them.
    assert (paths.CONFIG_DIR / "voice.yaml").exists()
