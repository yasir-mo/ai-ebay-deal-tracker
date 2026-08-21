"""Guards against becoming a nuisance to eBay.

Three separate concerns, deliberately kept apart from the HTTP client so each
can be reasoned about and tested on its own:

* A circuit breaker for authentication. Repeated `invalid_client` failures from
  one address look exactly like credential probing, and are the fastest way to
  get an IP blocked. Bad credentials are never transient, so retrying them on a
  schedule is both useless and hostile.
* A daily call budget. A bug in the scheduler should cost a wasted day of
  polling, not an API ban.
* Retry-After compliance. When a server states how long to wait, guessing a
  shorter interval is the wrong answer.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Consecutive auth failures before the breaker opens. Two is enough to rule
#: out a one-off blip while still reacting fast.
AUTH_FAILURE_THRESHOLD = 2

#: Cooldown after the breaker opens, doubling each time it reopens.
AUTH_COOLDOWN_START = 15 * 60
AUTH_COOLDOWN_MAX = 24 * 60 * 60

#: Default ceiling on requests per rolling day. eBay's production Browse quota
#: is higher than this for most keysets; the point is to bound a runaway loop,
#: not to use the whole allowance.
DEFAULT_DAILY_CALL_LIMIT = 4000

#: Never sleep longer than this on a Retry-After, however large the header.
MAX_RETRY_AFTER = 15 * 60


class CircuitOpen(Exception):
    """Raised instead of making a request the client should not make."""


class QuotaExceeded(Exception):
    """Raised when the local daily call budget is spent."""


@dataclass
class AuthCircuitBreaker:
    """Stops calling the token endpoint once credentials are clearly wrong.

    Wrong credentials do not fix themselves. Continuing to present them every
    sweep is the behaviour that gets an address blocked, so after a couple of
    consecutive failures this refuses to make the call at all until a cooldown
    has passed, backing off further each time it reopens.
    """

    threshold: int = AUTH_FAILURE_THRESHOLD
    cooldown: float = AUTH_COOLDOWN_START
    max_cooldown: float = AUTH_COOLDOWN_MAX
    _clock: callable = time.monotonic
    _failures: int = 0
    _open_until: float = 0.0
    _current_cooldown: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def before_request(self) -> None:
        with self._lock:
            if self._open_until and self._clock() < self._open_until:
                remaining = int(self._open_until - self._clock())
                raise CircuitOpen(
                    f"not retrying authentication for another {remaining}s: "
                    f"{self._failures} consecutive failures suggest the eBay "
                    "credentials are wrong. Fix them and restart."
                )

    def record_success(self) -> None:
        with self._lock:
            if self._failures:
                log.info("eBay authentication recovered after %d failures", self._failures)
            self._failures = 0
            self._open_until = 0.0
            self._current_cooldown = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures < self.threshold:
                return
            self._current_cooldown = (
                self.cooldown
                if not self._current_cooldown
                else min(self._current_cooldown * 2, self.max_cooldown)
            )
            self._open_until = self._clock() + self._current_cooldown
            log.error(
                "pausing eBay authentication for %d minutes after %d consecutive "
                "failures; check EBAY_CLIENT_ID and EBAY_CLIENT_SECRET",
                int(self._current_cooldown // 60),
                self._failures,
            )

    @property
    def is_open(self) -> bool:
        with self._lock:
            return bool(self._open_until and self._clock() < self._open_until)

    @property
    def consecutive_failures(self) -> int:
        return self._failures


@dataclass
class CallBudget:
    """A rolling daily ceiling on outbound requests."""

    limit: int = DEFAULT_DAILY_CALL_LIMIT
    window_seconds: float = 24 * 60 * 60
    _clock: callable = time.monotonic
    _used: int = 0
    _window_start: float = field(default_factory=lambda: 0.0)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _warned: bool = False

    def _roll(self) -> None:
        now = self._clock()
        if not self._window_start:
            self._window_start = now
        elif now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._used = 0
            self._warned = False

    def consume(self, n: int = 1) -> None:
        with self._lock:
            self._roll()
            if self._used + n > self.limit:
                if not self._warned:
                    log.error(
                        "daily eBay call budget of %d reached; pausing requests "
                        "until the window rolls over",
                        self.limit,
                    )
                    self._warned = True
                raise QuotaExceeded(
                    f"local daily call budget of {self.limit} reached"
                )
            self._used += n

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit - self._used)


def parse_retry_after(value: str | None, default: float) -> float:
    """Honour a Retry-After header, in seconds, clamped to something sane.

    Only the delta-seconds form is handled; the HTTP-date form is rare in
    practice here and falling back to the default is safer than mis-parsing
    a date and hammering the endpoint.
    """
    if not value:
        return default
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return default
    if seconds < 0:
        return default
    return min(seconds, MAX_RETRY_AFTER)
