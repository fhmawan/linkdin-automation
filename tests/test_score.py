"""The experience bounds that gate an 'N+ years required' posting.

Seniority words in a title are a poor proxy for the years line, so `Targets`
carries `years_experience` / `max_years_required` and the scoring prompt turns
them into an explicit instruction. These tests pin the two things that would
break silently: the ceiling reaching the prompt at all, and the cache key
changing when the bounds change (otherwise editing them would keep serving
verdicts scored under the old rule).
"""

from __future__ import annotations

from lgrow import config
from lgrow.jobs import score


def _profile(**targets) -> config.Profile:
    base = {"roles": ["Backend Engineer"], "skills_core": ["TypeScript"]}
    return config.Profile(targets=config.Targets(**{**base, **targets}))


def test_bounds_appear_in_the_prompt() -> None:
    brief = score._profile_brief(_profile(years_experience=2, max_years_required=3))
    assert "Professional experience: 2 years" in brief
    assert "they will apply to: 3 years" in brief


def test_whole_numbers_render_without_a_decimal_point() -> None:
    # "2.0 years" in a prompt reads as a formatting bug and invites the model
    # to be pedantic about it.
    assert score._years(2.0) == "2"
    assert score._years(2.5) == "2.5"


def test_rule_tells_the_model_not_to_penalise_within_the_ceiling() -> None:
    rule = score._experience_rule(_profile(years_experience=2, max_years_required=3))
    assert "3 years" in rule
    assert "red_flags" in rule
    assert "not" in rule.lower()


def test_rule_falls_back_when_bounds_are_unset() -> None:
    rule = score._experience_rule(_profile())
    assert "Seniority mismatch" in rule
    assert "red_flags" not in rule


def test_ceiling_is_part_of_the_cache_key() -> None:
    two = score.profile_version(_profile(years_experience=2, max_years_required=2))
    three = score.profile_version(_profile(years_experience=2, max_years_required=3))
    assert two != three


def test_unrelated_edits_do_not_invalidate_the_cache() -> None:
    a = _profile(years_experience=2, max_years_required=3)
    b = _profile(years_experience=2, max_years_required=3)
    b.identity.phone = "+92 0000000000"
    assert score.profile_version(a) == score.profile_version(b)
