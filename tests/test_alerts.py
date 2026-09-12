"""Offline tests — no network, no API keys. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from subito_alerts import classify as C
from subito_alerts.config import ConfigError, Search, load_dotenv, load_searches
from subito_alerts.main import Counts, prefilter
from subito_alerts.state import MAX_SEEN, SearchState, State
from subito_alerts.subito import IMAGE_RULE, Ad, _is_yes, _parse_price, build_params, parse_ad
from subito_alerts.telegram import TelegramNotifier, format_message

# Several tests deliberately exercise failure paths, which log warnings.
logging.disable(logging.CRITICAL)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def make_ad(ad_id="1", title="Bici da corsa", body="", price=300, image=None, posted=NOW):
    return Ad(
        id=ad_id, title=title, body=body, price=price,
        price_text=f"{price} €" if price else "", condition="Buono", shippable=True,
        town="Roma", province="Roma", region="Lazio", posted_at=posted,
        url=f"https://subito.it/{ad_id}", image_url=image,
    )


def make_search(**kw):
    base = dict(name="s", query="bici", prompt="a road bike", interval_minutes=60)
    base.update(kw)
    return Search(**base)


def write_yaml(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "searches.yaml"
    path.write_text(text)
    return path


class TestParsing(unittest.TestCase):
    def test_price(self):
        self.assertEqual(_parse_price("1.250 €"), 1250)
        self.assertEqual(_parse_price("300 €"), 300)
        self.assertIsNone(_parse_price(""))
        self.assertIsNone(_parse_price("Gratis"))

    def test_shippable_survives_either_unicode_form(self):
        import unicodedata
        self.assertTrue(_is_yes("Sì"))
        self.assertTrue(_is_yes(unicodedata.normalize("NFD", "Sì")))
        self.assertFalse(_is_yes("No"))
        self.assertFalse(_is_yes(""))

    def test_parse_ad_pulls_listing_id_from_urn(self):
        ad = parse_ad({
            "urn": "id:ad:abc-def:list:660464971",
            "subject": "Bici", "body": "desc",
            "dates": {"display_iso8601": "2026-09-12T15:26:47.336+0200"},
            "features": [
                {"uri": "/price", "values": [{"value": "300 €"}]},
                {"uri": "/item_shippable", "values": [{"value": "Sì"}]},
            ],
            "geo": {"town": {"value": "Modena"}, "city": {"value": "Modena"}},
            "urls": {"default": "https://subito.it/x"},
            "images": [{"cdn_base_url": "https://img/1"}],
        })
        self.assertEqual(ad.id, "660464971")
        self.assertEqual(ad.price, 300)
        self.assertTrue(ad.shippable)
        self.assertEqual(ad.image_url, f"https://img/1?rule={IMAGE_RULE}")

    def test_image_url_carries_a_rendition_rule(self):
        # The bare cdn_base_url serves nothing (HTTP 400) — without a `rule` the
        # photo silently fails and every alert degrades to text.
        ad = parse_ad({
            "urn": "id:ad:x:list:1", "subject": "Bici",
            "urls": {"default": "https://subito.it/x"},
            "images": [{"cdn_base_url": "https://images.sbito.it/api/v1/x/images/54/abc"}],
        })
        self.assertEqual(ad.image_url, f"https://images.sbito.it/api/v1/x/images/54/abc?rule={IMAGE_RULE}")

    def test_image_rule_forces_jpeg(self):
        # The "-auto" renditions serve AVIF for some sources, which Telegram rejects.
        self.assertTrue(IMAGE_RULE.endswith("-jpeg"), IMAGE_RULE)

    def test_ad_without_images_has_no_image_url(self):
        ad = parse_ad({"urn": "id:ad:x:list:1", "subject": "B",
                       "urls": {"default": "https://subito.it/x"}, "images": []})
        self.assertIsNone(ad.image_url)

    def test_parse_ad_rejects_unusable(self):
        self.assertIsNone(parse_ad({"urn": "", "urls": {}}))
        self.assertIsNone(parse_ad({"urn": "id:ad:x:list:5", "urls": {}}))

    def test_build_params_maps_filter_names(self):
        p = build_params("bici", {
            "price_min": 200, "price_max": 600, "region": 12,
            "shippable": True, "title_only": True,
        }, start=50)
        self.assertEqual(p["ps"], 200)
        self.assertEqual(p["pe"], 600)
        self.assertEqual(p["r"], 12)
        self.assertEqual(p["shp"], "true")
        self.assertEqual(p["qso"], "true")
        self.assertEqual(p["start"], 50)
        self.assertEqual(p["sort"], "datedesc")

    def test_build_params_omits_unset_filters(self):
        p = build_params("bici", {"shippable": False}, start=0)
        self.assertNotIn("shp", p)
        self.assertNotIn("ps", p)


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        self.state = SearchState()
        self.counts = Counts()

    def check(self, ad, search=None, cutoff=NOW - timedelta(hours=1)):
        return prefilter(ad, search or make_search(), self.state, cutoff, self.counts)

    def test_passes_a_fresh_ad(self):
        self.assertTrue(self.check(make_ad()))

    def test_rejects_already_seen(self):
        self.state.mark_seen("1")
        self.assertFalse(self.check(make_ad("1")))
        self.assertEqual(self.counts.skipped_seen, 1)

    def test_rejects_older_than_cutoff(self):
        self.assertFalse(self.check(make_ad(posted=NOW - timedelta(days=2))))
        self.assertEqual(self.counts.skipped_old, 1)

    def test_rejects_out_of_price_range(self):
        s = make_search(filters={"price_min": 200, "price_max": 600})
        self.assertFalse(self.check(make_ad(price=900), s))
        self.assertFalse(self.check(make_ad("2", price=50), s))
        self.assertEqual(self.counts.skipped_price, 2)

    def test_keeps_ads_with_no_price(self):
        # subito's server-side price filter drops these; we'd rather see them.
        s = make_search(filters={"price_min": 200, "price_max": 600})
        self.assertTrue(self.check(make_ad(price=None), s))

    def test_rejects_excluded_keyword_in_title_or_body(self):
        s = make_search(exclude_keywords=["ricambi"])
        self.assertFalse(self.check(make_ad(title="Ricambi bici"), s))
        self.assertFalse(self.check(make_ad("2", body="vendo solo RICAMBI"), s))
        self.assertEqual(self.counts.skipped_keyword, 2)


class TestState(unittest.TestCase):
    def test_cold_start_is_bounded_by_interval(self):
        s = SearchState()
        self.assertEqual(s.cutoff(30, NOW), NOW - timedelta(minutes=60))

    def test_cold_start_never_exceeds_24h(self):
        s = SearchState()
        self.assertEqual(s.cutoff(10_000, NOW), NOW - timedelta(hours=24))

    def test_cutoff_uses_last_run_when_known(self):
        last = NOW - timedelta(minutes=5)
        self.assertEqual(SearchState(last_run=last).cutoff(60, NOW), last)

    def test_is_due(self):
        self.assertTrue(SearchState().is_due(30, NOW))
        self.assertFalse(SearchState(last_run=NOW - timedelta(minutes=5)).is_due(30, NOW))
        self.assertTrue(SearchState(last_run=NOW - timedelta(minutes=31)).is_due(30, NOW))

    def test_seen_ids_are_trimmed_fifo(self):
        s = SearchState()
        for i in range(MAX_SEEN + 100):
            s.mark_seen(str(i))
        self.assertEqual(len(s.seen_ids), MAX_SEEN)
        self.assertFalse(s.has_seen("0"))          # oldest dropped
        self.assertTrue(s.has_seen(str(MAX_SEEN + 99)))  # newest kept

    def test_roundtrip(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        st = State(path)
        st.for_search("a").mark_seen("1")
        st.for_search("a").last_run = NOW
        st.save()
        again = State.load(path)
        self.assertTrue(again.for_search("a").has_seen("1"))
        self.assertEqual(again.for_search("a").last_run, NOW)

    def test_corrupt_state_cold_starts_rather_than_crashing(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text("{not json")
        self.assertEqual(State.load(path).searches, {})


class TestConfig(unittest.TestCase):
    def test_defaults_are_inherited_and_overridden(self):
        p = write_yaml(
            "defaults: {interval_minutes: 60, max_pages: 3}\n"
            "searches:\n"
            "  - {name: a, query: bici, prompt: x, interval_minutes: 15}\n"
            "  - {name: b, query: moto, prompt: y}\n"
        )
        a, b = load_searches(p)
        self.assertEqual((a.interval_minutes, a.max_pages), (15, 3))
        self.assertEqual((b.interval_minutes, b.max_pages), (60, 3))

    def test_rejections(self):
        bad = {
            "missing prompt": "searches:\n  - {name: a, query: b}\n",
            "empty query": "searches:\n  - {name: a, query: '', prompt: x}\n",
            "unknown filter": "searches:\n  - {name: a, query: b, prompt: x, filters: {colour: red}}\n",
            "inverted price": "searches:\n  - {name: a, query: b, prompt: x, filters: {price_min: 900, price_max: 1}}\n",
            "duplicate name": "searches:\n  - {name: a, query: b, prompt: x}\n  - {name: a, query: c, prompt: y}\n",
            "zero interval": "searches:\n  - {name: a, query: b, prompt: x, interval_minutes: 0}\n",
            "no searches": "defaults: {max_pages: 1}\n",
            "bad yaml": "searches: [\n",
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                load_searches(write_yaml(text))

    def test_keywords_are_lowercased(self):
        p = write_yaml("searches:\n  - {name: a, query: b, prompt: x, exclude_keywords: [RICAMBI]}\n")
        self.assertEqual(load_searches(p)[0].exclude_keywords, ["ricambi"])


VERDICTS = json.dumps({"results": [
    {"id": "1", "interested": True, "confidence": 0.9, "reason": "road bike"},
    {"id": "2", "interested": False, "confidence": 0.95, "reason": "mountain bike"},
]})


class TestDotenv(unittest.TestCase):
    def test_loads_values(self):
        d = Path(tempfile.mkdtemp()) / ".env"
        d.write_text("# comment\nFOO=bar\nQUOTED='baz'\n\nSPACED = qux \n")
        with patch.dict("os.environ", {}, clear=False):
            for k in ("FOO", "QUOTED", "SPACED"):
                import os; os.environ.pop(k, None)
            load_dotenv(d)
            import os
            self.assertEqual(os.environ["FOO"], "bar")
            self.assertEqual(os.environ["QUOTED"], "baz")
            self.assertEqual(os.environ["SPACED"], "qux")

    def test_real_environment_wins(self):
        # CI secrets must never be shadowed by a committed .env.
        d = Path(tempfile.mkdtemp()) / ".env"
        d.write_text("TOKEN=from-file\n")
        with patch.dict("os.environ", {"TOKEN": "from-ci"}):
            load_dotenv(d)
            import os
            self.assertEqual(os.environ["TOKEN"], "from-ci")

    def test_missing_file_is_fine(self):
        load_dotenv(Path(tempfile.mkdtemp()) / "nope.env")


class TestClassifier(unittest.TestCase):
    def setUp(self):
        # 3 ads, but the canned reply only covers 2 — ad 3 is the "model
        # skipped it" case, which must not silently vanish.
        self.ads = [make_ad(str(i)) for i in (1, 2, 3)]

    def classify_with(self, reply):
        c = C.GeminiClassifier(api_key="k")
        with patch.object(c, "_call", return_value=reply):
            return {v.ad.id: v for v in c.classify(self.ads, "road bike")}

    def test_parses_each_response_shape(self):
        shapes = {
            "interactions string": {"output": VERDICTS},
            "interactions parts": {"output": [{"content": [{"text": VERDICTS}]}]},
            "legacy generateContent": {"candidates": [{"content": {"parts": [{"text": VERDICTS}]}}]},
        }
        for label, payload in shapes.items():
            with self.subTest(label):
                got = self.classify_with(C._reply_text(payload))
                self.assertTrue(got["1"].interested)
                self.assertFalse(got["2"].interested)

    def test_tolerates_markdown_fences(self):
        got = self.classify_with(f"```json\n{VERDICTS}\n```")
        self.assertTrue(got["1"].interested)

    def test_missing_verdict_fails_open(self):
        got = self.classify_with(VERDICTS)
        self.assertTrue(got["3"].interested)
        self.assertTrue(got["3"].failed)

    def test_api_failure_fails_open_for_whole_batch(self):
        c = C.GeminiClassifier(api_key="k")
        with patch.object(c, "_call", side_effect=C.ClassifierError("boom")):
            verdicts = c.classify(self.ads, "x")
        self.assertEqual(len(verdicts), 3)
        self.assertTrue(all(v.interested and v.failed for v in verdicts))

    def test_confidence_is_clamped(self):
        reply = json.dumps({"results": [
            {"id": "1", "interested": True, "confidence": 7, "reason": "r"},
            {"id": "2", "interested": True, "confidence": "junk", "reason": "r"},
        ]})
        got = self.classify_with(reply)
        self.assertEqual(got["1"].confidence, 1.0)
        self.assertEqual(got["2"].confidence, 0.0)

    def test_batches_large_runs(self):
        c = C.GeminiClassifier(api_key="k")
        calls = []
        with patch.object(c, "_call", side_effect=lambda p: calls.append(p) or VERDICTS):
            c.classify([make_ad(str(i)) for i in range(C.BATCH_SIZE * 2 + 1)], "x")
        self.assertEqual(len(calls), 3)

    def test_falls_back_to_legacy_endpoint_once(self):
        c = C.GeminiClassifier(api_key="k")
        hit = []

        def post(url, json=None, timeout=None):
            hit.append("interactions" if url.endswith("/interactions") else "legacy")
            m = Mock()
            if hit[-1] == "interactions":
                m.status_code, m.ok = 404, False
            else:
                m.status_code, m.ok = 200, True
                m.json.return_value = {"candidates": [{"content": {"parts": [{"text": VERDICTS}]}}]}
            return m

        with patch.object(c.session, "post", side_effect=post):
            c.classify(self.ads, "x")
            c.classify(self.ads, "x")
        self.assertEqual(hit.count("interactions"), 1)

    def test_model_default_survives_empty_env_var(self):
        # An unset GitHub Actions repo variable arrives as "".
        with patch.dict("os.environ", {"GEMINI_MODEL": ""}):
            self.assertEqual(C.GeminiClassifier(api_key="k").model, C.DEFAULT_MODEL)

    def test_missing_key_raises(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            with self.assertRaises(C.ClassifierError):
                C.GeminiClassifier()


class TestTelegram(unittest.TestCase):
    def send(self, ad, photo_ok=True):
        n = TelegramNotifier(token="t", chat_id="c")
        hit = []

        def post(url, json=None, timeout=None):
            method = url.rsplit("/", 1)[-1]
            hit.append(method)
            ok = photo_ok or method != "sendPhoto"
            m = Mock(status_code=200 if ok else 400)
            m.json.return_value = {"ok": ok, "description": "bad file id"}
            return m

        with patch.object(n.session, "post", side_effect=post), \
             patch("subito_alerts.telegram.time.sleep"):
            n.send(C.Verdict(ad, True, 0.9, "matches"), "s")
        return hit

    def test_photo_when_image_present(self):
        self.assertEqual(self.send(make_ad(image="https://img/1")), ["sendPhoto"])

    def test_text_when_no_image(self):
        self.assertEqual(self.send(make_ad()), ["sendMessage"])

    def test_falls_back_to_text_if_photo_rejected(self):
        self.assertEqual(
            self.send(make_ad(image="https://img/1"), photo_ok=False),
            ["sendPhoto", "sendMessage"],
        )

    def test_escapes_user_content_but_keeps_own_markup(self):
        ad = make_ad(title='<b>Bici</b> & "corsa"')
        out = format_message(C.Verdict(ad, True, 0.5, "a < b"), "s")
        self.assertIn("&lt;b&gt;Bici&lt;/b&gt; &amp;", out)
        self.assertIn("a &lt; b", out)
        self.assertTrue(out.startswith("<b>"))

    def test_caption_fits_telegram_limit(self):
        out = format_message(C.Verdict(make_ad(), True, 0.9, "x" * 3000), "s")
        self.assertLessEqual(len(out), 1024)

    def test_network_error_never_leaks_the_token(self):
        # Telegram carries the token in the URL path, so a raw requests error
        # embeds it — and Actions logs are public on a public repo.
        import requests as rq
        token = "8123456789:AAHfake-token"
        n = TelegramNotifier(token=token, chat_id="1")
        with patch.object(n.session, "post", side_effect=rq.ConnectionError(
                f"Max retries exceeded with url: /bot{token}/sendMessage")), \
             patch("subito_alerts.telegram.time.sleep"):
            with self.assertRaises(Exception) as ctx:
                n._call("sendMessage", {"text": "x"})
        self.assertNotIn(token, str(ctx.exception))
        self.assertIn("<token>", str(ctx.exception))

    def test_api_error_description_is_redacted(self):
        token = "8123456789:AAHfake-token"
        n = TelegramNotifier(token=token, chat_id="1")
        m = Mock(status_code=400)
        m.json.return_value = {"ok": False, "description": f"bad /bot{token}/x"}
        with patch.object(n.session, "post", return_value=m), \
             patch("subito_alerts.telegram.time.sleep"):
            with self.assertRaises(Exception) as ctx:
                n._call("sendMessage", {"text": "x"})
        self.assertNotIn(token, str(ctx.exception))

    def test_unclassified_is_flagged(self):
        out = format_message(C.Verdict.unclassified(make_ad(), "classifier unavailable"), "s")
        self.assertIn("⚠️", out)


if __name__ == "__main__":
    unittest.main()
