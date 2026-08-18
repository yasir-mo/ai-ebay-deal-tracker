# Design notes

Background on why the tracker is built the way it is. Useful if you are
changing the scoring rules or the polling schedule.

## Constraints

The central constraint is that eBay's public Browse API returns active
listings only. Sold and completed prices live in the Marketplace Insights API,
which is a limited release requiring separate approval. Most personal accounts
do not have it.

That rules out the obvious design, which would be to compare an asking price
against recent sold prices. Instead the tracker accumulates its own price
history and compares against the median asking price it has seen for the same
search and condition over the last 30 days. This is weaker, and the code says
so wherever it surfaces a number, but it needs no extra access and improves on
its own as history builds up.

## Choices

| Area | Choice | Reason |
|---|---|---|
| Hosting | One process, SQLite | Nothing to administer, trivial to back up |
| Scoring | Deterministic rules | No sold comps exist yet to calibrate a model against |
| Notification | Telegram | Instant on mobile, no hosting, supports links |
| Config | TOML via stdlib `tomllib` | Avoids a YAML dependency |
| HTTP | `requests` | Sync is sufficient at this volume |

## Not included yet

* Model based scoring. This is on the roadmap, and `scoring.py` is written as
  a pure function specifically so it can be added behind the rules without
  restructuring. It is not implemented; nothing here calls an LLM today. The
  ordering is deliberate: a model needs history to calibrate against, and the
  tracker has to collect that history first.
* Sold price comparison, for the access reason above.
* Detecting relists under a changed item id.
* Buying anything. The tracker notifies, the human decides.

## Structure

One process, one thread, one tick every five seconds that runs three jobs when
they come due.

**Sweep**, default 30 minutes. For every enabled search: query the API,
normalise, upsert, record a price observation, recompute the baseline, score,
and notify.

**Endgame**, 60 seconds. Only listings already in the database whose end time
falls within the next 15 minutes, refetched individually and rescored. A 30
minute sweep cannot react to an auction closing in 12 minutes, which is exactly
when the price is decided. It stays cheap because it only touches listings
already known to be worth watching.

**Heartbeat**, 6 hours. Posts liveness and counters to Telegram. The failure
mode of a tracker like this is stopping quietly, so silence has to be visible.

Single threaded deliberately: no locking, no races, and any run can be
reconstructed from the database afterwards.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Load and validate `profiles.toml` and env secrets |
| `models.py` | `Listing`, `Baseline`, `Verdict`, `Decision` |
| `ebay/auth.py` | Client credentials token, cached until near expiry |
| `ebay/browse.py` | Search and item detail, filter syntax, retry |
| `normalise.py` | eBay JSON into `Listing` |
| `store.py` | All SQL. Nothing else touches the database |
| `pricing.py` | Rolling median and p25 per search and condition |
| `scoring.py` | Pure `(Listing, Baseline, Profile) -> Decision` |
| `notify.py` | Telegram send, suppression, message formatting |
| `scheduler.py` | The three jobs and the tick loop |

`scoring.py` has no I/O on purpose. It is the part that gets tuned most often
and the part where a mistake costs real money, so it has to be testable from
fixtures alone.

## Data

All money is stored as integer pence. No float touches a price.

```sql
listings(item_id PK, profile_id, title, condition, condition_id, bucket,
         buying_option, price_pence, shipping_pence, total_pence, currency,
         seller_name, seller_feedback_pct, seller_feedback_score,
         end_time, item_url, bid_count, first_seen, last_seen, is_active)

price_history(item_id, observed_at, total_pence, bid_count)

baselines(profile_id, bucket, computed_at, median_pence, p25_pence, sample_n)

decisions(item_id, profile_id, verdict, decided_at, total_pence,
          baseline_pence, discount_pct, reason_json, notified_at, outcome)
```

`price_history` is what makes baselines possible. `decisions.outcome` is filled
in by hand and is the only feedback on whether the alerts were any good.

### Condition buckets

eBay's condition ids are collapsed into three buckets so that baselines reach a
usable sample size: `NEW` (1000 to 1500), `REFURB` (2000 to 2500) and `USED`
(everything else). Fifteen separate condition ids would fragment the sample too
finely to ever be meaningful.

### Sampling

Baselines take the most recent observation per item within the window, not
every row. A listing that sits unsold for a month is observed 48 times a day;
counting each one would let stale, overpriced stock dominate the median.

The sample is then trimmed 10% at each end before the median is taken, which
removes both the obvious scams and the wildly optimistic prices.

## Scoring

Hard rejects, in order:

1. Title matches the blocklist
2. Seller feedback percentage below the search's floor
3. Seller feedback count below the search's floor
4. Landed cost above the search's ceiling
5. Landed cost below 15% of baseline

The last one matters more than it looks. Something priced at a tenth of market
is not the best deal of the year, it is a scam or a listing for an accessory,
and without this rule those would be the highest confidence alerts the tracker
ever sent.

Then, by discount against baseline:

| Verdict | Rule |
|---|---|
| BUY NOW | Fixed price, 35% or more below |
| GOOD DEAL | 20% or more below |
| WATCH | Auction 30% or more below, closing within 24 hours |
| SKIP | Everything else |

Auctions cannot reach BUY NOW because the current bid is not the final price.
They need a wider gap than fixed price listings for the same reason.

Below 15 observations there is no baseline and the search falls back to the
manual `target`. The heartbeat reports which searches are still in that state.

### Unknown postage

`shippingOptions` is absent on calculated shipping and collection only
listings. Treating that as free postage would defeat the point of comparing
landed cost, so it is modelled as genuinely unknown: the total becomes a lower
bound, the alert is flagged, and the verdict is capped below BUY NOW.

## Error handling

* 429 and 5xx: exponential backoff, four attempts, then the search is skipped
  for this sweep and the error counted. One failing search never aborts a
  sweep.
* 4xx other than 429 is not retried. A malformed request is not transient and
  retrying only burns quota.
* Tokens are refreshed with a 60 second margin.
* A failed Telegram send leaves the decision unmarked, so the next sweep
  retries it.
* Any unhandled exception in a tick is logged and swallowed. The loop outlives
  individual failures.

## Testing

`scoring.py`, `normalise.py`, `pricing.py` and the filter builder are unit
tested against recorded responses. `store.py` is tested against a temporary
SQLite file. `test_integration.py` runs the whole pipeline against a fake API
and a fake Telegram. The scheduler loop itself is not tested; it is thin
wiring.

## Verifying against the live API

Three things could not be settled without production credentials:

1. Whether `bidCount` appears on search results or needs a per item call,
   which changes what the endgame loop costs.
2. Whether `shippingOptions` is reliably populated in search results.
3. The real daily quota on a given keyset.

`scripts/smoke.py` answers all three in one run.
