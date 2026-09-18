"""Telegram Bot API notifier."""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Any

import requests

from .classify import Verdict

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Telegram throttles bots at roughly one message per second per chat.
SEND_INTERVAL = 1.1

# Bot API limits: 1024 chars for a photo caption, 4096 for a plain message.
CAPTION_LIMIT = 1024


class TelegramError(Exception):
    pass


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_message(verdict: Verdict, query: str) -> str:
    ad = verdict.ad
    lines = [f"<b>{_esc(ad.title)}</b>"]

    facts = []
    if ad.price_text:
        facts.append(f"💰 {_esc(ad.price_text)}")
    if ad.location:
        facts.append(f"📍 {_esc(ad.location)}")
    if facts:
        lines.append("  ·  ".join(facts))

    extras = []
    if ad.condition:
        extras.append(f"🏷 {_esc(ad.condition)}")
    if ad.shippable:
        extras.append("📦 spedizione")
    if extras:
        lines.append("  ·  ".join(extras))

    if verdict.failed:
        lines.append(f"⚠️ <i>unclassified — {_esc(verdict.reason)}</i>")
    elif verdict.reason:
        confidence = f" ({verdict.confidence:.0%})" if verdict.confidence else ""
        lines.append(f"🤖 <i>{_esc(verdict.reason)}</i>{confidence}")

    if ad.posted_at:
        lines.append(f"🕑 {ad.posted_at.strftime('%d/%m %H:%M')}  ·  🔎 {_esc(query)}")

    text = "\n".join(lines)
    if len(text) > CAPTION_LIMIT:
        text = text[: CAPTION_LIMIT - 1] + "…"
    return text


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout: int = 30,
    ):
        self.token = (token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not self.token or not self.chat_id:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set"
            )
        self.timeout = timeout
        self.session = requests.Session()
        self._last_send = 0.0

    def _redact(self, text: str) -> str:
        """Strip the bot token out of anything we're about to log.

        Telegram carries the token in the URL path, so a network error's message
        contains it verbatim. Actions masks registered secrets in logs, but on a
        public repository that is the only thing standing between a transient
        connection error and a leaked token — so scrub it here too.
        """
        return text.replace(self.token, "<token>") if self.token else text

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Self-throttle so bursts of matches don't trip Telegram's rate limit.
        elapsed = time.monotonic() - self._last_send
        if elapsed < SEND_INTERVAL:
            time.sleep(SEND_INTERVAL - elapsed)

        try:
            resp = self.session.post(
                f"{API_ROOT}/bot{self.token}/{method}",
                json={"chat_id": self.chat_id, **payload},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TelegramError(f"{method}: {self._redact(str(exc))}") from None
        finally:
            self._last_send = time.monotonic()
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}, non-JSON reply") from None
        if not body.get("ok"):
            raise TelegramError(
                f"{method}: {self._redact(str(body.get('description', resp.status_code)))}"
            )
        return body

    def check(self) -> str:
        """Verify the token works. Returns the bot's username."""
        return self._call("getMe", {}).get("result", {}).get("username", "?")

    def send(self, verdict: Verdict, query: str) -> None:
        caption = format_message(verdict, query)
        keyboard = {
            "inline_keyboard": [[{"text": "Apri su Subito →", "url": verdict.ad.url}]]
        }

        if verdict.ad.image_url:
            try:
                self._call(
                    "sendPhoto",
                    {
                        "photo": verdict.ad.image_url,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
                return
            except TelegramError as exc:
                # Telegram couldn't fetch the image (expired/blocked CDN URL).
                # The alert still matters, so fall through to a text message.
                log.warning("sendPhoto failed for ad %s (%s), sending text", verdict.ad.id, exc)

        self._call(
            "sendMessage",
            {
                "text": caption,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
                "link_preview_options": {"is_disabled": True},
            },
        )
