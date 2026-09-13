"""Proxy pool tests. Fully offline — no proxies are contacted."""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from subito_alerts.proxies import (
    MAX_STRIKES,
    STALE_AFTER,
    ProxyPool,
    ProxyRecord,
    as_requests_proxies,
    parse_list,
)
from subito_alerts.subito import SubitoClient

logging.disable(logging.CRITICAL)
NOW = datetime.now(timezone.utc)


class TestParsing(unittest.TestCase):
    def test_reads_bare_and_schemed_lines(self):
        text = (
            "# Updated: 2026-09-12\n"
            "1.2.3.4:8080\n"
            "socks5://5.6.7.8:1080\n"
            "\n"
            "not-a-proxy\n"
            "9.9.9.9:1\n"
        )
        self.assertEqual(
            parse_list(text, "socks5"),
            ["socks5://1.2.3.4:8080", "socks5://5.6.7.8:1080", "socks5://9.9.9.9:1"],
        )

    def test_existing_scheme_is_not_overridden(self):
        self.assertEqual(parse_list("http://1.2.3.4:80\n", "socks5"), ["http://1.2.3.4:80"])

    def test_as_requests_proxies(self):
        self.assertIsNone(as_requests_proxies(None))
        self.assertEqual(
            as_requests_proxies("socks5://1.2.3.4:1080"),
            {"http": "socks5://1.2.3.4:1080", "https": "socks5://1.2.3.4:1080"},
        )


class TestRecord(unittest.TestCase):
    def test_host_ignores_port(self):
        # Several ports on one machine are one IP to the far end.
        self.assertEqual(ProxyRecord("socks5://1.2.3.4:9").host, "1.2.3.4")
        self.assertEqual(ProxyRecord("http://1.2.3.4:80").host, "1.2.3.4")

    def test_expires_after_strike_limit(self):
        self.assertTrue(ProxyRecord("x", strikes=MAX_STRIKES).expired(NOW))
        self.assertFalse(ProxyRecord("x", strikes=MAX_STRIKES - 1).expired(NOW))

    def test_expires_when_stale(self):
        old = ProxyRecord("x", successes=5, last_ok=NOW - STALE_AFTER - timedelta(hours=1))
        fresh = ProxyRecord("y", successes=5, last_ok=NOW)
        self.assertTrue(old.expired(NOW))
        self.assertFalse(fresh.expired(NOW))

    def test_roundtrip(self):
        r = ProxyRecord("socks5://1.2.3.4:1080", successes=3, strikes=1, last_ok=NOW)
        self.assertEqual(ProxyRecord.from_json(r.to_json()), r)


class TestPool(unittest.TestCase):
    def test_proven_ranked_before_untried(self):
        pool = ProxyPool(records=[
            ProxyRecord("http://unproven:1"),
            ProxyRecord("http://good:2", successes=5, last_ok=NOW),
        ])
        self.assertEqual(pool.candidates()[0], "http://good:2")

    def test_one_entry_per_host(self):
        # Otherwise a single machine with 7 open ports monopolises every attempt.
        pool = ProxyPool(records=[
            ProxyRecord("http://1.1.1.1:1", successes=1, last_ok=NOW),
            ProxyRecord("http://1.1.1.1:2", successes=1, last_ok=NOW),
            ProxyRecord("http://2.2.2.2:1", successes=1, last_ok=NOW),
        ])
        self.assertEqual(len(pool.candidates()), 2)

    def test_success_clears_strikes(self):
        pool = ProxyPool(records=[ProxyRecord("http://p:1", strikes=2)])
        pool.record_success("http://p:1")
        self.assertEqual(pool.records["http://p:1"].strikes, 0)
        self.assertTrue(pool.records["http://p:1"].proven)

    def test_prune_drops_struck_out(self):
        pool = ProxyPool(records=[
            ProxyRecord("http://dead:1", strikes=MAX_STRIKES),
            ProxyRecord("http://ok:1", successes=1, last_ok=NOW),
        ])
        self.assertEqual(pool.prune(), 1)
        self.assertEqual(list(pool.records), ["http://ok:1"])

    def test_refresh_skipped_when_pool_is_healthy(self):
        # Distinct hosts: five ports on one machine would count as one usable
        # transport, and the pool would correctly decide it needs topping up.
        pool = ProxyPool(
            records=[
                ProxyRecord(f"http://10.0.0.{i}:1080", successes=1, last_ok=NOW)
                for i in range(5)
            ],
            min_pool=3,
        )
        probe = Mock()
        fetch = Mock()
        pool.fetch_candidates = fetch
        self.assertEqual(pool.refresh(probe), 0)
        probe.assert_not_called()
        fetch.assert_not_called()  # must not touch the network either

    def test_many_ports_on_one_host_count_once(self):
        pool = ProxyPool(
            records=[
                ProxyRecord(f"socks5://45.74.31.30:{port}", successes=1, last_ok=NOW)
                for port in (1080, 1081, 1082, 1083)
            ],
            min_pool=3,
        )
        self.assertEqual(len(pool.proven), 4)
        self.assertEqual(len(pool.proven_hosts), 1)
        # So the pool knows it is short, and tops up rather than declaring victory.
        pool.fetch_candidates = lambda: ["socks5://10.0.0.9:1080"]
        pool.refresh(lambda url: "ok")
        self.assertEqual(len(pool.proven_hosts), 2)

    @staticmethod
    def _octet(url: str) -> int:
        return int(url.rsplit(".", 1)[1].split(":")[0])

    def test_refresh_adds_only_working_proxies(self):
        pool = ProxyPool(records=[], min_pool=2)
        pool.fetch_candidates = lambda: [f"http://10.0.0.{i}:1080" for i in range(10)]
        # Only even-numbered candidates "work"; the rest report why they didn't.
        pool.refresh(lambda url: "ok" if self._octet(url) % 2 == 0 else "connect-timeout")
        self.assertGreaterEqual(len(pool.proven), 2)
        self.assertTrue(all(self._octet(r.url) % 2 == 0 for r in pool.proven))

    def test_refresh_ignores_non_ok_outcomes(self):
        # Anything other than "ok" must not be treated as a working proxy.
        pool = ProxyPool(records=[], min_pool=2)
        pool.fetch_candidates = lambda: [f"http://10.0.0.{i}:1080" for i in range(6)]
        pool.refresh(lambda url: "blocked-403")
        self.assertEqual(pool.proven, [])

    def test_refresh_survives_unreachable_sources(self):
        pool = ProxyPool(records=[], min_pool=2, sources=["https://example.invalid/x.txt"])
        with patch("subito_alerts.proxies.requests.get",
                   side_effect=requests.ConnectionError("no dns")):
            self.assertEqual(pool.refresh(Mock()), 0)

    def test_json_roundtrip_ignores_malformed(self):
        pool = ProxyPool.from_json([
            {"url": "http://p:1", "successes": 2, "strikes": 0, "last_ok": None},
            {"nonsense": True},
        ])
        self.assertEqual(list(pool.records), ["http://p:1"])


class TestClientTransportSelection(unittest.TestCase):
    """The failover logic, exercised without touching the network."""

    def make(self, **kw):
        pool = ProxyPool(records=[
            ProxyRecord("http://a:1", successes=1, last_ok=NOW),
            ProxyRecord("http://b:1", successes=1, last_ok=NOW),
        ])
        return SubitoClient(pool=pool, delay=False, **kw), pool

    def responses(self, outcomes):
        """outcomes maps a proxy url (or None for direct) to a status or Exception."""
        seen = []

        def get(url, params=None, timeout=None, proxies=None):
            key = proxies["https"] if proxies else None
            seen.append(key)
            result = outcomes.get(key, 200)
            if isinstance(result, Exception):
                raise result
            resp = Mock(status_code=result)
            resp.json.return_value = {"ads": []}
            resp.raise_for_status = Mock()
            return resp

        return get, seen

    def test_direct_is_preferred_when_it_works(self):
        client, _ = self.make()
        get, seen = self.responses({})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        self.assertEqual(seen, [None])

    def test_falls_back_to_proxy_when_direct_is_403(self):
        client, _ = self.make()
        get, seen = self.responses({None: 403})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        self.assertEqual(seen, [None, "http://a:1"])
        self.assertTrue(client._direct_blocked)

    def test_direct_is_not_retried_once_blocked(self):
        client, _ = self.make()
        get, seen = self.responses({None: 403})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
            client._get({})
        # The second call goes straight to the proxy that worked.
        self.assertEqual(seen, [None, "http://a:1", "http://a:1"])

    def test_skips_dead_proxies_and_records_strikes(self):
        client, pool = self.make(allow_direct=False)
        get, seen = self.responses({"http://a:1": requests.ConnectionError("dead")})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        self.assertEqual(seen, ["http://a:1", "http://b:1"])
        self.assertEqual(pool.records["http://a:1"].strikes, 1)
        self.assertEqual(pool.records["http://b:1"].successes, 2)

    def test_403_from_a_proxy_is_a_strike(self):
        client, pool = self.make(allow_direct=False)
        get, _ = self.responses({"http://a:1": 403})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        self.assertEqual(pool.records["http://a:1"].strikes, 1)

    def test_raises_when_every_transport_fails(self):
        client, _ = self.make(allow_direct=False)
        get, _ = self.responses({
            "http://a:1": requests.ConnectionError("x"),
            "http://b:1": requests.ConnectionError("y"),
        })
        with patch.object(client.session, "get", side_effect=get):
            with self.assertRaises(RuntimeError):
                client._get({})

    def test_no_pool_means_direct_only(self):
        client = SubitoClient(pool=None, delay=False)
        self.assertEqual(list(client._transports()), [None])


if __name__ == "__main__":
    unittest.main()


class TestLazyPoolRefill(unittest.TestCase):
    """The pool is only built once a direct connection has actually failed."""

    def make(self):
        pool = ProxyPool(records=[], min_pool=1)
        pool.fetch_candidates = lambda: ["http://10.0.0.1:1080"]
        return SubitoClient(pool=pool, delay=False), pool

    def responses(self, outcomes):
        seen = []

        def get(url, params=None, timeout=None, proxies=None):
            key = proxies["https"] if proxies else None
            seen.append(key)
            resp = Mock(status_code=outcomes.get(key, 200))
            resp.json.return_value = {"ads": []}
            resp.raise_for_status = Mock()
            return resp

        return get, seen

    def test_no_probing_while_direct_works(self):
        client, pool = self.make()
        probed = Mock()
        pool.refresh = probed
        get, _ = self.responses({})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        probed.assert_not_called()

    def test_probes_and_recovers_when_direct_is_blocked(self):
        client, pool = self.make()
        get, seen = self.responses({None: 403})
        with patch.object(client.session, "get", side_effect=get), \
             patch.object(client, "prober", return_value=lambda url: "ok"):
            client._get({})
        # Direct refused, pool filled on the spot, request served by a proxy.
        self.assertEqual(seen, [None, "http://10.0.0.1:1080"])
        self.assertEqual(len(pool.proven_hosts), 1)

    def test_does_not_reprobe_when_pool_already_has_hosts(self):
        client, pool = self.make()
        pool.record_success("http://10.0.0.5:1080")
        probed = Mock()
        pool.refresh = probed
        get, _ = self.responses({None: 403})
        with patch.object(client.session, "get", side_effect=get):
            client._get({})
        probed.assert_not_called()
