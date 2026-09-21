"""HTML flattening and the dedupe keys."""

from __future__ import annotations

from lgrow.jobs import textutil


class TestHtmlToText:
    def test_strips_tags_and_keeps_structure(self):
        out = textutil.html_to_text("<p>One</p><p>Two</p>")
        assert "One" in out and "Two" in out
        assert "<" not in out

    def test_list_items_become_bullets(self):
        out = textutil.html_to_text("<ul><li>alpha</li><li>beta</li></ul>")
        assert "• alpha" in out
        assert "• beta" in out

    def test_double_encoded_input(self):
        # Arbeitnow and Greenhouse both return entity-encoded markup.
        raw = "&lt;div class=&quot;x&quot;&gt;&lt;p&gt;Hello&lt;/p&gt;&lt;/div&gt;"
        assert textutil.looks_double_encoded(raw)
        out = textutil.smart_html_to_text(raw)
        assert out.strip() == "Hello"
        assert "<" not in out and "&lt;" not in out

    def test_single_encoded_input_not_over_unescaped(self):
        out = textutil.smart_html_to_text("<p>5 &gt; 3 and cost &lt; budget</p>")
        assert "5 > 3" in out
        assert "cost < budget" in out

    def test_scripts_removed(self):
        out = textutil.html_to_text("<p>keep</p><script>alert('no')</script>")
        assert "keep" in out
        assert "alert" not in out

    def test_empty_and_none(self):
        assert textutil.html_to_text(None) == ""
        assert textutil.html_to_text("") == ""

    def test_collapses_excess_blank_lines(self):
        out = textutil.html_to_text("<p>a</p>" + "<br>" * 8 + "<p>b</p>")
        assert "\n\n\n" not in out


class TestNormalisationKeys:
    def test_company_legal_suffixes_collapse(self):
        keys = {
            textutil.normalize_company(n)
            for n in ("Acme, Inc.", "acme inc", "ACME Ltd", "Acme Technologies")
        }
        assert len(keys) == 1, keys

    def test_title_reference_numbers_stripped(self):
        # Himalayas prefixes some titles with "#969 - ".
        assert textutil.normalize_title("#969 - Backend Engineer") == \
               textutil.normalize_title("Backend Engineer")

    def test_title_noise_stripped(self):
        assert textutil.normalize_title("Senior Python Developer (Remote, Full-Time)") == \
               textutil.normalize_title("Senior Python Developer")

    def test_dedupe_key_matches_across_sources(self):
        a = textutil.dedupe_key("Stripe, Inc.", "Backend Engineer (Remote)")
        b = textutil.dedupe_key("Stripe", "#12 - Backend Engineer")
        assert a == b

    def test_different_roles_do_not_collide(self):
        a = textutil.dedupe_key("Stripe", "Backend Engineer")
        b = textutil.dedupe_key("Stripe", "Frontend Engineer")
        assert a != b


class TestIds:
    def test_stable_id_is_deterministic(self):
        assert textutil.stable_id("a", "b") == textutil.stable_id("a", "b")
        assert textutil.stable_id("a", "b") != textutil.stable_id("a", "c")

    def test_stable_id_handles_none(self):
        assert len(textutil.stable_id(None, "x")) == 16


class TestTruncate:
    def test_short_text_untouched(self):
        assert textutil.truncate("hello", 100) == "hello"

    def test_cuts_at_word_boundary(self):
        out = textutil.truncate("alpha beta gamma delta epsilon", 20)
        assert out.startswith("alpha")
        assert "truncated" in out
