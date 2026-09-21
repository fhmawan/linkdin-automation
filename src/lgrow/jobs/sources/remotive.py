"""Remotive — https://remotive.com/api/remote-jobs

Their terms ask that jobs not be republished to third-party job sites and that
you link back to the URL in the feed. We only ever show you the listing and send
you to that URL to apply, which respects both.

`candidate_required_location` is prose, not codes ("LATAM, Europe, USA, Canada,
APAC"), so it goes through the geo normaliser.
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import Fetcher

NAME = "remotive"
API = "https://remotive.com/api/remote-jobs"


def _to_job(raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("title") or "").strip()
    company = (raw.get("company_name") or "").strip()
    url = (raw.get("url") or "").strip()
    if not title or not company or not url:
        return None

    location_text = raw.get("candidate_required_location") or ""
    codes = geo.normalize(location_text=location_text)

    return models.Job(
        source=NAME,
        source_id=str(raw.get("id") or url),
        url=url,
        apply_url=url,
        company=company,
        title=title,
        location_raw=location_text,
        geo_tags=sorted(codes),
        employment_type=raw.get("job_type"),
        salary_raw=(raw.get("salary") or "").strip() or None,
        description=textutil.smart_html_to_text(raw.get("description")),
        posted_at=models.iso_to_dt(raw.get("publication_date")),
        tags=[str(t) for t in raw.get("tags") or []][:20],
    )


def fetch(
    fetcher: Fetcher, *, limit: int = 150, queries: list[str] | None = None, **_: Any
) -> list[models.Job]:
    """Fetch once per query, then an unfiltered page as a safety net."""
    collected: dict[str, models.Job] = {}
    attempts: list[dict[str, Any]] = []
    for query in queries or []:
        attempts.append({"limit": limit, "search": query})
    attempts.append({"limit": limit})

    for params in attempts:
        if len(collected) >= limit:
            break
        payload = fetcher.get_json(API, params=params)
        if not isinstance(payload, dict):
            continue
        for raw in payload.get("jobs") or []:
            if isinstance(raw, dict):
                job = _to_job(raw)
                if job:
                    collected.setdefault(job.id, job)

    return list(collected.values())[:limit]


def probe(fetcher: Fetcher) -> str:
    payload = fetcher.get_json(API, params={"limit": 1})
    count = payload.get("job-count") if isinstance(payload, dict) else None
    return f"{count} jobs" if count else "reachable"
