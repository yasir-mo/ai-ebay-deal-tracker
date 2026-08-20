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


class TestDryRun:
    def test_dry_run_does_not_consume_the_alert(self, rig):
        """A trial run must not suppress the alert once sending is enabled.

        The README tells users to run with dry_run for a day first, so this
        would otherwise swallow every deal found during that period.
        """
        tracker, store, notifier, _ = rig([[item("a", 600)]])
        notifier._dry_run = True
        tracker.sweep(NOW)

        assert len(notifier.sent) == 1
        assert store.already_notified("a", Verdict.BUY_NOW) is False

    def test_alert_fires_for_real_after_a_dry_run(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 600)], [item("a", 600)]])
        notifier._dry_run = True
        tracker.sweep(NOW)
        notifier._dry_run = False
        tracker.sweep(NOW + timedelta(minutes=30))

        assert len(notifier.sent) == 2
        assert store.already_notified("a", Verdict.BUY_NOW) is True

    def test_real_send_still_suppresses(self, rig):
        tracker, store, notifier, _ = rig([[item("a", 600)], [item("a", 600)]])
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        assert len(notifier.sent) == 1


# --------------------------------------------------------------------------
# Model judging stage, end to end through the scheduler
# --------------------------------------------------------------------------


class FakeJudge:
    """Stands in for tracker.ai.judge.Judge."""

    def __init__(self, judgements=None, error=None):
        self._judgements = judgements or {}
        self._error = error
        self.batches = []

    def judge(self, records, today):
        self.batches.append(records)
        if self._error:
            raise self._error
        from tracker.ai.schema import Judgement

        out = []
        for record in records:
            spec = self._judgements.get(record["item_id"])
            if spec:
                out.append(Judgement(item_id=record["item_id"], **spec))
        return out


GOOD = dict(
    is_target_item=True,
    condition_risk="none",
    resale_confidence="high",
    concerns=[],
    verdict="promote",
    rationale="Genuine body, complete.",
)
ACCESSORY = dict(
    is_target_item=False,
    condition_risk="none",
    resale_confidence="low",
    concerns=["This is a lens cap, not the camera"],
    verdict="reject",
    rationale="Listing is an accessory.",
)
RISKY = dict(
    is_target_item=True,
    condition_risk="undisclosed",
    resale_confidence="low",
    concerns=["Description does not mention the shutter count"],
    verdict="keep",
    rationale="Suspiciously thin description for the price.",
)


def rig_with_judge(rig, pages, judgements=None, error=None, profile=PROFILE):
    tracker, store, notifier, client = rig(pages, profile=profile)
    tracker.judge = FakeJudge(judgements, error)
    return tracker, store, notifier, client


class TestJudgingPipeline:
    def test_model_can_reject_an_accessory_the_rules_liked(self, rig):
        """The whole point of the stage: rules see a cheap A7 III, the model
        sees that it is a lens cap."""
        tracker, store, notifier, _ = rig_with_judge(
            rig, [[item("a", 600)]], {"a": ACCESSORY}
        )
        stats = tracker.sweep(NOW)

        assert stats["alerts"] == 0
        assert notifier.sent == []
        row = store.conn.execute(
            "SELECT verdict, reason_json FROM decisions WHERE item_id='a'"
        ).fetchone()
        assert row["verdict"] == "SKIP"
        assert "ai_reject" in row["reason_json"]

    def test_model_downgrades_on_undisclosed_condition(self, rig):
        tracker, store, notifier, _ = rig_with_judge(
            rig, [[item("a", 600)]], {"a": RISKY}
        )
        tracker.sweep(NOW)
        assert "GOOD DEAL" in notifier.sent[0]
        assert "thin description" in notifier.sent[0].lower()

    def test_rationale_and_concerns_reach_the_alert(self, rig):
        tracker, _, notifier, _ = rig_with_judge(
            rig, [[item("a", 600)]], {"a": RISKY}
        )
        tracker.sweep(NOW)
        assert "Check:" in notifier.sent[0]
        assert "shutter count" in notifier.sent[0]

    def test_only_rule_survivors_are_sent_to_the_model(self, rig):
        """Cost control: the model must never see the rejects."""
        page = [
            item("cheap", 600),
            item("market", 1000),
            item("junk", 200, title="Sony A7 III for parts not working"),
        ]
        tracker, _, _, _ = rig_with_judge(rig, [page], {"cheap": GOOD})
        tracker.sweep(NOW)

        judged = [r["item_id"] for r in tracker.judge.batches[0]]
        assert judged == ["cheap"]

    def test_no_candidates_means_no_model_call(self, rig):
        tracker, _, _, _ = rig_with_judge(rig, [[item("a", 1000)]], {})
        tracker.sweep(NOW)
        assert tracker.judge.batches == []

    def test_judgement_is_cached_and_not_repaid_next_sweep(self, rig):
        tracker, store, _, _ = rig_with_judge(
            rig, [[item("a", 600)], [item("a", 600)]], {"a": RISKY}
        )
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))

        assert len(tracker.judge.batches) == 1
        assert store.get_judgement("a", 60_000) is not None

    def test_price_change_forces_a_re_judge(self, rig):
        """A cached verdict is only valid for the price it was made about."""
        tracker, _, _, _ = rig_with_judge(
            rig, [[item("a", 600)], [item("a", 500)]], {"a": RISKY}
        )
        tracker.sweep(NOW)
        tracker.sweep(NOW + timedelta(minutes=30))
        assert len(tracker.judge.batches) == 2

    def test_model_failure_falls_back_to_rule_verdicts(self, rig):
        """An outage must not cost the user their alerts."""
        tracker, _, notifier, _ = rig_with_judge(
            rig, [[item("a", 600)]], error=RuntimeError("api down")
        )
        stats = tracker.sweep(NOW)
        assert stats["alerts"] == 1
        assert "BUY NOW" in notifier.sent[0]

    def test_disabled_judge_leaves_rule_verdicts_untouched(self, rig):
        tracker, _, notifier, _ = rig([[item("a", 600)]])
        assert tracker.judge is None
        assert tracker.sweep(NOW)["alerts"] == 1
        assert "BUY NOW" in notifier.sent[0]


class TestPriorityPromotion:
    def test_priority_needs_a_real_baseline(self, rig):
        """On a provisional baseline the margin is a guess, so no priority."""
        tracker, _, notifier, _ = rig_with_judge(
            rig, [[item("a", 400)]], {"a": GOOD}
        )
        tracker.sweep(NOW)
        assert "BUY NOW" in notifier.sent[0]
        assert "PRIORITY" not in notifier.sent[0]

    def test_priority_fires_on_real_data_with_a_fat_margin(self, rig):
        page = [item(f"i{n}", 1000) for n in range(MIN_SAMPLES + 5)]
        page.append(item("bargain", 400))
        tracker, _, notifier, _ = rig_with_judge(rig, [page], {"bargain": GOOD})
        tracker.sweep(NOW)

        priority = [m for m in notifier.sent if "PRIORITY" in m]
        assert len(priority) == 1
        assert "resale margin" in priority[0].lower()

    def test_no_priority_when_margin_is_thin(self, rig):
        page = [item(f"i{n}", 1000) for n in range(MIN_SAMPLES + 5)]
        page.append(item("meh", 760))
        tracker, _, notifier, _ = rig_with_judge(rig, [page], {"meh": GOOD})
        tracker.sweep(NOW)
        assert not any("PRIORITY" in m for m in notifier.sent)


class TestCredentialFailure:
    def test_bad_credentials_are_counted_not_crashed_on(self, rig):
        """An expired keyset should not traceback on every sweep."""
        from tracker.ebay.auth import AuthError

        tracker, _, notifier, client = rig([[item("a", 600)]])

        def bad_auth(*a, **kw):
            raise AuthError("token request failed: 401 invalid_client")

        client.search = bad_auth
        stats = tracker.sweep(NOW)

        assert stats["errors"] == 1
        assert stats["alerts"] == 0
        assert tracker.errors_this_period == 1
