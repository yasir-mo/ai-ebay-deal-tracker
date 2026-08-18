"""Live API probe. Run once, by hand, before trusting the tracker.

Answers the three things the design could not settle offline:
  1. Does item_summary/search return bidCount, or does the endgame loop need
     a per-item detail call?
  2. Is shippingOptions reliably populated in search results?
  3. What does the response actually look like for this keyset?

    python scripts/smoke.py "sony a7 iii body"
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracker.__main__ import load_dotenv  # noqa: E402
from tracker.ebay.auth import TokenProvider  # noqa: E402
from tracker.ebay.browse import BrowseClient  # noqa: E402
from tracker.normalise import normalise_all  # noqa: E402


def main() -> int:
    load_dotenv()
    query = sys.argv[1] if len(sys.argv) > 1 else "sony a7 iii body"

    missing = [
        k for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET") if not os.environ.get(k)
    ]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    tokens = TokenProvider(
        os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]
    )
    client = BrowseClient(tokens, marketplace=os.environ.get("EBAY_MARKETPLACE", "EBAY_GB"))

    print(f"searching: {query!r}")
    items = client.search(query, limit=50, filters={"buying_options": ["AUCTION", "FIXED_PRICE"]})
    print(f"returned {len(items)} summaries\n")

    if not items:
        print("no results - check the marketplace id and the query")
        return 1

    out = Path("smoke_response.json")
    out.write_text(json.dumps(items[:5], indent=2), encoding="utf-8")
    print(f"first 5 raw items written to {out} (useful as a test fixture)\n")

    auctions = [i for i in items if "AUCTION" in (i.get("buyingOptions") or [])]
    print("--- Q1: bidCount on search results? ---")
    print(f"auction summaries: {len(auctions)}")
    print(f"  with bidCount:        {sum('bidCount' in i for i in auctions)}")
    print(f"  with currentBidPrice: {sum('currentBidPrice' in i for i in auctions)}")
    print(f"  with itemEndDate:     {sum('itemEndDate' in i for i in auctions)}")
    if auctions and not any("bidCount" in i for i in auctions):
        print("  -> endgame loop MUST use get_item() for live bid counts")

    print("\n--- Q2: shipping present in search results? ---")
    with_shipping = sum(bool(i.get("shippingOptions")) for i in items)
    print(f"  {with_shipping}/{len(items)} have shippingOptions")
    if with_shipping < len(items):
        print(
            f"  -> {len(items) - with_shipping} listings will be flagged "
            "'postage unknown' and capped below BUY NOW"
        )

    print("\n--- Q3: normalisation ---")
    listings = normalise_all(items, "smoke", os.environ.get("EBAY_CURRENCY", "GBP"))
    print(f"  {len(listings)}/{len(items)} normalised cleanly")
    print(f"  condition buckets: {dict(Counter(l.bucket.value for l in listings))}")

    print("\n--- sample ---")
    for listing in listings[:5]:
        ship = (
            f"+{listing.shipping_pence / 100:.2f}"
            if listing.shipping_known
            else "+???"
        )
        print(
            f"  {listing.total_pence / 100:>9,.2f} ({listing.price_pence / 100:.2f}{ship}) "
            f"{listing.buying_option.value:<11} {listing.title[:55]}"
        )

    print(f"\nAPI calls used: {client.calls_made}")
    print(
        "\nQuota: check developer.ebay.com -> Application Keys -> Analytics for "
        "this keyset's actual daily Browse limit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
