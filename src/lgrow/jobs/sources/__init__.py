"""Source registry.

Each aggregator module exposes `NAME`, `fetch(fetcher, **opts)` and
`probe(fetcher)`. ATS boards are handled separately in `ats` because they're
driven by a per-company watchlist rather than a single feed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import models
from ..fetcher import Fetcher
from . import arbeitnow, ats, himalayas, jobicy, remoteok, remotive

AGGREGATORS = {
    himalayas.NAME: himalayas,
    remotive.NAME: remotive,
    arbeitnow.NAME: arbeitnow,
    jobicy.NAME: jobicy,
    remoteok.NAME: remoteok,
}

__all__ = [
    "AGGREGATORS",
    "arbeitnow",
    "ats",
    "fetch_aggregator",
    "himalayas",
    "jobicy",
    "reachability_probes",
    "remoteok",
    "remotive",
]


def fetch_aggregator(
    name: str, fetcher: Fetcher, *, queries: list[str] | None = None, **opts: Any
) -> list[models.Job]:
    module = AGGREGATORS.get(name)
    if module is None:
        raise KeyError(f"unknown job source: {name}")
    return module.fetch(fetcher, queries=queries, **opts)


def reachability_probes() -> dict[str, Callable[[], str]]:
    """Callables for `lgrow doctor --network`, one per aggregator plus ATS slugs."""
    from .. import config as _cfg  # local import to avoid a cycle at module load

    sources = _cfg.load_sources()
    probes: dict[str, Callable[[], str]] = {}

    for name, module in AGGREGATORS.items():
        toggle = sources.aggregators.get(name)
        if toggle is not None and not toggle.enabled:
            continue

        def make(mod: Any = module) -> Callable[[], str]:
            def run() -> str:
                with Fetcher(sources.http) as fetcher:
                    return mod.probe(fetcher)

            return run

        probes[name] = make()

    if sources.ats.enabled:
        board_specs = (
            ("greenhouse", sources.ats.greenhouse, ats.fetch_greenhouse),
            ("lever", sources.ats.lever, ats.fetch_lever),
            ("ashby", sources.ats.ashby, ats.fetch_ashby),
        )
        for board, slugs, fetch_fn in board_specs:
            for slug in slugs:

                def make_ats(
                    fn: Any = fetch_fn, s: str = slug
                ) -> Callable[[], str]:
                    def run() -> str:
                        with Fetcher(sources.http) as fetcher:
                            return f"{len(fn(fetcher, s))} openings"

                    return run

                probes[f"{board}:{slug}"] = make_ats()

    return probes
