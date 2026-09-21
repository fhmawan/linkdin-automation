"""Geo normalisation, including the ambiguity traps that caused real bugs."""

from __future__ import annotations

import pytest

from lgrow.jobs import geo


class TestAmbiguousAbbreviations:
    """These are regressions. Each one was a live false positive."""

    def test_us_state_ca_is_not_canada(self):
        # "San Francisco, CA" tagged as Canada, which would let US-only roles
        # look Canada-eligible.
        codes = geo.codes_from_text("San Francisco, CA")
        assert "CA" not in codes
        assert codes == {"US"} or codes == set()

    def test_california_spelled_out_is_not_canada(self):
        assert "CA" not in geo.codes_from_text("Los Angeles, California")

    def test_lowercase_us_pronoun_is_not_the_country(self):
        # "come work with us" must not geo-tag a job as United States.
        assert "US" not in geo.codes_from_text("Come and work with us, it's great")

    def test_uppercase_US_in_location_is_the_country(self):
        assert "US" in geo.codes_from_text("US")
        assert "US" in geo.codes_from_text("Remote (US only)")

    def test_canada_requires_the_actual_word(self):
        assert "CA" in geo.codes_from_text("Canada")
        assert "CA" in geo.codes_from_text("Toronto, Ontario")


class TestNormalisation:
    def test_prose_list_from_remotive(self):
        codes = geo.codes_from_text("LATAM, Europe, USA, Canada, APAC")
        assert codes == {"LATAM", "EU", "US", "CA", "APAC"}

    def test_worldwide_synonyms(self):
        for text in ("Worldwide", "Anywhere", "work from anywhere", "Global"):
            assert geo.WORLDWIDE in geo.codes_from_text(text), text

    def test_empty_becomes_unknown_not_worldwide(self):
        # Conflating "we couldn't tell" with "open to all" fills the queue with
        # jobs the user cannot take.
        assert geo.normalize(location_text="") == {geo.UNKNOWN}
        assert geo.normalize(location_text="Blorpville") == {geo.UNKNOWN}

    def test_list_field_from_himalayas(self):
        assert geo.codes_from_list(["United States", "Canada"]) == {"US", "CA"}
        assert geo.codes_from_list([]) == set()

    def test_timezone_offsets(self):
        assert "AMERICAS" in geo.regions_from_timezones([-8, -7, -6])
        assert "ASIA_PACIFIC" in geo.regions_from_timezones([5, 8])
        assert geo.regions_from_timezones(None) == set()
        assert geo.regions_from_timezones(["junk"]) == set()


class TestAllowGate:
    ALLOW = ["worldwide", "US", "CA", "AU", "GB", "IE", "NZ"]

    def test_worldwide_always_allowed(self):
        ok, _ = geo.is_allowed({geo.WORLDWIDE}, self.ALLOW)
        assert ok

    def test_unknown_passes_for_the_ai_to_judge(self):
        # Rejecting unknowns on string evidence alone would discard many
        # genuinely open roles; score.py reads the description instead.
        ok, reason = geo.is_allowed({geo.UNKNOWN}, self.ALLOW)
        assert ok
        assert "description" in reason

    def test_disallowed_region_rejected(self):
        ok, reason = geo.is_allowed({"LATAM"}, self.ALLOW)
        assert not ok
        assert "LATAM" in reason

    def test_namer_covers_us_and_ca(self):
        ok, _ = geo.is_allowed({"NAMER"}, ["US"])
        assert ok

    def test_partial_overlap_allowed(self):
        ok, reason = geo.is_allowed({"LATAM", "US"}, self.ALLOW)
        assert ok
        assert "US" in reason


class TestEnglishMarket:
    @pytest.mark.parametrize("codes", [{"US"}, {"GB"}, {"AU"}, {geo.WORLDWIDE}, {"NAMER"}])
    def test_included(self, codes):
        assert geo.english_market(codes)

    @pytest.mark.parametrize("codes", [{"LATAM"}, {"MENA"}, {"EU"}, {"APAC"}])
    def test_excluded(self, codes):
        assert not geo.english_market(codes)
