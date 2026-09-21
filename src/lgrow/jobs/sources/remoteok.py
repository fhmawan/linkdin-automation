"""RemoteOK — https://remoteok.com/api

**The first element of the returned array is not a job.** It's a metadata object
carrying their legal notice. Iterating the raw list without skipping it produces
one garbage record per fetch; we filter by shape rather than by index so it stays
correct if they ever reorder.

Their terms require linking back to the RemoteOK URL and crediting them as the
source. We keep `url` intact as the only apply route, which honours that; they
state they will suspend API access otherwise.
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import Fetcher

NAME = "remoteok"
API = "https://remoteok.com/api"
ATTRIBUTION = "Remote OK (https://remoteok.com)"


def _is_job(raw: Any) -> bool:
    return isinstance(raw, dict) and bool(raw.get("position")) and bool(raw.get("company"))


def _to_job(raw: dict[str, Any]) -> models.Job | None:
    title = (raw.get("position") or "").strip()
    company = (raw.get("company") or "").strip()
    url = (raw.get("url") or raw.get("apply_url") or "").strip()
    if not title or not company or not url:
        return None

    location_text = (raw.get("location") or "").strip().rstrip(", ")
    tags = [str(t) for t in raw.get("tags") or []]
    codes = geo.normalize(location_text=location_text, extra_text=" ".join(tags))

    salary = None
    lo, hi = raw.get("salary_min") or 0, raw.get("salary_max") or 0
    if lo or hi:
        salary = f"USD {lo:,}–{hi:,}"

    return models.Job(
        source=NAME,
        source_id=str(raw.get("id") or raw.get("slug") or url),
        url=url,
        apply_url=(raw.get("apply_url") or url).strip(),
        company=company,
        title=title,
        location_raw=location_text or "remote",
        geo_tags=sorted(codes),
        salary_raw=salary,
        description=textutil.smart_html_to_text(raw.get("description")),
        posted_at=models.epoch_to_dt(raw.get("epoch")) or models.iso_to_dt(raw.get("date")),
        tags=tags[:20],
    )


def fetch(fetcher: Fetcher, **_: Any) -> list[models.Job]:
    payload = fetcher.get_json(API)
    if not isinstance(payload, list):
        return []
    jobs: list[models.Job] = []
    for raw in payload:
        if not _is_job(raw):
            continue  # the legal/metadata element
        job = _to_job(raw)
        if job:
            jobs.append(job)
    return jobs


def probe(fetcher: Fetcher) -> str:
    payload = fetcher.get_json(API)
    if isinstance(payload, list):
        return f"{sum(1 for r in payload if _is_job(r))} jobs"
    return "reachable"
