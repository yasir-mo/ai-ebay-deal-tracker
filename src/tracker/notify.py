"""Telegram notification with per-(item, verdict) suppression."""
from __future__ import annotations

import html
import logging
from datetime import datetime

import requests

from .models import Decision, Listing, Verdict
from .store import Store

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def _money(pence: int, currency: str = "GBP") -> str:
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{pence / 100:,.2f}"


def _time_left(end: datetime | None, now: datetime) -> str | None:
    if not end:
        return None
    seconds = int((end - now).total_seconds())
    if seconds <= 0:
        return "ended"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def format_message(
    listing: Listing,
    decision: Decision,
    now: datetime,
    judgement=None,
    margin=None,
) -> str:
    """HTML-formatted Telegram message. Everything user-supplied is escaped."""
    verdict = decision.verdict
    lines = [
        f"{verdict.emoji} <b>{verdict.value.replace('_', ' ')}</b>",
        f"<a href=\"{html.escape(listing.item_url, quote=True)}\">"
        f"{html.escape(listing.title[:120])}</a>",
        "",
        f"Price: <b>{_money(listing.price_pence, listing.currency)}</b>",
    ]

    if listing.shipping_known:
        lines.append(
            f"Postage: {_money(listing.shipping_pence, listing.currency)}  "
            f"→ total <b>{_money(listing.total_pence, listing.currency)}</b>"
        )
    else:
        # Do not imply free postage that was never published.
        lines.append("Postage: <i>not listed - check before bidding</i>")

    if decision.baseline_pence:
        lines.append(
            f"Market ask: {_money(decision.baseline_pence, listing.currency)}"
            + (
                f"  (<b>{decision.discount_pct:.0%} below</b>)"
                if decision.discount_pct is not None
                else ""
            )
        )

    if listing.condition:
        lines.append(f"Condition: {html.escape(listing.condition)}")

    left = _time_left(listing.end_time, now)
    if left:
        bids = f", {listing.bid_count} bids" if listing.bid_count else ""
        lines.append(f"Ends in: <b>{left}</b>{bids}")

    if listing.seller_name:
        fb = (
            f" ({listing.seller_feedback_pct:.1f}%, "
            f"{listing.seller_feedback_score:,})"
            if listing.seller_feedback_pct is not None
            and listing.seller_feedback_score is not None
            else ""
        )
        lines.append(f"Seller: {html.escape(listing.seller_name)}{fb}")

    if margin is not None and margin.margin_pence > 0:
        lines.append(
            f"Est. resale margin: <b>{_money(margin.margin_pence, listing.currency)}</b>"
            f" ({margin.margin_pct:.0%} on cost, after fees and postage)"
        )

    if judgement is not None:
        lines.append("")
        lines.append(f"<i>{html.escape(judgement.rationale)}</i>")
        if judgement.concerns:
            checks = "; ".join(html.escape(c) for c in judgement.concerns[:3])
            lines.append(f"Check: {checks}")

    flags = [r for r in decision.reasons if r in ("provisional_baseline", "shipping_unknown")]
    if flags:
        pretty = {
            "provisional_baseline": "baseline is your manual estimate, not real data",
            "shipping_unknown": "postage unknown, total is a lower bound",
        }
        lines.append("")
        lines.append("⚠ " + "; ".join(pretty[f] for f in flags))

    return "\n".join(lines)


class Notifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        store: Store,
        dry_run: bool = False,
        session=None,
    ):
        self._token = token
        self._chat_id = chat_id
        self._store = store
        self._dry_run = dry_run
        self._session = session or requests.Session()

    def send_raw(self, text: str) -> bool:
        if self._dry_run:
            log.info("[dry-run] would send:\n%s", text)
            return True
        try:
            resp = self._session.post(
                API.format(token=self._token),
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("telegram send failed: %s", exc)
            return False
        if resp.status_code != 200:
            log.error("telegram rejected message: %s %s", resp.status_code, resp.text[:300])
            return False
        return True

    def maybe_notify(
        self,
        listing: Listing,
        decision: Decision,
        now: datetime,
        judgement=None,
        margin=None,
    ) -> bool:
        """Send unless this (item, verdict) pair has already been sent.

        Returns True if a message was actually sent.
        """
        if not decision.verdict.notifiable:
            return False
        if self._store.already_notified(listing.item_id, decision.verdict):
            return False

        if not self.send_raw(
            format_message(listing, decision, now, judgement, margin)
        ):
            # Left unmarked on failure so the next sweep retries it.
            return False

        if self._dry_run:
            # Deliberately not marked. A dry run must not consume the one
            # alert this (item, verdict) pair gets, or every deal found while
            # trialling the config would be silently suppressed once the
            # tracker is switched on for real.
            return True

        self._store.mark_notified(listing.item_id, decision.verdict, now)
        return True
