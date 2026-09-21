"""Location normalisation and the English-market gate.

Feeds describe location in wildly different ways — "USA, Canada", "Worldwide",
"EMEA Field Sales", `['United States']`, `[-8, -7, -6]`. This module collapses
all of it into region codes so filtering is consistent, then decides whether a
job is worth your time given where you can actually work.

The codes are deliberately coarse. Precision here is false comfort: the
authoritative restriction is usually buried in the description, which is why the
Gemini scoring pass reads the text as well.
"""

from __future__ import annotations

import re

WORLDWIDE = "WORLDWIDE"
UNKNOWN = "UNKNOWN"

# Region codes we care about, in the order we prefer to report them.
ENGLISH_PRIMARY = ("US", "CA", "AU", "GB", "IE", "NZ")

# Full place names, matched case-insensitively.
#
# Note what is absent: no bare two-letter codes live here. "San Francisco, CA"
# is California, not Canada, and "come work with us" is not the United States —
# both were real false positives during development. Abbreviations are handled
# separately below, case-sensitively.
_CI_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(worldwide|world wide|anywhere|global(?:ly)?|any location|"
     r"fully remote|no location restriction|location.?independent|"
     r"work from anywhere)\b", WORLDWIDE),
    (r"\b(united states|u\.s\.a\.|u\.s\.|stateside|conus)\b", "US"),
    (r"\b(canada|canadian|toronto|vancouver|montreal|ottawa|calgary)\b", "CA"),
    (r"\b(australia|australian|sydney|melbourne|brisbane|perth)\b", "AU"),
    (r"\b(united kingdom|great britain|england|scotland|wales|london|"
     r"manchester|edinburgh|northern ireland)\b", "GB"),
    (r"\b(ireland|republic of ireland|dublin)\b", "IE"),
    (r"\b(new zealand|auckland|wellington)\b", "NZ"),
    (r"\b(north america)\b", "NAMER"),
    (r"\b(latin america|south america|brazil|mexico|argentina|colombia|"
     r"chile|peru)\b", "LATAM"),
    (r"\b(europe|european union|germany|france|spain|italy|netherlands|"
     r"poland|portugal|sweden|norway|denmark|finland|switzerland|austria|"
     r"belgium|czech|romania|ukraine|serbia)\b", "EU"),
    (r"\b(asia|asia.?pacific|india|singapore|japan|china|hong kong|"
     r"philippines|indonesia|vietnam|thailand|malaysia|pakistan|"
     r"bangladesh|sri lanka)\b", "APAC"),
    (r"\b(middle east|uae|dubai|saudi|qatar|israel|turkey|egypt)\b", "MENA"),
    (r"\b(africa|nigeria|kenya|south africa|ghana)\b", "AFRICA"),
)

# Uppercase-only abbreviations. Case sensitivity is what makes these safe:
# "US" in a location field is the country, "us" in prose is a pronoun.
# "CA" is deliberately excluded as irredeemably ambiguous.
_CS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(US|USA)\b", "US"),
    (r"\b(UK|GB)\b", "GB"),
    (r"\b(AU|AUS)\b", "AU"),
    (r"\bNZ\b", "NZ"),
    (r"\bIE\b", "IE"),
    (r"\b(NAMER|NA)\b", "NAMER"),
    (r"\bLATAM\b", "LATAM"),
    (r"\b(EMEA|EU)\b", "EU"),
    (r"\bAPAC\b", "APAC"),
    (r"\bMENA\b", "MENA"),
)

_COMPILED = tuple(
    (re.compile(pat, re.IGNORECASE), code) for pat, code in _CI_PATTERNS
) + tuple((re.compile(pat), code) for pat, code in _CS_PATTERNS)

# UTC offsets that indicate a region, used for Himalayas' timezoneRestrictions.
_TZ_RANGES: tuple[tuple[range, str], ...] = (
    (range(-10, -3), "AMERICAS"),   # UTC-10..-4  (US, Canada, LATAM)
    (range(-3, 4), "EUROPE_AFRICA"),  # UTC-3..+3
    (range(4, 13), "ASIA_PACIFIC"),  # UTC+4..+12
)


def codes_from_text(*texts: str | None) -> set[str]:
    """All region codes mentioned across the given strings."""
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for pattern, code in _COMPILED:
            if pattern.search(text):
                found.add(code)
    return found


def codes_from_list(values: list | None) -> set[str]:
    """Region codes from a list field (Himalayas locationRestrictions, tags)."""
    if not values:
        return set()
    return codes_from_text(" , ".join(str(v) for v in values))


def regions_from_timezones(offsets: list | None) -> set[str]:
    """Coarse regions implied by a list of UTC hour offsets."""
    regions: set[str] = set()
    for raw in offsets or []:
        try:
            hour = int(raw)
        except (TypeError, ValueError):
            continue
        for rng, name in _TZ_RANGES:
            if hour in rng:
                regions.add(name)
    return regions


def normalize(
    *,
    location_text: str | None = None,
    location_list: list | None = None,
    extra_text: str | None = None,
) -> set[str]:
    """Region codes for a job, from whichever fields the feed provided.

    An empty result becomes {UNKNOWN} rather than {WORLDWIDE}: "we couldn't tell"
    and "open to everyone" are very different, and conflating them fills your
    queue with jobs you can't take.
    """
    codes = codes_from_text(location_text, extra_text) | codes_from_list(location_list)
    if not codes:
        return {UNKNOWN}
    return codes


def is_allowed(codes: set[str], allow: list[str]) -> tuple[bool, str]:
    """Whether a job's regions overlap what you can work in.

    Returns (allowed, reason). UNKNOWN is allowed through — the Gemini pass
    reads the description and will demote it if there's a hidden restriction.
    Rejecting unknowns here would throw away a lot of genuinely open roles.
    """
    allow_upper = {a.strip().upper() for a in allow}
    if "WORLDWIDE" in allow_upper:
        allow_upper.add(WORLDWIDE)

    if WORLDWIDE in codes:
        return True, "open worldwide"
    if UNKNOWN in codes and len(codes) == 1:
        return True, "location unstated — needs the description read"

    overlap = codes & allow_upper
    if overlap:
        return True, f"matches {', '.join(sorted(overlap))}"

    # Treat NAMER as covering US/CA when either is permitted.
    if "NAMER" in codes and allow_upper & {"US", "CA"}:
        return True, "North America"

    return False, f"restricted to {', '.join(sorted(codes))}"


def english_market(codes: set[str]) -> bool:
    """True if the job plausibly sits in an English-speaking market."""
    if WORLDWIDE in codes or UNKNOWN in codes:
        return True
    return bool(codes & (set(ENGLISH_PRIMARY) | {"NAMER"}))
