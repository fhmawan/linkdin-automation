"""Text cleaning shared by every source adapter."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLOCK_END_RE = re.compile(
    r"</(p|div|li|h[1-6]|tr|table|ul|ol|blockquote)\s*>", re.IGNORECASE
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t ]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None, *, unescape_passes: int = 1) -> str:
    """Flatten an HTML job description into readable plain text.

    `unescape_passes` exists because some feeds double-encode: Arbeitnow and
    Greenhouse return `&lt;div&gt;` rather than `<div>`, so the entities have to
    be decoded before the tags can be stripped.
    """
    if not raw:
        return ""
    text = raw
    for _ in range(max(1, unescape_passes)):
        text = html.unescape(text)

    text = _SCRIPT_RE.sub(" ", text)
    text = _BR_RE.sub("\n", text)
    text = _LI_RE.sub("\n• ", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    # A second pass catches entities that were hidden inside tags.
    text = html.unescape(text)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def looks_double_encoded(raw: str | None) -> bool:
    """True when the payload still contains encoded tags like `&lt;p&gt;`."""
    if not raw:
        return False
    head = raw[:2000]
    return "&lt;" in head or "&quot;" in head or "&amp;lt;" in head


def smart_html_to_text(raw: str | None) -> str:
    """html_to_text with the number of unescape passes detected automatically."""
    return html_to_text(raw, unescape_passes=2 if looks_double_encoded(raw) else 1)


def normalize_company(name: str | None) -> str:
    """Company name reduced to a comparable key.

    Strips the legal-suffix noise that makes the same employer look like three
    different ones across feeds ("Acme, Inc." / "acme inc" / "Acme").
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 &]+", " ", text)
    text = re.sub(
        r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|bv|nv|plc|sa|ag|pty|co|"
        r"company|group|holdings|technologies|technology|labs|studio|studios)\b",
        " ",
        text,
    )
    return _WS_RE.sub(" ", text).strip()


_TITLE_NOISE_RE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|full[- ]?time|part[- ]?time|contract|"
    r"permanent|urgent|hiring|w2|f/?t|new)\b",
    re.IGNORECASE,
)


def normalize_title(title: str | None) -> str:
    """Job title reduced to a comparable key."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = text.lower()
    # Strip leading reference numbers like "#969 - " or "[REQ-123] ".
    text = re.sub(r"^\s*[\[\(#]?\s*(req|job|ref)?[-_ ]?\d{2,}\s*[\]\)]?\s*[-–:]?\s*", "", text)
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = _TITLE_NOISE_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9+#/ ]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


def dedupe_key(company: str | None, title: str | None) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def stable_id(*parts: str | None) -> str:
    """Short deterministic id — the primary key for a job row."""
    joined = "\x1f".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def truncate(text: str, limit: int, *, suffix: str = " […truncated]") -> str:
    """Cut long descriptions down for prompt budgets, at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut + suffix
