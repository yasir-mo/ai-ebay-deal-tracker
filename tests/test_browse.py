import pytest

from tracker.ebay.browse import BrowseClient, BrowseError, RateLimited


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        if not self.responses:
            raise AssertionError("more requests than queued responses")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTokens:
    def token(self):
        return "fake"


def client(responses):
    slept = []
    c = BrowseClient(
        FakeTokens(),
        session=FakeSession(responses),
        sleep=slept.append,
    )
    return c, slept


class TestBuildFilters:
    def test_no_filters_is_empty(self):
        assert BrowseClient.build_filters(None, "GBP") == {}

    def test_buying_options_syntax(self):
        out = BrowseClient.build_filters(
            {"buying_options": ["FIXED_PRICE", "AUCTION"]}, "GBP"
        )
        assert out["filter"] == "buyingOptions:{FIXED_PRICE|AUCTION}"

    def test_conditions_syntax(self):
        out = BrowseClient.build_filters({"conditions": ["USED"]}, "GBP")
        assert out["filter"] == "conditions:{USED}"

    def test_price_filter_carries_currency(self):
        """eBay returns 400 for a price filter with no priceCurrency."""
        out = BrowseClient.build_filters({"price_max": 500}, "GBP")
        assert "price:[..500]" in out["filter"]
        assert "priceCurrency:GBP" in out["filter"]

    def test_currency_is_not_hardcoded(self):
        out = BrowseClient.build_filters({"price_max": 500}, "EUR")
        assert "priceCurrency:EUR" in out["filter"]

    def test_price_range_emits_currency_only_once(self):
        out = BrowseClient.build_filters({"price_min": 10, "price_max": 500}, "GBP")
        assert out["filter"].count("priceCurrency") == 1

    def test_category_ids_are_a_separate_param_not_a_filter(self):
        out = BrowseClient.build_filters({"category_ids": [625, 31388]}, "GBP")
        assert out["category_ids"] == "625,31388"
        assert "filter" not in out

    def test_unknown_keys_are_ignored_not_fatal(self):
        assert BrowseClient.build_filters({"nonsense": [1]}, "GBP") == {}


class TestSearch:
    def test_returns_summaries(self):
        c, _ = client([FakeResponse(200, {"itemSummaries": [{"itemId": "a"}]})])
        assert c.search("thing") == [{"itemId": "a"}]

    def test_missing_summaries_key_is_empty_list(self):
        c, _ = client([FakeResponse(200, {"total": 0})])
        assert c.search("thing") == []

    def test_limit_is_capped_at_ebays_maximum(self):
        c, _ = client([FakeResponse(200, {})])
        c.search("thing", limit=9999)
        assert c._session.requests[0]["params"]["limit"] == 200

    def test_marketplace_header_is_sent(self):
        c, _ = client([FakeResponse(200, {})])
        c.search("thing")
        assert c._session.requests[0]["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"


class TestRetries:
    def test_retries_on_429_then_succeeds(self):
        c, slept = client(
            [FakeResponse(429), FakeResponse(200, {"itemSummaries": [{"itemId": "a"}]})]
        )
        assert len(c.search("thing")) == 1
        assert slept == [1.0]

    def test_retries_on_500(self):
        c, slept = client([FakeResponse(503), FakeResponse(200, {})])
        c.search("thing")
        assert len(slept) == 1

    def test_backoff_grows(self):
        c, slept = client([FakeResponse(429)] * 4)
        with pytest.raises(RateLimited):
            c.search("thing")
        assert slept == [1.0, 2.0, 4.0]

    def test_gives_up_after_max_attempts(self):
        c, _ = client([FakeResponse(429)] * 4)
        with pytest.raises(RateLimited):
            c.search("thing")
        assert c.calls_made == 4

    def test_400_is_not_retried(self):
        """A malformed request is not transient, so retrying only burns quota."""
        c, slept = client([FakeResponse(400, text="bad filter syntax")])
        with pytest.raises(BrowseError, match="bad filter"):
            c.search("thing")
        assert slept == []
        assert c.calls_made == 1

    def test_network_errors_are_retried(self):
        import requests

        c, slept = client(
            [requests.RequestException("connection reset"), FakeResponse(200, {})]
        )
        c.search("thing")
        assert len(slept) == 1

    def test_call_counter_tracks_quota_usage(self):
        c, _ = client([FakeResponse(200, {}), FakeResponse(200, {})])
        c.search("a")
        c.search("b")
        assert c.calls_made == 2


class TestRetryAfterCompliance:
    def test_retry_after_header_overrides_backoff(self):
        """When the server says how long to wait, waiting less is wrong."""
        c, slept = client([
            FakeResponse(429, headers={"Retry-After": "30"}),
            FakeResponse(200, {}),
        ])
        c.search("thing")
        assert slept == [30.0]

    def test_retry_after_honoured_on_503(self):
        c, slept = client([
            FakeResponse(503, headers={"Retry-After": "12"}),
            FakeResponse(200, {}),
        ])
        c.search("thing")
        assert slept == [12.0]

    def test_absent_header_falls_back_to_exponential_backoff(self):
        c, slept = client([FakeResponse(429), FakeResponse(200, {})])
        c.search("thing")
        assert slept == [1.0]

    def test_absurd_retry_after_is_clamped(self):
        """A misconfigured header must not wedge the tracker for a week."""
        from tracker.ebay.limits import MAX_RETRY_AFTER

        c, slept = client([
            FakeResponse(429, headers={"Retry-After": "999999"}),
            FakeResponse(200, {}),
        ])
        c.search("thing")
        assert slept == [float(MAX_RETRY_AFTER)]

    def test_garbage_header_falls_back(self):
        c, slept = client([
            FakeResponse(429, headers={"Retry-After": "next tuesday"}),
            FakeResponse(200, {}),
        ])
        c.search("thing")
        assert slept == [1.0]


class TestCallBudget:
    def test_budget_stops_requests(self):
        from tracker.ebay.limits import CallBudget

        c = BrowseClient(FakeTokens(), session=FakeSession([FakeResponse(200, {})] * 5),
                         sleep=lambda s: None, budget=CallBudget(limit=2))
        c.search("a")
        with pytest.raises(BrowseError, match="budget"):
            c.search("b")
            c.search("c")

    def test_budget_reports_remaining(self):
        from tracker.ebay.limits import CallBudget

        b = CallBudget(limit=10)
        b.consume(3)
        assert b.remaining == 7
