"""Time-driven retention and holder-method tests. No network."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SCAN  # noqa: E402
from desk import chain  # noqa: E402
from desk.chain import HolderReadiness  # noqa: E402
from desk import scanner  # noqa: E402
from desk.scanner import free_kill  # noqa: E402
from desk.watch import CandidateQueue  # noqa: E402
from run_crypto import cycle  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _token(mint, created, **kwargs):
    row = {
        "mint": mint, "symbol": mint, "dex": "pump-fun", "pool": "P",
        "pool_created_at": created,
        "price_usd": 0.1, "liquidity_usd": 30_000, "volume_h1": 60_000,
        "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250,
    }
    row.update(kwargs)
    return row


def _iso(moment):
    return moment.astimezone(UTC).isoformat()


class Watch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "queue.json"
        self.queue = CandidateQueue(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_too_young_pool_is_rechecked_after_it_leaves_the_feed(self):
        created = _iso(T0)
        young = _token("M", created, age_minutes=1)
        calls = []

        def refresh(mint, session=None):
            calls.append(mint)
            return {"token": _token(mint, created, age_minutes=11)}

        self.queue.observe([young], T0 + timedelta(minutes=1))
        early = self.queue.refresh_due(refresh, T0 + timedelta(minutes=1), sleep=lambda _s: None)
        self.assertEqual(calls, [])
        self.assertEqual(early["fetched"], 0)
        self.queue.observe([], T0 + timedelta(minutes=2))
        self.assertIn("M", self.queue.state["candidates"])
        later = self.queue.refresh_due(refresh, T0 + timedelta(minutes=10), sleep=lambda _s: None)
        self.assertEqual(calls, ["M"])
        self.assertEqual([row["mint"] for row in later["ready"]], ["M"])
        self.assertEqual(later["ready"][0]["age_minutes"], 10)

    def test_recheck_uses_fresh_fields_not_the_discovery_snapshot(self):
        created = _iso(T0)
        self.queue.observe([_token("M", created, liquidity_usd=9_000_000)], T0 + timedelta(minutes=1))

        def refresh(mint, session=None):
            fresh = _token(mint, created)
            fresh["liquidity_usd"] = None
            fresh["price_usd"] = None
            return {"token": fresh}

        result = self.queue.refresh_due(refresh, T0 + timedelta(minutes=10), sleep=lambda _s: None)
        self.assertEqual(result["ready"], [])
        self.assertEqual(self.queue.state["candidates"]["M"]["reason"], "missing_price_usd")
        self.assertEqual(free_kill({"mint": "M", "price_usd": None, "age_minutes": 10,
                                    "liquidity_usd": 9_000_000, "volume_h1": 60_000,
                                    "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250}),
                         "missing_price_usd")

    def test_expired_candidate_is_not_fetched(self):
        created = _iso(T0)
        calls = []
        self.queue.observe([_token("OLD", created)], T0 + timedelta(hours=7))
        result = self.queue.refresh_due(lambda mint, session=None: calls.append(mint), T0 + timedelta(hours=7))
        self.assertEqual(calls, [])
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(self.queue.state["candidates"]["OLD"]["status"], "expired")

    def test_restart_keeps_discovery_time(self):
        created = _iso(T0)
        now = T0 + timedelta(minutes=1)
        self.queue.observe([_token("M", created)], now)
        discovered = self.queue.state["candidates"]["M"]["discovered_at"]
        again = CandidateQueue(self.path)
        self.assertEqual(again.state["candidates"]["M"]["discovered_at"], discovered)
        self.assertEqual(again.state["candidates"]["M"]["pool_created_at"], created)
        self.assertEqual(again.state["candidates"]["M"]["age_minutes_at_discovery"], 1.0)

    def test_duplicate_feed_does_not_reset_discovery(self):
        created = _iso(T0)
        first = self.queue.observe([_token("M", created)], T0 + timedelta(minutes=1))
        discovered = self.queue.state["candidates"]["M"]["discovered_at"]
        second = self.queue.observe([_token("M", _iso(T0 + timedelta(minutes=5)))], T0 + timedelta(minutes=6))
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.queue.state["candidates"]["M"]["discovered_at"], discovered)
        self.assertEqual(self.queue.state["candidates"]["M"]["pool_created_at"], created)
        self.assertEqual(len(self.queue.state["candidates"]), 1)

    def test_malformed_row_is_not_a_future_pass(self):
        stats = self.queue.observe([{"mint": "BAD"}, "nope", {"symbol": "X"}], T0)
        self.assertEqual(stats["rejected"], 3)
        self.assertEqual(self.queue.state["candidates"]["BAD"]["status"], "rejected")
        self.assertEqual(self.queue.refresh_due(lambda *a, **k: None, T0 + timedelta(hours=1))["fetched"], 0)

    def test_rate_limit_stops_the_batch_and_obeys_retry_after(self):
        created = _iso(T0 - timedelta(minutes=30))
        now = T0
        self.queue.observe([_token("A", created), _token("B", created)], now)
        calls = []

        def refresh(mint, session=None):
            calls.append(mint)
            return {"rate_limited": True, "retry_after": "15", "token": None}

        slept = []
        result = self.queue.refresh_due(refresh, now, sleep=slept.append)
        self.assertEqual(calls, ["A"])
        self.assertTrue(result["stopped_for_rate_limit"])
        nxt = datetime.fromisoformat(self.queue.state["candidates"]["A"]["next_check_at"])
        self.assertEqual(int((nxt - now).total_seconds()), 15)
        self.assertEqual(self.queue.state["candidates"]["B"]["status"], "waiting")

    def test_queue_is_bounded_and_concurrent_writes_keep_both_mints(self):
        original = SCAN["watch_limit"]
        SCAN["watch_limit"] = 1
        try:
            created = _iso(T0 - timedelta(minutes=30))
            stats = self.queue.observe([_token("A", created), _token("B", created)], T0)
            self.assertEqual(stats["added"], 1)
            self.assertEqual(stats["not_retained"], 1)
        finally:
            SCAN["watch_limit"] = original
        path = self.path
        barrier = threading.Barrier(2)

        def add(mint):
            queue = CandidateQueue(path)
            barrier.wait(timeout=5)
            queue.observe([_token(mint, _iso(T0 - timedelta(minutes=30)))], T0)

        SCAN["watch_limit"] = 50
        try:
            threads = [threading.Thread(target=add, args=(mint,)) for mint in ("C", "D")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
            saved = json.loads(path.read_text())
            self.assertIn("C", saved["candidates"])
            self.assertIn("D", saved["candidates"])
        finally:
            SCAN["watch_limit"] = original


class RefreshPayload(unittest.TestCase):
    def test_refresh_stops_on_429_and_rejects_a_malformed_body(self):
        class Session:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def get(self, url, params=None, headers=None, timeout=None):
                return SimpleNamespace(status_code=self.status, headers={"Retry-After": "9"},
                                       json=lambda: self.body)

        limited = scanner.refresh_token("M", Session(429, {}))
        self.assertTrue(limited["rate_limited"])
        self.assertEqual(limited["retry_after"], "9")
        self.assertIsNone(limited["token"])
        malformed = scanner.refresh_token("M", Session(200, {"data": "not-a-list"}))
        self.assertEqual(malformed["reason"], "incomplete_refresh")
        self.assertIsNone(malformed["token"])


class CycleRetention(unittest.TestCase):
    def test_cycle_does_not_fetch_until_due_and_then_refreshes_off_feed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = CandidateQueue(Path(tmp.name) / "queue.json")
        created = _iso(T0)
        calls = []
        facts = []

        def refresh(mint, session=None):
            calls.append((mint, "refresh"))
            return {"token": _token(mint, created)}

        def facts_for(token):
            facts.append(token["mint"])
            raise AssertionError("chain should not run for this assertion path")

        from desk.ops import Bench
        cycle(None, Bench(str(Path(tmp.name) / "bench.db")), shadow=True, queue=queue,
              tokens=[_token("M", created, age_minutes=1, liquidity_usd=1_000)],
              now=T0 + timedelta(minutes=1), refresh_for=refresh, sleep=lambda _s: None,
              facts_for=facts_for, analyze=lambda *a, **k: None)
        self.assertEqual(calls, [])
        self.assertEqual(facts, [])
        cycle(None, Bench(str(Path(tmp.name) / "bench.db")), shadow=True, queue=queue,
              tokens=[], now=T0 + timedelta(minutes=15), refresh_for=refresh,
              sleep=lambda _s: None, facts_for=lambda token: facts.append(token["mint"]) or {
                  "mint_authority": "SET", "freeze_authority": None,
                  "concentration_available": False, "coverage_complete": False,
              }, analyze=lambda *a, **k: (_ for _ in ()).throw(AssertionError("model ran")))
        self.assertEqual(calls, [("M", "refresh")])
        self.assertEqual(facts, ["M"])


class HolderMethod(unittest.TestCase):
    def test_429_is_remembered_and_does_not_pass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        readiness = HolderReadiness(Path(tmp.name) / "ready.json")
        calls = []

        def boom(method, params, session=None):
            calls.append(method)
            if method == "getTokenLargestAccounts":
                response = SimpleNamespace(status_code=429, headers={
                    "Retry-After": "10", "x-ratelimit-method-limit": "0"})
                raise requests.HTTPError("HTTP 429 for getTokenLargestAccounts", response=response)
            return {"value": {"data": {"parsed": {"info": {
                "decimals": 6, "supply": "1000000", "mintAuthority": None, "freezeAuthority": None}}}}}

        original = chain.rpc
        chain.rpc = boom
        now = T0
        try:
            first = chain.facts("MINT", readiness=readiness, now=now)
            second = chain.facts("OTHER", readiness=readiness, now=now + timedelta(seconds=1))
        finally:
            chain.rpc = original
        self.assertEqual(first["concentration_failure"], "holder_method_unavailable")
        self.assertEqual(chain.chain_kill(first), "holder_method_unavailable")
        self.assertEqual(chain.chain_kill(second), "holder_method_unavailable")
        self.assertEqual(calls, ["getAccountInfo", "getTokenLargestAccounts"])
        text = Path(tmp.name, "ready.json").read_text()
        self.assertNotIn("api-key", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    def test_fresh_cache_is_reused_and_a_stale_cache_is_not(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        readiness = HolderReadiness(Path(tmp.name) / "ready.json")
        calls = []

        def fake(method, params, session=None):
            calls.append(method)
            if method == "getAccountInfo":
                return {"value": {"data": {"parsed": {"info": {
                    "decimals": 0, "supply": "10", "mintAuthority": None, "freezeAuthority": None}}}}}
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": "A", "uiAmount": 10}]}
            if method == "getMultipleAccounts" and params[1]["encoding"] == "jsonParsed":
                return {"value": [{"data": {"parsed": {"info": {"owner": "W"}}}}]}
            return {"value": [{"owner": chain.SYSTEM_PROGRAM}]}

        original = chain.rpc
        chain.rpc = fake
        try:
            chain.facts("MINT", readiness=readiness, now=T0)
            chain.facts("MINT", readiness=readiness, now=T0 + timedelta(seconds=30))
            self.assertEqual(calls.count("getAccountInfo"), 1)
            chain.facts("MINT", readiness=readiness, now=T0 + timedelta(seconds=121))
            self.assertEqual(calls.count("getAccountInfo"), 2)
        finally:
            chain.rpc = original
