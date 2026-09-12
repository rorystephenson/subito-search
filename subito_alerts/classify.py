"""LLM interest classification via the Gemini API.

Everything provider-specific lives behind `classify()`, so swapping to another
endpoint later is a one-file change.

Two request shapes are supported. The current API is `POST /v1beta/interactions`
with a flat {model, input, response_format} body; older keys/projects may only
have `POST /v1beta/models/<model>:generateContent`. We try the new shape first
and fall back automatically, so this keeps working either way.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Sequence

import requests

from .subito import Ad

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Ads per request. Large enough to keep call volume (and free-tier quota use)
# low, small enough that one bad ad can't poison a whole run.
BATCH_SIZE = 15

SYSTEM_PROMPT = """\
You screen classified ads from subito.it, an Italian marketplace, for a buyer.

The listings are written in Italian, often informally, with typos, abbreviations \
and missing details. The buyer's criteria are given in English.

For each ad decide whether it is worth the buyer's attention.

Guidance:
- Judge only against the buyer's stated criteria. Do not invent extra requirements.
- READ THE TITLE AS CAREFULLY AS THE DESCRIPTION. Sellers routinely put the \
decisive specification — storage size, capacity, model, year, frame size — in the \
title alone, and the description often does not repeat it. A detail present only \
in the title is still stated, not missing. Where the two disagree, say so and \
treat the listing as ambiguous.
- Subito's own search is loose and returns loosely-related items; those are exactly \
what you should reject.
- Reject accessories, spare parts, and related-but-different products when the \
buyer clearly wants the whole item (and vice versa).
- Many genuine listings are terse. If an ad is plausibly a match but simply \
under-described, mark it interested with lower confidence. A missed bargain costs \
the buyer far more than one extra notification, so when genuinely torn, include it.
- Reject only when the ad gives you positive evidence it is NOT what the buyer wants.
- `reason` must be one short English clause (max 15 words) saying what decided it.

Return one result per ad, using the exact `id` given.\
"""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The ad's id, copied exactly"},
                    "interested": {"type": "boolean"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0"},
                    "reason": {"type": "string", "description": "Max 15 words"},
                },
                "required": ["id", "interested", "confidence", "reason"],
            },
        }
    },
    "required": ["results"],
}


class ClassifierError(Exception):
    pass


@dataclass
class Verdict:
    ad: Ad
    interested: bool
    confidence: float
    reason: str
    failed: bool = False

    @classmethod
    def unclassified(cls, ad: Ad, why: str) -> Verdict:
        """Fail open: surface the ad rather than silently dropping a possible match."""
        return cls(ad=ad, interested=True, confidence=0.0, reason=why, failed=True)


def _build_input(ads: Sequence[Ad], criteria: str) -> str:
    listing = json.dumps([ad.for_llm() for ad in ads], ensure_ascii=False, indent=1)
    return (
        f"The buyer is looking for:\n{criteria.strip()}\n\n"
        f"Evaluate these {len(ads)} ads:\n{listing}"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the model's reply, tolerating ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ClassifierError(f"no JSON object in model reply: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _reply_text(payload: dict[str, Any]) -> str:
    """Pull the text out of either response shape."""
    if isinstance(payload.get("output"), str):
        return payload["output"]

    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                chunks.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for key in ("output", "candidates", "response"):
        if key in payload:
            walk(payload[key])
            break
    else:
        walk(payload)

    if not chunks:
        raise ClassifierError(f"no text in model response: {json.dumps(payload)[:300]}")
    return "".join(chunks)


class GeminiClassifier:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 60,
    ):
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
        if not self.api_key:
            raise ClassifierError(
                "GEMINI_API_KEY is not set — get a free key at "
                "https://aistudio.google.com/apikey"
            )
        # `or` rather than a .get() default: an unset GitHub Actions repo
        # variable arrives as an empty string, not as an absent key.
        self.model = (model or os.environ.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        )
        # Set once we learn which shape this key/model actually speaks.
        self._use_legacy = False

    # -- the two request shapes ------------------------------------------------

    def _post_interactions(self, prompt: str) -> requests.Response:
        return self.session.post(
            f"{API_ROOT}/interactions",
            json={
                "model": self.model,
                "input": prompt,
                "system_instruction": SYSTEM_PROMPT,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": RESULT_SCHEMA,
                },
            },
            timeout=self.timeout,
        )

    def _post_legacy(self, prompt: str) -> requests.Response:
        return self.session.post(
            f"{API_ROOT}/models/{self.model}:generateContent",
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": RESULT_SCHEMA,
                    "temperature": 0.1,
                },
            },
            timeout=self.timeout,
        )

    def _call(self, prompt: str) -> str:
        if not self._use_legacy:
            resp = self._post_interactions(prompt)
            if resp.status_code == 404:
                log.info("interactions endpoint unavailable, using generateContent")
                self._use_legacy = True
            elif resp.ok:
                return _reply_text(resp.json())
            else:
                raise ClassifierError(
                    f"Gemini HTTP {resp.status_code}: {resp.text[:300]}"
                )

        resp = self._post_legacy(prompt)
        if not resp.ok:
            raise ClassifierError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        return _reply_text(resp.json())

    # -- public API ------------------------------------------------------------

    def ping(self) -> str:
        """Smoke-test key, model and request shape. Returns the shape in use."""
        self._call('Reply with exactly {"results": []}')
        return "generateContent (legacy)" if self._use_legacy else "interactions"

    def classify(self, ads: Sequence[Ad], criteria: str) -> list[Verdict]:
        verdicts: list[Verdict] = []
        for i in range(0, len(ads), BATCH_SIZE):
            batch = ads[i : i + BATCH_SIZE]
            try:
                verdicts.extend(self._classify_batch(batch, criteria))
            except (ClassifierError, requests.RequestException, json.JSONDecodeError) as exc:
                log.error("classification failed for %d ad(s): %s", len(batch), exc)
                verdicts.extend(
                    Verdict.unclassified(ad, "classifier unavailable") for ad in batch
                )
        return verdicts

    def _classify_batch(self, ads: Sequence[Ad], criteria: str) -> list[Verdict]:
        payload = _extract_json(self._call(_build_input(ads, criteria)))
        by_id = {
            str(r.get("id")): r
            for r in payload.get("results") or []
            if isinstance(r, dict)
        }

        verdicts = []
        for ad in ads:
            result = by_id.get(ad.id)
            if result is None:
                # Model skipped this ad — don't let that silently lose it.
                log.warning("no verdict returned for ad %s", ad.id)
                verdicts.append(Verdict.unclassified(ad, "no verdict returned"))
                continue
            try:
                confidence = float(result.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            verdicts.append(
                Verdict(
                    ad=ad,
                    interested=bool(result.get("interested")),
                    confidence=max(0.0, min(1.0, confidence)),
                    reason=str(result.get("reason") or "").strip(),
                )
            )
        return verdicts
