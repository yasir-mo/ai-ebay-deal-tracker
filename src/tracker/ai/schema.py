"""Structured output schema for the judging stage.

The model is constrained to this shape, so the pipeline never has to parse
prose or guess at a verdict. Every field is something rules genuinely cannot
determine from price and title alone.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Judgement(BaseModel):
    """One model verdict on one listing."""

    item_id: str = Field(description="The item_id exactly as given in the input.")

    is_target_item: bool = Field(
        description=(
            "True only if this listing is the actual product being searched for. "
            "False for accessories, compatible-with items, empty boxes, manuals, "
            "protective cases, or a different model that merely mentions the "
            "target in its title."
        )
    )

    condition_risk: Literal["none", "minor", "major", "undisclosed"] = Field(
        description=(
            "Risk implied by the description relative to the stated condition. "
            "'undisclosed' when the description is suspiciously thin or evasive "
            "for the price. 'major' for damage that affects function."
        )
    )

    resale_confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence this could be resold near the market rate, considering "
            "completeness, condition, seller signals, and how specific the "
            "listing is."
        )
    )

    concerns: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Short, concrete concerns a buyer should check before committing. "
            "Empty when there are none. Never speculative filler."
        ),
    )

    verdict: Literal["promote", "keep", "reject"] = Field(
        description=(
            "'reject' if this is not the target item or carries major undisclosed "
            "risk. 'promote' only when it is clearly the target item, condition "
            "risk is none or minor, and resale confidence is high. 'keep' "
            "otherwise."
        )
    )

    rationale: str = Field(
        max_length=280,
        description="One or two sentences justifying the verdict. No preamble.",
    )


class JudgementBatch(BaseModel):
    """The model judges a batch of listings in a single request."""

    judgements: list[Judgement] = Field(
        description="Exactly one entry per listing in the input, same item_ids."
    )
