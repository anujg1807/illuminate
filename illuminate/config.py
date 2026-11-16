"""Settings, read from config.toml (every value has a default)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_QUERIES = [
    "product manager",
    "product management",
    "product owner",
    "head of product",
]


@dataclass
class Config:
    location: str = "India"
    queries: list[str] = field(default_factory=lambda: list(DEFAULT_QUERIES))
    full_scan: bool = True
    interval_minutes: float = 15
    backfill_hours: float = 24
    min_delay_seconds: float = 3
    max_delay_seconds: float = 6
    refine_requests_per_cycle: int = 30
    port: int = 8000
    scoring_model: str = "claude-opus-5-5"
    database: Path = Path("data/illuminate.db")


def load(path: Path | str = "config.toml") -> Config:
    path = Path(path)
    config = Config()
    if not path.exists():
        return config
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    search = data.get("search", {})
    schedule = data.get("schedule", {})
    pacing = data.get("pacing", {})
    server = data.get("server", {})
    config.location = search.get("location", config.location)
    config.queries = [q for q in search.get("queries", config.queries) if str(q).strip()]
    config.full_scan = bool(search.get("full_scan", config.full_scan))
    config.interval_minutes = schedule.get("interval_minutes", config.interval_minutes)
    config.backfill_hours = schedule.get("backfill_hours", config.backfill_hours)
    config.min_delay_seconds = pacing.get("min_delay_seconds", config.min_delay_seconds)
    config.max_delay_seconds = max(
        config.min_delay_seconds, pacing.get("max_delay_seconds", config.max_delay_seconds)
    )
    config.refine_requests_per_cycle = pacing.get(
        "refine_requests_per_cycle", config.refine_requests_per_cycle
    )
    config.port = server.get("port", config.port)
    config.scoring_model = data.get("scoring", {}).get("model", config.scoring_model)
    config.database = path.parent / data.get("database", str(config.database))
    if not config.queries and not config.full_scan:
        raise ValueError("config.toml: [search] needs at least one query, or full_scan = true")
    return config
