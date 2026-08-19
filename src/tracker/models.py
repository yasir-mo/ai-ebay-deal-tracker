"""Core data types. Money is always integer pence; no float touches a price."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Verdict(str, Enum):
    PRIORITY = "PRIORITY"
    BUY_NOW = "BUY_NOW"
    GOOD_DEAL = "GOOD_DEAL"
    WATCH = "WATCH"
    SKIP = "SKIP"

    @property
    def emoji(self) -> str:
        return {
            Verdict.PRIORITY: "\U0001f6a8",
            Verdict.BUY_NOW: "\U0001f525",
            Verdict.GOOD_DEAL: "\U0001f7e2",
            Verdict.WATCH: "\U0001f7e1",
            Verdict.SKIP: "\U0001f534",
        }[self]

    @property
    def notifiable(self) -> bool:
        return self is not Verdict.SKIP

    @property
    def rank(self) -> int:
        """Ordering for display, and for deciding what counts as an upgrade."""
        return {
            Verdict.SKIP: 0,
            Verdict.WATCH: 1,
            Verdict.GOOD_DEAL: 2,
            Verdict.BUY_NOW: 3,
            Verdict.PRIORITY: 4,
        }[self]


class ConditionBucket(str, Enum):
    NEW = "NEW"
    REFURB = "REFURB"
    USED = "USED"


def bucket_for(condition_id: int | None) -> ConditionBucket:
    """Collapse eBay's condition IDs into three buckets.

    Baselines need enough samples to be meaningful, and eBay's ~15 condition
    IDs fragment them too finely to ever reach MIN_SAMPLES per profile.
    """
    if condition_id is None:
        return ConditionBucket.USED
    if 1000 <= condition_id <= 1500:
        return ConditionBucket.NEW
    if 2000 <= condition_id <= 2500:
        return ConditionBucket.REFURB
    return ConditionBucket.USED


class BuyingOption(str, Enum):
    AUCTION = "AUCTION"
    FIXED_PRICE = "FIXED_PRICE"


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    query: str
    ceiling_pence: int
    target_pence: int | None = None
    min_feedback_pct: float = 95.0
    min_feedback_count: int = 10
    filters: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class Listing:
    item_id: str
    profile_id: str
    title: str
    condition: str | None
    condition_id: int | None
    buying_option: BuyingOption
    price_pence: int
    shipping_pence: int | None
    currency: str
    seller_name: str | None
    seller_feedback_pct: float | None
    seller_feedback_score: int | None
    end_time: datetime | None
    item_url: str
    bid_count: int | None = None

    @property
    def shipping_known(self) -> bool:
        return self.shipping_pence is not None

    @property
    def total_pence(self) -> int:
        """Landed cost: item price plus postage.

        When postage is unknown this is a lower bound rather than the real
        cost, so callers should check `shipping_known` first.
        """
        return self.price_pence + (self.shipping_pence or 0)

    @property
    def bucket(self) -> ConditionBucket:
        return bucket_for(self.condition_id)


@dataclass(frozen=True)
class Baseline:
    profile_id: str
    bucket: ConditionBucket
    median_pence: int
    p25_pence: int
    sample_n: int
    provisional: bool = False
    """True when derived from the profile's manual target rather than
    observed history."""


@dataclass(frozen=True)
class Decision:
    item_id: str
    profile_id: str
    verdict: Verdict
    total_pence: int
    baseline_pence: int | None
    discount_pct: float | None
    reasons: list[str]
