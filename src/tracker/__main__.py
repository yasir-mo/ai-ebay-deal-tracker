"""CLI entrypoint: python -m tracker [run|once|endgame|heartbeat|outcome]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from datetime import datetime, timezone

from .config import ConfigError, load_profiles, load_settings
from .ebay.auth import TokenProvider
from .ebay.browse import BrowseClient
from .models import Verdict
from .notify import Notifier
from .scheduler import Tracker
from .store import Store


def setup_logging(verbose: bool = False) -> None:
    """UTF-8 forced: verdict emoji crash the default Windows console codec."""
    handler = logging.StreamHandler(sys.stdout)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler],
    )


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader so there is no python-dotenv dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build(config_path: str, require_secrets: bool = True) -> tuple[Tracker, Store]:
    settings = load_settings(config_path, require_secrets=require_secrets)
    store = Store(settings.db_path)

    profiles = store.list_profiles()
    if not profiles:
        # First run: seed the database from the TOML file. After this the
        # database is the source of truth and the dashboard edits it.
        seeded = load_profiles(config_path)
        now = datetime.now(timezone.utc)
        for profile in seeded:
            store.save_profile(profile, now)
        profiles = store.list_profiles()
        print(f"seeded {len(profiles)} searches from {config_path} into the database")
    tokens = TokenProvider(settings.ebay_client_id, settings.ebay_client_secret)
    client = BrowseClient(
        tokens, marketplace=settings.marketplace, currency=settings.currency
    )
    notifier = Notifier(
        settings.telegram_token,
        settings.telegram_chat_id,
        store,
        dry_run=settings.dry_run,
    )

    judge = None
    if settings.ai_enabled:
        from .ai.judge import DailyBudget, Judge, build_client

        judge = Judge(
            build_client(settings.ai_api_key),
            DailyBudget(limit_pence=settings.ai_daily_budget_pence),
            effort=settings.ai_effort,
            batch_size=settings.ai_batch_size,
            max_tokens=settings.ai_max_tokens,
        )

    tracker = Tracker(settings, profiles, store, client, notifier, judge)
    tracker.reload_profiles = lambda: store.list_profiles()
    return tracker, store


def _start_dashboard(tracker, store, required: bool = False):
    """Start the web UI. A dashboard failure must not take the tracker down."""
    settings = tracker.settings
    if not settings.web_enabled and not required:
        return None
    from .web.server import Dashboard, start_in_thread, serve

    dashboard = Dashboard(store, settings, tracker.margin_config)
    try:
        if required:
            httpd = serve(dashboard, settings.web_host, settings.web_port, settings.web_token)
            print(f"dashboard on http://{settings.web_host}:{settings.web_port}")
            return httpd
        return start_in_thread(
            dashboard, settings.web_host, settings.web_port, settings.web_token
        )
    except ValueError as exc:
        print(f"dashboard not started: {exc}", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"dashboard not started: {exc}", file=sys.stderr)
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tracker")
    parser.add_argument(
        "command",
        choices=["run", "once", "endgame", "heartbeat", "outcome", "web", "import-config"],
        help="run = daemon plus dashboard; once = a single sweep and exit; "
             "web = dashboard only",
    )
    parser.add_argument("-c", "--config", default="profiles.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--item", help="item id (outcome only)")
    parser.add_argument("--verdict", help="verdict tier (outcome only)")
    parser.add_argument("--result", help="e.g. bought / missed / bad (outcome only)")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    load_dotenv()

    # The dashboard only reads the database, so it does not need eBay or
    # Telegram credentials to be configured.
    needs_secrets = args.command not in ("web", "import-config")
    try:
        tracker, store = build(args.config, require_secrets=needs_secrets)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "run":
            _start_dashboard(tracker, store)
            tracker.run_forever()
        elif args.command == "web":
            httpd = _start_dashboard(tracker, store, required=True)
            if httpd is None:
                return 2
            print("dashboard running, press Ctrl-C to stop")
            httpd.serve_forever()
        elif args.command == "import-config":
            seeded = load_profiles(args.config)
            now = datetime.now(timezone.utc)
            for profile in seeded:
                store.save_profile(profile, now)
            print(f"imported {len(seeded)} searches into {tracker.settings.db_path}")
        elif args.command == "once":
            stats = tracker.sweep()
            print(stats)
        elif args.command == "endgame":
            print(tracker.endgame())
        elif args.command == "heartbeat":
            print(tracker.heartbeat())
        elif args.command == "outcome":
            if not (args.item and args.verdict and args.result):
                print("outcome needs --item, --verdict and --result", file=sys.stderr)
                return 2
            store.set_outcome(args.item, Verdict(args.verdict), args.result)
            print("recorded")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
