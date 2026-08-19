"""Prompt construction for the judging stage.

The system prompt is deliberately stable across every request: it carries no
timestamps, no per-listing detail, and no per-run identifiers. That is what
makes it cacheable, and the cache is the single largest cost lever here after
batching. Anything that varies per sweep belongs in the user turn.
"""
from __future__ import annotations

import json

from ..margin import MarginEstimate
from ..models import Listing, Profile

SYSTEM_PROMPT = """\
You screen second-hand eBay listings that have already passed a deterministic \
filter on price, seller feedback, and title blocklist. Your job is the part \
those rules cannot do: reading the listing and deciding whether it is really \
what it claims to be, and whether it is worth a human's attention.

You are the last check before a human is interrupted, so a false promote costs \
more than a false keep.

For each listing, decide three things.

First, is this the target item. The single most common failure in this data is \
an accessory, a spare part, a protective case, a manual, an empty box, or a \
different model that merely mentions the target in its title. A price far below \
market is far more often one of these than a genuine bargain. Judge from the \
title and description, not from the price being attractive.

Second, what condition risk the description implies relative to the stated \
condition. Listings that state a good condition but describe damage, or that \
are conspicuously vague for the money being asked, carry real risk. A thin \
description on an expensive item is itself a signal. Mark that 'undisclosed' \
rather than assuming the best.

Third, how confident you are it could be resold near the going rate. Weigh \
completeness, whether accessories are included, condition, and how specific and \
verifiable the listing is.

Then give a verdict.

Reject when it is not the target item, or when there is major or undisclosed \
condition risk. Promote only when it is clearly the target item, condition risk \
is none or minor, and resale confidence is high. Keep for everything else.

Two things to hold onto. A large discount is not evidence in favour of a \
listing; it is the reason the listing reached you, and it is equally consistent \
with the item being wrong. And the market baseline you are shown is an average \
of current asking prices, not of completed sales, so treat it as a rough guide \
rather than a valuation.

Be concrete. A concern like "check whether the battery is included" is useful; \
"buyer should do their own research" is not. If there are no real concerns, \
return none rather than inventing some.\
"""


def render_listing(
    listing: Listing,
    profile: Profile,
    baseline_pence: int | None,
    discount_pct: float | None,
    margin: MarginEstimate | None,
    description: str | None = None,
) -> dict:
    """One listing as a compact JSON-serialisable record.

    Money is rendered in pounds here purely because the model reasons about
    prices more reliably in the units a human would use.
    """
    record: dict = {
        "item_id": listing.item_id,
        "searching_for": profile.name,
        "title": listing.title,
        "stated_condition": listing.condition,
        "listing_type": listing.buying_option.value,
        "price": round(listing.price_pence / 100, 2),
        "postage": (
            round(listing.shipping_pence / 100, 2)
            if listing.shipping_known
            else "not stated"
        ),
        "total_cost": round(listing.total_pence / 100, 2),
    }

    if baseline_pence:
        record["market_asking_average"] = round(baseline_pence / 100, 2)
    if discount_pct is not None:
        record["percent_below_market"] = round(discount_pct * 100)
    if listing.seller_feedback_pct is not None:
        record["seller_feedback_percent"] = listing.seller_feedback_pct
    if listing.seller_feedback_score is not None:
        record["seller_feedback_count"] = listing.seller_feedback_score
    if listing.bid_count is not None:
        record["bid_count"] = listing.bid_count
    if margin is not None:
        record["estimated_resale_margin"] = round(margin.margin_pence / 100, 2)
    if description:
        record["description"] = description[:1500]

    return record


def render_batch(records: list[dict]) -> str:
    """The user turn. Volatile content only, so the cached prefix survives."""
    return (
        "Judge each of the following listings. Return exactly one judgement per "
        "listing, using the item_id exactly as given.\n\n"
        + json.dumps(records, indent=2, ensure_ascii=False)
    )
