"""Config loading. TOML via stdlib tomllib - no third-party dependency."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import Profile


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    ebay_client_id: str
    ebay_client_secret: str
    telegram_token: str
    telegram_chat_id: str
    marketplace: str = "EBAY_GB"
    currency: str = "GBP"
    db_path: str = "tracker.db"
    sweep_minutes: int = 30
    endgame_seconds: int = 60
    endgame_horizon_minutes: int = 15
    heartbeat_hours: int = 6
    results_per_profile: int = 20
    dry_run: bool = False


def _pounds_to_pence(value) -> int:
    """Config is written in pounds because that is how humans think."""
    from decimal import Decimal

    return int((Decimal(str(value)) * 100).to_integral_value())


def load_settings(path: str | Path = "profiles.toml") -> Settings:
    """Secrets come from the environment, everything else from TOML.

    Keeping credentials out of the config file means it can be committed
    without redaction.
    """
    raw = _read_toml(path).get("settings", {})

    missing = [
        name
        for name in (
            "EBAY_CLIENT_ID",
            "EBAY_CLIENT_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise ConfigError(
            "missing environment variables: "
            + ", ".join(missing)
            + " (copy .env.example to .env and fill it in)"
        )

    return Settings(
        ebay_client_id=os.environ["EBAY_CLIENT_ID"],
        ebay_client_secret=os.environ["EBAY_CLIENT_SECRET"],
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        marketplace=raw.get("marketplace", "EBAY_GB"),
        currency=raw.get("currency", "GBP"),
        db_path=raw.get("db_path", "tracker.db"),
        sweep_minutes=int(raw.get("sweep_minutes", 30)),
        endgame_seconds=int(raw.get("endgame_seconds", 60)),
        endgame_horizon_minutes=int(raw.get("endgame_horizon_minutes", 15)),
        heartbeat_hours=int(raw.get("heartbeat_hours", 6)),
        results_per_profile=int(raw.get("results_per_profile", 20)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def load_profiles(path: str | Path = "profiles.toml") -> list[Profile]:
    data = _read_toml(path)
    entries = data.get("profile", [])
    if not entries:
        raise ConfigError(f"{path} defines no [[profile]] entries")

    profiles = []
    seen: set[str] = set()
    for entry in entries:
        profile = _build_profile(entry, path)
        if profile.id in seen:
            raise ConfigError(f"duplicate profile id: {profile.id}")
        seen.add(profile.id)
        profiles.append(profile)
    return profiles


def _build_profile(entry: dict, path) -> Profile:
    for required in ("id", "query", "ceiling"):
        if required not in entry:
            raise ConfigError(f"{path}: profile missing required key '{required}'")

    ceiling = _pounds_to_pence(entry["ceiling"])
    target = _pounds_to_pence(entry["target"]) if "target" in entry else None

    if target is not None and target > ceiling:
        raise ConfigError(
            f"profile '{entry['id']}': target ({entry['target']}) exceeds "
            f"ceiling ({entry['ceiling']}), so nothing could ever alert"
        )

    return Profile(
        id=str(entry["id"]),
        name=entry.get("name", entry["id"]),
        query=entry["query"],
        ceiling_pence=ceiling,
        target_pence=target,
        min_feedback_pct=float(entry.get("min_feedback_pct", 95.0)),
        min_feedback_count=int(entry.get("min_feedback_count", 10)),
        filters=entry.get("filters", {}),
        enabled=bool(entry.get("enabled", True)),
    )


def _read_toml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"{p} not found (copy profiles.toml.example to profiles.toml)"
        )
    with p.open("rb") as fh:
        return tomllib.load(fh)
