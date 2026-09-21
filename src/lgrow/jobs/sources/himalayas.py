"""Himalayas — https://himalayas.app/jobs/api

The richest of the free feeds: it reports `locationRestrictions` and
`timezoneRestrictions` as structured fields rather than prose, which makes the
geo gate far more reliable here than elsewhere.

Pagination note (verified 2026-08): the API now prefers **cursor** pagination.
`offset` is deprecated and slated for removal, and cursor is documented as never
returning the same job twice — so we follow `nextCursor`.
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import Fetcher

NAME = "himalayas"
API = "https://himalayas.app/jobs/api"
PAGE_LIMIT = 50


def _to_job(raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("title") or "").strip()
    company = (raw.get("companyName") or "").strip()
    link = (raw.get("applicationLink") or raw.get("guid") or "").strip()
    if not title or not company or not link:
        return None

    location_list = raw.get("locationRestrictions") or []
    codes = geo.normalize(
        location_list=location_list,
        extra_text=" ".join(str(c) for c in raw.get("categories") or []),
    )
    # An empty restriction list on Himalayas genuinely means "no restriction".
    if not location_list and geo.UNKNOWN in codes:
        codes = {geo.WORLDWIDE}
    codes |= geo.regions_from_timezones(raw.get("timezoneRestrictions"))

    salary = None
    lo, hi = raw.get("minSalary"), raw.get("maxSalary")
    if lo or hi:
        cur = raw.get("currency") or ""
        period = raw.get("salaryPeriod") or ""
        salary = f"{cur} {lo or '?'}–{hi or '?'} {period}".strip()

    return models.Job(
        source=NAME,
        source_id=str(raw.get("guid") or link),
        url=link,
        apply_url=link,
        company=company,
        title=title,
        location_raw=", ".join(str(c) for c in location_list) or "worldwide",
        geo_tags=sorted(codes),
        employment_type=raw.get("employmentType"),
        salary_raw=salary,
        description=textutil.smart_html_to_text(raw.get("description") or raw.get("excerpt")),
        posted_at=models.epoch_to_dt(raw.get("pubDate")),
        seniority_raw=[str(s) for s in raw.get("seniority") or []],
        tags=[str(c) for c in raw.get("categories") or []][:15],
    )


def fetch(fetcher: Fetcher, *, limit: int = 200, **_: Any) -> list[models.Job]:
    jobs: list[models.Job] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while len(jobs) < limit:
        params: dict[str, Any] = {"limit": min(PAGE_LIMIT, limit - len(jobs))}
        if cursor:
            params["cursor"] = cursor
        payload = fetcher.get_json(API, params=params)
        if not isinstance(payload, dict):
            break
        batch = payload.get("jobs") or []
        for raw in batch:
            job = _to_job(raw) if isinstance(raw, dict) else None
            if job:
                jobs.append(job)

        cursor = payload.get("nextCursor")
        # Guard against a server that keeps handing back the same cursor.
        if not cursor or not batch or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return jobs[:limit]


def probe(fetcher: Fetcher) -> str:
    payload = fetcher.get_json(API, params={"limit": 1})
    total = payload.get("totalCount") if isinstance(payload, dict) else None
    return f"{total:,} jobs indexed" if isinstance(total, int) else "reachable"
