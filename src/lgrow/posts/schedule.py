"""Slot picking for approved posts.

Three rules, all from voice.yaml: only on the configured weekdays, at most one
post per day, and every time jittered by a few minutes. The jitter matters — a
post landing at exactly 08:15:00 every Tuesday is a machine signature, and the
whole point of this project is that nothing about it looks automated.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .. import config

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

SEARCH_HORIZON_DAYS = 60


def _local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _parse_slot(slot: str) -> tuple[int, int]:
    hour_str, _, minute_str = slot.partition(":")
    return int(hour_str), int(minute_str or 0)


def _taken_local_dates(taken_iso: list[str]) -> set[object]:
    tz = _local_tz()
    dates = set()
    for iso in taken_iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dates.add(dt.astimezone(tz).date())
    return dates


def next_slots(
    count: int,
    *,
    voice: config.Voice | None = None,
    taken: list[str] | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> list[datetime]:
    """The next `count` free publishing slots, as timezone-aware datetimes."""
    voice = voice or config.load_voice()
    cadence = voice.cadence
    rng = rng or random.Random()
    tz = _local_tz()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)

    wanted_days = {
        _WEEKDAYS[d.strip().lower()]
        for d in cadence.days
        if d.strip().lower() in _WEEKDAYS
    }
    if not wanted_days:
        wanted_days = {1, 2, 3}  # sane default: Tue/Wed/Thu

    slots = [_parse_slot(s) for s in cadence.slots] or [(8, 15)]
    busy = _taken_local_dates(taken or [])

    chosen: list[datetime] = []
    day = now.date()
    for offset in range(SEARCH_HORIZON_DAYS):
        if len(chosen) >= count:
            break
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() not in wanted_days:
            continue
        if candidate_day in busy:
            continue

        hour, minute = slots[len(chosen) % len(slots)]
        jitter = rng.randint(-cadence.jitter_minutes, cadence.jitter_minutes)
        when = datetime(
            candidate_day.year, candidate_day.month, candidate_day.day,
            hour, minute, tzinfo=tz,
        ) + timedelta(minutes=jitter)

        # Never schedule into the past, and leave a little breathing room.
        if when <= now + timedelta(minutes=5):
            continue

        chosen.append(when)
        busy.add(candidate_day)

    return chosen


def describe(when: datetime) -> str:
    local = when.astimezone(_local_tz())
    return local.strftime("%a %d %b at %H:%M %Z").strip()
