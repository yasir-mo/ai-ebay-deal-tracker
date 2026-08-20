"""The dashboard HTTP server.

Standard library only. Runs in a daemon thread inside the tracker process, so
one command gives you both the daemon and its interface, and both read the
same SQLite file.

Binds to 127.0.0.1 by default. A dashboard that records purchases and edits
what gets tracked should not be reachable from the network without a token,
so binding elsewhere requires one (see Settings.web_token).
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..margin import MarginConfig, estimate as estimate_margin
from ..models import Profile, Verdict
from ..scoring import DEFAULT_THRESHOLDS, Thresholds
from . import backtest, render

log = logging.getLogger(__name__)

THRESHOLD_KEY = "thresholds"
MAX_BODY_BYTES = 64 * 1024


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_thresholds(store) -> Thresholds:
    raw = store.get_setting(THRESHOLD_KEY)
    if not raw:
        return DEFAULT_THRESHOLDS
    return Thresholds(**{**DEFAULT_THRESHOLDS.__dict__, **raw})


def _pounds_to_pence(value: str) -> int:
    from decimal import Decimal

    return int((Decimal(str(value).strip()) * 100).to_integral_value())


class Dashboard:
    """Request handling, kept out of the HTTP plumbing so it can be tested."""

    def __init__(self, store, settings, margin_config: MarginConfig | None = None):
        self.store = store
        self.settings = settings
        self.margin_config = margin_config or MarginConfig()

    # -- pages ------------------------------------------------------------

    def deals(self, flash=None) -> str:
        now = utcnow()
        rows = self.store.pending_decisions()
        for row in rows:
            row["margin_pence"] = self._margin_for(row)
        rows.sort(key=lambda r: (-Verdict(r["verdict"]).rank, r["end_time"] or datetime.max.replace(tzinfo=timezone.utc)))
        return render.deals_page(rows, now, flash)

    def searches(self, flash=None) -> str:
        now = utcnow()
        week_ago = now - timedelta(days=7)
        rows = []
        for profile in self.store.list_profiles():
            best = None
            for bucket_baseline in self._baselines(profile.id):
                if best is None or bucket_baseline.sample_n > best.sample_n:
                    best = bucket_baseline
            rows.append(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "query": profile.query,
                    "ceiling_pence": profile.ceiling_pence,
                    "target_pence": profile.target_pence,
                    "enabled": profile.enabled,
                    "median_pence": best.median_pence if best else None,
                    "sample_n": best.sample_n if best else 0,
                    "alerts_7d": self.store.alert_count(profile.id, week_ago),
                }
            )
        return render.searches_page(rows, flash)

    def history(self, profile_id: str | None) -> str:
        profiles = [{"id": p.id, "name": p.name} for p in self.store.list_profiles()]
        if profile_id is None and profiles:
            known = {p["id"] for p in profiles}
            busiest = self.store.busiest_profile()
            profile_id = busiest if busiest in known else profiles[0]["id"]
        points = (
            self.store.price_series(profile_id, 30, utcnow()) if profile_id else []
        )
        rows = self.store.decisions_for_profile(profile_id) if profile_id else []
        return render.history_page(profiles, profile_id, points, rows)

    def settings_view(self, backtest_result=None, flash=None) -> str:
        current = load_thresholds(self.store)
        now = utcnow()
        status = {
            "active": self.store.active_listing_count(),
            "profiles": len(self.store.list_profiles()),
            "alerts_today": sum(
                v
                for k, v in self.store.counts_since(now - timedelta(days=1)).items()
                if k != "SKIP"
            ),
            "ai_enabled": getattr(self.settings, "ai_enabled", False),
            "dry_run": getattr(self.settings, "dry_run", True),
        }
        return render.settings_page(
            _thresholds_dict(current), backtest_result, status, flash
        )

    # -- actions ----------------------------------------------------------

    def record_outcome(self, form: dict) -> str:
        item_id = form.get("item_id")
        verdict = form.get("verdict")
        outcome = form.get("outcome")
        if not (item_id and verdict and outcome):
            return "Missing field, nothing recorded."
        self.store.set_outcome(item_id, Verdict(verdict), outcome)
        return f"Recorded '{outcome}'."

    def add_search(self, form: dict) -> str:
        try:
            ceiling = _pounds_to_pence(form["ceiling"])
            target = (
                _pounds_to_pence(form["target"])
                if form.get("target", "").strip()
                else None
            )
        except Exception:
            return "Ceiling and target must be numbers."

        if target is not None and target > ceiling:
            return "Target is above the ceiling, so nothing could ever alert."

        profile_id = (form.get("id") or "").strip()
        if not profile_id:
            return "An id is required."
        if self.store.get_profile(profile_id):
            return f"A search with id '{profile_id}' already exists."

        self.store.save_profile(
            Profile(
                id=profile_id,
                name=(form.get("name") or profile_id).strip(),
                query=(form.get("query") or "").strip(),
                ceiling_pence=ceiling,
                target_pence=target,
                min_feedback_pct=float(form.get("min_feedback_pct") or 95.0),
                min_feedback_count=int(form.get("min_feedback_count") or 10),
            ),
            utcnow(),
        )
        return f"Added '{profile_id}'. It will be picked up on the next sweep."

    def toggle_search(self, form: dict) -> str:
        profile = self.store.get_profile(form.get("id", ""))
        if not profile:
            return "No such search."
        self.store.set_profile_enabled(profile.id, not profile.enabled, utcnow())
        return f"{'Disabled' if profile.enabled else 'Enabled'} '{profile.id}'."

    def delete_search(self, form: dict) -> str:
        profile_id = form.get("id", "")
        if not self.store.get_profile(profile_id):
            return "No such search."
        self.store.delete_profile(profile_id)
        return f"Deleted '{profile_id}'. Its price history is kept."

    def update_settings(self, form: dict):
        try:
            candidate = Thresholds(
                buy_now_discount=float(form["buy_now_discount"]),
                good_deal_discount=float(form["good_deal_discount"]),
                watch_discount=float(form["watch_discount"]),
                scam_floor=float(form["scam_floor"]),
                watch_horizon_hours=int(float(form["watch_horizon_hours"])),
            )
        except (KeyError, ValueError):
            return None, "Every threshold must be a number."

        if form.get("action") == "apply":
            self.store.set_setting(THRESHOLD_KEY, _thresholds_dict(candidate), utcnow())
            return None, "Thresholds applied. They take effect on the next sweep."

        result = backtest.run(
            self.store, load_thresholds(self.store), candidate, utcnow()
        )
        return result, None

    # -- internals --------------------------------------------------------

    def _baselines(self, profile_id: str):
        from ..models import ConditionBucket

        for bucket in ConditionBucket:
            baseline = self.store.get_baseline(profile_id, bucket)
            if baseline:
                yield baseline

    def _margin_for(self, row: dict) -> int | None:
        baseline_pence = row.get("baseline_pence")
        if not baseline_pence:
            return None
        listing = self.store.get_listing(row["item_id"])
        if listing is None:
            return None
        baseline = self.store.get_baseline(listing.profile_id, listing.bucket)
        if baseline is None:
            return None
        est = estimate_margin(listing, baseline, self.margin_config)
        return est.margin_pence if est.margin_pence > 0 else None


def _thresholds_dict(t: Thresholds) -> dict:
    return {
        "buy_now_discount": t.buy_now_discount,
        "good_deal_discount": t.good_deal_discount,
        "watch_discount": t.watch_discount,
        "scam_floor": t.scam_floor,
        "watch_horizon_hours": t.watch_horizon_hours,
    }


def make_handler(dashboard: Dashboard, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DealTracker"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # -- helpers --

        def _authorised(self, query: dict) -> bool:
            if not token:
                return True
            supplied = (
                query.get("token", [""])[0]
                or self.headers.get("X-Auth-Token", "")
                or _cookie_token(self.headers.get("Cookie", ""))
            )
            return hmac.compare_digest(supplied, token)

        def _send(self, body: str, status: int = 200, content_type="text/html; charset=utf-8",
                  extra_headers: dict | None = None):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, location: str, flash: str | None = None):
            if flash:
                location += ("&" if "?" in location else "?") + urllib.parse.urlencode(
                    {"flash": flash}
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                return {}
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        # -- routes --

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)

            if not self._authorised(query):
                self._send("<h1>401</h1><p>Missing or bad token.</p>", 401)
                return

            flash = query.get("flash", [None])[0]
            path = parsed.path
            headers = {}
            if token and query.get("token"):
                headers["Set-Cookie"] = f"token={token}; Path=/; SameSite=Strict; HttpOnly"

            try:
                if path == "/":
                    self._send(dashboard.deals(flash), extra_headers=headers)
                elif path == "/searches":
                    self._send(dashboard.searches(flash), extra_headers=headers)
                elif path == "/history":
                    self._send(
                        dashboard.history(query.get("profile", [None])[0]),
                        extra_headers=headers,
                    )
                elif path == "/settings":
                    self._send(dashboard.settings_view(flash=flash), extra_headers=headers)
                elif path == "/api/deals":
                    rows = dashboard.store.pending_decisions()
                    self._send(
                        json.dumps({"count": len(rows)}),
                        content_type="application/json",
                    )
                elif path == "/healthz":
                    self._send("ok", content_type="text/plain; charset=utf-8")
                else:
                    self._send("<h1>404</h1>", 404)
            except Exception:
                log.exception("error rendering %s", path)
                self._send("<h1>500</h1><p>See the log.</p>", 500)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if not self._authorised(urllib.parse.parse_qs(parsed.query)):
                self._send("<h1>401</h1>", 401)
                return

            form = self._form()
            path = parsed.path
            try:
                if path == "/action":
                    self._redirect("/", dashboard.record_outcome(form))
                elif path == "/searches/add":
                    self._redirect("/searches", dashboard.add_search(form))
                elif path == "/searches/toggle":
                    self._redirect("/searches", dashboard.toggle_search(form))
                elif path == "/searches/delete":
                    self._redirect("/searches", dashboard.delete_search(form))
                elif path == "/settings":
                    result, flash = dashboard.update_settings(form)
                    if result is None:
                        self._redirect("/settings", flash)
                    else:
                        self._send(dashboard.settings_view(backtest_result=result))
                else:
                    self._send("<h1>404</h1>", 404)
            except Exception:
                log.exception("error handling %s", path)
                self._send("<h1>500</h1><p>See the log.</p>", 500)

    return Handler


def _cookie_token(cookie_header: str) -> str:
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "token":
            return value
    return ""


def serve(dashboard: Dashboard, host: str, port: int, token: str | None):
    """Create the server. The caller decides whether to block or thread it."""
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise ValueError(
            f"refusing to bind {host} without web_token set: a dashboard that "
            "records purchases and edits tracking should not be open on the "
            "network. Set web_token in profiles.toml, or bind 127.0.0.1."
        )
    return ThreadingHTTPServer((host, port), make_handler(dashboard, token))


def start_in_thread(dashboard: Dashboard, host: str, port: int, token: str | None):
    httpd = serve(dashboard, host, port, token)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="web")
    thread.start()
    log.info("dashboard on http://%s:%d%s", host, port, " (token required)" if token else "")
    return httpd
