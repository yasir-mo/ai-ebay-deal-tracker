"""All SQL lives here. Nothing else in the codebase touches the database."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Decision,
    Listing,
    Verdict,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id               TEXT PRIMARY KEY,
    profile_id            TEXT NOT NULL,
    title                 TEXT NOT NULL,
    condition             TEXT,
    condition_id          INTEGER,
    bucket                TEXT NOT NULL,
    buying_option         TEXT NOT NULL,
    price_pence           INTEGER NOT NULL,
    shipping_pence        INTEGER,
    total_pence           INTEGER NOT NULL,
    currency              TEXT NOT NULL,
    seller_name           TEXT,
    seller_feedback_pct   REAL,
    seller_feedback_score INTEGER,
    end_time              TEXT,
    item_url              TEXT NOT NULL,
    bid_count             INTEGER,
    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,
    is_active             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listings_profile ON listings(profile_id, bucket);
CREATE INDEX IF NOT EXISTS idx_listings_end ON listings(end_time)
    WHERE end_time IS NOT NULL;

CREATE TABLE IF NOT EXISTS price_history (
    item_id     TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    total_pence INTEGER NOT NULL,
    bid_count   INTEGER,
    PRIMARY KEY (item_id, observed_at)
);

CREATE TABLE IF NOT EXISTS baselines (
    profile_id    TEXT NOT NULL,
    bucket        TEXT NOT NULL,
    computed_at   TEXT NOT NULL,
    median_pence  INTEGER NOT NULL,
    p25_pence     INTEGER NOT NULL,
    sample_n      INTEGER NOT NULL,
    PRIMARY KEY (profile_id, bucket)
);

CREATE TABLE IF NOT EXISTS decisions (
    item_id        TEXT NOT NULL,
    profile_id     TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    total_pence    INTEGER NOT NULL,
    baseline_pence INTEGER,
    discount_pct   REAL,
    reason_json    TEXT NOT NULL,
    notified_at    TEXT,
    outcome        TEXT,
    PRIMARY KEY (item_id, verdict)
);
CREATE INDEX IF NOT EXISTS idx_decisions_decided ON decisions(decided_at);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- listings ---------------------------------------------------------

    def upsert_listing(self, listing: Listing, now: datetime) -> None:
        """Insert or refresh. `first_seen` is preserved across updates."""
        self.conn.execute(
            """
            INSERT INTO listings (
                item_id, profile_id, title, condition, condition_id, bucket,
                buying_option, price_pence, shipping_pence, total_pence,
                currency, seller_name, seller_feedback_pct, seller_feedback_score,
                end_time, item_url, bid_count, first_seen, last_seen, is_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(item_id) DO UPDATE SET
                title=excluded.title,
                price_pence=excluded.price_pence,
                shipping_pence=excluded.shipping_pence,
                total_pence=excluded.total_pence,
                end_time=excluded.end_time,
                bid_count=excluded.bid_count,
                last_seen=excluded.last_seen,
                is_active=1
            """,
            (
                listing.item_id,
                listing.profile_id,
                listing.title,
                listing.condition,
                listing.condition_id,
                listing.bucket.value,
                listing.buying_option.value,
                listing.price_pence,
                listing.shipping_pence,
                listing.total_pence,
                listing.currency,
                listing.seller_name,
                listing.seller_feedback_pct,
                listing.seller_feedback_score,
                _iso(listing.end_time),
                listing.item_url,
                listing.bid_count,
                _iso(now),
                _iso(now),
            ),
        )
        self.conn.commit()

    def get_listing(self, item_id: str) -> Listing | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE item_id = ?", (item_id,)
        ).fetchone()
        return self._row_to_listing(row) if row else None

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> Listing:
        return Listing(
            item_id=row["item_id"],
            profile_id=row["profile_id"],
            title=row["title"],
            condition=row["condition"],
            condition_id=row["condition_id"],
            buying_option=BuyingOption(row["buying_option"]),
            price_pence=row["price_pence"],
            shipping_pence=row["shipping_pence"],
            currency=row["currency"],
            seller_name=row["seller_name"],
            seller_feedback_pct=row["seller_feedback_pct"],
            seller_feedback_score=row["seller_feedback_score"],
            end_time=_dt(row["end_time"]),
            item_url=row["item_url"],
            bid_count=row["bid_count"],
        )

    def ending_between(self, start: datetime, end: datetime) -> list[Listing]:
        """Active auctions ending in the window. The endgame loop's work list."""
        rows = self.conn.execute(
            """
            SELECT * FROM listings
            WHERE is_active = 1
              AND end_time IS NOT NULL
              AND end_time > ? AND end_time <= ?
            ORDER BY end_time
            """,
            (_iso(start), _iso(end)),
        ).fetchall()
        return [self._row_to_listing(r) for r in rows]

    def deactivate_ended(self, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE listings SET is_active = 0 "
            "WHERE is_active = 1 AND end_time IS NOT NULL AND end_time <= ?",
            (_iso(now),),
        )
        self.conn.commit()
        return cur.rowcount

    # -- price history ----------------------------------------------------

    def record_price(self, listing: Listing, now: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO price_history "
            "(item_id, observed_at, total_pence, bid_count) VALUES (?,?,?,?)",
            (listing.item_id, _iso(now), listing.total_pence, listing.bid_count),
        )
        self.conn.commit()

    def observation_sample(
        self, profile_id: str, bucket: ConditionBucket, now: datetime, window_days: int
    ) -> list[int]:
        """One price per item: the most recent observation inside the window.

        Taking every row would count a stale listing once per sweep, letting
        long-unsold stock dominate the median.
        """
        since = _iso(now - timedelta(days=window_days))
        rows = self.conn.execute(
            """
            SELECT ph.total_pence
            FROM price_history ph
            JOIN listings l ON l.item_id = ph.item_id
            JOIN (
                SELECT item_id, MAX(observed_at) AS newest
                FROM price_history
                WHERE observed_at >= ?
                GROUP BY item_id
            ) latest
              ON latest.item_id = ph.item_id AND latest.newest = ph.observed_at
            WHERE l.profile_id = ? AND l.bucket = ?
            """,
            (since, profile_id, bucket.value),
        ).fetchall()
        return [r["total_pence"] for r in rows]

    # -- baselines --------------------------------------------------------

    def save_baseline(self, baseline: Baseline, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO baselines
                (profile_id, bucket, computed_at, median_pence, p25_pence, sample_n)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(profile_id, bucket) DO UPDATE SET
                computed_at=excluded.computed_at,
                median_pence=excluded.median_pence,
                p25_pence=excluded.p25_pence,
                sample_n=excluded.sample_n
            """,
            (
                baseline.profile_id,
                baseline.bucket.value,
                _iso(now),
                baseline.median_pence,
                baseline.p25_pence,
                baseline.sample_n,
            ),
        )
        self.conn.commit()

    def get_baseline(
        self, profile_id: str, bucket: ConditionBucket
    ) -> Baseline | None:
        row = self.conn.execute(
            "SELECT * FROM baselines WHERE profile_id = ? AND bucket = ?",
            (profile_id, bucket.value),
        ).fetchone()
        if not row:
            return None
        return Baseline(
            profile_id=row["profile_id"],
            bucket=ConditionBucket(row["bucket"]),
            median_pence=row["median_pence"],
            p25_pence=row["p25_pence"],
            sample_n=row["sample_n"],
        )

    # -- decisions and notification suppression ---------------------------

    def record_decision(self, decision: Decision, now: datetime) -> None:
        """Upsert on (item_id, verdict).

        `notified_at` is deliberately not overwritten; that is what stops the
        same alert firing on every sweep.
        """
        self.conn.execute(
            """
            INSERT INTO decisions (
                item_id, profile_id, verdict, decided_at, total_pence,
                baseline_pence, discount_pct, reason_json
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id, verdict) DO UPDATE SET
                decided_at=excluded.decided_at,
                total_pence=excluded.total_pence,
                baseline_pence=excluded.baseline_pence,
                discount_pct=excluded.discount_pct,
                reason_json=excluded.reason_json
            """,
            (
                decision.item_id,
                decision.profile_id,
                decision.verdict.value,
                _iso(now),
                decision.total_pence,
                decision.baseline_pence,
                decision.discount_pct,
                json.dumps(decision.reasons),
            ),
        )
        self.conn.commit()

    def already_notified(self, item_id: str, verdict: Verdict) -> bool:
        row = self.conn.execute(
            "SELECT notified_at FROM decisions WHERE item_id = ? AND verdict = ?",
            (item_id, verdict.value),
        ).fetchone()
        return bool(row and row["notified_at"])

    def mark_notified(self, item_id: str, verdict: Verdict, now: datetime) -> None:
        self.conn.execute(
            "UPDATE decisions SET notified_at = ? WHERE item_id = ? AND verdict = ?",
            (_iso(now), item_id, verdict.value),
        )
        self.conn.commit()

    def set_outcome(self, item_id: str, verdict: Verdict, outcome: str) -> None:
        """Manual feedback on whether an alert was worth acting on."""
        self.conn.execute(
            "UPDATE decisions SET outcome = ? WHERE item_id = ? AND verdict = ?",
            (outcome, item_id, verdict.value),
        )
        self.conn.commit()

    # -- reporting --------------------------------------------------------

    def counts_since(self, since: datetime) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM decisions "
            "WHERE decided_at >= ? GROUP BY verdict",
            (_iso(since),),
        ).fetchall()
        return {r["verdict"]: r["n"] for r in rows}

    def active_listing_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE is_active = 1"
        ).fetchone()["n"]
