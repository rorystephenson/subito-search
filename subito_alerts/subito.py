"""Client for subito.it's undocumented JSON search API."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import requests
from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://hades.subito.it/v1/search/items"

# curl_cffi reproduces a real Chrome TLS and HTTP/2 fingerprint, and supplies
# the matching User-Agent, Sec-Ch-Ua and Accept-Encoding itself. Overriding the
# User-Agent here would contradict the negotiated handshake, which is a louder
# signal than sending nothing at all — so these are only the headers Chrome adds
# for an XHR issued by the subito single-page app.
IMPERSONATE = "chrome136"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://www.subito.it/",
    "Origin": "https://www.subito.it",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
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
            # The seller's own shipping flag. Worth showing even though the
            # description usually repeats it: a prompt can key off "will they
            # post it" without having to infer it from free text.
            "seller_ships": "sì" if self.shippable else "non indicato",
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
    """Paginating, politely-throttled reader for the search API."""

    def __init__(
        self,
        timeout: int = 20,
        max_retries: int = 3,
        delay: bool = True,
        impersonate: str = IMPERSONATE,
    ):
        self.impersonate = impersonate
        self.session = curl_requests.Session(impersonate=impersonate)
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(SEARCH_URL, params=params, timeout=self.timeout)
                if resp.status_code == 403:
                    # Subito scores the client's TLS and HTTP/2 fingerprint, and
                    # this one has been rejected. Retrying cannot help; the fix
                    # is a newer `impersonate` profile.
                    raise RuntimeError(
                        "HTTP 403 from subito — the "
                        f"{self.impersonate!r} fingerprint is being refused. "
                        "Try a newer profile via the `impersonate` setting."
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except RuntimeError:
                raise
            except Exception as exc:
                # curl_cffi raises its own error hierarchy, not requests'.
                last_error = exc
                backoff = 2**attempt + random.uniform(0, 1)
                log.warning("subito request failed (%s), retrying in %.1fs", exc, backoff)
                time.sleep(backoff)
        raise RuntimeError(
            f"subito request failed after {self.max_retries} attempts: {last_error}"
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
