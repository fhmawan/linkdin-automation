"""Commentary encoding and payload shape.

These are the highest-value tests in the project: a mistake here either gets the
post rejected or publishes mangled text under the user's own name, and neither
is visible until it's already public.
"""

from __future__ import annotations

import pytest

from lgrow.linkedin import client


class TestEscapeLittle:
    """LinkedIn requires ALL reserved characters escaped, used or not."""

    @pytest.mark.parametrize("ch", list("|{}@[]()<>#*_~"))
    def test_every_reserved_char_is_escaped(self, ch):
        assert client.escape_little(ch) == "\\" + ch

    def test_backslash_doubled(self):
        assert client.escape_little("\\") == "\\\\"

    def test_parentheses_the_common_case(self):
        # "I shipped it (finally)" is exactly the kind of ordinary sentence that
        # breaks without escaping.
        out = client.escape_little("I shipped it (finally)")
        assert out == "I shipped it \\(finally\\)"

    def test_ordinary_text_untouched(self):
        text = "Spent three hours on a caching bug today. Worth it."
        assert client.escape_little(text) == text

    def test_newlines_preserved(self):
        assert client.escape_little("a\n\nb") == "a\n\nb"

    def test_unicode_preserved(self):
        assert client.escape_little("café — 日本語") == "café — 日本語"


class TestRenderCommentary:
    def test_inline_hashtag_becomes_template(self):
        out = client.render_commentary("Learned a lot #python")
        assert "{hashtag|\\#|python}" in out
        # The literal '#' must not survive as an escaped plain character.
        assert "\\#python" not in out

    def test_multiple_hashtags(self):
        out = client.render_commentary("#python and #fastapi")
        assert "{hashtag|\\#|python}" in out
        assert "{hashtag|\\#|fastapi}" in out

    def test_hashtag_and_reserved_chars_together(self):
        out = client.render_commentary("Fixed it (finally) #python")
        assert "\\(finally\\)" in out
        assert "{hashtag|\\#|python}" in out

    def test_bare_hash_not_a_hashtag_is_escaped(self):
        # "issue #" with no word after it is literal text, so it must escape.
        out = client.render_commentary("bug # here")
        assert "\\#" in out
        assert "{hashtag" not in out

    def test_mid_word_hash_not_treated_as_hashtag(self):
        out = client.render_commentary("C#")
        assert "{hashtag" not in out
        assert "\\#" in out

    def test_empty_string(self):
        assert client.render_commentary("") == ""


class TestBuildPayload:
    URN = "urn:li:person:ABC123"

    def test_required_fields_present(self):
        p = client.build_payload("hello", person_urn=self.URN)
        assert p["author"] == self.URN
        assert p["lifecycleState"] == "PUBLISHED"
        assert p["visibility"] == "PUBLIC"
        assert p["distribution"] == {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        }
        assert p["isReshareDisabledByAuthor"] is False

    def test_commentary_is_encoded_not_raw(self):
        p = client.build_payload("ship it (v2)", person_urn=self.URN)
        assert p["commentary"] == "ship it \\(v2\\)"

    def test_connections_visibility_allowed(self):
        p = client.build_payload("x", person_urn=self.URN, visibility="CONNECTIONS")
        assert p["visibility"] == "CONNECTIONS"

    def test_invalid_visibility_rejected(self):
        with pytest.raises(ValueError, match="PUBLIC or CONNECTIONS"):
            client.build_payload("x", person_urn=self.URN, visibility="PRIVATE")

    def test_payload_is_json_serialisable_with_escapes_intact(self):
        import json

        p = client.build_payload("a (b) #tag", person_urn=self.URN)
        # Round-tripping must preserve the single backslashes the API expects.
        assert json.loads(json.dumps(p))["commentary"] == p["commentary"]
        assert "\\(" in p["commentary"]


class TestGuards:
    def test_empty_post_refused(self):
        with pytest.raises(ValueError, match="empty"):
            client.publish_text("   ")

    def test_overlong_post_refused(self):
        with pytest.raises(ValueError, match="limit"):
            client.publish_text("x" * (client.MAX_COMMENTARY + 1))


class TestPermalink:
    def test_shape(self):
        url = client.permalink("urn:li:share:123")
        assert url == "https://www.linkedin.com/feed/update/urn:li:share:123/"
