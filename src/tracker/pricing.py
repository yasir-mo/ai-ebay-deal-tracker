"""Baselines derived from the tracker's own accumulated observations.

This is a baseline of asking prices, not of sold prices. eBay's public Browse
API returns active listings only; sold prices need the Marketplace Insights
API, which requires separate approval. What is computed here is what the item
is currently being asked for, which is at least the right comparison for
another asking price.
"""
from __future__ import annotations

import statistics

from .models import Baseline, ConditionBucket, Profile

#: Below this many observations a median is noise, not a baseline.
MIN_SAMPLES = 15

#: How far back observations stay relevant. Long enough to accumulate a sample
#: on slow-moving profiles, short enough that a year-old price does not anchor
#: a market that has since moved.
WINDOW_DAYS = 30

#: Trim this fraction from each tail before taking the median, removing both
#: the obvious scams and the wildly optimistic prices.
TRIM = 0.10


def _trimmed(values: list[int]) -> list[int]:
    if len(values) < 10:
        return sorted(values)
    ordered = sorted(values)
    cut = int(len(ordered) * TRIM)
    return ordered[cut : len(ordered) - cut] or ordered


def compute_baseline(
    profile: Profile, bucket: ConditionBucket, sample: list[int]
) -> Baseline | None:
    """Build a baseline from observed prices, or fall back to the manual target.

    Returns None when there is neither enough data nor a configured target,
    in which case scoring declines to judge rather than guessing.
    """
    if len(sample) >= MIN_SAMPLES:
        trimmed = _trimmed(sample)
        return Baseline(
            profile_id=profile.id,
            bucket=bucket,
            median_pence=int(statistics.median(trimmed)),
            p25_pence=int(_percentile(trimmed, 0.25)),
            sample_n=len(sample),
            provisional=False,
        )

    if profile.target_pence:
        # Cold start: the user's own estimate stands in until real data lands.
        return Baseline(
            profile_id=profile.id,
            bucket=bucket,
            median_pence=profile.target_pence,
            p25_pence=profile.target_pence,
            sample_n=len(sample),
            provisional=True,
        )

    return None


def _percentile(ordered: list[int], q: float) -> float:
    """Linear-interpolated percentile over an already-sorted list."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac
