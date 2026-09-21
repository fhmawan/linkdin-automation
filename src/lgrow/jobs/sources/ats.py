"""Applicant-tracking-system boards: Greenhouse, Lever, Ashby.

The highest-signal source in the whole pipeline. These are the employer's own
feed, so postings appear here before aggregators index them, the text is
complete, and `apply_url` goes straight to the real application form rather than
through a middleman.

The trade-off is coverage: one company per request. That's why they're driven by
a watchlist in config/sources.yaml rather than crawled broadly.

Endpoint shapes verified 2026-08:
  Greenhouse  boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
              -> {"jobs":[{title, absolute_url, location:{name}, content(escaped
                 HTML), updated_at, first_published, departments[], offices[]}]}
  Lever       api.lever.co/v0/postings/{slug}?mode=json
              -> [{text, hostedUrl, applyUrl, categories:{location, commitment,
                 department}, descriptionPlain, createdAt(ms), country,
                 workplaceType}]
  Ashby       api.ashbyhq.com/posting-api/job-board/{slug}
              -> {"jobs":[{title, location, secondaryLocations[], isRemote,
                 workplaceType, employmentType, descriptionPlain, jobUrl,
                 applyUrl, publishedAt, isListed}]}
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import FetchError, Fetcher

NAME = "ats"

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{slug}"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


# ─── Greenhouse ──────────────────────────────────────────────────────────────


def _greenhouse_job(slug: str, raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("title") or "").strip()
    url = (raw.get("absolute_url") or "").strip()
    if not title or not url:
        return None

    location = ((raw.get("location") or {}).get("name") or "").strip()
    offices = [
        str((o or {}).get("name") or "") for o in raw.get("offices") or [] if isinstance(o, dict)
    ]
    departments = [
        str((d or {}).get("name") or "")
        for d in raw.get("departments") or []
        if isinstance(d, dict)
    ]
    codes = geo.normalize(location_text=location, location_list=offices)

    company = (raw.get("company_name") or slug).strip()
    return models.Job(
        source=f"greenhouse:{slug}",
        source_id=str(raw.get("id") or url),
        url=url,
        apply_url=url,
        company=company,
        title=title,
        location_raw=location,
        geo_tags=sorted(codes),
        # `content` is HTML-escaped once by Greenhouse.
        description=textutil.smart_html_to_text(raw.get("content")),
        posted_at=models.iso_to_dt(raw.get("first_published"))
        or models.iso_to_dt(raw.get("updated_at")),
        tags=[d for d in departments if d][:10],
    )


def fetch_greenhouse(fetcher: Fetcher, slug: str) -> list[models.Job]:
    payload = fetcher.get_json(GREENHOUSE_API.format(slug=slug), params={"content": "true"})
    if not isinstance(payload, dict):
        return []
    jobs = []
    for raw in payload.get("jobs") or []:
        if isinstance(raw, dict):
            job = _greenhouse_job(slug, raw)
            if job:
                jobs.append(job)
    return jobs


# ─── Lever ───────────────────────────────────────────────────────────────────


def _lever_job(slug: str, raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("text") or "").strip()
    url = (raw.get("hostedUrl") or "").strip()
    if not title or not url:
        return None

    categories = raw.get("categories") or {}
    location = str(categories.get("location") or "").strip()
    country = str(raw.get("country") or "").strip()
    codes = geo.normalize(location_text=f"{location} {country}".strip())

    # Lever splits the body across several fields; concatenate what exists.
    body_parts = [
        raw.get("descriptionPlain") or raw.get("description"),
        raw.get("descriptionBodyPlain") or raw.get("descriptionBody"),
        raw.get("additionalPlain") or raw.get("additional"),
    ]
    for entry in raw.get("lists") or []:
        if isinstance(entry, dict):
            body_parts.append(f"\n{entry.get('text', '')}\n{entry.get('content', '')}")
    description = textutil.smart_html_to_text("\n\n".join(p for p in body_parts if p))

    workplace = str(raw.get("workplaceType") or "").strip()
    return models.Job(
        source=f"lever:{slug}",
        source_id=str(raw.get("id") or url),
        url=url,
        apply_url=(raw.get("applyUrl") or url).strip(),
        company=slug.replace("-", " ").title(),
        title=title,
        location_raw=f"{location} ({workplace})" if workplace else location,
        geo_tags=sorted(codes),
        employment_type=str(categories.get("commitment") or "") or None,
        description=description,
        posted_at=models.epoch_to_dt(raw.get("createdAt")),
        tags=[str(categories.get("department") or "")][:1],
    )


def fetch_lever(fetcher: Fetcher, slug: str) -> list[models.Job]:
    payload = fetcher.get_json(LEVER_API.format(slug=slug), params={"mode": "json"})
    if not isinstance(payload, list):
        return []
    jobs = []
    for raw in payload:
        if isinstance(raw, dict):
            job = _lever_job(slug, raw)
            if job:
                jobs.append(job)
    return jobs


# ─── Ashby ───────────────────────────────────────────────────────────────────


def _ashby_job(slug: str, raw: dict[str, Any]) -> models.Job | None:
    if raw.get("isListed") is False:
        return None
    title = (raw.get("title") or "").strip()
    url = (raw.get("jobUrl") or "").strip()
    if not title or not url:
        return None

    location = (raw.get("location") or "").strip()
    secondary = [str(s) for s in raw.get("secondaryLocations") or []]
    country = ((raw.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
    codes = geo.normalize(
        location_text=f"{location} {country or ''}".strip(), location_list=secondary
    )
    if raw.get("isRemote") and geo.UNKNOWN in codes:
        codes = {geo.WORLDWIDE}

    workplace = str(raw.get("workplaceType") or "").strip()
    return models.Job(
        source=f"ashby:{slug}",
        source_id=str(raw.get("id") or url),
        url=url,
        apply_url=(raw.get("applyUrl") or url).strip(),
        company=slug.replace("-", " ").title(),
        title=title,
        location_raw=f"{location} ({workplace})" if workplace else location,
        geo_tags=sorted(codes),
        employment_type=str(raw.get("employmentType") or "") or None,
        description=textutil.smart_html_to_text(
            raw.get("descriptionPlain") or raw.get("descriptionHtml")
        ),
        posted_at=models.iso_to_dt(raw.get("publishedAt")),
        tags=[t for t in (raw.get("department"), raw.get("team")) if t][:5],
    )


def fetch_ashby(fetcher: Fetcher, slug: str) -> list[models.Job]:
    payload = fetcher.get_json(ASHBY_API.format(slug=slug))
    if not isinstance(payload, dict):
        return []
    jobs = []
    for raw in payload.get("jobs") or []:
        if isinstance(raw, dict):
            job = _ashby_job(slug, raw)
            if job:
                jobs.append(job)
    return jobs


# ─── driver ──────────────────────────────────────────────────────────────────

_BOARDS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(
    fetcher: Fetcher,
    *,
    greenhouse: list[str] | None = None,
    lever: list[str] | None = None,
    ashby: list[str] | None = None,
    on_error: Any = None,
) -> list[models.Job]:
    """Walk the watchlist. One bad slug must not sink the rest of the crawl."""
    plan = {
        "greenhouse": greenhouse or [],
        "lever": lever or [],
        "ashby": ashby or [],
    }
    jobs: list[models.Job] = []
    for board, slugs in plan.items():
        fetch_fn = _BOARDS[board]
        for slug in slugs:
            try:
                jobs.extend(fetch_fn(fetcher, slug))
            except FetchError as exc:
                if on_error:
                    on_error(f"{board}:{slug}", exc)
    return jobs
