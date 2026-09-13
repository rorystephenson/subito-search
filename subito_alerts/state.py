"""Run state: which ads we've already handled, and when each search last ran.

Persisted as a single JSON file. In CI this file lives in the GitHub Actions
cache, which can be evicted (7 days idle). Losing it must not cause a flood of
duplicate alerts, so a search with no recorded state falls back to a timestamp
floor instead of treating the whole first page as new.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Most ads we remember per search. Ample for dedupe, tiny in the cache.
MAX_SEEN = 1000

# On a cache miss we only look this far back, at most.
COLD_START_CAP = timedelta(hours=24)


@dataclass
class SearchState:
    last_run: datetime | None = None
    seen_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._seen = set(self.seen_ids)

    def has_seen(self, ad_id: str) -> bool:
        return ad_id in self._seen

    def mark_seen(self, ad_id: str) -> None:
        if ad_id in self._seen:
            return
        self._seen.add(ad_id)
        self.seen_ids.append(ad_id)
        # FIFO trim: oldest ids fall off first.
        if len(self.seen_ids) > MAX_SEEN:
            dropped = self.seen_ids[:-MAX_SEEN]
            self.seen_ids = self.seen_ids[-MAX_SEEN:]
            self._seen.difference_update(dropped)


    def cutoff(self, cold_start_minutes: int, now: datetime) -> datetime:
        """Ignore ads posted before this instant.

        With state, that's simply the last run. Without it (first run, or an
        evicted cache) we use a bounded look-back window rather than accepting
        everything the first page happens to contain.
        """
        if self.last_run is not None:
            return self.last_run
        window = min(timedelta(minutes=cold_start_minutes), COLD_START_CAP)
        log.info("no stored state, cold-starting with a %s look-back", window)
        return now - window


class State:
    def __init__(self, path: Path, searches: dict[str, SearchState] | None = None):
        self.path = path
        self.searches = searches or {}

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            log.info("no state file at %s, starting cold", path)
            return cls(path)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt cache entry shouldn't wedge the bot; cold-start instead.
            log.warning("could not read state (%s), starting cold", exc)
            return cls(path)

        searches = {}
        for name, raw in (data.get("searches") or {}).items():
            last_run = raw.get("last_run")
            searches[name] = SearchState(
                last_run=datetime.fromisoformat(last_run) if last_run else None,
                seen_ids=list(raw.get("seen_ids") or []),
            )
        log.info("loaded state for %d search(es) from %s", len(searches), path)
        return cls(path, searches)

    def for_search(self, name: str) -> SearchState:
        return self.searches.setdefault(name, SearchState())

    def save(self) -> None:
        payload: dict[str, Any] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "searches": {
                name: {
                    "last_run": s.last_run.isoformat() if s.last_run else None,
                    "seen_ids": s.seen_ids,
                }
                for name, s in self.searches.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted run can't leave a half-written file.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)
        log.info("saved state to %s", self.path)
