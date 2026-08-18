"""Raw eBay Browse API JSON -> Listing.

Kept separate from the HTTP client so it can be tested against recorded
fixtures without a network, and so a change in eBay's response shape has
exactly one place to be fixed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import BuyingOption, Listing

log = logging.getLogger(__name__)


class SkipListing(Exception):
    """Raised when a raw item cannot be turned into a usable Listing."""


def to_pence(amount: str | int | float | None) -> int | None:
    """Parse eBay's string money into integer minor units.

    Decimal, not float: '19.99' through float rounds to 1998 pence often
    enough to matter when it decides whether something crosses a threshold.
    """
    if amount is None:
        return None
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def parse_end_time(raw: str | None) -> datetime | None:
    """eBay returns ISO-8601 with a trailing Z, which older Python cannot parse."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _buying_option(raw: dict) -> BuyingOption:
    options = raw.get("buyingOptions") or []
    if "AUCTION" in options:
        return BuyingOption.AUCTION
    return BuyingOption.FIXED_PRICE


def _price_pence(raw: dict, option: BuyingOption) -> int | None:
    """For an auction the meaningful number is the current bid, not `price`."""
    if option is BuyingOption.AUCTION:
        bid = to_pence((raw.get("currentBidPrice") or {}).get("value"))
        if bid is not None:
            return bid
    return to_pence((raw.get("price") or {}).get("value"))


def _shipping_pence(raw: dict) -> int | None:
    """None means unknown, which is not the same as free.

    Calculated shipping and collection only listings omit the cost entirely.
    Defaulting those to zero would treat a cheap item with expensive postage
    as a bargain.
    """
    options = raw.get("shippingOptions") or []
    if not options:
        return None
    costs = [
        to_pence((opt.get("shippingCost") or {}).get("value"))
        for opt in options
    ]
    costs = [c for c in costs if c is not None]
    return min(costs) if costs else None


def _condition_id(raw: dict) -> int | None:
    value = raw.get("conditionId")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seller(raw: dict) -> tuple[str | None, float | None, int | None]:
    seller = raw.get("seller") or {}
    pct = seller.get("feedbackPercentage")
    try:
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct = None
    score = seller.get("feedbackScore")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return seller.get("username"), pct, score


def normalise(raw: dict, profile_id: str, expected_currency: str) -> Listing:
    """Convert one item_summary entry. Raises SkipListing if unusable."""
    item_id = raw.get("itemId")
    if not item_id:
        raise SkipListing("missing itemId")

    option = _buying_option(raw)
    price = _price_pence(raw, option)
    if price is None:
        raise SkipListing(f"{item_id}: unparseable price")

    currency = (raw.get("price") or {}).get("currency") or expected_currency
    if currency != expected_currency:
        # Cross-currency comparison against a same-currency baseline would be
        # meaningless, and we have no FX source.
        raise SkipListing(f"{item_id}: currency {currency} != {expected_currency}")

    username, pct, score = _seller(raw)

    return Listing(
        item_id=item_id,
        profile_id=profile_id,
        title=raw.get("title") or "",
        condition=raw.get("condition"),
        condition_id=_condition_id(raw),
        buying_option=option,
        price_pence=price,
        shipping_pence=_shipping_pence(raw),
        currency=currency,
        seller_name=username,
        seller_feedback_pct=pct,
        seller_feedback_score=score,
        end_time=parse_end_time(raw.get("itemEndDate")),
        item_url=raw.get("itemWebUrl") or "",
        bid_count=raw.get("bidCount"),
    )


def normalise_all(
    items: list[dict], profile_id: str, expected_currency: str
) -> list[Listing]:
    """Normalise a page of results. A bad item is dropped, never fatal."""
    out = []
    for raw in items:
        try:
            out.append(normalise(raw, profile_id, expected_currency))
        except SkipListing as exc:
            log.debug("skipped listing: %s", exc)
    return out
