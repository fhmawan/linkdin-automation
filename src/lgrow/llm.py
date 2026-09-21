"""The only place in this project that talks to an AI model.

Calls the Gemini and OpenRouter REST APIs over HTTPS. This replaced shelling out
to the `gemini` CLI, which was the original design for a reason that no longer
holds: the CLI's "Login with Google" used to spend a Google AI Pro subscription,
and Google has since retired the free "Gemini Code Assist for individuals" tier
for that client. Personal login now writes valid-looking credentials and then
fails every call with IneligibleTierError, so an API key is the only route left
— and once you are paying the API's terms anyway, the CLI is pure overhead.

Which provider answers is set per tier in config/ai.yaml, and the split is about
privacy as much as cost: job scoring carries no personal detail and goes to
OpenRouter's free models, while anything holding the resume, name or contact
details stays on Gemini.

Four things it was costing us, all measured against this project:

1. **It is an agent, not an endpoint.** Asked to draft screener answers, it
   decided to `glob` for the candidate's resume, hit the sandbox boundary, and
   killed the turn. Three attempts, three failures. The API has no tools unless
   you define them.
2. **Speed.** A cover letter took 118s through the CLI. A direct call of the
   same shape takes single-digit seconds.
3. **No schema enforcement.** The CLI had none, so ~130 lines here existed to
   ask for JSON, find the first balanced brace, validate, and retry with the
   error appended. `responseSchema` does it server-side.
4. **Windows.** npm installs the CLI as a batch shim that CreateProcess cannot
   execute, which needed its own interpreter workaround.

What is still load-bearing here, and was worth keeping from the CLI era:

* The usage ledger and the self-imposed daily ceiling. These matter *more* now:
  the API's free tier allows only 20 requests/day per model family, so a wasted
  call is a meaningful fraction of the budget.
* Retry with backoff, then fallback across model tiers. Quota is per model, so
  a fallback chain spanning families genuinely buys headroom.
* Error classification. A 429 is worth retrying; a bad key never is.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import config, db

T = TypeVar("T", bound=BaseModel)

GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_ROOT = "https://openrouter.ai/api/v1"
# Used only when a tier is left unset in ai.yaml. Neither API auto-routes, so
# unlike the CLI there is no "let it choose" option to fall back on.
DEFAULT_MODEL = "gemini-3.5-flash-lite"

PROVIDERS = ("gemini", "openrouter")


def split_spec(spec: str) -> tuple[str, str]:
    """`provider:model` -> (provider, model). A bare name means Gemini.

    Split on the first colon only: OpenRouter model IDs carry their own, as in
    `openrouter:z-ai/glm-5.2:free`.
    """
    if ":" in spec:
        head, rest = spec.split(":", 1)
        if head in PROVIDERS:
            return head, rest
    return "gemini", spec


class LLMError(RuntimeError):
    """Base class for every failure originating from the API."""


class LLMAuthError(LLMError):
    """No usable credentials. A human has to fix this; retrying cannot."""


class LLMQuotaError(LLMError):
    """Rate-limited or out of allowance, after retries and fallbacks."""


class LLMBudgetError(LLMError):
    """Our own daily ceiling, not Google's. Raised before any call is made."""


class LLMSchemaError(LLMError):
    """The model never produced JSON matching the requested schema."""


@dataclass
class Result:
    """One completed call."""

    text: str
    model: str
    duration_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


# ─── schema translation ──────────────────────────────────────────────────────

# Keys `responseSchema` accepts. Anything else is dropped rather than passed
# through, because an unrecognised key is a 400 rather than a warning. Pydantic
# emits several of them as a matter of course: `title` on every model and field,
# `default` on every optional, and `$defs`/`$ref` for nested models.
_SCHEMA_KEYS = frozenset(
    {
        "type", "format", "description", "nullable", "enum",
        "items", "properties", "required", "anyOf", "propertyOrdering",
        "minItems", "maxItems", "minimum", "maximum",
    }
)


def to_response_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON Schema -> the OpenAPI subset `responseSchema` accepts.

    `$ref` is the important one: `BatchVerdict` holds a list of `JobVerdict`,
    which Pydantic renders as a `$defs` entry plus a reference. Gemini rejects
    references, so definitions are inlined at every use site.
    """
    raw = schema.model_json_schema()
    defs = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name not in defs:
                raise LLMSchemaError(f"unresolvable schema reference {name!r}")
            return walk(defs[name])

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in _SCHEMA_KEYS:
                continue
            if key == "properties":
                out[key] = {k: walk(v) for k, v in value.items()}
            elif key == "items":
                out[key] = walk(value)
            elif key == "anyOf":
                # How Pydantic renders `X | None`. Gemini wants one type plus
                # an explicit nullable flag.
                branches = [b for b in value if b.get("type") != "null"]
                if len(value) == 2 and len(branches) == 1:
                    inner = walk(branches[0])
                    inner["nullable"] = True
                    return inner
                out[key] = [walk(b) for b in value]
            else:
                out[key] = value
        return out

    return walk(raw)


# ─── low-level call ──────────────────────────────────────────────────────────


_KEY_ENV = {
    "gemini": ("GEMINI_API_KEY", "https://aistudio.google.com/apikey"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
}


def api_key(provider: str = "gemini") -> str:
    config.load_env()
    import os

    var, where = _KEY_ENV[provider]
    key = (os.environ.get(var) or "").strip()
    if not key:
        raise LLMAuthError(
            f"{var} is not set.\n"
            f"Create a key at {where}, then put it in .env (not your shell\n"
            f"profile — cron does not read that):\n"
            f"    {var}=..."
        )
    return key


def _classify_status(status: int, message: str) -> type[LLMError]:
    low = message.lower()
    if status in (401, 403) or "api_key_invalid" in low or "api key not valid" in low:
        return LLMAuthError
    if status == 429 or "resource_exhausted" in low or "quota" in low:
        return LLMQuotaError
    if status >= 500 or "high demand" in low or "overloaded" in low:
        return LLMQuotaError
    return LLMError


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull the answer out of a generateContent response.

    Two shapes bite here. A blocked prompt returns no candidates at all, only
    `promptFeedback`. And 3.x models interleave reasoning into `parts` marked
    `thought`, which must not be concatenated into the answer — doing so
    corrupts otherwise valid JSON.
    """
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise LLMError(f"prompt blocked: {feedback['blockReason']}")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise LLMError(f"no candidates returned: {json.dumps(payload)[:300]}")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        p["text"] for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")
    )

    reason = candidate.get("finishReason")
    if not text.strip():
        raise LLMError(f"empty response (finishReason={reason})")
    if reason == "MAX_TOKENS":
        raise LLMError(
            "response hit the output token limit and is truncated "
            "(finishReason=MAX_TOKENS). Reduce the batch size."
        )
    return text


def _extract_openrouter_text(payload: dict[str, Any]) -> str:
    """Pull the answer out of an OpenAI-shaped chat completion.

    OpenRouter can return a 200 whose body is an error object — an upstream
    provider being down looks like success at the HTTP layer — so that case is
    checked before anything else.
    """
    if payload.get("error"):
        error = payload["error"]
        message = str(error.get("message") if isinstance(error, dict) else error)
        raise _classify_status(
            int((error or {}).get("code") or 0) if isinstance(error, dict) else 0,
            message,
        )(message)

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"no choices returned: {json.dumps(payload)[:300]}")

    choice = choices[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    reason = choice.get("finish_reason")
    if not text:
        raise LLMError(f"empty response (finish_reason={reason})")
    if reason == "length":
        raise LLMError(
            "response hit the output token limit and is truncated "
            "(finish_reason=length). Reduce the batch size."
        )
    return text


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict[str, Any]:
    """Shared HTTP mechanics: transport errors, status mapping, JSON decode."""
    try:
        response = httpx.post(url, json=body, timeout=timeout, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMQuotaError(f"request timed out after {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise LLMQuotaError(f"transport error: {exc}") from exc

    if response.status_code != 200:
        try:
            error = response.json().get("error", {})
            message = str(error.get("message") or response.text)[:500]
        except ValueError:
            message = response.text[:500]
        raise _classify_status(response.status_code, message)(
            f"HTTP {response.status_code}: {message}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LLMError(f"response was not JSON: {response.text[:300]}") from exc


def _call(
    prompt: str,
    *,
    model: str,
    timeout: float,
    response_schema: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """One HTTP call to whichever provider `model` names.

    Both providers get the same inlined schema from `to_response_schema`. The
    OpenAI-shaped side would tolerate `$ref`, but sending one self-contained
    schema everywhere means there is a single thing to debug when a model
    returns the wrong shape.
    """
    provider, name = split_spec(model)

    if provider == "openrouter":
        body: dict[str, Any] = {
            "model": name,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                # Not `strict`: that demands every property be required and
                # additionalProperties be false, which Pydantic's output is not.
                # Pydantic re-validates anyway.
                "json_schema": {"name": "response", "schema": response_schema},
            }
        payload = _post(
            f"{OPENROUTER_ROOT}/chat/completions",
            body,
            {
                "Authorization": f"Bearer {api_key('openrouter')}",
                "Content-Type": "application/json",
                "X-Title": "lgrow",
            },
            timeout,
        )
        return _extract_openrouter_text(payload), payload

    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if response_schema is not None:
        body["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        }
    payload = _post(
        f"{GEMINI_ROOT}/models/{name}:generateContent",
        body,
        {"x-goog-api-key": api_key("gemini"), "Content-Type": "application/json"},
        timeout,
    )
    return _extract_text(payload), payload


# ─── budget + retry wrapper ──────────────────────────────────────────────────


def remaining_budget() -> int:
    ai = config.load_ai()
    with db.session() as conn:
        used = db.calls_today(conn)
    return max(0, ai.budget.max_calls_per_day - used)


def _models_to_try(tier: str) -> list[str]:
    """Preferred model for a tier, then each configured fallback.

    Order matters more than it used to. Every free tier here caps by the day,
    and each provider counts separately, so a chain that crosses providers keeps
    working after the first allowance is spent.
    """
    return config.load_ai().models.chain(tier) or [DEFAULT_MODEL]


def generate(
    prompt: str,
    *,
    purpose: str,
    tier: str = "quality",
    timeout: float | None = None,
    response_schema: dict[str, Any] | None = None,
) -> Result:
    """Run one prompt, with budget check, retries and model fallback.

    `purpose` is recorded in the usage ledger so `lgrow usage` can show where
    the day's calls went.
    """
    ai = config.load_ai()
    rt = ai.runtime
    timeout = timeout or rt.timeout_seconds

    if remaining_budget() <= 0:
        raise LLMBudgetError(
            f"Daily budget of {ai.budget.max_calls_per_day} calls is spent. "
            "Raise budget.max_calls_per_day in config/ai.yaml, or wait for the "
            "UTC day to roll over."
        )

    last_exc: Exception | None = None
    for model in _models_to_try(tier):
        for attempt in range(1, rt.max_retries + 1):
            started = time.monotonic()
            try:
                text, raw = _call(
                    prompt,
                    model=model,
                    timeout=timeout,
                    response_schema=response_schema,
                )
            except LLMAuthError:
                raise  # a human must fix the key; retrying is pointless
            except LLMError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                last_exc = exc
                _log_usage(
                    model=model,
                    purpose=purpose,
                    ok=False,
                    duration_ms=elapsed,
                    prompt_chars=len(prompt),
                    response_chars=0,
                    error=str(exc)[:500],
                )
                retryable = isinstance(exc, LLMQuotaError)
                if retryable and attempt < rt.max_retries:
                    time.sleep(
                        min(
                            rt.backoff_base_seconds * (2 ** (attempt - 1)),
                            rt.backoff_max_seconds,
                        )
                    )
                    continue
                break  # try the next model in the chain
            else:
                elapsed = int((time.monotonic() - started) * 1000)
                _log_usage(
                    model=model,
                    purpose=purpose,
                    ok=True,
                    duration_ms=elapsed,
                    prompt_chars=len(prompt),
                    response_chars=len(text),
                    error=None,
                )
                return Result(text=text, model=model, duration_ms=elapsed, raw=raw)

    assert last_exc is not None
    if isinstance(last_exc, LLMQuotaError):
        raise LLMQuotaError(
            f"Gemini unavailable after retries across all model tiers: {last_exc}"
        ) from last_exc
    raise LLMError(str(last_exc)) from last_exc


def _log_usage(**kwargs: Any) -> None:
    """Ledger writes must never take down the caller."""
    try:
        with db.session() as conn:
            db.record_usage(conn, **kwargs)
    except Exception:  # noqa: BLE001 — telemetry is not worth an outage
        pass


# ─── structured output ───────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    With `responseSchema` set, the body should already be bare JSON. This stays
    as a lenient fallback: it costs nothing on the happy path, and a stray fence
    or preamble would otherwise waste a call out of a 20-a-day allowance.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty response")

    candidates: list[str] = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    candidates.append(text)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        blob = _first_balanced(candidate)
        if blob is not None:
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON value found in response: {text[:300]!r}")


def _first_balanced(text: str) -> str | None:
    """First balanced {...} or [...] run, ignoring braces inside strings."""
    start = None
    opener = closer = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener = ch
            closer = "}" if ch == "{" else "]"
            break
    if start is None:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def generate_structured(
    prompt: str,
    schema: type[T],
    *,
    purpose: str,
    tier: str = "quality",
    timeout: float | None = None,
) -> T:
    """Generate and validate against a Pydantic model.

    The schema is enforced server-side, so the retry ladder that used to carry
    this is now a backstop rather than the mechanism. Pydantic still validates:
    `responseSchema` guarantees shape, not that `score` is within 0-100 or that
    a list respects its cap.
    """
    ai = config.load_ai()
    max_attempts = max(1, ai.schema_retry.max_attempts)
    response_schema = to_response_schema(schema)

    attempt_prompt = prompt
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        result = generate(
            attempt_prompt,
            purpose=purpose,
            tier=tier,
            timeout=timeout,
            response_schema=response_schema,
        )
        try:
            payload = extract_json(result.text)
        except ValueError as exc:
            errors.append(f"attempt {attempt}: {exc}")
        else:
            try:
                return schema.model_validate(payload)
            except ValidationError as exc:
                errors.append(f"attempt {attempt}: {exc.errors(include_url=False)}")

        if attempt < max_attempts:
            attempt_prompt = (
                f"{prompt}\n\n"
                f"Your previous answer was rejected. Fix exactly this and return "
                f"the corrected JSON:\n{errors[-1]}"
            )

    raise LLMSchemaError(
        f"{purpose}: no schema-valid JSON after {max_attempts} attempts. "
        + " | ".join(errors)
    )


# ─── diagnostics ─────────────────────────────────────────────────────────────


def probe(tier: str = "quality") -> tuple[bool, str]:
    """Cheapest possible real call, for `lgrow doctor --probe`."""
    try:
        result = generate(
            "Reply with exactly this word and nothing else: OK",
            purpose=f"probe:{tier}",
            tier=tier,
            timeout=60,
        )
    except LLMError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    body = result.text.strip()
    return "OK" in body.upper(), (
        f"model={result.model} {result.duration_ms}ms reply={body[:60]!r}"
    )
