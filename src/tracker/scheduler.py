"""The three jobs and the tick loop that drives them.

Single-threaded on purpose: no locks, no races, and the whole run is
reproducible from the database afterwards.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .config import Settings
from .ebay.browse import BrowseClient, BrowseError
from .models import ConditionBucket, Profile, Verdict
from .normalise import normalise_all
from .notify import Notifier
from .pricing import MIN_SAMPLES, WINDOW_DAYS, compute_baseline
from .scoring import score
from .store import Store

log = logging.getLogger(__name__)

TICK_SECONDS = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tracker:
    def __init__(
        self,
        settings: Settings,
        profiles: list[Profile],
        store: Store,
        client: BrowseClient,
        notifier: Notifier,
    ):
        self.settings = settings
        self.profiles = [p for p in profiles if p.enabled]
        self.store = store
        self.client = client
        self.notifier = notifier
        self.errors_this_period = 0

    # -- jobs -------------------------------------------------------------

    def sweep(self, now: datetime | None = None) -> dict:
        """Full pass over every enabled profile."""
        now = now or utcnow()
        stats = {"profiles": 0, "listings": 0, "alerts": 0, "errors": 0}

        self.store.deactivate_ended(now)

        for profile in self.profiles:
            try:
                raw = self.client.search(
                    profile.query,
                    limit=self.settings.results_per_profile,
                    filters=profile.filters,
                )
            except BrowseError as exc:
                # One failing search must not abort the whole sweep.
                log.error("profile %s search failed: %s", profile.id, exc)
                stats["errors"] += 1
                self.errors_this_period += 1
                continue

            listings = normalise_all(raw, profile.id, self.settings.currency)
            stats["profiles"] += 1
            stats["listings"] += len(listings)

            for listing in listings:
                self.store.upsert_listing(listing, now)
                self.store.record_price(listing, now)

            self._refresh_baselines(profile, listings, now)

            for listing in listings:
                if self._judge_and_notify(listing, profile, now):
                    stats["alerts"] += 1

        log.info(
            "sweep done: %(profiles)d profiles, %(listings)d listings, "
            "%(alerts)d alerts, %(errors)d errors",
            stats,
        )
        return stats

    def endgame(self, now: datetime | None = None) -> dict:
        """Recheck only the auctions about to close.

        A 30 minute sweep cannot react to an auction ending in 12 minutes,
        which is when the price is actually decided. Cheap because it only
        touches listings already known to be worth watching.
        """
        now = now or utcnow()
        horizon = now + timedelta(minutes=self.settings.endgame_horizon_minutes)
        due = self.store.ending_between(now, horizon)
        stats = {"checked": 0, "alerts": 0, "errors": 0}

        profiles = {p.id: p for p in self.profiles}

        for stale in due:
            profile = profiles.get(stale.profile_id)
            if profile is None:
                continue
            try:
                raw = self.client.get_item(stale.item_id)
            except BrowseError as exc:
                log.warning("endgame refresh failed for %s: %s", stale.item_id, exc)
                stats["errors"] += 1
                continue

            fresh = normalise_all([raw], profile.id, self.settings.currency)
            if not fresh:
                continue
            listing = fresh[0]
            self.store.upsert_listing(listing, now)
            self.store.record_price(listing, now)
            stats["checked"] += 1

            if self._judge_and_notify(listing, profile, now):
                stats["alerts"] += 1

        if stats["checked"]:
            log.info("endgame: %(checked)d checked, %(alerts)d alerts", stats)
        return stats

    def heartbeat(self, now: datetime | None = None) -> str:
        """Liveness ping, so that the tracker stopping is visible."""
        now = now or utcnow()
        counts = self.store.counts_since(now - timedelta(hours=self.settings.heartbeat_hours))
        cold = [p.id for p in self.profiles if self._is_cold(p)]

        lines = [
            "<b>Tracker alive</b>",
            f"Profiles: {len(self.profiles)} | Active listings: "
            f"{self.store.active_listing_count()}",
            f"API calls this run: {self.client.calls_made}",
            f"Last {self.settings.heartbeat_hours}h: "
            + (
                ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                if counts
                else "nothing scored"
            ),
        ]
        if self.errors_this_period:
            lines.append(f"⚠ Errors since last heartbeat: {self.errors_this_period}")
        if cold:
            lines.append(
                f"Still building baselines (<{MIN_SAMPLES} samples): {', '.join(cold)}"
            )

        text = "\n".join(lines)
        self.notifier.send_raw(text)
        self.errors_this_period = 0
        return text

    # -- internals --------------------------------------------------------

    def _is_cold(self, profile: Profile) -> bool:
        """True when no condition bucket yet has a real (non-provisional) baseline."""
        for bucket in ConditionBucket:
            baseline = self.store.get_baseline(profile.id, bucket)
            if baseline is not None and baseline.sample_n >= MIN_SAMPLES:
                return False
        return True

    def _refresh_baselines(self, profile: Profile, listings, now: datetime) -> None:
        """Recompute only the buckets this sweep actually saw."""
        for bucket in {item.bucket for item in listings}:
            sample = self.store.observation_sample(
                profile.id, bucket, now, WINDOW_DAYS
            )
            baseline = compute_baseline(profile, bucket, sample)
            if baseline and not baseline.provisional:
                self.store.save_baseline(baseline, now)

    def _baseline_for(self, profile: Profile, bucket: ConditionBucket, now: datetime):
        stored = self.store.get_baseline(profile.id, bucket)
        if stored:
            return stored
        sample = self.store.observation_sample(profile.id, bucket, now, WINDOW_DAYS)
        return compute_baseline(profile, bucket, sample)

    def _judge_and_notify(self, listing, profile: Profile, now: datetime) -> bool:
        baseline = self._baseline_for(profile, listing.bucket, now)
        decision = score(listing, profile, baseline, now)
        if decision.verdict is not Verdict.SKIP:
            self.store.record_decision(decision, now)
            return self.notifier.maybe_notify(listing, decision, now)
        return False

    # -- loop -------------------------------------------------------------

    def run_forever(self) -> None:
        s = self.settings
        now = utcnow()
        next_sweep = now
        next_endgame = now + timedelta(seconds=s.endgame_seconds)
        next_heartbeat = now + timedelta(hours=s.heartbeat_hours)

        log.info(
            "starting: %d profiles, sweep every %dm, endgame every %ds",
            len(self.profiles),
            s.sweep_minutes,
            s.endgame_seconds,
        )

        while True:
            now = utcnow()
            try:
                if now >= next_sweep:
                    next_sweep = now + timedelta(minutes=s.sweep_minutes)
                    self.sweep(now)
                if now >= next_endgame:
                    next_endgame = now + timedelta(seconds=s.endgame_seconds)
                    self.endgame(now)
                if now >= next_heartbeat:
                    next_heartbeat = now + timedelta(hours=s.heartbeat_hours)
                    self.heartbeat(now)
            except Exception:
                # The loop has to outlive individual failures.
                log.exception("unhandled error in tick; continuing")
                self.errors_this_period += 1

            time.sleep(TICK_SECONDS)
