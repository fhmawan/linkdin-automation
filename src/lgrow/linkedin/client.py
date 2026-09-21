"""LinkedIn Posts API client.

Publishes to your own feed via `POST /rest/posts` with the `w_member_social`
scope. That is the officially sanctioned route — the same one commercial
schedulers use — so nothing here carries suspension risk. The member throttle is
150 requests/day and three posts a week uses a fraction of it.

The subtlety that will bite anyone writing this from scratch is `commentary`
encoding. It is not plain text: it's LinkedIn's "little" format, and the docs are
unambiguous that *all* reserved characters must be backslash-escaped "even if
those characters are not used in one of the supported elements or templates".
Parentheses are reserved. Post "I shipped it (finally)" unescaped and you get a
rejection or mangled output, so `render_commentary` handles this for every post.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

from .. import paths
from .auth import Tokens, valid_tokens

POSTS_URL = "https://api.linkedin.com/rest/posts"
DEFAULT_VERSION = "202608"

# Reserved by the `little` grammar. Order matters: backslash is handled first.
_RESERVED = set("|{}@[]()<>#*_~")

# Inline hashtags in a draft, e.g. "#python". Converted to HashtagTemplate so
# they stay clickable instead of being escaped into literal text.
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w{2,60})")

MAX_COMMENTARY = 3000  # LinkedIn's own limit for a feed post


class LinkedInApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class PublishResult:
    post_urn: str
    permalink: str
    visibility: str


def escape_little(text: str) -> str:
    """Backslash-escape every character reserved by the `little` format."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch in _RESERVED:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def render_commentary(text: str) -> str:
    """Prepare draft text for the `commentary` field.

    Inline `#hashtags` become HashtagTemplates so they remain real, clickable
    hashtags; everything else is escaped as literal text.
    """
    parts: list[str] = []
    cursor = 0
    for match in _HASHTAG_RE.finditer(text):
        parts.append(escape_little(text[cursor : match.start()]))
        # The '#' inside the template is itself escaped, per the docs' example.
        parts.append("{hashtag|\\#|" + match.group(1) + "}")
        cursor = match.end()
    parts.append(escape_little(text[cursor:]))
    return "".join(parts)


def api_version() -> str:
    return os.environ.get("LINKEDIN_API_VERSION", DEFAULT_VERSION).strip() or DEFAULT_VERSION


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": api_version(),
        "Content-Type": "application/json",
    }


def build_payload(
    text: str, *, person_urn: str, visibility: str = "PUBLIC"
) -> dict[str, object]:
    """The exact JSON body sent to /rest/posts."""
    if visibility not in ("PUBLIC", "CONNECTIONS"):
        raise ValueError(f"visibility must be PUBLIC or CONNECTIONS, got {visibility!r}")
    return {
        "author": person_urn,
        "commentary": render_commentary(text),
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


def permalink(post_urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_urn}/"


def publish_text(
    text: str,
    *,
    visibility: str = "PUBLIC",
    tokens: Tokens | None = None,
    dry_run: bool = False,
) -> PublishResult:
    """Publish a text post. Raises LinkedInApiError on failure."""
    text = text.strip()
    if not text:
        raise ValueError("refusing to publish an empty post")
    if len(text) > MAX_COMMENTARY:
        raise ValueError(
            f"post is {len(text)} characters; LinkedIn's limit is {MAX_COMMENTARY}"
        )

    tokens = tokens or valid_tokens()
    if not tokens.person_urn:
        raise LinkedInApiError(
            "No person URN stored — run `lgrow linkedin login` to fetch it."
        )

    payload = build_payload(text, person_urn=tokens.person_urn, visibility=visibility)

    if dry_run:
        import json

        print(f"POST {POSTS_URL}")
        for key, value in _headers("<redacted>").items():
            print(f"  {key}: {value}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return PublishResult(post_urn="urn:li:share:DRYRUN", permalink="", visibility=visibility)

    resp = httpx.post(
        POSTS_URL, headers=_headers(tokens.access_token), json=payload, timeout=45
    )

    if resp.status_code != 201:
        raise LinkedInApiError(
            _explain(resp.status_code, resp.text), resp.status_code, resp.text[:1000]
        )

    post_urn = (
        resp.headers.get("x-restli-id")
        or resp.headers.get("X-RestLi-Id")
        or ""
    )
    if not post_urn:
        raise LinkedInApiError(
            "Post appears to have been created but no x-restli-id header came back; "
            "check your feed before republishing to avoid a duplicate.",
            resp.status_code,
            resp.text[:500],
        )
    return PublishResult(
        post_urn=post_urn, permalink=permalink(post_urn), visibility=visibility
    )


def _explain(status: int, body: str) -> str:
    """Turn LinkedIn's terse errors into something actionable."""
    hints = {
        401: "Access token is invalid or expired — run `lgrow linkedin refresh`.",
        403: (
            "Permission denied. Confirm the 'Share on LinkedIn' product is added to "
            "your app (it grants w_member_social) and that you re-ran "
            "`lgrow linkedin login` after adding it."
        ),
        422: "LinkedIn rejected the post body — most often a commentary encoding problem.",
        429: (
            "Rate limited. The member throttle is 150 requests/day; something is "
            "publishing far more than this tool should."
        ),
        400: "Malformed request — check the author URN and commentary escaping.",
    }
    hint = hints.get(status, "")
    return f"POST /rest/posts returned {status}. {hint}\nResponse: {body[:400]}"


def delete_post(post_urn: str, *, tokens: Tokens | None = None) -> bool:
    """Delete one of your posts. Idempotent — a repeat returns 204."""
    tokens = tokens or valid_tokens()
    import urllib.parse

    encoded = urllib.parse.quote(post_urn, safe="")
    resp = httpx.delete(
        f"{POSTS_URL}/{encoded}",
        headers={**_headers(tokens.access_token), "X-RestLi-Method": "DELETE"},
        timeout=30,
    )
    if resp.status_code in (204, 200):
        return True
    raise LinkedInApiError(_explain(resp.status_code, resp.text), resp.status_code, resp.text[:500])


def self_check() -> str:
    """Confirm credentials work without publishing anything."""
    tokens = valid_tokens()
    _ = paths.TOKENS_PATH  # tokens are on disk by this point
    return f"authorised as {tokens.person_name or '?'} ({tokens.person_urn})"
