"""Deterministic deal scoring.

Pure functions only - no I/O, no clock reads except the `now` argument. This is
the module that gets tuned constantly and where a bug costs real money, so it
must be fully testable from fixtures. A future LLM stage slots in *behind*
these rules, judging only what survives them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Baseline, BuyingOption, Decision, Listing, Profile, Verdict

#: Titles matching any of these are rejected outright. Ordered roughly by how
#: often they show up in practice.
BLOCKLIST = (
    "for parts",
    "spares or repair",
    "spares/repair",
    "not working",
    "faulty",
    "broken",
    "case only",
    "box only",
    "empty box",
    "read description",
    "read full description",
    "replacement lens only",
    "no return",
)

_BLOCK_RE = re.compile("|".join(re.escape(p) for p in BLOCKLIST), re.IGNORECASE)

#: Defaults. These are the numbers the tracker ships with; the UI can override
#: them per run by passing a Thresholds instance to score().
BUY_NOW_DISCOUNT = 0.35
GOOD_DEAL_DISCOUNT = 0.20
WATCH_DISCOUNT = 0.30
WATCH_HORIZON = timedelta(hours=24)

#: Below this fraction of baseline a listing is implausible. Far too cheap is
#: usually a scam or an accessory listing rather than a bargain.
SCAM_FLOOR = 0.15


@dataclass(frozen=True)
class Thresholds:
    """Tunable scoring numbers, injected rather than read from globals.

    Passing these in is what lets the settings screen re-score stored listings
    under candidate values and report what would have alerted, before anything
    is committed. Tuning blind is how you end up either spammed or silent.
    """

    buy_now_discount: float = BUY_NOW_DISCOUNT
    good_deal_discount: float = GOOD_DEAL_DISCOUNT
    watch_discount: float = WATCH_DISCOUNT
    scam_floor: float = SCAM_FLOOR
    watch_horizon_hours: int = 24

    @property
    def watch_horizon(self) -> timedelta:
        return timedelta(hours=self.watch_horizon_hours)


DEFAULT_THRESHOLDS = Thresholds()


def discount_pct(total_pence: int, baseline_pence: int) -> float:
    """Fraction below baseline, in [0, 1]. Negative when above baseline."""
    if baseline_pence <= 0:
        return 0.0
    return (baseline_pence - total_pence) / baseline_pence


def _hard_rejects(
    listing: Listing,
    profile: Profile,
    baseline: Baseline | None,
    thresholds: Thresholds,
) -> list[str]:
    reasons: list[str] = []

    match = _BLOCK_RE.search(listing.title)
    if match:
        reasons.append(f"blocklist:{match.group(0).lower()}")

    if (
        listing.seller_feedback_pct is not None
        and listing.seller_feedback_pct < profile.min_feedback_pct
    ):
        reasons.append(
            f"seller_pct:{listing.seller_feedback_pct:.1f}<{profile.min_feedback_pct:.1f}"
        )

    if (
        listing.seller_feedback_score is not None
        and listing.seller_feedback_score < profile.min_feedback_count
    ):
        reasons.append(
            f"seller_count:{listing.seller_feedback_score}<{profile.min_feedback_count}"
        )

    if listing.total_pence > profile.ceiling_pence:
        reasons.append(f"over_ceiling:{listing.total_pence}>{profile.ceiling_pence}")

    if (
        baseline is not None
        and listing.total_pence < baseline.median_pence * thresholds.scam_floor
    ):
        reasons.append("implausibly_cheap")

    return reasons


def score(
    listing: Listing,
    profile: Profile,
    baseline: Baseline | None,
    now: datetime,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Classify one listing. `now` is injected so tests need no clock control."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    reasons = _hard_rejects(listing, profile, baseline, thresholds)
    if reasons:
        return Decision(
            item_id=listing.item_id,
            profile_id=profile.id,
            verdict=Verdict.SKIP,
            total_pence=listing.total_pence,
            baseline_pence=baseline.median_pence if baseline else None,
            discount_pct=None,
            reasons=reasons,
        )

    if baseline is None:
        return Decision(
            item_id=listing.item_id,
            profile_id=profile.id,
            verdict=Verdict.SKIP,
            total_pence=listing.total_pence,
            baseline_pence=None,
            discount_pct=None,
            reasons=["no_baseline"],
        )

    disc = discount_pct(listing.total_pence, baseline.median_pence)
    verdict = Verdict.SKIP
    reasons = [f"discount:{disc:.0%}"]
    if baseline.provisional:
        reasons.append("provisional_baseline")

    is_auction = listing.buying_option is BuyingOption.AUCTION

    if not is_auction and disc >= thresholds.buy_now_discount:
        verdict = Verdict.BUY_NOW
    elif disc >= thresholds.good_deal_discount and not is_auction:
        verdict = Verdict.GOOD_DEAL
    elif is_auction and disc >= thresholds.watch_discount:
        # An auction's current price is not its final price, so it never
        # earns BUY_NOW - only a WATCH, and only if it ends soon enough
        # to be actionable.
        if (
            listing.end_time is not None
            and listing.end_time - now <= thresholds.watch_horizon
        ):
            verdict = Verdict.WATCH
            reasons.append(f"ends_in:{_fmt_delta(listing.end_time - now)}")
        else:
            reasons.append("ends_too_far_out")
    elif is_auction and disc >= thresholds.good_deal_discount:
        reasons.append("auction_below_watch_threshold")

    if not listing.shipping_known and verdict is not Verdict.SKIP:
        # `total_pence` is only a lower bound here, so the strongest verdict
        # is not defensible. Downgrade rather than suppress: the listing may
        # still be worth a look, the user just has to check postage.
        reasons.append("shipping_unknown")
        if verdict is Verdict.BUY_NOW:
            verdict = Verdict.GOOD_DEAL

    return Decision(
        item_id=listing.item_id,
        profile_id=profile.id,
        verdict=verdict,
        total_pence=listing.total_pence,
        baseline_pence=baseline.median_pence,
        discount_pct=disc,
        reasons=reasons,
    )


def _fmt_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        return "ended"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"
