"""Arbeitnow — https://www.arbeitnow.com/api/job-board-api

Two quirks that bite:

1. Descriptions are **double HTML-encoded** (`&lt;div class=&quot;...&quot;&gt;`),
   so the entities must be decoded before tags can be stripped. `smart_html_to_text`
   detects this.
2. It is a general job board, not a remote-only one — the `remote` flag is False
   for most postings. We drop non-remote rows rather than let them pad the queue.
"""

from __future__ import annotations

from typing import Any

from .. import geo, models, textutil
from ..fetcher import Fetcher

NAME = "arbeitnow"
API = "https://www.arbeitnow.com/api/job-board-api"


def _to_job(raw: dict[str, Any]) -> models.Job | None:
    if not raw.get("remote"):
        return None  # see quirk 2 above

    title = (raw.get("title") or "").strip()
    company = (raw.get("company_name") or "").strip()
    url = (raw.get("url") or "").strip()
    if not title or not company or not url:
        return None

    location_text = (raw.get("location") or "").strip()
    tags = [str(t) for t in raw.get("tags") or []]
    codes = geo.normalize(location_text=location_text, extra_text=" ".join(tags))

    job_types = [str(t) for t in raw.get("job_types") or []]
    return models.Job(
        source=NAME,
        source_id=str(raw.get("slug") or url),
        url=url,
        apply_url=url,
        company=company,
        title=title,
        location_raw=location_text or "remote",
        geo_tags=sorted(codes),
        employment_type=job_types[0] if job_types else None,
        description=textutil.smart_html_to_text(raw.get("description")),
        posted_at=models.epoch_to_dt(raw.get("created_at")),
        tags=tags[:20],
    )


def fetch(fetcher: Fetcher, *, pages: int = 3, **_: Any) -> list[models.Job]:
    jobs: list[models.Job] = []
    for page in range(1, max(1, pages) + 1):
        payload = fetcher.get_json(API, params={"page": page})
        if not isinstance(payload, dict):
            break
        batch = payload.get("data") or []
        if not batch:
            break
        for raw in batch:
            if isinstance(raw, dict):
                job = _to_job(raw)
                if job:
                    jobs.append(job)
    return jobs


def probe(fetcher: Fetcher) -> str:
    payload = fetcher.get_json(API, params={"page": 1})
    batch = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(batch, list):
        return "reachable"
    remote = sum(1 for r in batch if isinstance(r, dict) and r.get("remote"))
    return f"{len(batch)} on page 1, {remote} remote"
