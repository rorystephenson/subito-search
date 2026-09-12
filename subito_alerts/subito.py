"""Client for subito.it's undocumented JSON search API."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import requests

from .proxies import PROBE_TIMEOUT, Prober, ProxyPool, as_requests_proxies

log = logging.getLogger(__name__)

SEARCH_URL = "https://hades.subito.it/v1/search/items"

# A realistic desktop Chrome fingerprint. subito serves this endpoint to its own
# web front-end, so we look like that front-end.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://www.subito.it/",
    "Origin": "https://www.subito.it",
}

PAGE_SIZE = 50

# The API hands back a bare image base URL that serves nothing on its own — the
# CDN requires a `rule` naming a rendition. ~250KB, sharp on a phone screen and
# far inside Telegram's 5MB limit for photos sent by URL.
#
# Explicitly "-jpeg", not the "-auto" variant: -auto serves AVIF for some source
# images, which Telegram's fetcher rejects, silently costing us the photo.
IMAGE_RULE = "gallery-desktop-2x-jpeg"


@dataclass(frozen=True)
class Ad:
    id: str
    title: str
    body: str
    price: int | None
    price_text: str
    condition: str
    shippable: bool
    town: str
    province: str
    region: str
    posted_at: datetime | None
    url: str
    image_url: str | None

    @property
    def location(self) -> str:
        if self.town and self.province and self.town != self.province:
            return f"{self.town} ({self.province})"
        return self.town or self.province or self.region

    def for_llm(self) -> dict[str, Any]:
        """The compact view handed to the classifier."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.body[:600],
            "price": self.price_text or "non indicato",
            "condition": self.condition or "non indicata",
            "location": self.location,
        }


def _features(ad: dict[str, Any]) -> dict[str, str]:
    """Flatten the `features` list into {uri: first value}."""
    out: dict[str, str] = {}
    for feature in ad.get("features") or []:
        values = feature.get("values") or []
        if values and isinstance(values[0], dict):
            out[feature.get("uri", "")] = str(values[0].get("value", ""))
    return out


def _parse_price(text: str) -> int | None:
    """'1.250 €' -> 1250. Returns None when the ad has no price."""
    digits = "".join(c for c in text if c.isdigit())
    return int(digits) if digits else None


def _is_yes(value: str) -> bool:
    """True for subito's affirmative ('Sì').

    Matched on the leading letter rather than the whole string: the accent can
    arrive in either Unicode normalisation form, and comparing to a literal
    "sì" silently returns False for the decomposed one.
    """
    return value.strip().lower().startswith("s")


def _parse_date(ad: dict[str, Any]) -> datetime | None:
    raw = (ad.get("dates") or {}).get("display_iso8601")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.debug("unparseable date %r", raw)
        return None


def _image_url(image: dict[str, Any]) -> str | None:
    """Build a fetchable image URL from the API's base URL."""
    base = image.get("cdn_base_url")
    return f"{base}?rule={IMAGE_RULE}" if base else None


def parse_ad(raw: dict[str, Any]) -> Ad | None:
    """Turn one API ad into an Ad. Returns None if it lacks a usable id/url."""
    # urn looks like "id:ad:<uuid>:list:660464971"; the trailing int is the
    # listing id shown in the ad URL, and is what we dedupe on.
    urn = raw.get("urn") or ""
    ad_id = urn.rsplit(":", 1)[-1] if ":" in urn else urn
    url = (raw.get("urls") or {}).get("default")
    if not ad_id or not url:
        return None

    feats = _features(raw)
    price_text = feats.get("/price", "")
    geo = raw.get("geo") or {}
    images = raw.get("images") or []

    return Ad(
        id=ad_id,
        title=(raw.get("subject") or "").strip(),
        body=(raw.get("body") or "").strip(),
        price=_parse_price(price_text),
        price_text=price_text,
        condition=feats.get("/item_condition", ""),
        shippable=_is_yes(feats.get("/item_shippable", "")),
        town=(geo.get("town") or {}).get("value", ""),
        province=(geo.get("city") or {}).get("value", ""),
        region=(geo.get("region") or {}).get("value", ""),
        posted_at=_parse_date(raw),
        url=url,
        image_url=_image_url(images[0]) if images else None,
    )


def build_params(query: str, filters: dict[str, Any], start: int) -> dict[str, Any]:
    """Map our friendly filter names onto subito's terse query params."""
    params: dict[str, Any] = {
        "q": query,
        "lim": PAGE_SIZE,
        "start": start,
        "sort": "datedesc",
    }
    mapping = {
        "category": "c",
        "region": "r",
        "town": "tn",
        "price_min": "ps",
        "price_max": "pe",
    }
    for name, param in mapping.items():
        value = filters.get(name)
        if value is not None:
            params[param] = value
    if filters.get("shippable"):
        params["shp"] = "true"
    if filters.get("title_only"):
        params["qso"] = "true"
    return params


class SubitoClient:
    """Paginating, politely-throttled reader for the search API.

    Connects directly by default. Where the network is blocked — subito refuses
    the major cloud ASNs outright — an optional ProxyPool supplies borrowed IPs
    to fall back to. Only these search requests are ever proxied.
    """

    def __init__(
        self,
        timeout: int = 20,
        max_retries: int = 3,
        delay: bool = True,
        pool: ProxyPool | None = None,
        allow_direct: bool = True,
        max_proxy_attempts: int = 6,
    ):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.pool = pool
        self.allow_direct = allow_direct
        self.max_proxy_attempts = max_proxy_attempts
        # Once a transport works, keep using it for the rest of the run rather
        # than re-testing a blocked direct connection on every page.
        self._preferred: str | None = None
        self._direct_blocked = False

    def prober(self, timeout: int | None = None) -> Prober:
        """A probe that checks a proxy against the real endpoint."""
        return Prober(
            SEARCH_URL,
            {"q": "bici", "lim": 2, "sort": "datedesc"},
            dict(HEADERS),
            timeout=timeout or PROBE_TIMEOUT,
        )

    def _transports(self) -> Iterator[str | None]:
        """Transports to try in order; None means a direct connection."""
        if self._preferred is not None:
            yield self._preferred
        if self.allow_direct and not self._direct_blocked and self._preferred is None:
            yield None
        if self.pool is None:
            return
        for url in self.pool.candidates()[: self.max_proxy_attempts]:
            if url != self._preferred:
                yield url

    def _fetch_once(self, params: dict[str, Any], proxy: str | None) -> dict[str, Any]:
        """One attempt over one transport, with backoff for transient errors."""
        last_error: Exception | None = None
        # A proxied request is slower, and retrying a dead proxy is pointless —
        # move on to the next transport instead.
        attempts = self.max_retries if proxy is None else 1
        for attempt in range(attempts):
            try:
                resp = self.session.get(
                    SEARCH_URL,
                    params=params,
                    timeout=self.timeout,
                    proxies=as_requests_proxies(proxy),
                )
                if resp.status_code == 403:
                    # Definitive: this IP is blocked. Retrying will not help.
                    raise PermissionError(f"HTTP 403 from {proxy or 'direct connection'}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    backoff = 2**attempt + random.uniform(0, 1)
                    log.warning("subito request failed (%s), retrying in %.1fs", exc, backoff)
                    time.sleep(backoff)
        raise RuntimeError(last_error)

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        for proxy in self._transports():
            label = proxy or "direct"
            try:
                payload = self._fetch_once(params, proxy)
            except PermissionError:
                log.info("%s is blocked (HTTP 403)", label)
                if proxy is None:
                    self._direct_blocked = True
                elif self.pool is not None:
                    self.pool.record_failure(proxy)
                errors.append(f"{label}: blocked")
                continue
            except Exception as exc:
                # Dead proxies fail in every imaginable way; treat all the same.
                log.debug("%s failed: %s", label, exc)
                if proxy is not None and self.pool is not None:
                    self.pool.record_failure(proxy)
                errors.append(f"{label}: {exc}")
                continue

            if proxy is not None and self.pool is not None:
                self.pool.record_success(proxy)
            if self._preferred != proxy:
                log.info("using %s", label)
            self._preferred = proxy
            return payload

        raise RuntimeError(
            "could not reach subito over any transport: " + "; ".join(errors[:4])
        )

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        max_pages: int = 2,
        stop_before: datetime | None = None,
    ) -> Iterator[Ad]:
        """Yield ads newest-first.

        Because results are sorted by date descending, `stop_before` lets us bail
        out of pagination as soon as we reach ads we've certainly already seen.
        """
        filters = filters or {}
        for page in range(max_pages):
            if page and self.delay:
                time.sleep(random.uniform(1.0, 3.0))

            payload = self._get(build_params(query, filters, page * PAGE_SIZE))
            raw_ads = payload.get("ads") or []
            if not raw_ads:
                return

            for raw in raw_ads:
                ad = parse_ad(raw)
                if ad is None:
                    continue
                if stop_before and ad.posted_at and ad.posted_at < stop_before:
                    log.debug("reached cutoff at ad %s, stopping pagination", ad.id)
                    return
                yield ad

            if len(raw_ads) < PAGE_SIZE:
                return
