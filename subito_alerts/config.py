"""Load and validate searches.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .proxies import DEFAULT_SOURCES
from .schedule import Schedule, ScheduleError, parse_schedule

VALID_FILTERS = {
    "category", "region", "town", "price_min", "price_max",
    "shippable", "title_only",
}


class ConfigError(Exception):
    pass


@dataclass
class ProxySettings:
    """How to reach subito when the local network is blocked."""

    mode: str = "auto"          # auto | always | never
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    min_pool: int = 3
    max_attempts: int = 6
    probe_concurrency: int = 25
    probe_batch: int = 150
    impersonate: str = "chrome136"

    @property
    def enabled(self) -> bool:
        return self.mode != "never"

    @property
    def allow_direct(self) -> bool:
        return self.mode != "always"


@dataclass
class Config:
    searches: list[Search]
    schedule: Schedule
    proxies: ProxySettings = field(default_factory=ProxySettings)

    @property
    def min_interval(self) -> int:
        """The cron must wake up at least this often to honour every search."""
        return min(s.interval_minutes for s in self.searches)


@dataclass
class Search:
    name: str
    query: str
    prompt: str
    interval_minutes: int = 60
    max_pages: int = 2
    filters: dict[str, Any] = field(default_factory=dict)
    exclude_keywords: list[str] = field(default_factory=list)


def _validate(search: Search) -> None:
    if not search.query.strip():
        raise ConfigError(f"search {search.name!r}: 'query' must not be empty")
    if not search.prompt.strip():
        raise ConfigError(
            f"search {search.name!r}: 'prompt' is required — it tells the "
            "classifier what you're actually interested in"
        )
    if search.interval_minutes < 1:
        raise ConfigError(f"search {search.name!r}: 'interval_minutes' must be >= 1")
    unknown = set(search.filters) - VALID_FILTERS
    if unknown:
        raise ConfigError(
            f"search {search.name!r}: unknown filter(s) {sorted(unknown)}; "
            f"valid filters are {sorted(VALID_FILTERS)}"
        )
    lo, hi = search.filters.get("price_min"), search.filters.get("price_max")
    if lo is not None and hi is not None and lo > hi:
        raise ConfigError(
            f"search {search.name!r}: price_min ({lo}) is above price_max ({hi})"
        )


def load_config(path: Path) -> Config:
    """Load searches.yaml — the single source of truth for what runs and when."""
    searches = load_searches(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
        schedule = parse_schedule(data.get("schedule"))
    except ScheduleError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    raw_proxies = data.get("proxies") or {}
    mode = str(raw_proxies.get("mode", "auto")).lower()
    if mode not in ("auto", "always", "never"):
        raise ConfigError(f"{path}: proxies.mode must be auto, always or never, got {mode!r}")
    proxies = ProxySettings(
        mode=mode,
        sources=list(raw_proxies.get("sources") or DEFAULT_SOURCES),
        min_pool=int(raw_proxies.get("min_pool", 3)),
        max_attempts=int(raw_proxies.get("max_attempts", 6)),
        probe_concurrency=int(raw_proxies.get("probe_concurrency", 25)),
        probe_batch=int(raw_proxies.get("probe_batch", 150)),
        impersonate=str(raw_proxies.get("impersonate", "chrome136")),
    )
    return Config(searches=searches, schedule=schedule, proxies=proxies)


def load_searches(path: Path) -> list[Search]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    defaults = data.get("defaults") or {}
    entries = data.get("searches")
    if not entries:
        raise ConfigError(f"{path} defines no searches")

    searches: list[Search] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"searches[{index}] must be a mapping")
        name = entry.get("name") or f"search-{index + 1}"
        if name in seen_names:
            raise ConfigError(f"duplicate search name {name!r}")
        seen_names.add(name)

        search = Search(
            name=name,
            query=entry.get("query", ""),
            prompt=entry.get("prompt", ""),
            interval_minutes=int(
                entry.get("interval_minutes", defaults.get("interval_minutes", 60))
            ),
            max_pages=int(entry.get("max_pages", defaults.get("max_pages", 2))),
            filters=entry.get("filters") or {},
            exclude_keywords=[k.lower() for k in (entry.get("exclude_keywords") or [])],
        )
        _validate(search)
        searches.append(search)
    return searches


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a .env file into the environment.

    Real environment variables always win, so GitHub Actions secrets are never
    shadowed by a stray .env that got committed.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
