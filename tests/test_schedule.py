"""Slot picking: correct days, one per day, jittered, never in the past."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from lgrow import config
from lgrow.posts import schedule as sched


def voice(**overrides) -> config.Voice:
    data = {
        "cadence": {
            "posts_per_week": 3,
            "days": ["tuesday", "wednesday", "thursday"],
            "slots": ["08:15", "12:30"],
            "jitter_minutes": 20,
            "max_per_day": 1,
        },
        "pillars": [{"name": "x", "weight": 100}],
    }
    data.update(overrides)
    return config.Voice.model_validate(data)


# A Monday, so the next valid day is the following Tuesday.
MONDAY = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


class TestDaySelection:
    def test_only_configured_weekdays(self):
        slots = sched.next_slots(6, voice=voice(), now=MONDAY, rng=random.Random(1))
        assert slots
        for slot in slots:
            assert slot.weekday() in (1, 2, 3), slot.strftime("%A")

    def test_one_post_per_day(self):
        slots = sched.next_slots(6, voice=voice(), now=MONDAY, rng=random.Random(1))
        dates = [s.date() for s in slots]
        assert len(dates) == len(set(dates))

    def test_requested_count_returned(self):
        slots = sched.next_slots(3, voice=voice(), now=MONDAY, rng=random.Random(2))
        assert len(slots) == 3

    def test_chronological(self):
        slots = sched.next_slots(5, voice=voice(), now=MONDAY, rng=random.Random(3))
        assert slots == sorted(slots)


class TestConstraints:
    def test_never_in_the_past(self):
        slots = sched.next_slots(3, voice=voice(), now=MONDAY, rng=random.Random(4))
        for slot in slots:
            assert slot > MONDAY

    def test_taken_dates_are_skipped(self):
        first = sched.next_slots(1, voice=voice(), now=MONDAY, rng=random.Random(5))[0]
        again = sched.next_slots(
            1, voice=voice(), now=MONDAY, taken=[first.isoformat()],
            rng=random.Random(5),
        )[0]
        assert again.date() != first.date()

    def test_jitter_within_bounds_and_actually_varies(self):
        seen: set[int] = set()
        for seed in range(25):
            slot = sched.next_slots(
                1, voice=voice(), now=MONDAY, rng=random.Random(seed)
            )[0]
            local = slot.astimezone(sched._local_tz())
            minutes = local.hour * 60 + local.minute
            base_candidates = [8 * 60 + 15, 12 * 60 + 30]
            assert min(abs(minutes - b) for b in base_candidates) <= 20
            seen.add(minutes)
        # A fixed publish time is a machine signature; jitter must be real.
        assert len(seen) > 5

    def test_empty_days_config_falls_back(self):
        slots = sched.next_slots(
            1, voice=voice(cadence={"days": [], "slots": ["09:00"], "jitter_minutes": 0}),
            now=MONDAY, rng=random.Random(6),
        )
        assert slots
        assert slots[0].weekday() in (1, 2, 3)

    def test_late_in_the_day_skips_to_next_valid_day(self):
        # 23:00 Tuesday — that day's slots have passed.
        tuesday_late = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
        slot = sched.next_slots(
            1, voice=voice(), now=tuesday_late, rng=random.Random(7)
        )[0]
        assert slot > tuesday_late


class TestDescribe:
    def test_human_readable(self):
        slot = MONDAY + timedelta(days=1)
        text = sched.describe(slot)
        assert "Tue" in text or "Wed" in text
