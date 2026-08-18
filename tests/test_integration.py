"""End to end pipeline tests with a fake eBay API and a fake Telegram.

Covers the wiring that unit tests cannot: whether a sweep stores, scores,
notifies and suppresses correctly, and whether the endgame loop picks up a
closing auction.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tracker.config import Settings
from tracker.models import Profile, Verdict
from tracker.notify import Notifier
from tracker.pricing import MIN_SAMPLES
from tracker.scheduler import Tracker
from tracker.store import Store

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    """Stands in for BrowseClient. Returns whatever pages it is given."""

    def __init__(self, pages=None, item_detail=None):
        self.pages = pages or []
        self.item_detail = item_detail or {}
        self.calls_made = 0
        self.searches = []
        self.detail_calls = []

    def search(self, query, limit=20, filters=None):
        self.calls_made += 1
        self.searches.append(query)
        return self.pages.pop(0) if self.pages else []

    def get_item(self, item_id):
        self.calls_made += 1
        self.detail_calls.append(item_id)
        return self.item_detail.get(item_id, {})


class FakeNotifier(Notifier):
    def __init__(self, store):
        super().__init__("token", "chat", store, dry_run=False)
        self.sent = []

    def send_raw(self, text):
        self.sent.append(text)
        return True


class FailingNotifier(FakeNotifier):
    def send_raw(self, text):
        self.sent.append(text)
        return False


def item(item_id, price, *, auction=False, end=None, title="Sony A7 III Body",
         shipping="0.00", condition_id="3000", feedback="99.0", score=500):
    raw = {
        "itemId": item_id,
        "title": title,
        "price": {"value": str(price), "currency": "GBP"},
        "condition": "Used",
        "conditionId": condition_id,
        "buyingOptions": ["AUCTION"] if auction else ["FIXED_PRICE"],
        "itemWebUrl": f"https://ebay.co.uk/itm/{item_id}",
        "seller": {
            "username": "seller",
            "feedbackPercentage": feedback,
            "feedbackScore": score,
        },
    }
    if shipping is not None:
        raw["shippingOptions"] = [
            {"shippingCostType": "FIXED", "shippingCost": {"value": shipping, "currency": "GBP"}}
        ]
    if auction:
        raw["currentBidPrice"] = {"value": str(price), "currency": "GBP"}
        raw["bidCount"] = 7
    if end:
        raw["itemEndDate"] = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return raw


PROFILE = Profile(
    id="a7iii",
    name="Sony A7 III",
    query="sony a7 iii body",
    ceiling_pence=150_000,
    target_pence=100_000,
    min_feedback_pct=95.0,
    min_feedback_count=10,
)

SETTINGS = Settings(
    ebay_client_id="x",
    ebay_client_secret="x",
    telegram_token="x",
    telegram_chat_id="x",
    db_path=":memory:",
)


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "t.db")
    notifier = FakeNotifier(store)

    def build(pages=None, item_detail=None, notifier_cls=None, profile=PROFILE):
        nonlocal notifier
        if notifier_cls:
            notifier = notifier_cls(store)
        client = FakeClient(pages, item_detail)
        return Tracker(SETTINGS, [profile], store, client, notifier), store, notifier, client

    yield build
    store.close()


class TestSweep:
    def test_stores_and_alerts_on_a_bargain(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 600)]])
        stats = tracker.sweep(NOW)

        assert stats["listings"] == 1
        assert store.get_listing("a").total_pence == 60_000
        assert stats["alerts"] == 1
        assert "BUY NOW" in notifier.sent[0]

    def test_market_priced_item_is_stored_but_silent(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 1000)]])
        stats = tracker.sweep(NOW)

        assert store.get_listing("a") is not None
        assert stats["alerts"] == 0
        assert notifier.sent == []

    def test_same_listing_does_not_alert_twice(self, rig):
        """48 sweeps a day, one alert."""
        tracker, store, notifier, _ = rig([[item("a", 600)], [item("a", 600)]])
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        assert len(notifier.sent) == 1

    def test_price_drop_into_a_higher_tier_alerts_again(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 790)], [item("a", 600)]])
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        assert len(notifier.sent) == 2
        assert "GOOD DEAL" in notifier.sent[0]
        assert "BUY NOW" in notifier.sent[1]

    def test_price_history_accumulates_per_sweep(self, rig):
        tracker, store, _, _ = rig([[item("a", 900)], [item("a", 880)]])
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        rows = store.conn.execute(
            "SELECT total_pence FROM price_history WHERE item_id='a' ORDER BY observed_at"
        ).fetchall()
        assert [r["total_pence"] for r in rows] == [90_000, 88_000]

    def test_blocklisted_junk_never_alerts(self, rig):
        tracker, _, notifier, _ = rig(
            [[item("a", 200, title="Sony A7 III for parts not working")]]
        )
        assert tracker.sweep(NOW)["alerts"] == 0

    def test_search_failure_does_not_abort_the_sweep(self, rig):
        from tracker.ebay.browse import BrowseError

        tracker, store, notifier, client = rig([[item("a", 600)]])

        def boom(*a, **kw):
            raise BrowseError("eBay is down")

        client.search = boom
        stats = tracker.sweep(NOW)
        assert stats["errors"] == 1
        assert tracker.errors_this_period == 1

    def test_failed_send_is_retried_next_sweep(self, rig):
        tracker, store, notifier, _ = rig(
            [[item("a", 600)], [item("a", 600)]], notifier_cls=FailingNotifier
        )
        tracker.sweep(NOW)
        assert store.already_notified("a", Verdict.BUY_NOW) is False
        tracker.sweep(NOW + timedelta(minutes=30))
        assert len(notifier.sent) == 2


class TestBaselineLifecycle:
    def test_provisional_target_is_used_before_enough_data(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 600)]])
        tracker.sweep(NOW)
        assert "manual estimate" in notifier.sent[0]

    def test_real_baseline_replaces_the_target_once_data_lands(self, rig):
        page = [item(f"i{n}", 500) for n in range(MIN_SAMPLES + 5)]
        tracker, store, notifier, _ = rig([page])
        tracker.sweep(NOW)

        from tracker.models import ConditionBucket

        baseline = store.get_baseline("a7iii", ConditionBucket.USED)
        assert baseline is not None
        assert baseline.sample_n >= MIN_SAMPLES
        assert baseline.median_pence == 50_000

    def test_market_wide_price_means_nothing_alerts(self, rig):
        """If everything costs the same, nothing is a deal."""
        page = [item(f"i{n}", 500) for n in range(MIN_SAMPLES + 5)]
        tracker, store, notifier, _ = rig([page, page])
        tracker.sweep(NOW)
        notifier.sent.clear()
        tracker.sweep(NOW + timedelta(minutes=30))
        assert notifier.sent == []


class TestShippingUnknown:
    def test_unknown_postage_downgrades_and_warns(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 600, shipping=None)]])
        tracker.sweep(NOW)
        assert "GOOD DEAL" in notifier.sent[0]
        assert "postage unknown" in notifier.sent[0].lower()


class TestEndgame:
    def test_refreshes_only_auctions_closing_soon(self, rig):
        soon = NOW + timedelta(minutes=8)
        far = NOW + timedelta(hours=10)
        page = [
            item("closing", 900, auction=True, end=soon),
            item("later", 900, auction=True, end=far),
        ]
        detail = {"closing": item("closing", 600, auction=True, end=soon)}

        tracker, store, notifier, client = rig([page], item_detail=detail)
        tracker.sweep(NOW)
        notifier.sent.clear()

        stats = tracker.endgame(NOW)
        assert client.detail_calls == ["closing"]
        assert stats["checked"] == 1

    def test_late_price_collapse_triggers_a_watch(self, rig):
        soon = NOW + timedelta(minutes=8)
        page = [item("closing", 950, auction=True, end=soon)]
        detail = {"closing": item("closing", 600, auction=True, end=soon)}

        tracker, store, notifier, _ = rig([page], item_detail=detail)
        tracker.sweep(NOW)
        notifier.sent.clear()

        assert tracker.endgame(NOW)["alerts"] == 1
        assert "WATCH" in notifier.sent[0]
        assert "8m" in notifier.sent[0]

    def test_ended_auctions_are_deactivated(self, rig):
        past = NOW - timedelta(minutes=5)
        tracker, store, _, _ = rig([[item("gone", 600, auction=True, end=past)]])
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        assert store.active_listing_count() == 0


class TestHeartbeat:
    def test_reports_liveness_and_cold_profiles(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 900)]])
        tracker.sweep(NOW)
        text = tracker.heartbeat(NOW)
        assert "Tracker alive" in text
        assert "a7iii" in text

    def test_reports_errors_then_resets(self, rig):
        tracker, store, notifier, _ = rig([[]])
        tracker.errors_this_period = 3
        assert "Errors since last heartbeat: 3" in tracker.heartbeat(NOW)
        assert tracker.errors_this_period == 0
        assert "Errors since last heartbeat" not in tracker.heartbeat(NOW)
