"""A self-maintaining pool of free proxies for reaching subito.

Subito sits behind Akamai, which refuses the major cloud ASNs outright — a
GitHub-hosted runner gets HTTP 403 on every request, including the bare
homepage, regardless of headers. A residential connection is fine. So when the
bot runs somewhere blocked, it needs to borrow an IP that isn't.

Public proxy lists are mostly dead: measured against subito, roughly 3% of
HTTP entries and 14% of SOCKS5 entries actually work. That is plenty for this
workload (about 56 requests a day) but only if we remember which ones worked,
which is what this module does. Scores persist in the state file, so each run
starts from what the last one learned instead of re-probing from scratch.

ONLY subito traffic goes through these. They are operated by unknown third
parties who can read and alter what passes through them, so Telegram and Gemini
calls — the ones carrying credentials — always connect directly.
"""

from __future__ import annotations

import collections
import concurrent.futures
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

import requests
from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

# A bare "1.2.3.4:8080" line, the usual format in these lists.
BARE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}$")

DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt",
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_ssl_elite.txt",
)

# Drop a proxy after this many consecutive failures.
MAX_STRIKES = 3
# Forget a proxy that hasn't worked in this long, so the pool can't fossilise.
STALE_AFTER = timedelta(days=3)

# Generous: a runner may sit far from both the proxy and subito, and a probe
# that times out is indistinguishable from a proxy that is simply dead.
PROBE_TIMEOUT = 15
REQUEST_TIMEOUT = 20


class ProxyError(Exception):
    pass


@dataclass
class ProxyRecord:
    """What we've learned about one proxy."""

    url: str
    successes: int = 0
    strikes: int = 0
    last_ok: datetime | None = None

    @property
    def host(self) -> str:
        """Two ports on one machine are one IP as far as subito is concerned."""
        return self.url.split("://")[-1].split(":")[0]

    @property
    def proven(self) -> bool:
        return self.successes > 0

    def rank(self, now: datetime) -> tuple:
        """Sort key: proven first, then most recently confirmed."""
        age = (now - self.last_ok).total_seconds() if self.last_ok else float("inf")
        return (0 if self.proven else 1, self.strikes, age)

    def expired(self, now: datetime) -> bool:
        if self.strikes >= MAX_STRIKES:
            return True
        return bool(self.last_ok and now - self.last_ok > STALE_AFTER)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "successes": self.successes,
            "strikes": self.strikes,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ProxyRecord:
        last_ok = raw.get("last_ok")
        return cls(
            url=raw["url"],
            successes=int(raw.get("successes", 0)),
            strikes=int(raw.get("strikes", 0)),
            last_ok=datetime.fromisoformat(last_ok) if last_ok else None,
        )


def parse_list(text: str, default_scheme: str) -> list[str]:
    """Pull proxy URLs out of a list file, ignoring comments and junk."""
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            found.append(line)
        elif BARE.match(line):
            found.append(f"{default_scheme}://{line}")
    return found


def _scheme_for(source: str) -> str:
    lowered = source.lower()
    for scheme in ("socks5", "socks4"):
        if scheme in lowered:
            return scheme
    return "http"


class ProxyPool:
    """Known-good proxies, topped up from public lists as they die off."""

    def __init__(
        self,
        records: Iterable[ProxyRecord] = (),
        sources: Sequence[str] = DEFAULT_SOURCES,
        min_pool: int = 3,
        probe_concurrency: int = 25,
        probe_batch: int = 150,
    ):
        self.records: dict[str, ProxyRecord] = {r.url: r for r in records}
        self.sources = list(sources)
        self.min_pool = min_pool
        self.probe_concurrency = probe_concurrency
        self.probe_batch = probe_batch

    # -- bookkeeping -----------------------------------------------------------

    @property
    def proven(self) -> list[ProxyRecord]:
        return [r for r in self.records.values() if r.proven]

    @property
    def proven_hosts(self) -> set[str]:
        """Distinct working IPs.

        Two ports on one machine are one address to subito, and `candidates()`
        only offers one entry per host — so counting records rather than hosts
        would overstate how many transports we actually have.
        """
        return {r.host for r in self.proven}

    def record_success(self, url: str) -> None:
        record = self.records.setdefault(url, ProxyRecord(url))
        record.successes += 1
        record.strikes = 0
        record.last_ok = datetime.now(timezone.utc)

    def record_failure(self, url: str) -> None:
        record = self.records.setdefault(url, ProxyRecord(url))
        record.strikes += 1

    def prune(self) -> int:
        now = datetime.now(timezone.utc)
        dead = [url for url, r in self.records.items() if r.expired(now)]
        for url in dead:
            del self.records[url]
        return len(dead)

    def candidates(self) -> list[str]:
        """Proven proxies first, best-ranked, one entry per host."""
        now = datetime.now(timezone.utc)
        ordered = sorted(self.records.values(), key=lambda r: r.rank(now))
        seen_hosts: set[str] = set()
        out: list[str] = []
        for record in ordered:
            if record.host in seen_hosts:
                continue
            seen_hosts.add(record.host)
            out.append(record.url)
        return out

    def to_json(self) -> list[dict[str, Any]]:
        return [r.to_json() for r in self.records.values()]

    @classmethod
    def from_json(cls, raw: list[dict[str, Any]] | None, **kw) -> ProxyPool:
        records = []
        for entry in raw or []:
            try:
                records.append(ProxyRecord.from_json(entry))
            except (KeyError, TypeError, ValueError):
                log.debug("skipping malformed proxy record %r", entry)
        return cls(records=records, **kw)

    # -- refilling -------------------------------------------------------------

    def fetch_candidates(self) -> list[str]:
        """Download the configured lists. Failures are non-fatal."""
        found: list[str] = []
        for source in self.sources:
            try:
                resp = requests.get(source, timeout=20)
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning("could not fetch proxy list %s: %s", source, exc)
                continue
            entries = parse_list(resp.text, _scheme_for(source))
            log.info("fetched %d proxies from %s", len(entries), source.rsplit("/", 1)[-1])
            found.extend(entries)
        return found

    def refresh(self, probe: "Prober") -> int:
        """Probe fresh candidates until the pool has enough proven proxies.

        Probing runs concurrently because the hit rate is low — most candidates
        are dead and time out, and doing that serially would dominate the run.
        """
        self.prune()
        if len(self.proven_hosts) >= self.min_pool:
            return 0

        known = set(self.records)
        fresh = [p for p in self.fetch_candidates() if p not in known]
        if not fresh:
            log.warning("no new proxy candidates available")
            return 0

        random.shuffle(fresh)
        batch = fresh[: self.probe_batch]
        log.info("probing %d candidates for working proxies…", len(batch))

        added = 0
        outcomes: collections.Counter[str] = collections.Counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.probe_concurrency) as pool:
            for url, outcome in zip(batch, pool.map(probe, batch)):
                outcomes[outcome] += 1
                if outcome == "ok":
                    self.record_success(url)
                    added += 1
                    if len(self.proven_hosts) >= self.min_pool:
                        break

        log.info(
            "probe outcomes: %s",
            ", ".join(f"{n} {name}" for name, n in outcomes.most_common()),
        )
        log.info(
            "added %d working proxies (%d distinct hosts in pool)",
            added, len(self.proven_hosts),
        )
        return added


class Prober:
    """Callable that reports whether a proxy can actually reach the target.

    Returns a short outcome string rather than a bool: when a whole batch fails
    it matters a great deal whether they timed out (too slow from here, raise
    the timeout) or came back 403 (the proxy is itself blocked), and throwing
    that away leaves you guessing.
    """

    def __init__(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: int = PROBE_TIMEOUT,
        impersonate: str | None = None,
    ):
        self.url, self.params, self.headers = url, params, headers
        self.timeout = timeout
        self.impersonate = impersonate

    def __call__(self, proxy: str) -> str:
        kw = {"impersonate": self.impersonate} if self.impersonate else {}
        try:
            resp = curl_requests.get(
                self.url,
                params=self.params,
                headers=self.headers,
                proxies={"http": proxy, "https": proxy},
                timeout=self.timeout,
                **kw,
            )
        except Exception as exc:
            # curl_cffi raises its own error types; map the common shapes by
            # message so the outcome summary stays readable.
            text = str(exc).lower()
            if "timed out" in text or "timeout" in text:
                return "timeout"
            if "proxy" in text:
                return "proxy-error"
            if "ssl" in text or "tls" in text:
                return "tls-error"
            return type(exc).__name__
        if resp.status_code == 403:
            return "blocked-403"
        if resp.status_code != 200:
            return f"http-{resp.status_code}"
        try:
            return "ok" if resp.json().get("ads") else "empty"
        except ValueError:
            return "bad-json"


def as_requests_proxies(url: str | None) -> dict[str, str] | None:
    return None if url is None else {"http": url, "https": url}
