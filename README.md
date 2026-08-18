# AI eBay Deal Tracker

Polls eBay on a schedule for a set of saved searches, builds its own price
baselines over time, and sends a Telegram alert when something is listed well
below the going rate.

Single user, SQLite, one process. No external services beyond eBay and
Telegram.

## Status

Scoring is currently **deterministic rules**, not a model. The pipeline is
built so that a model based stage drops in behind those rules without
restructuring anything (see [Roadmap](#roadmap)), and that is the direction
the project is going, but it is not there yet. Nothing in this repository
calls an LLM today.

The reason for that ordering is practical rather than ideological: a model
scoring listings needs something to calibrate against, and until the tracker
has collected its own price history there is nothing to calibrate with. Rules
first, history second, model third.

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

142 tests, no network access required. The important ones are in
`test_scoring.py` (what counts as a deal), `test_normalise.py` (parsing real
API responses), `test_browse.py` (filter syntax and retry behaviour) and
`test_integration.py` (the whole pipeline against a fake eBay and a fake
Telegram).

## Roadmap

The planned model based stage sits behind the existing rules rather than
replacing them. `scoring.py` is a pure function with no I/O specifically so
this can be added without restructuring anything else.

The intended shape:

1. Rules run first and reject the obvious cases, which is most of the volume.
2. Only the survivors go to a model, since sending every listing would be
   expensive and inconsistent for no benefit.
3. The model is asked the things rules genuinely cannot judge: is this the
   actual item or an accessory or a compatible-with listing, does the
   description contradict the stated condition, is the photo a stock image.
4. Verdicts are recorded either way, and `decisions.outcome` is filled in by
   hand, which is what gives something to calibrate against later.

Also planned: detecting relists under a changed item id, and per search poll
intervals rather than one global sweep.

## Licence

MIT. See [LICENSE](LICENSE).
