"""Shared HTTP client for job sources.

Deliberately a polite client: identifying User-Agent, a delay between requests,
bounded retries with backoff, and no concurrency. These are public feeds run by
small teams — RemoteOK's terms say outright they'll suspend API access for
misuse — so being well-behaved is both correct and self-interested.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .. import config


class FetchError(RuntimeError):
    """A source could not be read. Never fatal — other sources still run."""


_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class Fetcher:
    def __init__(self, http: config.HttpConfig | None = None) -> None:
        self.cfg = http or config.load_sources().http
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=self.cfg.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        gap = self.cfg.delay_between_requests - (time.monotonic() - self._last_request_at)
        if gap > 0:
            time.sleep(gap)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_error: str = "unknown"
        for attempt in range(1, self.cfg.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url, params=params)
                self._last_request_at = time.monotonic()
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise FetchError(
                            f"{url} returned non-JSON "
                            f"({resp.headers.get('content-type')}): {exc}"
                        ) from exc
                if resp.status_code == 404:
                    # A wrong company slug is a config error, not a transient one.
                    raise FetchError(f"{url} -> 404 (check the board slug)")
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in _RETRY_STATUS:
                    raise FetchError(f"{url} -> {last_error}")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
                    continue

            if attempt < self.cfg.max_retries:
                time.sleep(min(2**attempt, 30))
        raise FetchError(
            f"{url} failed after {self.cfg.max_retries} attempts: {last_error}"
        )
