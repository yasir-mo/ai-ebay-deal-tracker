from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tracker.models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Decision,
    Listing,
    Verdict,
)
from tracker.store import Store

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_listing(item_id="v1|1|0", **over):
    base = dict(
        item_id=item_id,
        profile_id="a7iii",
        title="Sony A7 III Body",
        condition="Used",
        condition_id=3000,
        buying_option=BuyingOption.FIXED_PRICE,
        price_pence=80_000,
        shipping_pence=500,
        currency="GBP",
        seller_name="seller",
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        end_time=None,
        item_url="https://ebay.co.uk/itm/1",
        bid_count=None,
    )
    base.update(over)
    return Listing(**base)


class TestListings:
    def test_roundtrip(self, store):
        original = make_listing()
        store.upsert_listing(original, NOW)
        loaded = store.get_listing("v1|1|0")
        assert loaded == original

    def test_unknown_shipping_survives_roundtrip_as_none(self, store):
        """Must not come back as 0, which would mean free postage."""
        store.upsert_listing(make_listing(shipping_pence=None), NOW)
        loaded = store.get_listing("v1|1|0")
        assert loaded.shipping_pence is None
        assert loaded.shipping_known is False

    def test_upsert_preserves_first_seen(self, store):
        store.upsert_listing(make_listing(), NOW)
        later = NOW + timedelta(days=2)
        store.upsert_listing(make_listing(price_pence=70_000), later)
        row = store.conn.execute(
            "SELECT first_seen, last_seen, price_pence FROM listings"
        ).fetchone()
        assert row["first_seen"] == NOW.isoformat()
        assert row["last_seen"] == later.isoformat()
        assert row["price_pence"] == 70_000

    def test_missing_listing_is_none(self, store):
        assert store.get_listing("nope") is None

    def test_aware_datetimes_roundtrip(self, store):
        end = NOW + timedelta(hours=5)
        store.upsert_listing(make_listing(end_time=end), NOW)
        assert store.get_listing("v1|1|0").end_time == end


class TestEndgameQuery:
    def test_finds_only_auctions_inside_the_window(self, store):
        store.upsert_listing(
            make_listing("soon", end_time=NOW + timedelta(minutes=10)), NOW
        )
        store.upsert_listing(
            make_listing("later", end_time=NOW + timedelta(hours=6)), NOW
        )
        store.upsert_listing(make_listing("no_end", end_time=None), NOW)

        found = store.ending_between(NOW, NOW + timedelta(minutes=15))
        assert [item.item_id for item in found] == ["soon"]

    def test_results_are_ordered_by_end_time(self, store):
        store.upsert_listing(
            make_listing("b", end_time=NOW + timedelta(minutes=12)), NOW
        )
        store.upsert_listing(
            make_listing("a", end_time=NOW + timedelta(minutes=3)), NOW
        )
        found = store.ending_between(NOW, NOW + timedelta(minutes=15))
        assert [item.item_id for item in found] == ["a", "b"]

    def test_deactivated_items_are_excluded(self, store):
        store.upsert_listing(
            make_listing("gone", end_time=NOW - timedelta(minutes=5)), NOW
        )
        assert store.deactivate_ended(NOW) == 1
        assert store.ending_between(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1)
        ) == []


class TestPriceHistory:
    def test_sample_takes_latest_observation_per_item(self, store):
        """A stale listing observed 48 times a day should count once."""
        store.upsert_listing(make_listing("a"), NOW)
        for hours, price in [(0, 90_000), (1, 85_000), (2, 80_000)]:
            store.record_price(
                make_listing("a", price_pence=price, shipping_pence=0),
                NOW + timedelta(hours=hours),
            )
        store.upsert_listing(make_listing("b"), NOW)
        store.record_price(make_listing("b", price_pence=60_000, shipping_pence=0), NOW)

        sample = store.observation_sample(
            "a7iii", ConditionBucket.USED, NOW + timedelta(hours=3), 30
        )
        assert sorted(sample) == [60_000, 80_000]

    def test_window_excludes_old_observations(self, store):
        store.upsert_listing(make_listing("old"), NOW)
        store.record_price(
            make_listing("old", price_pence=10_000, shipping_pence=0),
            NOW - timedelta(days=60),
        )
        assert store.observation_sample("a7iii", ConditionBucket.USED, NOW, 30) == []

    def test_sample_is_scoped_to_profile_and_bucket(self, store):
        store.upsert_listing(make_listing("used", condition_id=3000), NOW)
        store.record_price(
            make_listing("used", price_pence=80_000, shipping_pence=0), NOW
        )
        store.upsert_listing(make_listing("new", condition_id=1000), NOW)
        store.record_price(
            make_listing("new", price_pence=140_000, shipping_pence=0), NOW
        )

        assert store.observation_sample("a7iii", ConditionBucket.USED, NOW, 30) == [
            80_000
        ]
        assert store.observation_sample("a7iii", ConditionBucket.NEW, NOW, 30) == [
            140_000
        ]

    def test_sample_uses_landed_cost(self, store):
        store.upsert_listing(make_listing("a"), NOW)
        store.record_price(
            make_listing("a", price_pence=80_000, shipping_pence=1_500), NOW
        )
        assert store.observation_sample("a7iii", ConditionBucket.USED, NOW, 30) == [
            81_500
        ]


class TestBaselines:
    def test_roundtrip(self, store):
        b = Baseline("a7iii", ConditionBucket.USED, 100_000, 90_000, 40)
        store.save_baseline(b, NOW)
        assert store.get_baseline("a7iii", ConditionBucket.USED) == b

    def test_upsert_replaces(self, store):
        store.save_baseline(
            Baseline("a7iii", ConditionBucket.USED, 100_000, 90_000, 40), NOW
        )
        store.save_baseline(
            Baseline("a7iii", ConditionBucket.USED, 95_000, 88_000, 45), NOW
        )
        loaded = store.get_baseline("a7iii", ConditionBucket.USED)
        assert loaded.median_pence == 95_000
        assert loaded.sample_n == 45

    def test_missing_is_none(self, store):
        assert store.get_baseline("nope", ConditionBucket.NEW) is None


def make_decision(item_id="v1|1|0", verdict=Verdict.GOOD_DEAL):
    return Decision(
        item_id=item_id,
        profile_id="a7iii",
        verdict=verdict,
        total_pence=80_000,
        baseline_pence=100_000,
        discount_pct=0.2,
        reasons=["discount:20%"],
    )


class TestDecisionsAndSuppression:
    def test_new_decision_is_not_yet_notified(self, store):
        store.record_decision(make_decision(), NOW)
        assert store.already_notified("v1|1|0", Verdict.GOOD_DEAL) is False

    def test_marking_notified_suppresses_repeats(self, store):
        store.record_decision(make_decision(), NOW)
        store.mark_notified("v1|1|0", Verdict.GOOD_DEAL, NOW)
        assert store.already_notified("v1|1|0", Verdict.GOOD_DEAL) is True

    def test_rescoring_does_not_clear_notified_flag(self, store):
        """48 sweeps a day should not mean 48 alerts."""
        store.record_decision(make_decision(), NOW)
        store.mark_notified("v1|1|0", Verdict.GOOD_DEAL, NOW)
        store.record_decision(make_decision(), NOW + timedelta(hours=1))
        assert store.already_notified("v1|1|0", Verdict.GOOD_DEAL) is True

    def test_suppression_is_per_verdict_tier(self, store):
        """A WATCH that becomes a BUY_NOW is new information."""
        store.record_decision(make_decision(verdict=Verdict.WATCH), NOW)
        store.mark_notified("v1|1|0", Verdict.WATCH, NOW)
        store.record_decision(make_decision(verdict=Verdict.BUY_NOW), NOW)
        assert store.already_notified("v1|1|0", Verdict.WATCH) is True
        assert store.already_notified("v1|1|0", Verdict.BUY_NOW) is False

    def test_outcome_can_be_recorded(self, store):
        store.record_decision(make_decision(), NOW)
        store.set_outcome("v1|1|0", Verdict.GOOD_DEAL, "bought")
        row = store.conn.execute("SELECT outcome FROM decisions").fetchone()
        assert row["outcome"] == "bought"

    def test_reasons_survive_as_json(self, store):
        store.record_decision(
            replace(make_decision(), reasons=["discount:35%", "shipping_unknown"]), NOW
        )
        row = store.conn.execute("SELECT reason_json FROM decisions").fetchone()
        assert "shipping_unknown" in row["reason_json"]


class TestReporting:
    def test_counts_by_verdict(self, store):
        store.record_decision(make_decision("a", Verdict.BUY_NOW), NOW)
        store.record_decision(make_decision("b", Verdict.GOOD_DEAL), NOW)
        store.record_decision(make_decision("c", Verdict.GOOD_DEAL), NOW)
        counts = store.counts_since(NOW - timedelta(hours=1))
        assert counts == {"BUY_NOW": 1, "GOOD_DEAL": 2}

    def test_counts_respect_the_window(self, store):
        store.record_decision(make_decision("old"), NOW - timedelta(days=3))
        assert store.counts_since(NOW - timedelta(hours=1)) == {}

    def test_active_listing_count(self, store):
        store.upsert_listing(make_listing("a"), NOW)
        store.upsert_listing(
            make_listing("b", end_time=NOW - timedelta(hours=1)), NOW
        )
        store.deactivate_ended(NOW)
        assert store.active_listing_count() == 1
