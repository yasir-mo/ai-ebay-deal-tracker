from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tracker.models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Listing,
    Profile,
    Verdict,
)
from tracker.scoring import SCAM_FLOOR, discount_pct, score

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

PROFILE = Profile(
    id="a7iii",
    name="Sony A7 III",
    query="sony a7 iii body",
    ceiling_pence=150_000,
    min_feedback_pct=95.0,
    min_feedback_count=10,
)

BASELINE = Baseline(
    profile_id="a7iii",
    bucket=ConditionBucket.USED,
    median_pence=100_000,
    p25_pence=90_000,
    sample_n=40,
)


def make_listing(**over):
    base = dict(
        item_id="v1|123|0",
        profile_id="a7iii",
        title="Sony A7 III Body Excellent Condition",
        condition="Used",
        condition_id=3000,
        buying_option=BuyingOption.FIXED_PRICE,
        price_pence=80_000,
        shipping_pence=0,
        currency="GBP",
        seller_name="camerashop",
        seller_feedback_pct=99.2,
        seller_feedback_score=1400,
        end_time=None,
        item_url="https://ebay.co.uk/itm/123",
    )
    base.update(over)
    return Listing(**base)


class TestDiscount:
    def test_below_baseline_is_positive(self):
        assert discount_pct(80_000, 100_000) == pytest.approx(0.20)

    def test_above_baseline_is_negative(self):
        assert discount_pct(120_000, 100_000) == pytest.approx(-0.20)

    def test_zero_baseline_does_not_divide_by_zero(self):
        assert discount_pct(500, 0) == 0.0


class TestLandedCost:
    def test_shipping_counts_toward_total(self):
        listing = make_listing(price_pence=100, shipping_pence=4_000)
        assert listing.total_pence == 4_100

    def test_cheap_item_with_gouging_postage_is_not_a_deal(self):
        """Cheap item, expensive postage, so not actually a deal."""
        listing = make_listing(price_pence=100, shipping_pence=79_900)
        d = score(listing, PROFILE, BASELINE, NOW)
        assert d.total_pence == 80_000
        assert d.discount_pct == pytest.approx(0.20)
        assert d.verdict is Verdict.GOOD_DEAL


class TestHardRejects:
    @pytest.mark.parametrize(
        "title",
        [
            "Sony A7 III FOR PARTS not working",
            "Sony A7 III spares or repair",
            "Sony A7 III Body - READ DESCRIPTION",
            "Sony A7 III box only",
            "Empty Box for Sony A7 III",
        ],
    )
    def test_blocklisted_titles_are_skipped(self, title):
        d = score(make_listing(title=title, price_pence=10_000), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP
        assert any(r.startswith("blocklist:") for r in d.reasons)

    def test_blocklist_is_case_insensitive(self):
        d = score(make_listing(title="sony a7 iii FoR PaRtS"), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP

    def test_low_feedback_pct_rejected(self):
        d = score(make_listing(seller_feedback_pct=88.0), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP
        assert any(r.startswith("seller_pct:") for r in d.reasons)

    def test_low_feedback_count_rejected(self):
        d = score(make_listing(seller_feedback_score=3), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP
        assert any(r.startswith("seller_count:") for r in d.reasons)

    def test_over_ceiling_rejected_even_if_below_baseline(self):
        pricey = replace(PROFILE, ceiling_pence=50_000)
        d = score(make_listing(price_pence=80_000), pricey, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP
        assert any(r.startswith("over_ceiling:") for r in d.reasons)

    def test_implausibly_cheap_is_a_scam_signal_not_a_deal(self):
        too_good = int(BASELINE.median_pence * SCAM_FLOOR) - 1
        d = score(make_listing(price_pence=too_good), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP
        assert "implausibly_cheap" in d.reasons

    def test_missing_seller_feedback_does_not_reject(self):
        """Absent data is not disqualifying data."""
        d = score(
            make_listing(seller_feedback_pct=None, seller_feedback_score=None),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert d.verdict is Verdict.GOOD_DEAL


class TestFixedPriceVerdicts:
    def test_deep_discount_is_buy_now(self):
        d = score(make_listing(price_pence=60_000), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.BUY_NOW

    def test_exactly_at_buy_now_threshold(self):
        d = score(make_listing(price_pence=65_000), PROFILE, BASELINE, NOW)
        assert d.discount_pct == pytest.approx(0.35)
        assert d.verdict is Verdict.BUY_NOW

    def test_moderate_discount_is_good_deal(self):
        d = score(make_listing(price_pence=75_000), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.GOOD_DEAL

    def test_exactly_at_good_deal_threshold(self):
        d = score(make_listing(price_pence=80_000), PROFILE, BASELINE, NOW)
        assert d.discount_pct == pytest.approx(0.20)
        assert d.verdict is Verdict.GOOD_DEAL

    def test_just_under_threshold_is_skip(self):
        d = score(make_listing(price_pence=80_001), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP

    def test_at_market_price_is_skip(self):
        d = score(make_listing(price_pence=100_000), PROFILE, BASELINE, NOW)
        assert d.verdict is Verdict.SKIP


class TestAuctionVerdicts:
    def test_auction_ending_soon_with_big_gap_is_watch(self):
        d = score(
            make_listing(
                buying_option=BuyingOption.AUCTION,
                price_pence=60_000,
                end_time=NOW + timedelta(hours=3),
            ),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert d.verdict is Verdict.WATCH
        assert any(r.startswith("ends_in:") for r in d.reasons)

    def test_auction_never_earns_buy_now(self):
        """The current bid is not the final price, however cheap it looks."""
        d = score(
            make_listing(
                buying_option=BuyingOption.AUCTION,
                price_pence=30_000,
                end_time=NOW + timedelta(minutes=20),
            ),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert d.verdict is Verdict.WATCH

    def test_auction_ending_far_out_is_skipped(self):
        d = score(
            make_listing(
                buying_option=BuyingOption.AUCTION,
                price_pence=60_000,
                end_time=NOW + timedelta(days=5),
            ),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert d.verdict is Verdict.SKIP
        assert "ends_too_far_out" in d.reasons

    def test_auction_with_no_end_time_is_skipped(self):
        d = score(
            make_listing(
                buying_option=BuyingOption.AUCTION,
                price_pence=60_000,
                end_time=None,
            ),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert d.verdict is Verdict.SKIP

    def test_auction_needs_bigger_gap_than_fixed_price(self):
        """25% off is a GOOD_DEAL fixed-price but not enough for an auction."""
        kw = dict(price_pence=75_000, end_time=NOW + timedelta(hours=2))
        fixed = score(make_listing(**kw), PROFILE, BASELINE, NOW)
        auction = score(
            make_listing(buying_option=BuyingOption.AUCTION, **kw),
            PROFILE,
            BASELINE,
            NOW,
        )
        assert fixed.verdict is Verdict.GOOD_DEAL
        assert auction.verdict is Verdict.SKIP
        assert "auction_below_watch_threshold" in auction.reasons


class TestBaselineHandling:
    def test_no_baseline_means_no_verdict(self):
        d = score(make_listing(price_pence=1_000), PROFILE, None, NOW)
        assert d.verdict is Verdict.SKIP
        assert d.reasons == ["no_baseline"]

    def test_provisional_baseline_is_flagged_but_still_scores(self):
        prov = Baseline(
            profile_id="a7iii",
            bucket=ConditionBucket.USED,
            median_pence=100_000,
            p25_pence=100_000,
            sample_n=0,
            provisional=True,
        )
        d = score(make_listing(price_pence=60_000), PROFILE, prov, NOW)
        assert d.verdict is Verdict.BUY_NOW
        assert "provisional_baseline" in d.reasons


class TestVerdictMeta:
    def test_only_skip_is_not_notifiable(self):
        assert not Verdict.SKIP.notifiable
        assert all(
            v.notifiable for v in (Verdict.BUY_NOW, Verdict.GOOD_DEAL, Verdict.WATCH)
        )
