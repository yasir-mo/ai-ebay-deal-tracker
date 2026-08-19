"""Tests for the model judging stage.

Driven entirely by a fake client, so the suite runs with no anthropic SDK
installed, no API key, and no network.
"""
from datetime import date, datetime, timezone

import pytest

from tracker.ai.judge import (
    DailyBudget,
    Judge,
    JudgeError,
    Usage,
)
from tracker.ai.schema import Judgement, JudgementBatch
from tracker.ai.stage import apply
from tracker.margin import MarginConfig, estimate, qualifies_for_priority
from tracker.models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Decision,
    Listing,
    Profile,
    Verdict,
)

TODAY = date(2026, 8, 19)
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

PROFILE = Profile(
    id="a7iii",
    name="Sony A7 III",
    query="sony a7 iii body",
    ceiling_pence=150_000,
    target_pence=100_000,
)

BASELINE = Baseline(
    profile_id="a7iii",
    bucket=ConditionBucket.USED,
    median_pence=100_000,
    p25_pence=90_000,
    sample_n=40,
)

PROVISIONAL = Baseline(
    profile_id="a7iii",
    bucket=ConditionBucket.USED,
    median_pence=100_000,
    p25_pence=100_000,
    sample_n=2,
    provisional=True,
)


def make_listing(item_id="v1|1|0", price=60_000):
    return Listing(
        item_id=item_id,
        profile_id="a7iii",
        title="Sony A7 III Body",
        condition="Used",
        condition_id=3000,
        buying_option=BuyingOption.FIXED_PRICE,
        price_pence=price,
        shipping_pence=0,
        currency="GBP",
        seller_name="seller",
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        end_time=None,
        item_url="https://ebay.co.uk/itm/1",
    )


def make_decision(item_id="v1|1|0", verdict=Verdict.BUY_NOW, total=60_000):
    return Decision(
        item_id=item_id,
        profile_id="a7iii",
        verdict=verdict,
        total_pence=total,
        baseline_pence=100_000,
        discount_pct=0.4,
        reasons=["discount:40%"],
    )


def make_judgement(item_id="v1|1|0", **over):
    base = dict(
        item_id=item_id,
        is_target_item=True,
        condition_risk="none",
        resale_confidence="high",
        concerns=[],
        verdict="promote",
        rationale="Genuine body, complete, strong seller.",
    )
    base.update(over)
    return Judgement(**base)


# --------------------------------------------------------------------------
# Fake client
# --------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, i=1000, o=200, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


class FakeResponse:
    def __init__(self, judgements=None, stop_reason="end_turn", usage=None,
                 stop_details=None, parsed=True):
        self.parsed_output = (
            JudgementBatch(judgements=judgements or []) if parsed else None
        )
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("more requests than queued responses")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def make_judge(responses, limit_pence=10_000, **kw):
    client = FakeClient(responses)
    budget = DailyBudget(limit_pence=limit_pence)
    return Judge(client, budget, **kw), client, budget


def records(n):
    return [{"item_id": f"item{i}", "title": "x"} for i in range(n)]


# --------------------------------------------------------------------------


class TestMargin:
    def test_margin_subtracts_fees_and_postage(self):
        est = estimate(make_listing(price=50_000), BASELINE)
        # resale 90000, fees 11550, outbound 500, cost 50000
        assert est.resale_pence == 90_000
        assert est.margin_pence == 90_000 - 11_550 - 500 - 50_000

    def test_margin_pct_is_relative_to_cost(self):
        est = estimate(make_listing(price=50_000), BASELINE)
        assert est.margin_pct == pytest.approx(est.margin_pence / 50_000)

    def test_expensive_listing_has_negative_margin(self):
        est = estimate(make_listing(price=95_000), BASELINE)
        assert est.margin_pence < 0
        assert not est.profitable

    def test_realisation_discounts_the_asking_baseline(self):
        """Asking prices are not sold prices."""
        est = estimate(make_listing(), BASELINE)
        assert est.resale_pence < BASELINE.median_pence

    def test_provisional_baseline_never_qualifies_for_priority(self):
        """A margin computed from a number the user typed is not arbitrage."""
        est = estimate(make_listing(price=10_000), PROVISIONAL)
        assert est.margin_pence > 50_000
        assert qualifies_for_priority(est) is False

    def test_thin_margin_does_not_qualify(self):
        est = estimate(make_listing(price=76_000), BASELINE)
        assert qualifies_for_priority(est) is False

    def test_fat_margin_qualifies(self):
        est = estimate(make_listing(price=40_000), BASELINE)
        assert qualifies_for_priority(est) is True


class TestStageRejection:
    def test_reject_verdict_forces_skip(self):
        result = apply(
            make_decision(),
            make_judgement(verdict="reject", concerns=["This is a lens cap"]),
            estimate(make_listing(), BASELINE),
        )
        assert result.decision.verdict is Verdict.SKIP
        assert any(r.startswith("ai_reject:") for r in result.decision.reasons)
        assert result.was_rejected_by_model

    def test_not_target_item_forces_skip_even_when_kept(self):
        """A 'keep' on something not the target item is contradictory."""
        result = apply(
            make_decision(),
            make_judgement(verdict="keep", is_target_item=False),
            estimate(make_listing(), BASELINE),
        )
        assert result.decision.verdict is Verdict.SKIP
        assert "ai_not_target_item" in result.decision.reasons

    @pytest.mark.parametrize("risk", ["major", "undisclosed"])
    def test_condition_risk_downgrades(self, risk):
        result = apply(
            make_decision(verdict=Verdict.BUY_NOW),
            make_judgement(verdict="keep", condition_risk=risk),
            estimate(make_listing(), BASELINE),
        )
        assert result.decision.verdict is Verdict.GOOD_DEAL
        assert f"ai_condition_risk:{risk}" in result.decision.reasons

    def test_downgrade_does_not_fall_below_watch(self):
        result = apply(
            make_decision(verdict=Verdict.WATCH),
            make_judgement(verdict="keep", condition_risk="major"),
            estimate(make_listing(), BASELINE),
        )
        assert result.decision.verdict is Verdict.WATCH


class TestStagePromotion:
    def test_promotes_to_priority_when_everything_agrees(self):
        result = apply(
            make_decision(total=40_000),
            make_judgement(),
            estimate(make_listing(price=40_000), BASELINE),
        )
        assert result.decision.verdict is Verdict.PRIORITY

    def test_no_promotion_without_margin(self):
        result = apply(make_decision(), make_judgement(), None)
        assert result.decision.verdict is Verdict.BUY_NOW

    def test_no_promotion_on_thin_margin(self):
        result = apply(
            make_decision(total=76_000),
            make_judgement(),
            estimate(make_listing(price=76_000), BASELINE),
        )
        assert result.decision.verdict is Verdict.BUY_NOW

    def test_no_promotion_on_medium_confidence(self):
        result = apply(
            make_decision(total=40_000),
            make_judgement(resale_confidence="medium"),
            estimate(make_listing(price=40_000), BASELINE),
        )
        assert result.decision.verdict is Verdict.BUY_NOW

    def test_no_promotion_on_keep_verdict(self):
        result = apply(
            make_decision(total=40_000),
            make_judgement(verdict="keep"),
            estimate(make_listing(price=40_000), BASELINE),
        )
        assert result.decision.verdict is Verdict.BUY_NOW

    def test_no_promotion_on_provisional_baseline(self):
        """The loudest alert must not fire off the user's own guess."""
        result = apply(
            make_decision(total=10_000),
            make_judgement(),
            estimate(make_listing(price=10_000), PROVISIONAL),
        )
        assert result.decision.verdict is Verdict.BUY_NOW


class TestStageFallback:
    def test_no_judgement_passes_the_rule_verdict_through(self):
        """Losing the model must never mean losing the alert."""
        original = make_decision()
        result = apply(original, None, None)
        assert result.decision == original
        assert result.judgement is None

    def test_rule_reasons_are_preserved(self):
        result = apply(make_decision(), make_judgement(verdict="keep"), None)
        assert "discount:40%" in result.decision.reasons


class TestBudget:
    def test_records_and_reports_spend(self):
        b = DailyBudget(limit_pence=1_000)
        b.record(Usage(input_tokens=1_000_000, output_tokens=0), TODAY)
        assert b.spent_pence(TODAY) == 400
        assert b.remaining_pence(TODAY) == 600

    def test_exhausts_at_the_limit(self):
        b = DailyBudget(limit_pence=100)
        assert not b.exhausted(TODAY)
        b.record(Usage(input_tokens=1_000_000), TODAY)
        assert b.exhausted(TODAY)

    def test_resets_on_a_new_day(self):
        b = DailyBudget(limit_pence=100)
        b.record(Usage(input_tokens=1_000_000), TODAY)
        assert b.exhausted(TODAY)
        assert not b.exhausted(date(2026, 8, 20))

    def test_cache_reads_are_cheaper_than_fresh_input(self):
        fresh = Usage(input_tokens=1_000_000)
        cached = Usage(cache_read_tokens=1_000_000)
        assert cached.estimated_pence < fresh.estimated_pence


class TestJudgeBatching:
    def test_single_batch_for_small_input(self):
        judge, client, _ = make_judge(
            [FakeResponse([make_judgement(f"item{i}") for i in range(3)])],
            batch_size=8,
        )
        out = judge.judge(records(3), TODAY)
        assert len(out) == 3
        assert len(client.messages.calls) == 1

    def test_splits_into_batches(self):
        judge, client, _ = make_judge(
            [
                FakeResponse([make_judgement(f"item{i}") for i in range(2)]),
                FakeResponse([make_judgement(f"item{i}") for i in (2, 3)]),
            ],
            batch_size=2,
        )
        out = judge.judge(records(4), TODAY)
        assert len(out) == 4
        assert len(client.messages.calls) == 2

    def test_empty_input_makes_no_call(self):
        judge, client, _ = make_judge([])
        assert judge.judge([], TODAY) == []
        assert client.messages.calls == []


class TestJudgeRequestShape:
    def test_uses_opus_5_and_caches_the_system_prompt(self):
        judge, client, _ = make_judge([FakeResponse([make_judgement("item0")])])
        judge.judge(records(1), TODAY)
        call = client.messages.calls[0]

        assert call["model"] == "claude-opus-5"
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_sends_no_removed_sampling_parameters(self):
        """temperature / top_p / top_k and budget_tokens all 400 on Opus 5."""
        judge, client, _ = make_judge([FakeResponse([make_judgement("item0")])])
        judge.judge(records(1), TODAY)
        call = client.messages.calls[0]

        for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
            assert banned not in call

    def test_effort_is_passed_through_output_config(self):
        judge, client, _ = make_judge(
            [FakeResponse([make_judgement("item0")])], effort="low"
        )
        judge.judge(records(1), TODAY)
        assert client.messages.calls[0]["output_config"]["effort"] == "low"

    def test_system_prompt_is_byte_stable_across_calls(self):
        """Any per-request variation would silently destroy the cache."""
        judge, client, _ = make_judge(
            [FakeResponse([make_judgement("item0")]),
             FakeResponse([make_judgement("item1")])],
            batch_size=1,
        )
        judge.judge(records(2), TODAY)
        first, second = client.messages.calls
        assert first["system"][0]["text"] == second["system"][0]["text"]


class TestJudgeFailureHandling:
    def test_refusal_is_not_read_as_a_result(self):
        """A refusal is HTTP 200 with empty content, so checking stop_reason
        first is the only thing standing between it and a crash."""
        judge, _, _ = make_judge(
            [FakeResponse([], stop_reason="refusal", parsed=False)]
        )
        assert judge.judge(records(1), TODAY) == []

    def test_truncation_is_treated_as_failure_not_partial_data(self):
        judge, _, _ = make_judge(
            [FakeResponse([make_judgement("item0")], stop_reason="max_tokens")]
        )
        assert judge.judge(records(1), TODAY) == []

    def test_api_error_falls_back_to_rules(self):
        judge, _, _ = make_judge([RuntimeError("connection reset")])
        assert judge.judge(records(1), TODAY) == []

    def test_one_bad_batch_does_not_stop_the_others(self):
        judge, client, _ = make_judge(
            [RuntimeError("boom"), FakeResponse([make_judgement("item1")])],
            batch_size=1,
        )
        out = judge.judge(records(2), TODAY)
        assert [j.item_id for j in out] == ["item1"]
        assert len(client.messages.calls) == 2

    def test_missing_structured_output_is_a_failure(self):
        judge, _, _ = make_judge([FakeResponse(parsed=False)])
        assert judge.judge(records(1), TODAY) == []


class TestJudgeReconciliation:
    def test_discards_judgement_for_an_item_not_sent(self):
        """Guards against a hallucinated id promoting something never in scope."""
        judge, _, _ = make_judge(
            [FakeResponse([make_judgement("item0"), make_judgement("ghost")])]
        )
        out = judge.judge(records(1), TODAY)
        assert [j.item_id for j in out] == ["item0"]

    def test_discards_duplicates(self):
        judge, _, _ = make_judge(
            [FakeResponse([make_judgement("item0"), make_judgement("item0")])]
        )
        assert len(judge.judge(records(1), TODAY)) == 1

    def test_tolerates_a_missing_judgement(self):
        judge, _, _ = make_judge(
            [FakeResponse([make_judgement("item0")])], batch_size=8
        )
        out = judge.judge(records(3), TODAY)
        assert [j.item_id for j in out] == ["item0"]


class TestJudgeBudgetEnforcement:
    def test_stops_calling_once_exhausted(self):
        judge, client, _ = make_judge(
            [
                FakeResponse(
                    [make_judgement("item0")],
                    usage=FakeUsage(i=1_000_000, o=0),
                ),
                FakeResponse([make_judgement("item1")]),
            ],
            limit_pence=100,
            batch_size=1,
        )
        out = judge.judge(records(2), TODAY)
        assert len(client.messages.calls) == 1
        assert [j.item_id for j in out] == ["item0"]

    def test_usage_accumulates_across_batches(self):
        judge, _, budget = make_judge(
            [
                FakeResponse([make_judgement("item0")], usage=FakeUsage(i=1000, o=100)),
                FakeResponse([make_judgement("item1")], usage=FakeUsage(i=1000, o=100)),
            ],
            batch_size=1,
        )
        judge.judge(records(2), TODAY)
        assert budget._usage.requests == 2
        assert budget._usage.input_tokens == 2000
