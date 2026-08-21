"""Guards against getting the deployment's IP blocked by eBay.

The failure mode these prevent is not a crash, it is being quietly banned, so
the behaviour is worth pinning down precisely.
"""
import pytest

from tracker.ebay.auth import AuthError, TokenProvider
from tracker.ebay.limits import (
    AUTH_COOLDOWN_START,
    MAX_RETRY_AFTER,
    AuthCircuitBreaker,
    CallBudget,
    CircuitOpen,
    QuotaExceeded,
    parse_retry_after,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def breaker(threshold=2):
    clock = FakeClock()
    return AuthCircuitBreaker(threshold=threshold, _clock=clock), clock


class TestAuthCircuitBreaker:
    def test_allows_requests_initially(self):
        b, _ = breaker()
        b.before_request()

    def test_stays_closed_below_the_threshold(self):
        b, _ = breaker(threshold=2)
        b.record_failure()
        b.before_request()

    def test_opens_at_the_threshold(self):
        b, _ = breaker(threshold=2)
        b.record_failure()
        b.record_failure()
        with pytest.raises(CircuitOpen, match="credentials are wrong"):
            b.before_request()

    def test_closes_after_the_cooldown(self):
        b, clock = breaker(threshold=1)
        b.record_failure()
        with pytest.raises(CircuitOpen):
            b.before_request()
        clock.advance(AUTH_COOLDOWN_START + 1)
        b.before_request()

    def test_cooldown_doubles_each_time_it_reopens(self):
        b, clock = breaker(threshold=1)
        b.record_failure()
        clock.advance(AUTH_COOLDOWN_START + 1)
        b.before_request()

        b.record_failure()
        clock.advance(AUTH_COOLDOWN_START + 1)
        with pytest.raises(CircuitOpen):
            b.before_request()  # second cooldown is twice as long

    def test_cooldown_is_capped(self):
        b, clock = breaker(threshold=1)
        for _ in range(20):
            b.record_failure()
            clock.advance(b.max_cooldown + 1)
            try:
                b.before_request()
            except CircuitOpen:
                pass
        assert b._current_cooldown <= b.max_cooldown

    def test_success_resets_everything(self):
        b, _ = breaker(threshold=2)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.before_request()
        assert b.consecutive_failures == 0

    def test_reports_whether_it_is_open(self):
        b, _ = breaker(threshold=1)
        assert b.is_open is False
        b.record_failure()
        assert b.is_open is True


class FakeAuthResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeAuthSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = 0

    def post(self, *a, **kw):
        self.posts += 1
        if not self._responses:
            raise AssertionError("unexpected extra token request")
        return self._responses.pop(0)


class TestTokenProviderBreaker:
    def test_bad_credentials_stop_being_retried(self):
        """The behaviour that gets an IP blocked: presenting rejected
        credentials on a schedule forever."""
        session = FakeAuthSession([
            FakeAuthResponse(401, text='{"error":"invalid_client"}'),
            FakeAuthResponse(401, text='{"error":"invalid_client"}'),
        ])
        b, _ = breaker(threshold=2)
        provider = TokenProvider("id", "secret", session=session, breaker=b)

        for _ in range(2):
            with pytest.raises(AuthError):
                provider.token()
        assert session.posts == 2

        # Third attempt must not reach the network at all.
        with pytest.raises(AuthError, match="not retrying authentication"):
            provider.token()
        assert session.posts == 2

    def test_transient_errors_do_not_open_the_breaker(self):
        """A 429 or 5xx is not a credential problem."""
        session = FakeAuthSession([
            FakeAuthResponse(503, text="upstream down"),
            FakeAuthResponse(503, text="upstream down"),
            FakeAuthResponse(503, text="upstream down"),
        ])
        b, _ = breaker(threshold=2)
        provider = TokenProvider("id", "secret", session=session, breaker=b)

        for _ in range(3):
            with pytest.raises(AuthError):
                provider.token()
        assert session.posts == 3
        assert b.is_open is False

    def test_success_after_failure_clears_the_breaker(self):
        session = FakeAuthSession([
            FakeAuthResponse(401, text="bad"),
            FakeAuthResponse(200, {"access_token": "t", "expires_in": 7200}),
        ])
        b, _ = breaker(threshold=2)
        provider = TokenProvider("id", "secret", session=session, breaker=b)

        with pytest.raises(AuthError):
            provider.token()
        assert provider.token() == "t"
        assert b.consecutive_failures == 0


class TestCallBudgetLimits:
    def test_allows_up_to_the_limit(self):
        b = CallBudget(limit=3, _clock=FakeClock())
        for _ in range(3):
            b.consume()
        assert b.used == 3

    def test_raises_past_the_limit(self):
        b = CallBudget(limit=1, _clock=FakeClock())
        b.consume()
        with pytest.raises(QuotaExceeded):
            b.consume()

    def test_window_rolls_over(self):
        clock = FakeClock()
        b = CallBudget(limit=1, window_seconds=100, _clock=clock)
        b.consume()
        with pytest.raises(QuotaExceeded):
            b.consume()
        clock.advance(101)
        b.consume()


class TestRetryAfterParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("30", 30.0),
            ("0", 0.0),
            ("  12  ", 12.0),
            (None, 5.0),
            ("", 5.0),
            ("tomorrow", 5.0),
            ("-4", 5.0),
        ],
    )
    def test_parsing(self, raw, expected):
        assert parse_retry_after(raw, 5.0) == expected

    def test_clamped_to_a_sane_ceiling(self):
        assert parse_retry_after("99999999", 5.0) == MAX_RETRY_AFTER
