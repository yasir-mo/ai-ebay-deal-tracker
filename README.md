# AI eBay Deal Tracker

Polls eBay on a schedule for a set of saved searches, builds its own price
baselines over time, and sends a Telegram alert when something is listed well
below the going rate.

Single user, SQLite, one process. No external services beyond eBay and
Telegram.

## Status

Two scoring stages, in order. **Deterministic rules** always run and are
sufficient on their own. An **optional model stage** then judges only the
listings the rules already rated worth sending, and is off by default
(`[ai] enabled = false`).

That ordering is the whole design. The rules reject the large majority of
listings for nothing, so the model only ever sees what is genuinely ambiguous,
which is both cheaper and the only place a model adds anything. If the model
is disabled, unreachable, or out of budget, the tracker keeps sending
rule-based alerts unchanged.

## What it compares against

Prices are judged against the median **asking** price the tracker has observed
itself over the last 30 days, per search and per condition.

That is not the same as market value. eBay's public Browse API returns active
listings only; sold prices are behind the Marketplace Insights API, which
requires separate approval that most accounts do not get. Rather than pretend
otherwise, every alert states which baseline it used and flags the ones still
running on your manual estimate.

In practice it is useful immediately on searches where you set a `target`, and
gets better after a week or two of collecting history.

## Setup

Requires Python 3.11 or newer.

1. Create an app at [developer.ebay.com](https://developer.ebay.com) and take
   the **Production** keyset (App ID and Cert ID).

2. Create a Telegram bot: message `@BotFather`, send `/newbot`, keep the token.
   Send your new bot a message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read your chat id.

3. Install and configure:

   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   cp profiles.toml.example profiles.toml
   ```

   Fill in the four secrets in `.env` and edit your searches in
   `profiles.toml`.

4. Check the live API before trusting anything. This reports whether your
   keyset returns bid counts and postage on search results, and writes a
   sample response you can inspect:

   ```bash
   python scripts/smoke.py "sony a7 iii body"
   ```

5. `dry_run = true` is the default, so alerts are logged instead of sent:

   ```bash
   python -m tracker once
   ```

   Once the output looks right, set `dry_run = false` and start it properly:

   ```bash
   python -m tracker run
   ```

## Commands

| Command | Purpose |
|---|---|
| `python -m tracker run` | Run continuously: sweep, endgame and heartbeat loops. |
| `python -m tracker once` | One sweep, then exit. Useful for cron or testing. |
| `python -m tracker endgame` | Force a check of auctions about to close. |
| `python -m tracker heartbeat` | Force a status message. |
| `python -m tracker outcome --item ID --verdict BUY_NOW --result bought` | Record whether an alert was worth acting on. |

## How listings are judged

Hard rejects are applied first: a title blocklist (`for parts`,
`spares or repair`, `box only` and similar), seller feedback floors, your price
ceiling, and a floor at 15% of baseline, since something far too cheap is
usually a scam rather than a bargain.

Surviving listings are compared to the baseline for that search and condition:

| Verdict | Rule |
|---|---|
| PRIORITY | Model stage only: strong resale margin on real data (see below) |
| BUY NOW | Fixed price, at least 35% below |
| GOOD DEAL | At least 20% below |
| WATCH | Auction at least 30% below, closing within 24 hours |
| SKIP | Recorded, never sent |

Auctions never reach BUY NOW, because the current bid is not the final price.

Everything is compared on landed cost, meaning item price plus postage. When a
listing does not publish a postage cost, the total is only a lower bound, so it
is flagged and held below BUY NOW instead of being treated as free delivery.

Thresholds are constants at the top of `src/tracker/scoring.py`.

## Repeat alerts

Alerts are suppressed per `(item_id, verdict)`. Each listing alerts once per
tier, so 48 sweeps a day produce one message, but a WATCH that later becomes a
BUY NOW will alert again because the situation has changed.

Known limitation: a seller relisting under a new item id will alert again.

## The three loops

* **Sweep**, every 30 minutes. Each search: query, store, rebuild baselines,
  score, alert.
* **Endgame**, every 60 seconds. Only listings closing within 15 minutes, each
  refreshed individually. Without this an auction ending in 12 minutes is not
  seen until after it has closed.
* **Heartbeat**, every 6 hours. Liveness, counters, error count, and which
  searches are still too new to have a real baseline. If the tracker dies
  quietly you would otherwise not notice for days.

## Running as a service

```ini
# /etc/systemd/system/ebay-tracker.service
[Unit]
Description=eBay deal tracker
After=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/ebay-tracker
ExecStart=/usr/bin/python3 -m tracker run
Environment=PYTHONPATH=/opt/ebay-tracker/src
Environment=PYTHONIOENCODING=utf-8
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## API quota

One search call per profile per sweep. Twenty searches on a 30 minute sweep is
roughly 960 calls a day, plus detail calls for auctions in their final minutes.
eBay's default production quota for Browse is well above that, but check yours
in the developer console; `scripts/smoke.py` prints where to look.

## Tests

```bash
python -m pytest tests/ -q
```

198 tests, no network access required and no API keys. The important ones are
in `test_scoring.py` (what counts as a deal), `test_normalise.py` (parsing real
API responses), `test_browse.py` (filter syntax and retry behaviour),
`test_ai_stage.py` (judging, budget enforcement, refusal and failure handling)
and `test_integration.py` (the whole pipeline against a fake eBay, a fake
Telegram and a fake model).

## The model stage

Off by default. To enable it, set `[ai] enabled = true`, put an
`ANTHROPIC_API_KEY` in `.env`, and `pip install anthropic`.

It is asked only the things rules genuinely cannot judge:

* **Is this actually the item?** The most common failure in this data is an
  accessory, a spare part, a case, a manual, an empty box, or a different
  model that merely mentions the target in its title. A price far below market
  is more often one of those than a real bargain.
* **Does the description contradict the stated condition,** or is it
  conspicuously thin for the money being asked?
* **Could this be resold near the going rate?**

What it is allowed to do with the answer is deliberately asymmetric. It can
always reject or downgrade. It can only promote to PRIORITY when the rules,
the margin model, and the model itself all agree: a real baseline built from
observed history, an estimated margin clearing both thresholds, and high
confidence from the model. A provisional baseline can never produce a priority
alert, because calling something arbitrage when the market price is a number
you typed in yourself is the fastest way to lose money with this tool.

### Cost control

Four mechanisms, in descending order of how much they save:

1. **The rules run first.** The model never sees a listing the rules rejected.
2. **Batching.** One request judges up to `batch_size` listings, so
   per-request overhead and the cached prompt are paid once, not once each.
3. **Prompt caching.** The system prompt is byte-stable across every request,
   so all but the first call read it at a fraction of the input price.
4. **Verdict reuse.** A judgement is cached against the price it was made
   about. An unchanged listing is never re-judged; a repriced one always is.

On top of that, `daily_budget` is a hard stop rather than a warning. When the
estimated spend for the day is reached, judging stops and the tracker carries
on with rule verdicts alone.

### Estimated resale margin

`margin.py` works out what a listing could return if resold at the going rate,
after eBay's fees and outbound postage. It discounts the baseline by a
realisation factor first, because the baseline is what things are *asked* for
and they generally sell for less. It is a ranking signal, not a valuation.

## Roadmap

Detecting relists under a changed item id, and per search poll intervals
rather than one global sweep.

## Licence

MIT. See [LICENSE](LICENSE).
