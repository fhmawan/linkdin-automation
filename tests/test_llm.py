"""The model transport.

Everything else in the project depends on this module. Its riskiest behaviours
are the ones that are invisible when things go well: schema translation for
nested models, reasoning parts that must not be concatenated into the answer,
and error classification deciding whether a failure is worth a retry. On a free
tier capped at 20 requests a day, a wasted retry is expensive, so all three are
pinned here against a faked transport.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from lgrow import llm


class Answer(BaseModel):
    name: str
    score: int


class Inner(BaseModel):
    label: str
    weight: int = 0


class Outer(BaseModel):
    """Nested, so Pydantic emits a definition plus a reference to it."""

    title: str
    items: list[Inner] = Field(default_factory=list, max_length=3)
    note: str | None = None


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Backoff is 5s, 10s, 20s. Tests must not actually wait."""
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


def reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "finishReason": "STOP"}]}


def fake_post(*responses):
    """Stand-in for httpx.post yielding canned (status, payload) in order."""
    calls = []
    queue = list(responses)

    def _post(url, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json"),
                      "headers": kwargs.get("headers")})
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=payload)

    _post.calls = calls
    return _post


# ─── schema translation ──────────────────────────────────────────────────────


class TestResponseSchema:
    def test_nested_model_is_inlined_not_referenced(self):
        schema = llm.to_response_schema(Outer)

        # Structural, not a substring scan: a model's docstring becomes the
        # schema `description`, so prose mentioning a reference would trip that.
        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        assert not {"$ref", "$defs"} & set(keys(schema))
        # The nested model's own fields survive the inlining.
        assert schema["properties"]["items"]["items"]["properties"]["label"] == {
            "type": "string"
        }

    def test_unsupported_keys_are_dropped(self):
        # `title` and `default` are emitted by Pydantic on nearly every field
        # and are a 400 from the API, not a warning.
        blob = json.dumps(llm.to_response_schema(Outer))
        assert "title" not in json.loads(blob).get("properties", {}).get("title", {})
        assert "default" not in blob
        assert "$schema" not in blob

    def test_optional_becomes_nullable_not_anyof(self):
        note = llm.to_response_schema(Outer)["properties"]["note"]
        assert note["type"] == "string"
        assert note["nullable"] is True
        assert "anyOf" not in note

    def test_required_and_caps_are_preserved(self):
        schema = llm.to_response_schema(Outer)
        assert schema["required"] == ["title"]
        assert schema["properties"]["items"]["maxItems"] == 3

    def test_dangling_reference_is_an_error_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(
            Outer, "model_json_schema",
            classmethod(lambda cls, **_k: {"$ref": "#/$defs/Missing"}),
        )
        with pytest.raises(llm.LLMSchemaError, match="unresolvable"):
            llm.to_response_schema(Outer)


# ─── response parsing ────────────────────────────────────────────────────────


class TestExtractText:
    def test_reasoning_parts_are_excluded(self):
        """3.x models interleave `thought` parts. Concatenating them corrupts
        otherwise valid JSON, which is the worst possible failure: it looks like
        the model cannot follow a schema."""
        payload = {"candidates": [{"content": {"parts": [
            {"text": "Let me think about this...", "thought": True},
            {"text": '{"a": 1}'},
        ]}}]}
        assert llm._extract_text(payload) == '{"a": 1}'

    def test_multiple_answer_parts_are_joined(self):
        payload = {"candidates": [{"content": {"parts": [
            {"text": '{"a":'}, {"text": ' 1}'},
        ]}}]}
        assert llm._extract_text(payload) == '{"a": 1}'

    def test_blocked_prompt_says_so(self):
        with pytest.raises(llm.LLMError, match="blocked: SAFETY"):
            llm._extract_text({"promptFeedback": {"blockReason": "SAFETY"}})

    def test_no_candidates_is_an_error(self):
        with pytest.raises(llm.LLMError, match="no candidates"):
            llm._extract_text({"candidates": []})

    def test_truncation_is_reported_as_truncation(self):
        payload = {"candidates": [{"content": {"parts": [{"text": '{"a": 1'}]},
                                   "finishReason": "MAX_TOKENS"}]}
        with pytest.raises(llm.LLMError, match="MAX_TOKENS"):
            llm._extract_text(payload)

    def test_empty_text_reports_the_finish_reason(self):
        payload = {"candidates": [{"content": {"parts": []},
                                   "finishReason": "RECITATION"}]}
        with pytest.raises(llm.LLMError, match="RECITATION"):
            llm._extract_text(payload)


# ─── error classification ────────────────────────────────────────────────────


class TestClassification:
    def test_bad_key_is_auth_never_retried(self):
        assert llm._classify_status(
            400, "API key not valid. Please pass a valid API key."
        ) is llm.LLMAuthError
        assert llm._classify_status(403, "forbidden") is llm.LLMAuthError

    def test_rate_limit_is_quota(self):
        assert llm._classify_status(
            429, "Quota exceeded for metric ... limit: 20"
        ) is llm.LLMQuotaError

    def test_server_errors_and_overload_are_retryable(self):
        assert llm._classify_status(503, "unavailable") is llm.LLMQuotaError
        assert llm._classify_status(
            200, "This model is currently experiencing high demand."
        ) is llm.LLMQuotaError

    def test_unknown_is_generic(self):
        assert llm._classify_status(418, "teapot") is llm.LLMError


class TestCallErrors:
    def test_missing_key_explains_where_to_put_it(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(llm.LLMAuthError, match="aistudio.google.com"):
            llm.api_key()

    def test_key_travels_in_the_header_not_the_url(self, monkeypatch):
        post = fake_post((200, reply("OK")))
        monkeypatch.setattr(llm.httpx, "post", post)
        llm._call("hi", model="m", timeout=5)
        assert post.calls[0]["headers"]["x-goog-api-key"] == "test-key"
        assert "test-key" not in post.calls[0]["url"]

    def test_quota_error_surfaces_the_limit(self, monkeypatch):
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (429, {"error": {"message": "Quota exceeded ... limit: 20",
                             "status": "RESOURCE_EXHAUSTED"}})))
        with pytest.raises(llm.LLMQuotaError, match="limit: 20"):
            llm._call("hi", model="m", timeout=5)

    def test_timeout_is_retryable(self, monkeypatch):
        def boom(*_a, **_k):
            raise httpx.ReadTimeout("too slow")

        monkeypatch.setattr(llm.httpx, "post", boom)
        with pytest.raises(llm.LLMQuotaError, match="timed out"):
            llm._call("hi", model="m", timeout=5)


# ─── retry and fallback ──────────────────────────────────────────────────────


class TestGenerate:
    def test_schema_is_only_sent_when_requested(self, monkeypatch):
        post = fake_post((200, reply("hello")))
        monkeypatch.setattr(llm.httpx, "post", post)
        llm._call("hi", model="m", timeout=5)
        assert "generationConfig" not in post.calls[0]["json"]

    def test_schema_sets_json_mime_type(self, monkeypatch):
        post = fake_post((200, reply("{}")))
        monkeypatch.setattr(llm.httpx, "post", post)
        llm._call("hi", model="m", timeout=5, response_schema={"type": "object"})
        cfg = post.calls[0]["json"]["generationConfig"]
        assert cfg["responseMimeType"] == "application/json"
        assert cfg["responseSchema"] == {"type": "object"}

    def test_auth_failure_is_not_retried(self, monkeypatch):
        post = fake_post((403, {"error": {"message": "API_KEY_INVALID"}}))
        monkeypatch.setattr(llm.httpx, "post", post)
        with pytest.raises(llm.LLMAuthError):
            llm.generate("hi", purpose="t", tier="cheap")
        assert len(post.calls) == 1

    def test_quota_exhaustion_falls_through_to_another_model(self, monkeypatch):
        """The whole point of a fallback chain: the free tier's 20/day is
        counted per model family, so a spent bucket must not end the run."""
        monkeypatch.setattr(
            llm, "_models_to_try",
            lambda _tier: ["gemini-3.5-flash", "gemini-3.5-flash-lite"],
        )
        post = fake_post(
            (429, {"error": {"message": "Quota exceeded, limit: 20"}}),
            (429, {"error": {"message": "Quota exceeded, limit: 20"}}),
            (429, {"error": {"message": "Quota exceeded, limit: 20"}}),
            (200, reply("recovered")),
        )
        monkeypatch.setattr(llm.httpx, "post", post)
        result = llm.generate("hi", purpose="t", tier="cheap")
        assert result.text == "recovered"
        assert result.model == "gemini-3.5-flash-lite"
        assert "gemini-3.5-flash:" in post.calls[0]["url"]

    def test_budget_ceiling_blocks_before_any_request(self, monkeypatch):
        post = fake_post((200, reply("OK")))
        monkeypatch.setattr(llm.httpx, "post", post)
        monkeypatch.setattr(llm, "remaining_budget", lambda: 0)
        with pytest.raises(llm.LLMBudgetError, match="budget"):
            llm.generate("hi", purpose="t")
        assert post.calls == []


# ─── structured output ───────────────────────────────────────────────────────


class TestGenerateStructured:
    def test_valid_first_try(self, monkeypatch):
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, reply('{"name": "x", "score": 3}'))))
        answer = llm.generate_structured("p", Answer, purpose="t")
        assert answer.name == "x" and answer.score == 3

    def test_fenced_json_still_parses(self, monkeypatch):
        # responseSchema should prevent this, but a wasted call costs 1/20th of
        # the day's allowance, so the lenient parse stays.
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, reply('```json\n{"name": "y", "score": 1}\n```'))))
        assert llm.generate_structured("p", Answer, purpose="t").name == "y"

    def test_retries_with_the_validation_error(self, monkeypatch):
        post = fake_post(
            (200, reply('{"name": "x"}')),            # missing `score`
            (200, reply('{"name": "x", "score": 9}')),
        )
        monkeypatch.setattr(llm.httpx, "post", post)
        assert llm.generate_structured("p", Answer, purpose="t").score == 9
        assert "rejected" in post.calls[1]["json"]["contents"][0]["parts"][0]["text"]

    def test_gives_up_with_the_reasons(self, monkeypatch):
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, reply('{"nope": true}'))))
        with pytest.raises(llm.LLMSchemaError, match="no schema-valid JSON"):
            llm.generate_structured("p", Answer, purpose="t")


# ─── JSON extraction (lenient fallback) ──────────────────────────────────────


class TestExtractJson:
    def test_bare_object(self):
        assert llm.extract_json('{"a": 1}') == {"a": 1}

    def test_bare_array(self):
        assert llm.extract_json("[1, 2]") == [1, 2]

    def test_fenced_block(self):
        assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert llm.extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_surrounded_by_prose(self):
        assert llm.extract_json('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        assert llm.extract_json('{"note": "a } not the end", "a": 1}')["a"] == 1

    def test_escaped_quote_inside_string(self):
        assert llm.extract_json('{"note": "he said \\"hi\\" }", "a": 2}')["a"] == 2

    def test_nested_structures(self):
        raw = 'text {"outer": {"inner": [1, {"deep": true}]}} more'
        assert llm.extract_json(raw)["outer"]["inner"][1]["deep"] is True

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="no JSON value"):
            llm.extract_json("there is nothing structured here")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            llm.extract_json("   ")


# ─── provider routing ────────────────────────────────────────────────────────


class TestSplitSpec:
    """OpenRouter model IDs contain a colon of their own, so the provider prefix
    can only be split off the first one."""

    def test_bare_name_defaults_to_gemini(self):
        assert llm.split_spec("gemini-3.5-flash") == ("gemini", "gemini-3.5-flash")

    def test_explicit_gemini_prefix(self):
        assert llm.split_spec("gemini:gemini-3.5-flash") == (
            "gemini", "gemini-3.5-flash")

    def test_openrouter_model_keeps_its_own_colon(self):
        assert llm.split_spec("openrouter:z-ai/glm-5.2:free") == (
            "openrouter", "z-ai/glm-5.2:free")

    def test_unknown_prefix_is_treated_as_a_gemini_model_name(self):
        # Not an error: a colon in an unrecognised position is far more likely
        # to be part of a model name than a provider we do not support.
        assert llm.split_spec("weird:name") == ("gemini", "weird:name")


class TestOpenRouterTransport:
    def _key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    def test_uses_bearer_auth_and_the_chat_endpoint(self, monkeypatch):
        self._key(monkeypatch)
        post = fake_post((200, {"choices": [
            {"message": {"content": "hi"}, "finish_reason": "stop"}]}))
        monkeypatch.setattr(llm.httpx, "post", post)
        text, _ = llm._call("p", model="openrouter:z-ai/glm-5.2:free", timeout=5)
        assert text == "hi"
        call = post.calls[0]
        assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer or-key"
        # The provider prefix must be stripped before it reaches the wire.
        assert call["json"]["model"] == "z-ai/glm-5.2:free"

    def test_schema_uses_openai_response_format(self, monkeypatch):
        self._key(monkeypatch)
        post = fake_post((200, {"choices": [{"message": {"content": "{}"}}]}))
        monkeypatch.setattr(llm.httpx, "post", post)
        llm._call("p", model="openrouter:m", timeout=5,
                  response_schema={"type": "object"})
        fmt = post.calls[0]["json"]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["schema"] == {"type": "object"}
        # `strict` demands every property be required, which Pydantic output
        # is not. Asking for it would reject valid schemas.
        assert "strict" not in fmt["json_schema"]

    def test_error_inside_a_200_is_still_an_error(self, monkeypatch):
        """An upstream provider being down looks like HTTP success here."""
        self._key(monkeypatch)
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, {"error": {"code": 429, "message": "rate limited upstream"}})))
        with pytest.raises(llm.LLMQuotaError, match="rate limited"):
            llm._call("p", model="openrouter:m", timeout=5)

    def test_truncation_is_named(self, monkeypatch):
        self._key(monkeypatch)
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, {"choices": [{"message": {"content": "{"},
                                "finish_reason": "length"}]})))
        with pytest.raises(llm.LLMError, match="truncated"):
            llm._call("p", model="openrouter:m", timeout=5)

    def test_missing_key_names_the_right_variable(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(llm.LLMAuthError, match="OPENROUTER_API_KEY"):
            llm.api_key("openrouter")


class TestCrossProviderFallback:
    def test_a_spent_provider_hands_over_to_the_other(self, monkeypatch):
        """The reason for mixing providers in one chain: each counts its own
        free allowance, so one being exhausted must not end the run."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        monkeypatch.setattr(
            llm, "_models_to_try",
            lambda _tier: ["openrouter:z-ai/glm-5.2:free", "gemini-3.5-flash-lite"],
        )
        post = fake_post(
            (429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}}),
            (429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}}),
            (429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}}),
            (200, reply("from gemini")),
        )
        monkeypatch.setattr(llm.httpx, "post", post)
        result = llm.generate("hi", purpose="t", tier="cheap")
        assert result.text == "from gemini"
        assert result.model == "gemini-3.5-flash-lite"
        assert "openrouter.ai" in post.calls[0]["url"]
        assert "generativelanguage" in post.calls[-1]["url"]

    def test_ledger_records_the_qualified_spec(self, monkeypatch):
        """`lgrow usage` has to show which provider actually answered."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        monkeypatch.setattr(llm, "_models_to_try",
                            lambda _tier: ["openrouter:z-ai/glm-5.2:free"])
        monkeypatch.setattr(llm.httpx, "post", fake_post(
            (200, {"choices": [{"message": {"content": "ok"}}]})))
        assert llm.generate("hi", purpose="t").model == "openrouter:z-ai/glm-5.2:free"


class TestChainConfig:
    def test_per_tier_fallbacks(self):
        from lgrow.config import Models

        m = Models(quality="a", cheap="b",
                   fallbacks={"quality": ["a2"], "cheap": ["b2", "b3"]})
        assert m.chain("quality") == ["a", "a2"]
        assert m.chain("cheap") == ["b", "b2", "b3"]

    def test_duplicates_are_collapsed(self):
        from lgrow.config import Models

        m = Models(quality="a", fallbacks={"quality": ["a", "b"]})
        assert m.chain("quality") == ["a", "b"]

    def test_missing_tier_yields_just_the_primary(self):
        from lgrow.config import Models

        assert Models(cheap="only").chain("cheap") == ["only"]
