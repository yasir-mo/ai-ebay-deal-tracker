import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracker.models import BuyingOption, ConditionBucket
from tracker.normalise import (
    SkipListing,
    normalise,
    normalise_all,
    parse_end_time,
    to_pence,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "search_response.json").read_text(
        encoding="utf-8"
    )
)
ITEMS = FIXTURE["itemSummaries"]


def by_id(item_id):
    return next(i for i in ITEMS if i.get("itemId") == item_id)


class TestToPence:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("849.00", 84_900),
            ("0.00", 0),
            ("19.99", 1_999),
            ("1050", 105_000),
            ("0.1", 10),
            (None, None),
            ("not a number", None),
        ],
    )
    def test_parses(self, raw, expected):
        assert to_pence(raw) == expected

    def test_uses_decimal_not_float(self):
        """1.15 * 100 in binary float is 114.999..., which truncates to 114."""
        assert to_pence("1.15") == 115
        assert to_pence("2.675") == 268


class TestParseEndTime:
    def test_parses_trailing_z(self):
        assert parse_end_time("2026-08-18T15:30:00.000Z") == datetime(
            2026, 8, 18, 15, 30, tzinfo=timezone.utc
        )

    def test_result_is_always_aware(self):
        assert parse_end_time("2026-08-18T15:30:00").tzinfo is not None

    @pytest.mark.parametrize("raw", [None, "", "garbage"])
    def test_bad_input_is_none(self, raw):
        assert parse_end_time(raw) is None


class TestNormaliseFixedPrice:
    def setup_method(self):
        self.listing = normalise(by_id("v1|285912345678|0"), "a7iii", "GBP")

    def test_price_and_shipping(self):
        assert self.listing.price_pence == 84_900
        assert self.listing.shipping_pence == 499
        assert self.listing.total_pence == 85_399

    def test_buying_option(self):
        assert self.listing.buying_option is BuyingOption.FIXED_PRICE

    def test_seller(self):
        assert self.listing.seller_name == "camera_world_uk"
        assert self.listing.seller_feedback_pct == pytest.approx(99.4)
        assert self.listing.seller_feedback_score == 18_422

    def test_condition_bucket(self):
        assert self.listing.bucket is ConditionBucket.USED


class TestNormaliseAuction:
    def setup_method(self):
        self.listing = normalise(by_id("v1|166012345679|0"), "a7iii", "GBP")

    def test_uses_current_bid_not_buy_it_now_price(self):
        """`price` is 1050 but the live bid is 610, and the bid is what matters."""
        assert self.listing.price_pence == 61_000

    def test_end_time_parsed(self):
        assert self.listing.end_time == datetime(
            2026, 8, 18, 15, 30, tzinfo=timezone.utc
        )

    def test_bid_count(self):
        assert self.listing.bid_count == 14

    def test_buying_option(self):
        assert self.listing.buying_option is BuyingOption.AUCTION


class TestShippingEdgeCases:
    def test_absent_shipping_options_is_unknown_not_free(self):
        listing = normalise(by_id("v1|186012345680|0"), "a7iii", "GBP")
        assert listing.shipping_pence is None
        assert listing.shipping_known is False

    def test_multiple_options_takes_cheapest(self):
        listing = normalise(by_id("v1|196012345681|0"), "a7iii", "GBP")
        assert listing.shipping_pence == 0

    def test_free_shipping_is_zero_not_unknown(self):
        listing = normalise(by_id("v1|226012345684|0"), "a7iii", "GBP")
        assert listing.shipping_pence == 0
        assert listing.shipping_known is True


class TestConditionBuckets:
    @pytest.mark.parametrize(
        "item_id,bucket",
        [
            ("v1|196012345681|0", ConditionBucket.NEW),
            ("v1|226012345684|0", ConditionBucket.REFURB),
            ("v1|285912345678|0", ConditionBucket.USED),
            ("v1|206012345682|0", ConditionBucket.USED),
        ],
    )
    def test_bucketing(self, item_id, bucket):
        assert normalise(by_id(item_id), "a7iii", "GBP").bucket is bucket


class TestRejections:
    def test_missing_item_id_is_skipped(self):
        bad = next(i for i in ITEMS if "itemId" not in i)
        with pytest.raises(SkipListing, match="itemId"):
            normalise(bad, "a7iii", "GBP")

    def test_foreign_currency_is_skipped(self):
        """With no FX rate, a USD price cannot be judged against a GBP baseline."""
        with pytest.raises(SkipListing, match="currency"):
            normalise(by_id("v1|216012345683|0"), "a7iii", "GBP")


class TestNormaliseAll:
    def test_drops_bad_items_without_failing_the_batch(self):
        listings = normalise_all(ITEMS, "a7iii", "GBP")
        # 8 raw items, minus the USD one and the one with no itemId.
        assert len(listings) == 6
        assert all(item.currency == "GBP" for item in listings)

    def test_every_listing_carries_the_profile_id(self):
        listings = normalise_all(ITEMS, "a7iii", "GBP")
        assert {item.profile_id for item in listings} == {"a7iii"}
