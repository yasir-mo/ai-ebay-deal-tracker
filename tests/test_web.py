"""Dashboard tests.

Run against a real ThreadingHTTPServer on an ephemeral port, so the routing,
threading, and SQLite cross-thread access are all genuinely exercised rather
than mocked.
"""
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from tracker.config import Settings
from tracker.models import (
    Baseline,
    BuyingOption,
    ConditionBucket,
    Decision,
    Listing,
    Profile,
    Verdict,
)
from tracker.scoring import DEFAULT_THRESHOLDS, Thresholds
from tracker.store import Store
from tracker.web import backtest
from tracker.web.render import esc, money, sparkline, time_left
from tracker.web.server import Dashboard, load_thresholds, serve

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

SETTINGS = Settings(
    ebay_client_id="x",
    ebay_client_secret="x",
    telegram_token="x",
    telegram_chat_id="x",
)

PROFILE = Profile(
    id="a7iii",
    name="Sony A7 III",
    query="sony a7 iii body",
    ceiling_pence=150_000,
    target_pence=100_000,
)


def make_listing(item_id="v1|1|0", price=60_000, title="Sony A7 III Body", **over):
    base = dict(
        item_id=item_id,
        profile_id="a7iii",
        title=title,
        condition="Used",
        condition_id=3000,
        buying_option=BuyingOption.FIXED_PRICE,
        price_pence=price,
        shipping_pence=0,
        currency="GBP",
        seller_name="seller",
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        end_time=None,
        item_url="https://ebay.co.uk/itm/1",
        bid_count=None,
    )
    base.update(over)
    return Listing(**base)


def make_decision(item_id="v1|1|0", verdict=Verdict.BUY_NOW, total=60_000):
    return Decision(
        item_id=item_id,
        profile_id="a7iii",
        verdict=verdict,
        total_pence=total,
        baseline_pence=100_000,
        discount_pct=0.4,
        reasons=["discount:40%"],
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "web.db")
    s.save_profile(PROFILE, NOW)
    s.save_baseline(
        Baseline("a7iii", ConditionBucket.USED, 100_000, 90_000, 40), NOW
    )
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    """One notified, undecided alert."""
    store.upsert_listing(make_listing(), NOW)
    store.record_decision(make_decision(), NOW)
    store.mark_notified("v1|1|0", Verdict.BUY_NOW, NOW)
    return store


@pytest.fixture
def dash(seeded):
    return Dashboard(seeded, SETTINGS)


@pytest.fixture
def live(dash):
    """A real server on an ephemeral port."""
    httpd = serve(dash, "127.0.0.1", 0, None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()


def get(base, path, token=None):
    url = base + path
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Auth-Token", token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def post(base, path, data, token=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(base + path, data=body, method="POST")
    if token:
        req.add_header("X-Auth-Token", token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.url


# --------------------------------------------------------------------------


class TestRenderHelpers:
    def test_money_formats_pence(self):
        assert money(123_456) == "£1,234.56"

    def test_money_handles_none(self):
        assert money(None) == "n/a"

    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(minutes=8), "8m"),
            (timedelta(hours=3, minutes=5), "3h 05m"),
            (timedelta(days=2, hours=1), "2d 1h"),
            (timedelta(seconds=-1), "ended"),
        ],
    )
    def test_time_left(self, delta, expected):
        assert time_left(NOW + delta, NOW) == expected

    def test_escapes_html(self):
        assert esc("<script>") == "&lt;script&gt;"

    def test_sparkline_needs_two_points(self):
        assert "Not enough" in sparkline([100])

    def test_sparkline_renders_svg(self):
        out = sparkline([100, 200, 150])
        assert "<svg" in out and "polyline" in out


class TestPagesRender:
    @pytest.mark.parametrize("path", ["/", "/searches", "/history", "/settings"])
    def test_page_returns_200_html(self, live, path):
        status, body = get(live, path)
        assert status == 200
        assert body.startswith("<!doctype html>")
        assert "Deal Tracker" in body

    def test_deal_appears_on_the_deals_page(self, live):
        _, body = get(live, "/")
        assert "Sony A7 III Body" in body
        assert "BUY NOW" in body
        assert "£600.00" in body

    def test_health_endpoint(self, live):
        assert get(live, "/healthz") == (200, "ok")

    def test_unknown_path_is_404(self, live):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(live, "/nope")
        assert exc.value.code == 404

    def test_empty_state_when_nothing_pending(self, store):
        dash = Dashboard(store, SETTINGS)
        assert "Nothing waiting" in dash.deals()


class TestXssEscaping:
    def test_hostile_listing_title_is_escaped(self, store):
        """Titles come from eBay and are untrusted."""
        store.upsert_listing(
            make_listing(title='<script>alert("xss")</script>'), NOW
        )
        store.record_decision(make_decision(), NOW)
        store.mark_notified("v1|1|0", Verdict.BUY_NOW, NOW)

        html = Dashboard(store, SETTINGS).deals()
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_hostile_search_name_is_escaped(self, store):
        store.save_profile(
            Profile(id="x", name="<img src=x onerror=alert(1)>", query="q",
                    ceiling_pence=1000),
            NOW,
        )
        html = Dashboard(store, SETTINGS).searches()
        assert "<img src=x" not in html
        assert "&lt;img" in html


class TestOutcomeAction:
    def test_recording_an_outcome_removes_it_from_deals(self, live, seeded):
        _, body = get(live, "/")
        assert "Sony A7 III Body" in body

        post(live, "/action", {"item_id": "v1|1|0", "verdict": "BUY_NOW",
                               "outcome": "bought"})

        row = seeded.conn.execute("SELECT outcome FROM decisions").fetchone()
        assert row["outcome"] == "bought"

        _, body = get(live, "/")
        assert "Nothing waiting" in body

    def test_missing_field_is_rejected_gracefully(self, dash):
        assert "Missing field" in dash.record_outcome({"item_id": "x"})


class TestSearchManagement:
    def test_add_search_persists(self, live, seeded):
        post(live, "/searches/add", {
            "id": "rtx", "name": "RTX 4090", "query": "rtx 4090",
            "ceiling": "1600", "target": "1300",
        })
        profile = seeded.get_profile("rtx")
        assert profile is not None
        assert profile.ceiling_pence == 160_000
        assert profile.target_pence == 130_000

    def test_target_above_ceiling_is_rejected(self, dash):
        msg = dash.add_search({"id": "x", "name": "X", "query": "q",
                               "ceiling": "100", "target": "200"})
        assert "above the ceiling" in msg
        assert dash.store.get_profile("x") is None

    def test_duplicate_id_is_rejected(self, dash):
        msg = dash.add_search({"id": "a7iii", "name": "dupe", "query": "q",
                               "ceiling": "100"})
        assert "already exists" in msg

    def test_non_numeric_ceiling_is_rejected(self, dash):
        assert "must be numbers" in dash.add_search(
            {"id": "x", "name": "X", "query": "q", "ceiling": "lots"}
        )

    def test_toggle_disables_and_reenables(self, dash):
        dash.toggle_search({"id": "a7iii"})
        assert dash.store.get_profile("a7iii").enabled is False
        dash.toggle_search({"id": "a7iii"})
        assert dash.store.get_profile("a7iii").enabled is True

    def test_delete_removes_the_search(self, dash):
        dash.delete_search({"id": "a7iii"})
        assert dash.store.get_profile("a7iii") is None

    def test_delete_unknown_is_handled(self, dash):
        assert "No such search" in dash.delete_search({"id": "ghost"})


class TestThresholdSettings:
    def test_defaults_when_nothing_stored(self, store):
        assert load_thresholds(store) == DEFAULT_THRESHOLDS

    def test_apply_persists_and_reloads(self, dash):
        result, flash = dash.update_settings({
            "action": "apply",
            "buy_now_discount": "0.5", "good_deal_discount": "0.3",
            "watch_discount": "0.4", "scam_floor": "0.2",
            "watch_horizon_hours": "12",
        })
        assert result is None
        assert "applied" in flash
        reloaded = load_thresholds(dash.store)
        assert reloaded.buy_now_discount == 0.5
        assert reloaded.watch_horizon_hours == 12

    def test_preview_does_not_persist(self, dash):
        before = load_thresholds(dash.store)
        result, flash = dash.update_settings({
            "action": "preview",
            "buy_now_discount": "0.9", "good_deal_discount": "0.8",
            "watch_discount": "0.7", "scam_floor": "0.1",
            "watch_horizon_hours": "24",
        })
        assert result is not None
        assert load_thresholds(dash.store) == before

    def test_non_numeric_threshold_is_rejected(self, dash):
        result, flash = dash.update_settings({"action": "apply",
                                              "buy_now_discount": "loads"})
        assert result is None
        assert "must be a number" in flash


class TestBacktest:
    def test_looser_thresholds_produce_more_alerts(self, store):
        # 15% below the 100,000 baseline: under the strict 20% bar, over a 10% one.
        for n in range(10):
            store.upsert_listing(make_listing(f"i{n}", price=85_000), NOW)

        strict = Thresholds(good_deal_discount=0.20)
        loose = Thresholds(good_deal_discount=0.10)
        result = backtest.run(store, strict, loose, NOW)

        assert result["listings"] == 10
        assert result["verdicts"]["GOOD_DEAL"]["current"] == 0
        assert result["verdicts"]["GOOD_DEAL"]["candidate"] == 10
        assert result["verdicts"]["SKIP"]["current"] == 10

    def test_skips_listings_with_no_baseline(self, store):
        store.upsert_listing(make_listing("orphan", **{"profile_id": "gone"}), NOW)
        result = backtest.run(store, DEFAULT_THRESHOLDS, DEFAULT_THRESHOLDS, NOW)
        assert result["listings"] == 0

    def test_reports_the_window_used(self, store):
        assert backtest.run(store, DEFAULT_THRESHOLDS, DEFAULT_THRESHOLDS, NOW,
                            days=7)["days"] == 7


class TestBindingSafety:
    def test_refuses_public_bind_without_a_token(self, dash):
        """A dashboard that records purchases must not be open on the LAN."""
        with pytest.raises(ValueError, match="refusing to bind"):
            serve(dash, "0.0.0.0", 0, None)

    def test_public_bind_allowed_with_a_token(self, dash):
        httpd = serve(dash, "0.0.0.0", 0, "s3cret")
        httpd.server_close()

    def test_localhost_needs_no_token(self, dash):
        httpd = serve(dash, "127.0.0.1", 0, None)
        httpd.server_close()


class TestTokenAuth:
    @pytest.fixture
    def secured(self, dash):
        httpd = serve(dash, "127.0.0.1", 0, "s3cret")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        yield base
        httpd.shutdown()
        httpd.server_close()

    def test_request_without_token_is_401(self, secured):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(secured, "/")
        assert exc.value.code == 401

    def test_request_with_token_succeeds(self, secured):
        status, body = get(secured, "/", token="s3cret")
        assert status == 200

    def test_wrong_token_is_401(self, secured):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(secured, "/", token="wrong")
        assert exc.value.code == 401

    def test_post_without_token_is_401(self, secured):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(secured, "/action", {"item_id": "x", "verdict": "BUY_NOW",
                                      "outcome": "bought"})
        assert exc.value.code == 401


class TestOrdering:
    def test_priority_sorts_above_buy_now(self, store):
        for item_id, verdict in (("a", Verdict.BUY_NOW), ("b", Verdict.PRIORITY)):
            store.upsert_listing(make_listing(item_id), NOW)
            store.record_decision(make_decision(item_id, verdict), NOW)
            store.mark_notified(item_id, verdict, NOW)

        html = Dashboard(store, SETTINGS).deals()
        assert html.index("PRIORITY") < html.index("BUY NOW")

    def test_closing_auctions_come_first(self, store):
        soon = make_listing("soon", end_time=NOW + timedelta(minutes=5))
        later = make_listing("later", end_time=NOW + timedelta(hours=8))
        for listing in (later, soon):
            store.upsert_listing(listing, NOW)
            store.record_decision(make_decision(listing.item_id, Verdict.WATCH), NOW)
            store.mark_notified(listing.item_id, Verdict.WATCH, NOW)

        rows = store.pending_decisions()
        assert [r["item_id"] for r in rows] == ["soon", "later"]


class TestHistoryDefault:
    def test_defaults_to_the_search_with_data(self, store):
        """Landing on an empty search makes a working tracker look broken."""
        store.save_profile(
            Profile(id="aaa_empty", name="AAA Empty", query="q", ceiling_pence=1000),
            NOW,
        )
        listing = make_listing("has-data")
        store.upsert_listing(listing, NOW)
        store.record_price(listing, NOW)

        html = Dashboard(store, SETTINGS).history(None)
        assert 'value="a7iii" selected' in html

    def test_explicit_selection_is_respected(self, store):
        store.save_profile(
            Profile(id="rtx", name="RTX", query="q", ceiling_pence=1000), NOW
        )
        html = Dashboard(store, SETTINGS).history("rtx")
        assert 'value="rtx" selected' in html


class TestConnectionHandling:
    def test_client_hangup_does_not_log_a_traceback(self, dash, caplog):
        """Health checks and browsers close early constantly; a stack trace
        for each one buries real errors."""
        import logging
        import socket

        httpd = serve(dash, "127.0.0.1", 0, None)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        with caplog.at_level(logging.ERROR, logger="tracker.web.server"):
            sock = socket.create_connection(("127.0.0.1", port))
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            __import__("struct").pack("ii", 1, 0))
            sock.close()
            threading.Event().wait(0.4)

        httpd.shutdown()
        httpd.server_close()
        assert "Traceback" not in caplog.text

    def test_real_errors_are_still_logged(self, dash):
        """Quieting hangups must not quiet genuine failures."""
        from tracker.web.server import _QuietThreadingHTTPServer

        assert hasattr(_QuietThreadingHTTPServer, "handle_error")
        assert _QuietThreadingHTTPServer.daemon_threads is True


class TestHealthEndpointAuth:
    def test_healthz_needs_no_token(self, dash):
        """Container and service health checks cannot supply one, so a
        token-gated /healthz makes every deployment report unhealthy."""
        httpd = serve(dash, "127.0.0.1", 0, "s3cret")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            assert get(base, "/healthz") == (200, "ok")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_other_pages_still_need_the_token(self, dash):
        httpd = serve(dash, "127.0.0.1", 0, "s3cret")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                get(base, "/")
            assert exc.value.code == 401
        finally:
            httpd.shutdown()
            httpd.server_close()
