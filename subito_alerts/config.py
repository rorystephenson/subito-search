"""Load and validate searches.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_FILTERS = {
    "category", "region", "town", "price_min", "price_max",
    "shippable", "title_only",
}


class ConfigError(Exception):
    pass


@dataclass
class FetchSettings:
    """How the HTTP client presents itself to subito."""

    # Subito scores the client's TLS and HTTP/2 fingerprint; curl_cffi
    # reproduces a real Chrome handshake. Roll this forward as Chrome versions
    # age — curl_cffi ships the list of profiles it supports.
    impersonate: str = "chrome136"


@dataclass
class Config:
    searches: list[Search]
    fetch: FetchSettings = field(default_factory=FetchSettings)



@dataclass
class Search:
    name: str
    query: str
    prompt: str
    # How far back to look the first time a search runs, or after the state
    # file is lost. Scheduling itself lives in the VPS crontab — this only
    # bounds a cold start, so it cannot alert on a whole page of old listings.
    cold_start_minutes: int = 30
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
    if search.cold_start_minutes < 1:
        raise ConfigError(f"search {search.name!r}: 'cold_start_minutes' must be >= 1")
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
    data = yaml.safe_load(path.read_text()) or {}

    raw_fetch = data.get("fetch") or {}
    fetch = FetchSettings(impersonate=str(raw_fetch.get("impersonate", "chrome136")))
    return Config(searches=searches, fetch=fetch)


def load_searches(path: Path) -> list[Search]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    defaults = data.get("defaults") or {}
    # An explicit `searches: []` is a deliberate pause between hunts: the VPS
    # trigger keeps dispatching and each run is a no-op. A missing key is
    # still an error, since that is far more likely a typo than an intent.
    entries = data.get("searches")
    if entries is None:
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
            cold_start_minutes=int(
                entry.get("cold_start_minutes", defaults.get("cold_start_minutes", 30))
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
