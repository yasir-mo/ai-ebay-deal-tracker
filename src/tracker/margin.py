"""Estimated resale margin.

Arithmetic on data the tracker already holds. Deliberately conservative: the
baseline is an *asking* price, and things generally sell for less than they are
listed at, so the estimate discounts it before subtracting fees.

Nothing here is a promise about what an item will actually fetch. It is a
ranking signal for which deals are worth looking at first.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Baseline, Listing

#: eBay UK final value fee for most categories, plus the fixed per-order charge.
DEFAULT_FEE_RATE = 0.128
DEFAULT_FIXED_FEE_PENCE = 30

#: Fraction of the observed asking baseline a resale is assumed to realise.
#: Asking prices are not sold prices; assuming otherwise inflates every margin.
DEFAULT_REALISATION = 0.90

#: What it costs to post the item on to a buyer.
DEFAULT_OUTBOUND_SHIPPING_PENCE = 500


@dataclass(frozen=True)
class MarginEstimate:
    resale_pence: int
    fees_pence: int
    outbound_shipping_pence: int
    cost_pence: int
    margin_pence: int
    margin_pct: float
    provisional: bool
    """True when derived from a manual target rather than observed history."""

    @property
    def profitable(self) -> bool:
        return self.margin_pence > 0


@dataclass(frozen=True)
class MarginConfig:
    fee_rate: float = DEFAULT_FEE_RATE
    fixed_fee_pence: int = DEFAULT_FIXED_FEE_PENCE
    realisation: float = DEFAULT_REALISATION
    outbound_shipping_pence: int = DEFAULT_OUTBOUND_SHIPPING_PENCE

    #: Thresholds for a priority alert. Both must clear.
    min_margin_pence: int = 5_000
    min_margin_pct: float = 0.25


def estimate(
    listing: Listing, baseline: Baseline, config: MarginConfig | None = None
) -> MarginEstimate:
    cfg = config or MarginConfig()

    resale = int(baseline.median_pence * cfg.realisation)
    fees = int(resale * cfg.fee_rate) + cfg.fixed_fee_pence
    cost = listing.total_pence
    margin = resale - fees - cfg.outbound_shipping_pence - cost

    return MarginEstimate(
        resale_pence=resale,
        fees_pence=fees,
        outbound_shipping_pence=cfg.outbound_shipping_pence,
        cost_pence=cost,
        margin_pence=margin,
        margin_pct=(margin / cost) if cost > 0 else 0.0,
        provisional=baseline.provisional,
    )


def qualifies_for_priority(
    est: MarginEstimate, config: MarginConfig | None = None
) -> bool:
    """Whether the margin alone justifies a priority alert.

    A provisional baseline never qualifies. Calling something arbitrage when
    the market price is a number the user typed in themselves is how this tool
    would lose them money.
    """
    cfg = config or MarginConfig()
    if est.provisional:
        return False
    return (
        est.margin_pence >= cfg.min_margin_pence
        and est.margin_pct >= cfg.min_margin_pct
    )
