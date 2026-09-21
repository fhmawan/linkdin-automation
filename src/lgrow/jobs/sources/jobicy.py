"""Jobicy — https://jobicy.com/api/v2/remote-jobs

Their notice asks for credit with a direct link to the source and that apply
buttons go to the original job URL. We keep `url` as the only route to applying,
which satisfies that.

Field names are camelCase and prefixed with `job` (jobTitle, jobGeo, jobLevel).
`jobGeo` is prose with irregular spacing, e.g. "Ireland,  UK".
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import Fetcher

NAME = "jobicy"
API = "https://jobicy.com/api/v2/remote-jobs"


def _to_job(raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("jobTitle") or "").strip()
    company = (raw.get("companyName") or "").strip()
    url = (raw.get("url") or "").strip()
    if not title or not company or not url:
        return None

    location_text = (raw.get("jobGeo") or "").strip()
    codes = geo.normalize(location_text=location_text)

    job_types = [str(t) for t in raw.get("jobType") or []]
    level = (raw.get("jobLevel") or "").strip()
    return models.Job(
        source=NAME,
        source_id=str(raw.get("id") or raw.get("jobSlug") or url),
        url=url,
        apply_url=url,
        company=company,
        title=title,
        location_raw=location_text or "remote",
        geo_tags=sorted(codes),
        employment_type=job_types[0] if job_types else None,
        description=textutil.smart_html_to_text(
            raw.get("jobDescription") or raw.get("jobExcerpt")
        ),
        posted_at=models.iso_to_dt(raw.get("pubDate")),
        seniority_raw=[level] if level and level.lower() != "any" else [],
        tags=[str(t) for t in raw.get("jobIndustry") or []][:10],
    )


def fetch(
    fetcher: Fetcher, *, count: int = 100, queries: list[str] | None = None, **_: Any
) -> list[models.Job]:
    collected: dict[str, models.Job] = {}
    attempts: list[dict[str, Any]] = [{"count": count}]
    for query in queries or []:
        attempts.append({"count": count, "tag": query})

    for params in attempts:
        if len(collected) >= count:
            break
        payload = fetcher.get_json(API, params=params)
        if not isinstance(payload, dict):
            continue
        for raw in payload.get("jobs") or []:
            if isinstance(raw, dict):
                job = _to_job(raw)
                if job:
                    collected.setdefault(job.id, job)
    return list(collected.values())[:count]


def probe(fetcher: Fetcher) -> str:
    payload = fetcher.get_json(API, params={"count": 1})
    if isinstance(payload, dict):
        version = payload.get("apiVersion")
        return f"api v{version}" if version else "reachable"
    return "reachable"
