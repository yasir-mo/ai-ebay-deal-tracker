"""The model-based judging stage.

Sits behind the deterministic rules: only listings the rules already rated
worth notifying reach this module. That ordering is the whole cost strategy:
the rules reject the large majority for free, and the model only sees what is
genuinely ambiguous.

Four further cost controls, in descending order of how much they save:

1. Batching. One request judges many listings; per-request overhead and the
   cached system prompt are paid once instead of once per listing.
2. Prompt caching. The system prompt is byte-stable, so every request after
   the first reads it from cache at a fraction of the input price.
3. Verdict reuse, handled by the caller: a listing whose price has not moved
   is not re-judged on the next sweep.
4. A hard daily spend cap that stops making calls rather than degrading
   quietly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .schema import Judgement, JudgementBatch

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

#: Listings per request. Large enough to amortise the cached prefix, small
#: enough that one malformed listing cannot spoil a whole sweep's judging.
DEFAULT_BATCH_SIZE = 8

#: Per-million-token list prices for the model above, in pence, used only for
#: the local spend estimate. Approximate by design: this guards a budget, it
#: is not an invoice.
INPUT_PENCE_PER_MTOK = 400
OUTPUT_PENCE_PER_MTOK = 2000
CACHE_READ_PENCE_PER_MTOK = 40


class JudgeError(Exception):
    pass


class BudgetExhausted(JudgeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.requests += other.requests

    @property
    def estimated_pence(self) -> int:
        return round(
            (self.input_tokens * INPUT_PENCE_PER_MTOK
             + self.output_tokens * OUTPUT_PENCE_PER_MTOK
             + self.cache_read_tokens * CACHE_READ_PENCE_PER_MTOK
             + self.cache_write_tokens * INPUT_PENCE_PER_MTOK * 1.25)
            / 1_000_000
        )


@dataclass
class DailyBudget:
    """A hard cap, not a warning. When it is reached, judging stops."""

    limit_pence: int
    _day: date | None = None
    _usage: Usage = field(default_factory=Usage)

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._usage = Usage()

    def remaining_pence(self, today: date) -> int:
        self._roll(today)
        return max(0, self.limit_pence - self._usage.estimated_pence)

    def exhausted(self, today: date) -> bool:
        return self.remaining_pence(today) <= 0

    def record(self, usage: Usage, today: date) -> None:
        self._roll(today)
        self._usage.add(usage)

    def spent_pence(self, today: date) -> int:
        self._roll(today)
        return self._usage.estimated_pence


def build_client(api_key: str | None = None):
    """Import the SDK lazily so the tracker runs without it when AI is off."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise JudgeError(
            "the anthropic package is required when ai.enabled is true "
            "(pip install anthropic)"
        ) from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


class Judge:
    """Wraps one structured-output call per batch of listings."""

    def __init__(
        self,
        client,
        budget: DailyBudget,
        effort: str = "medium",
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_tokens: int = 8000,
    ):
        self._client = client
        self._budget = budget
        self._effort = effort
        self._batch_size = max(1, batch_size)
        self._max_tokens = max_tokens

    def judge(self, records: list[dict], today: date) -> list[Judgement]:
        """Judge every record, in batches. Returns whatever succeeded.

        A failed batch is logged and dropped rather than raised: a model
        outage must not stop the tracker from sending rule-based alerts.
        """
        if not records:
            return []

        out: list[Judgement] = []
        for start in range(0, len(records), self._batch_size):
            batch = records[start : start + self._batch_size]

            if self._budget.exhausted(today):
                log.warning(
                    "daily AI budget exhausted (%.2f GBP); %d listings unjudged",
                    self._budget.limit_pence / 100,
                    len(records) - start,
                )
                break

            try:
                out.extend(self._judge_batch(batch, today))
            except BudgetExhausted:
                break
            except JudgeError as exc:
                log.error("judging batch failed, falling back to rules: %s", exc)
            except Exception:
                log.exception("unexpected error judging batch; falling back to rules")

        return out

    def _judge_batch(self, batch: list[dict], today: date) -> list[Judgement]:
        from .prompt import SYSTEM_PROMPT, render_batch

        try:
            response = self._client.messages.parse(
                model=MODEL,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        # Stable across every request, so this prefix is a
                        # cache read on all but the first call of the window.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": self._effort},
                messages=[{"role": "user", "content": render_batch(batch)}],
                output_format=JudgementBatch,
            )
        except Exception as exc:
            raise JudgeError(str(exc)) from exc

        self._record_usage(response, today)

        # Safety classifiers can decline with a normal 200 and an empty body,
        # so this must be checked before touching the parsed output.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise JudgeError(
                f"model declined to judge batch (category="
                f"{getattr(details, 'category', None)})"
            )

        if getattr(response, "stop_reason", None) == "max_tokens":
            raise JudgeError(
                "judgement truncated at max_tokens; reduce batch_size or raise "
                "ai.max_tokens"
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise JudgeError("no structured output in response")

        return self._reconcile(parsed.judgements, batch)

    @staticmethod
    def _reconcile(judgements: list[Judgement], batch: list[dict]) -> list[Judgement]:
        """Keep only judgements that map to a listing actually sent.

        Guards against a hallucinated or duplicated item_id silently promoting
        something that was never in the batch.
        """
        wanted = {record["item_id"] for record in batch}
        seen: set[str] = set()
        kept = []
        for judgement in judgements:
            if judgement.item_id not in wanted:
                log.warning("discarding judgement for unknown item %s", judgement.item_id)
                continue
            if judgement.item_id in seen:
                log.warning("discarding duplicate judgement for %s", judgement.item_id)
                continue
            seen.add(judgement.item_id)
            kept.append(judgement)

        missing = wanted - seen
        if missing:
            log.warning("model returned no judgement for %d listing(s)", len(missing))
        return kept

    def _record_usage(self, response, today: date) -> None:
        raw = getattr(response, "usage", None)
        if raw is None:
            return
        usage = Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            requests=1,
        )
        self._budget.record(usage, today)
        log.debug(
            "judged batch: %d in / %d out / %d cached, ~%.3f GBP, %.2f GBP left today",
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.estimated_pence / 100,
            self._budget.remaining_pence(today) / 100,
        )
