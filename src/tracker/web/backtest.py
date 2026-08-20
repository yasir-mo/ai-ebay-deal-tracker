"""Re-score stored listings under candidate thresholds.

This is the point of keeping scoring.py pure and I/O-free: the settings screen
can answer "what would these numbers actually have done" before anything is
committed. Tuning thresholds blind is how you end up either spammed or silent.
"""
from __future__ import annotations

from datetime import datetime

from ..models import Verdict
from ..scoring import Thresholds, score
from ..store import Store


def run(
    store: Store,
    current: Thresholds,
    candidate: Thresholds,
    now: datetime,
    days: int = 30,
) -> dict:
    """Compare verdict counts under two threshold sets over stored listings.

    Baselines are read as they stand today rather than reconstructed for each
    historical moment, so this is an approximation: it answers "what would
    these numbers do to the listings I have" and not "what would my alert
    history have looked like".
    """
    listings = store.recent_listings(days, now)
    profiles = {p.id: p for p in store.list_profiles()}

    counts = {
        v.value: {"current": 0, "candidate": 0}
        for v in sorted(Verdict, key=lambda x: -x.rank)
    }

    scored = 0
    for listing in listings:
        profile = profiles.get(listing.profile_id)
        if profile is None:
            continue
        baseline = store.get_baseline(listing.profile_id, listing.bucket)
        if baseline is None:
            continue

        now_verdict = score(listing, profile, baseline, now, current).verdict
        cand_verdict = score(listing, profile, baseline, now, candidate).verdict
        counts[now_verdict.value]["current"] += 1
        counts[cand_verdict.value]["candidate"] += 1
        scored += 1

    return {"listings": scored, "days": days, "verdicts": counts}
