"""All SQL lives here. Nothing else in the codebase touches the database."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Decision,
    Listing,
    Profile,
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

CREATE TABLE IF NOT EXISTS profiles (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    query              TEXT NOT NULL,
    ceiling_pence      INTEGER NOT NULL,
    target_pence       INTEGER,
    min_feedback_pct   REAL NOT NULL DEFAULT 95.0,
    min_feedback_count INTEGER NOT NULL DEFAULT 10,
    filters_json       TEXT NOT NULL DEFAULT '{}',
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_judgements (
    item_id           TEXT NOT NULL,
    judged_at_price   INTEGER NOT NULL,
    judged_at         TEXT NOT NULL,
    is_target_item    INTEGER NOT NULL,
    condition_risk    TEXT NOT NULL,
    resale_confidence TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    concerns_json     TEXT NOT NULL,
    rationale         TEXT NOT NULL,
    PRIMARY KEY (item_id, judged_at_price)
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class Store:
    """SQLite access, safe to share between the tick loop and the web thread.

    sqlite3 connections cannot cross threads, so each thread gets its own via
    thread-local storage. WAL mode allows concurrent readers alongside a single
    writer, and busy_timeout absorbs the brief contention that a one-user web
    UI can produce.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._shared_memory = self.path == ":memory:"
        if self._shared_memory:
            # An in-memory database is per-connection, so a thread-local one
            # would give each thread its own empty database. Tests want one.
            self._memory_conn = self._new_connection()
        self._init_schema()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared_memory:
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None) if not self._shared_memory else self._memory_conn
        if conn is not None:
            conn.close()
            if not self._shared_memory:
                self._local.conn = None

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

    # -- profiles ---------------------------------------------------------

    def save_profile(self, profile: Profile, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO profiles (
                id, name, query, ceiling_pence, target_pence, min_feedback_pct,
                min_feedback_count, filters_json, enabled, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                query=excluded.query,
                ceiling_pence=excluded.ceiling_pence,
                target_pence=excluded.target_pence,
                min_feedback_pct=excluded.min_feedback_pct,
                min_feedback_count=excluded.min_feedback_count,
                filters_json=excluded.filters_json,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (
                profile.id,
                profile.name,
                profile.query,
                profile.ceiling_pence,
                profile.target_pence,
                profile.min_feedback_pct,
                profile.min_feedback_count,
                json.dumps(profile.filters),
                int(profile.enabled),
                _iso(now),
                _iso(now),
            ),
        )
        self.conn.commit()

    def list_profiles(self, include_disabled: bool = True) -> list[Profile]:
        sql = "SELECT * FROM profiles"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return [self._row_to_profile(r) for r in self.conn.execute(sql).fetchall()]

    def get_profile(self, profile_id: str) -> Profile | None:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return self._row_to_profile(row) if row else None

    def delete_profile(self, profile_id: str) -> None:
        self.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self.conn.commit()

    def set_profile_enabled(self, profile_id: str, enabled: bool, now: datetime) -> None:
        self.conn.execute(
            "UPDATE profiles SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _iso(now), profile_id),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> Profile:
        return Profile(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            ceiling_pence=row["ceiling_pence"],
            target_pence=row["target_pence"],
            min_feedback_pct=row["min_feedback_pct"],
            min_feedback_count=row["min_feedback_count"],
            filters=json.loads(row["filters_json"]),
            enabled=bool(row["enabled"]),
        )

    # -- settings ---------------------------------------------------------

    def set_setting(self, key: str, value, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value), _iso(now)),
        )
        self.conn.commit()

    def get_setting(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def all_settings(self) -> dict:
        return {
            r["key"]: json.loads(r["value"])
            for r in self.conn.execute("SELECT key, value FROM settings").fetchall()
        }

    # -- ai judgements ----------------------------------------------------

    def get_judgement(self, item_id: str, total_pence: int) -> dict | None:
        """Reuse a judgement only while the price is unchanged.

        Keying on price is what stops the tracker paying to re-judge the same
        unchanged listing 48 times a day, while still re-judging the moment
        the thing it was judging actually changes.
        """
        row = self.conn.execute(
            "SELECT * FROM ai_judgements WHERE item_id = ? AND judged_at_price = ?",
            (item_id, total_pence),
        ).fetchone()
        return dict(row) if row else None

    def save_judgement(self, item_id: str, total_pence: int, judgement, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO ai_judgements (
                item_id, judged_at_price, judged_at, is_target_item,
                condition_risk, resale_confidence, verdict, concerns_json, rationale
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id, judged_at_price) DO UPDATE SET
                judged_at=excluded.judged_at,
                is_target_item=excluded.is_target_item,
                condition_risk=excluded.condition_risk,
                resale_confidence=excluded.resale_confidence,
                verdict=excluded.verdict,
                concerns_json=excluded.concerns_json,
                rationale=excluded.rationale
            """,
            (
                item_id,
                total_pence,
                _iso(now),
                int(judgement.is_target_item),
                judgement.condition_risk,
                judgement.resale_confidence,
                judgement.verdict,
                json.dumps(judgement.concerns),
                judgement.rationale,
            ),
        )
        self.conn.commit()

    def prune_judgements(self, keep_days: int, now: datetime) -> int:
        cutoff = _iso(now - timedelta(days=keep_days))
        cur = self.conn.execute(
            "DELETE FROM ai_judgements WHERE judged_at < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount

    # -- dashboard queries -------------------------------------------------

    def pending_decisions(self, limit: int = 100) -> list[dict]:
        """Notified alerts the user has not yet recorded an outcome for.

        Ordered by how soon a decision has to be made: anything with an end
        time first, soonest first, then the rest by verdict strength.
        """
        rows = self.conn.execute(
            """
            SELECT d.item_id, d.profile_id, d.verdict, d.total_pence,
                   d.baseline_pence, d.discount_pct, d.reason_json, d.decided_at,
                   l.title, l.item_url, l.end_time, l.bid_count, l.shipping_pence,
                   l.currency, p.name AS profile_name,
                   j.rationale
            FROM decisions d
            JOIN listings l ON l.item_id = d.item_id
            LEFT JOIN profiles p ON p.id = d.profile_id
            LEFT JOIN ai_judgements j
                   ON j.item_id = d.item_id AND j.judged_at_price = d.total_pence
            WHERE d.notified_at IS NOT NULL
              AND d.outcome IS NULL
              AND d.verdict != 'SKIP'
            ORDER BY (l.end_time IS NULL), l.end_time ASC, d.decided_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._decision_row(r) for r in rows]

    def decisions_for_profile(self, profile_id: str, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT d.*, l.title, l.item_url
            FROM decisions d
            LEFT JOIN listings l ON l.item_id = d.item_id
            WHERE d.profile_id = ?
            ORDER BY d.decided_at DESC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def busiest_profile(self) -> str | None:
        """The search with the most observations, for defaulting the history view.

        Landing on an alphabetically-first search that happens to have no data
        makes the page look broken when the tracker is working fine.
        """
        row = self.conn.execute(
            """
            SELECT l.profile_id, COUNT(*) AS n
            FROM price_history ph
            JOIN listings l ON l.item_id = ph.item_id
            GROUP BY l.profile_id
            ORDER BY n DESC
            LIMIT 1
            """
        ).fetchone()
        return row["profile_id"] if row else None

    def price_series(self, profile_id: str, days: int, now: datetime) -> list[int]:
        since = _iso(now - timedelta(days=days))
        rows = self.conn.execute(
            """
            SELECT ph.total_pence
            FROM price_history ph
            JOIN listings l ON l.item_id = ph.item_id
            WHERE l.profile_id = ? AND ph.observed_at >= ?
            ORDER BY ph.observed_at
            """,
            (profile_id, since),
        ).fetchall()
        return [r["total_pence"] for r in rows]

    def alert_count(self, profile_id: str, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions "
            "WHERE profile_id = ? AND verdict != 'SKIP' AND decided_at >= ?",
            (profile_id, _iso(since)),
        ).fetchone()["n"]

    def recent_listings(self, days: int, now: datetime, limit: int = 2000) -> list[Listing]:
        """Stored listings for the settings-screen backtest."""
        since = _iso(now - timedelta(days=days))
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [self._row_to_listing(r) for r in rows]

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["reasons"] = json.loads(out.pop("reason_json") or "[]")
        out["end_time"] = _dt(out["end_time"])
        return out

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
