"""eBay Browse API client: search, item detail, retry, quota accounting."""
from __future__ import annotations

import logging
import time

import requests

from .auth import TokenProvider

log = logging.getLogger(__name__)

BASE = "https://api.ebay.com/buy/browse/v1"

MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0


class BrowseError(Exception):
    pass


class RateLimited(BrowseError):
    pass


class BrowseClient:
    def __init__(
        self,
        tokens: TokenProvider,
        marketplace: str = "EBAY_GB",
        currency: str = "GBP",
        session=None,
        sleep=time.sleep,
    ):
        self._tokens = tokens
        self._marketplace = marketplace
        self._currency = currency
        self._session = session or requests.Session()
        self._sleep = sleep
        self.calls_made = 0

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict) -> dict:
        """GET with exponential backoff on 429 and 5xx.

        Only transient failures are retried. A 4xx other than 429 means the
        request itself is wrong, and retrying would only burn quota.
        """
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            self.calls_made += 1
            try:
                resp = self._session.get(
                    f"{BASE}{path}",
                    headers=self._headers(),
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_error = BrowseError(f"network error: {exc}")
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    last_error = RateLimited("rate limited by eBay")
                elif 500 <= resp.status_code < 600:
                    last_error = BrowseError(f"server error {resp.status_code}")
                else:
                    raise BrowseError(
                        f"{resp.status_code} {resp.text[:300]}"
                    )

            if attempt < MAX_ATTEMPTS - 1:
                delay = BACKOFF_BASE**attempt
                log.warning(
                    "%s - retrying in %.0fs (attempt %d/%d)",
                    last_error,
                    delay,
                    attempt + 1,
                    MAX_ATTEMPTS,
                )
                self._sleep(delay)

        raise last_error or BrowseError("request failed")

    def search(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        """One page of item summaries for a search profile."""
        params = {"q": query, "limit": min(limit, 200), "sort": "newlyListed"}
        params.update(self.build_filters(filters, self._currency))
        payload = self._get("/item_summary/search", params)
        return payload.get("itemSummaries") or []

    @staticmethod
    def build_filters(filters: dict | None, currency: str) -> dict:
        """Translate config keys into eBay's `filter` query syntax.

        Kept pure so the syntax can be tested without a network call. A
        malformed filter returns 400, which retrying will not fix.
        """
        parts: list[str] = []
        params: dict = {}

        for key, value in (filters or {}).items():
            if key == "price_max":
                # eBay rejects a price filter unless priceCurrency accompanies it.
                parts.append(f"price:[..{value}]")
                parts.append(f"priceCurrency:{currency}")
            elif key == "price_min":
                parts.append(f"price:[{value}..]")
                parts.append(f"priceCurrency:{currency}")
            elif key == "conditions":
                parts.append("conditions:{%s}" % "|".join(value))
            elif key == "buying_options":
                parts.append("buyingOptions:{%s}" % "|".join(value))
            elif key == "sellers":
                parts.append("sellers:{%s}" % "|".join(value))
            elif key == "category_ids":
                params["category_ids"] = ",".join(str(c) for c in value)
            else:
                log.warning("ignoring unknown filter key %r", key)

        # Deduplicate while preserving order: price_min and price_max together
        # would otherwise emit priceCurrency twice, which eBay rejects.
        seen = set()
        deduped = [p for p in parts if not (p in seen or seen.add(p))]
        if deduped:
            params["filter"] = ",".join(deduped)
        return params

    def get_item(self, item_id: str) -> dict:
        """Full detail for one item, used by the endgame loop for live bids."""
        return self._get(f"/item/{item_id}", {})
