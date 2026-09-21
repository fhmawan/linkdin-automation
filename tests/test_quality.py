"""Deterministic post checks.

These exist because prompt instructions are not guarantees — a model told "no
em-dashes" still produces them. Python has to be the backstop.
"""

from __future__ import annotations

from lgrow import config
from lgrow.posts import quality


def make_voice(**overrides) -> config.Voice:
    data = {
        "style": {
            "hook_max_chars": 140,
            "min_chars": 100,
            "max_chars": 1300,
            "max_hashtags": 3,
            "require_closing_question": True,
            "require_concrete_detail": True,
            "first_person": True,
        },
        "banned_phrases": ["I'm thrilled to announce", "game-changer"],
        "banned_chars": ["—"],
        "max_similarity_to_recent": 0.55,
        "pillars": [{"name": "build_in_public", "weight": 100}],
    }
    data.update(overrides)
    return config.Voice.model_validate(data)


GOOD = (
    "Spent 3 hours chasing a cache bug that only showed up under load.\n\n"
    "Turned out our redis client was reusing a connection across threads.\n"
    "One line fixed it: a connection pool per worker instead of a shared client.\n\n"
    "The lesson I keep relearning is that intermittent bugs are almost always "
    "about shared state, not about the library being broken.\n\n"
    "What is the most annoying concurrency bug you have shipped?\n\n"
    "#python #redis"
)


class TestAcceptance:
    def test_a_good_post_passes(self):
        report = quality.check(GOOD, voice=make_voice())
        assert report.ok, report.problems

    def test_hook_is_the_first_line(self):
        report = quality.check(GOOD, voice=make_voice())
        assert report.hook.startswith("Spent 3 hours")
        assert "\n" not in report.hook


class TestHardFailures:
    def test_banned_phrase_caught(self):
        text = GOOD.replace("Spent 3 hours", "I'm thrilled to announce that I spent 3 hours")
        report = quality.check(text, voice=make_voice())
        assert not report.ok
        assert any("banned phrase" in p for p in report.problems)

    def test_em_dash_caught(self):
        # The single clearest AI tell in a short post.
        text = GOOD.replace("under load.", "under load — every time.")
        report = quality.check(text, voice=make_voice())
        assert any("banned character" in p for p in report.problems)

    def test_too_long_hook_caught(self):
        text = ("x" * 200) + "\n\n" + GOOD
        report = quality.check(text, voice=make_voice())
        assert any("first line" in p for p in report.problems)

    def test_too_many_hashtags(self):
        text = GOOD + " #a #b #c #d"
        report = quality.check(text, voice=make_voice())
        assert any("hashtags" in p for p in report.problems)

    def test_missing_closing_question(self):
        text = GOOD.replace("What is the most annoying concurrency bug you have shipped?", "Anyway.")
        report = quality.check(text, voice=make_voice())
        assert any("question" in p for p in report.problems)

    def test_too_short(self):
        report = quality.check("Short. Why?", voice=make_voice())
        assert any("too short" in p for p in report.problems)

    def test_empty(self):
        report = quality.check("   ", voice=make_voice())
        assert not report.ok
        assert "empty post" in report.problems


class TestSimilarity:
    def test_near_duplicate_rejected(self):
        report = quality.check(GOOD, voice=make_voice(), recent=[GOOD])
        assert report.similarity > 0.9
        assert any("similar" in p for p in report.problems)

    def test_different_post_accepted(self):
        other = (
            "Rewrote our deploy script in bash today after the python one kept "
            "breaking on missing env vars.\n\nIt is 40 lines shorter.\n\n"
            "Do you still reach for bash, or has that ship sailed?"
        )
        report = quality.check(GOOD, voice=make_voice(), recent=[other])
        assert report.similarity < 0.55
        assert not any("similar" in p for p in report.problems)

    def test_no_history_is_zero(self):
        assert quality.similarity("anything", []) == 0.0


class TestWarnings:
    def test_no_concrete_detail_warns_not_fails(self):
        vague = (
            "I have been learning a lot about software engineering lately and it "
            "has been a really valuable experience for my personal growth as an "
            "engineer working on interesting problems every single day here.\n\n"
            "I think consistency is what matters most in the long run for anyone.\n\n"
            "What has your experience been like so far?"
        )
        report = quality.check(vague, voice=make_voice())
        # A vague post is a warning, so the human can still choose to post it.
        assert any("concrete" in w for w in report.warnings)

    def test_fix_instructions_only_when_problems(self):
        ok_report = quality.check(GOOD, voice=make_voice())
        assert quality.fix_instructions(ok_report) == ""
        bad = quality.check("nope", voice=make_voice())
        assert "Fix all of the following" in quality.fix_instructions(bad)
