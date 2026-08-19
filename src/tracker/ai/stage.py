"""Applying model judgements to rule-produced decisions.

The rules decide what is worth looking at; the model decides whether it is
what it claims to be. This module is where the two meet, and it is deliberately
the only place the model is allowed to change a verdict.

The asymmetry is intentional. The model can always reject, because "this is an
accessory, not the camera" is exactly the judgement rules cannot make. It can
only promote under conditions the rules have already vouched for: real observed
history, a margin that clears the threshold, and its own high confidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ..margin import MarginConfig, MarginEstimate, qualifies_for_priority
from ..models import Decision, Verdict
from .schema import Judgement

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgedDecision:
    decision: Decision
    judgement: Judgement | None
    margin: MarginEstimate | None

    @property
    def was_rejected_by_model(self) -> bool:
        return (
            self.judgement is not None
            and self.judgement.verdict == "reject"
            and self.decision.verdict is Verdict.SKIP
        )


def apply(
    decision: Decision,
    judgement: Judgement | None,
    margin: MarginEstimate | None,
    margin_config: MarginConfig | None = None,
) -> JudgedDecision:
    """Fold one judgement into one rule decision.

    With no judgement (AI disabled, budget spent, request failed) the rule
    decision passes through untouched. Losing the model must never mean losing
    the alert.
    """
    if judgement is None:
        return JudgedDecision(decision=decision, judgement=None, margin=margin)

    reasons = list(decision.reasons)

    if judgement.verdict == "reject":
        reasons.append(f"ai_reject:{_first_concern(judgement)}")
        return JudgedDecision(
            decision=replace(decision, verdict=Verdict.SKIP, reasons=reasons),
            judgement=judgement,
            margin=margin,
        )

    if not judgement.is_target_item:
        # Belt and braces: a 'keep' verdict on something the model also says
        # is not the target item is contradictory, and the safer reading wins.
        reasons.append("ai_not_target_item")
        return JudgedDecision(
            decision=replace(decision, verdict=Verdict.SKIP, reasons=reasons),
            judgement=judgement,
            margin=margin,
        )

    if judgement.condition_risk in ("major", "undisclosed"):
        reasons.append(f"ai_condition_risk:{judgement.condition_risk}")
        downgraded = _downgrade(decision.verdict)
        return JudgedDecision(
            decision=replace(decision, verdict=downgraded, reasons=reasons),
            judgement=judgement,
            margin=margin,
        )

    reasons.append(f"ai_confidence:{judgement.resale_confidence}")

    if _should_promote(judgement, margin, margin_config):
        reasons.append(f"ai_priority_margin:{margin.margin_pence}")
        return JudgedDecision(
            decision=replace(decision, verdict=Verdict.PRIORITY, reasons=reasons),
            judgement=judgement,
            margin=margin,
        )

    return JudgedDecision(
        decision=replace(decision, reasons=reasons),
        judgement=judgement,
        margin=margin,
    )


def _should_promote(
    judgement: Judgement,
    margin: MarginEstimate | None,
    margin_config: MarginConfig | None,
) -> bool:
    """Every condition must hold. Priority alerts are the loudest thing this
    tool does, so they need agreement from the rules, the margin, and the model.
    """
    if judgement.verdict != "promote":
        return False
    if judgement.resale_confidence != "high":
        return False
    if margin is None:
        return False
    # qualifies_for_priority already refuses a provisional baseline.
    return qualifies_for_priority(margin, margin_config)


def _downgrade(verdict: Verdict) -> Verdict:
    if verdict is Verdict.BUY_NOW:
        return Verdict.GOOD_DEAL
    if verdict is Verdict.GOOD_DEAL:
        return Verdict.WATCH
    return verdict


def _first_concern(judgement: Judgement) -> str:
    if judgement.concerns:
        return judgement.concerns[0][:60]
    return judgement.rationale[:60]
