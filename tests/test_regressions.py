"""Regression tests for the paper-simulator safety fixes. No network, no keys."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import EXITS, KNOWN_POOL_PROGRAMS, MODEL, RISK, SCAN  # noqa: E402
from desk import analyst, chain, scanner  # noqa: E402
from desk.broker import EXECUTION_ASSUMPTIONS, PaperBroker  # noqa: E402
from desk.budget import (  # noqa: E402
    BudgetExhausted, ModelBudget, PaidCallsDisabled, worst_case_micro,
)
from desk.ops import alert, alerts_enabled, journal, set_journal_path  # noqa: E402
from desk.risk import Account, check_stock_trade, check_token_trade  # noqa: E402
from desk.stocks import (  # noqa: E402
    PaperOnlyError, build_clients, consider_symbol, flatten_stocks,
)
from review import claude_review  # noqa: E402
from run_crypto import account_from, cycle, kill_message, open_broker, stop_for_kill  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UTC = ZoneInfo("UTC")
NOON = datetime(2026, 11, 8, 12, 0)
NIGHT = datetime(2026, 11, 8, 2, 30)


def sample_token(**kwargs):
    token = {
        "mint": "M", "symbol": "TEST", "age_minutes": 45, "liquidity_usd": 30_000,
        "volume_h1": 60_000, "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250,
        "price_usd": 0.0002,
    }
    token.update(kwargs)
    return token


def valid_thesis():
    return analyst.TokenThesis(
        verdict="valid_setup", score=8, red_flags=[], green_flags=["crowd"],
        shape="crowd", invalidation="buyers stall", sources=["buyers_h1"],
    )


def account(**kwargs):
    base = dict(equity=1_000, day_pnl=0, open_positions=0, cash=1_000,
                open_risk_usd=0, day_start=1_000)
    base.update(kwargs)
    return Account(**base)


def budget_cfg(**kwargs):
    cfg = {
        "enabled": True,
        "model": "fixture-model-not-a-quote",
        "review_model": "",
        "max_output_tokens": 1000,
        "max_input_tokens": 1000,
        "max_billable_retries": 0,
        "daily_budget_usd": 0.002,
        "price_input_usd_per_million": 1,
        "price_output_usd_per_million": 1,
        "timezone": "UTC",
    }
    cfg.update(kwargs)
    return cfg


def clock_at(*stamps):
    state = {"i": 0, "stamps": list(stamps)}

    def now():
        return state["stamps"][min(state["i"], len(state["stamps"]) - 1)]

    return state, now


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._journal = self.tmp / "journal.jsonl"
        set_journal_path(self._journal)
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        set_journal_path(None)
        self._tmp.cleanup()

    def rows(self):
        if not self._journal.exists():
            return []
        return [json.loads(line) for line in self._journal.read_text().splitlines() if line.strip()]


class VanishedQuote(TempCase):
    def test_twenty_dollar_holding_does_not_invent_proceeds(self):
        broker = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        fill = broker.buy("M", "TEST", 20, 1.0, 100_000)
        self.assertTrue(fill["filled"])
        self.assertEqual(broker.s["cash"], 980)
        broker.s["positions"]["M"]["last"] = 50  # a stale mark must not become a fill
        result = broker.attempt_exit("M", None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["proceeds_usd"], 0)
        self.assertIsNone(result["pnl_usd"])
        self.assertEqual(broker.s["cash"], 980)
        self.assertEqual(broker.realized_pnl(), 0)
        self.assertEqual(broker.s["closed"], [])
        self.assertEqual(broker.open_count(), 1)
        self.assertTrue(broker.s["positions"]["M"]["unresolved"])
        exposure = broker.exposure()
        self.assertEqual(exposure["loss_risk_usd"], 20)
        self.assertEqual(exposure["unresolved"][0]["zero_recovery_mark_usd"], 0.0)
        self.assertEqual(exposure["unresolved"][0]["cost_usd"], 20)
        self.assertEqual(exposure["realized_pnl_usd"], 0)
        self.assertEqual(exposure["cash"], 980)
        self.assertEqual(exposure["conservative_equity_usd"], 980)
        self.assertEqual(broker.equity({"M": 50}), 980)

    def test_zero_liquidity_is_not_a_fill(self):
        broker = PaperBroker(self.tmp / "ledger.json")
        broker.buy("M", "TEST", 20, 1.0, 100_000)
        result = broker.attempt_exit("M", {"price_usd": 1.0, "liquidity_usd": 0})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(broker.s["cash"], 980)
        self.assertTrue(broker.s["positions"]["M"]["unresolved"])

    def test_partial_fill_does_not_invent_the_rest(self):
        broker = PaperBroker(self.tmp / "ledger.json")
        broker.s["cash"] = 990
        broker.s["positions"]["M"] = {
            "symbol": "TEST", "qty": 10, "entry": 1, "last": 1, "opened": 0,
            "cost": 10, "half_taken": False, "proceeds": 0, "realized": 0,
            "unresolved": False, "unresolved_reason": None,
        }
        # 2% of $200 liquidity is $4 of notional, before the fee.
        result = broker.attempt_exit(
            "M", {"price_usd": 1, "liquidity_usd": 200}, force=True, reason="test",
        )
        self.assertEqual(result["status"], "partial")
        self.assertAlmostEqual(result["proceeds_usd"], 3.96, places=6)
        self.assertAlmostEqual(broker.s["cash"], 993.96, places=6)
        self.assertIn("M", broker.s["positions"])
        self.assertGreater(broker.s["positions"]["M"]["qty"], 0)
        self.assertFalse(broker.s["positions"]["M"]["unresolved"])
        self.assertEqual(broker.s["closed"], [])
        self.assertLess(result["proceeds_usd"], 9)


class KillSwitch(TempCase):
    def test_kill_reports_unresolved_and_does_not_claim_a_sale(self):
        broker = PaperBroker(self.tmp / "ledger.json")
        broker.buy("M", "TEST", 20, 1.0, 100_000)
        broker.s["positions"]["M"]["last"] = 80
        cash = broker.s["cash"]
        report = stop_for_kill(broker, {"M": None})
        self.assertTrue(report["entries_stopped"])
        self.assertFalse(report["all_sold"])
        self.assertEqual(report["still_open"], 1)
        self.assertEqual(broker.s["cash"], cash)
        self.assertEqual(broker.realized_pnl(), 0)
        self.assertEqual(len(report["unresolved"]), 1)
        message = kill_message(report)
        self.assertIn("Holdings still open: 1", message)
        self.assertNotIn("All paper positions closed", message)
        self.assertTrue(any(row["kind"] == "exit_failed" for row in self.rows()))

    def test_kill_can_fill_a_quoted_position_and_leave_the_other(self):
        broker = PaperBroker(self.tmp / "ledger.json")
        self.assertTrue(broker.buy("A", "AAA", 20, 1.0, 100_000)["filled"])
        self.assertTrue(broker.buy("B", "BBB", 20, 1.0, 100_000)["filled"])
        cash = broker.s["cash"]
        report = stop_for_kill(broker, {
            "A": {"price_usd": 1.0, "liquidity_usd": 100_000},
            "B": None,
        })
        self.assertNotIn("A", broker.s["positions"])
        self.assertIn("B", broker.s["positions"])
        self.assertTrue(broker.s["positions"]["B"]["unresolved"])
        self.assertGreater(broker.s["cash"], cash)
        self.assertFalse(report["all_sold"])
        self.assertEqual(report["still_open"], 1)


class Holders(unittest.TestCase):
    def _run(self, supply, rows):
        def fake(method, params, session=None):
            if method == "getAccountInfo":
                return {"value": {"data": {"parsed": {"info": {
                    "decimals": 6, "supply": str(int(supply * 10 ** 6)),
                    "mintAuthority": None, "freezeAuthority": None}}}}}
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": addr, "uiAmount": amount} for addr, amount, _owner, _prog in rows]}
            if method == "getMultipleAccounts" and params[1]["encoding"] == "jsonParsed":
                owners = {addr: owner for addr, _amount, owner, _prog in rows}
                return {"value": [{"data": {"parsed": {"info": {"owner": owners[addr]}}}} for addr in params[0]]}
            if method == "getMultipleAccounts":
                programs = {}
                for _addr, _amount, owner, program in rows:
                    programs.setdefault(owner, program)
                return {"value": [
                    None if programs[owner] is None else {"owner": programs[owner]}
                    for owner in params[0]
                ]}
            raise AssertionError(method)

        original = chain.rpc
        chain.rpc = fake
        try:
            return chain.facts("MINT")
        finally:
            chain.rpc = original

    def test_known_pool_excluded_and_owner_balances_aggregate(self):
        pump = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        self.assertIn(pump, KNOWN_POOL_PROGRAMS)
        facts = self._run(1_000_000_000, [
            ("pool", 500_000_000, "poolPDA", pump),
            ("a1", 20_000_000, "wallet", chain.SYSTEM_PROGRAM),
            ("a2", 20_000_000, "wallet", chain.SYSTEM_PROGRAM),
            ("b1", 10_000_000, "other", chain.SYSTEM_PROGRAM),
        ])
        self.assertTrue(facts["concentration_available"])
        self.assertAlmostEqual(facts["top_wallet_pct"], 0.04)
        self.assertAlmostEqual(facts["top_10_pct"], 0.05)
        self.assertAlmostEqual(facts["program_held_pct"], 0.50)
        self.assertEqual(facts["aggregated_owners"], 2)
        self.assertEqual(facts["excluded_as_pool"][0]["program"], pump)
        self.assertFalse(facts["developer_wallet_audited"])
        self.assertFalse(facts["coverage_complete"])
        self.assertIsNone(chain.chain_kill(facts))

    def test_unknown_program_fails_closed_and_is_not_treated_as_a_pool(self):
        facts = self._run(1_000_000_000, [
            ("a", 400_000_000, "mystery", "NotAKnownPoolProgram111111111111111111"),
            ("b", 10_000_000, "wallet", chain.SYSTEM_PROGRAM),
        ])
        self.assertFalse(facts["concentration_available"])
        self.assertEqual(facts["concentration_failure"], "unknown_holder")
        self.assertEqual(facts["excluded_as_pool"], [])
        self.assertIsNone(facts["top_wallet_pct"])
        self.assertEqual(chain.chain_kill(facts), "unknown_holder")
        self.assertIn("not a developer-wallet audit", facts["limitations"])

    def test_missing_owner_metadata_and_zero_supply_fail_closed(self):
        missing = self._run(1_000_000_000, [
            ("a", 10_000_000, "ghost", None),
        ])
        self.assertEqual(chain.chain_kill(missing), "unknown_holder")
        zero = self._run(0, [("a", 0, "wallet", chain.SYSTEM_PROGRAM)])
        self.assertEqual(zero["concentration_failure"], "unavailable_concentration")
        self.assertEqual(chain.chain_kill(zero), "unavailable_concentration")

    def test_missing_amount_fails_closed(self):
        facts = self._run(1_000_000_000, [
            ("a", None, "wallet", chain.SYSTEM_PROGRAM),
        ])
        self.assertEqual(chain.chain_kill(facts), "unknown_holder")

    def test_concentration_thresholds_are_exclusive_at_the_cap(self):
        base = {"mint_authority": None, "freeze_authority": None,
                "top_wallet_pct": 0.01, "top_10_pct": 0.10, "concentration_available": True}
        self.assertIsNone(chain.chain_kill({**base, "top_wallet_pct": 0.05, "top_10_pct": 0.25}))
        self.assertEqual(chain.chain_kill({**base, "top_wallet_pct": 0.05 + 1e-9}), "top_wallet")
        self.assertEqual(chain.chain_kill({**base, "top_10_pct": 0.25 + 1e-9}), "top_10")


class Limits(TempCase):
    def test_cash_and_aggregate_boundaries(self):
        ok, ticket, _ = check_token_trade(valid_thesis(), account(cash=20), 30_000, now=NOON)
        self.assertTrue(ok)
        self.assertEqual(ticket, 20.0)
        no, _, reason = check_token_trade(valid_thesis(), account(cash=19.99), 30_000, now=NOON)
        self.assertFalse(no)
        self.assertIn("insufficient cash", reason)

        new_risk = 20 * EXITS["stop_pct"]
        limit = RISK["max_aggregate_open_risk_pct"] * 1_000
        allow = check_token_trade(
            valid_thesis(), account(open_risk_usd=limit - new_risk, open_positions=1), 30_000, now=NOON)
        self.assertTrue(allow[0], allow[2])
        block = check_token_trade(
            valid_thesis(), account(open_risk_usd=limit - new_risk + 0.01, open_positions=1),
            30_000, now=NOON)
        self.assertFalse(block[0])
        self.assertIn("aggregate open-risk", block[2])

        self.assertFalse(check_token_trade(valid_thesis(), account(open_positions=3), 30_000, now=NOON)[0])
        self.assertTrue(check_token_trade(valid_thesis(), account(open_positions=2), 30_000, now=NOON)[0])

        broker = PaperBroker(self.tmp / "ledger.json")
        broker.s["cash"] = 19.99
        rejected = broker.buy("M", "TEST", 20, 1.0, 100_000)
        self.assertFalse(rejected["filled"])
        self.assertEqual(broker.s["cash"], 19.99)
        self.assertEqual(broker.open_count(), 0)

        broker.s["cash"] = 1_000
        broker.s["positions"]["OLD"] = {
            "symbol": "OLD", "qty": 1, "entry": 1, "last": 1, "opened": 0,
            "cost": limit - new_risk + 0.01, "half_taken": False, "proceeds": 0,
            "realized": 0, "unresolved": True, "unresolved_reason": "fixture",
        }
        rejected = broker.buy("M", "TEST", 20, 1.0, 100_000)
        self.assertFalse(rejected["filled"])
        self.assertEqual(broker.s["cash"], 1_000)
        self.assertNotIn("M", broker.s["positions"])

    def test_daily_loss_boundaries_use_the_day_start_baseline(self):
        limit = RISK["max_daily_loss_pct"] * 1_000
        halted = check_token_trade(valid_thesis(), account(day_pnl=-limit), 30_000, now=NOON)
        self.assertIn("daily loss", halted[2])
        self.assertIn("not a guaranteed max loss", halted[2])
        opened = check_token_trade(valid_thesis(), account(day_pnl=-limit + 0.01), 30_000, now=NOON)
        self.assertTrue(opened[0], opened[2])
        # A shrunken mark must not tighten the threshold below the day's baseline.
        shrunk = check_token_trade(
            valid_thesis(), account(equity=200, cash=200, day_pnl=-limit + 1), 30_000, now=NOON)
        self.assertNotIn("daily loss", shrunk[2])

    def test_quiet_hour_boundaries(self):
        for hour in (1, 6):
            reason = check_token_trade(
                valid_thesis(), account(), 30_000, now=datetime(2026, 11, 8, hour, 0))[2]
            self.assertIn("quiet", reason)
        self.assertIn("quiet", check_token_trade(
            valid_thesis(), account(), 30_000, now=datetime(2026, 11, 8, 6, 59))[2])
        for stamp in (datetime(2026, 11, 8, 0, 59), datetime(2026, 11, 8, 7, 0)):
            ok, _, reason = check_token_trade(valid_thesis(), account(), 30_000, now=stamp)
            self.assertTrue(ok, reason)

    def test_unresolved_mark_trips_the_gate_without_closing_the_position(self):
        broker = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        broker.s["cash"] = 960
        broker.s["day_start"] = 1_000
        broker.s["positions"]["M"] = {
            "symbol": "TEST", "qty": 1, "entry": 100, "last": 100, "opened": 0,
            "cost": 40, "half_taken": False, "proceeds": 0, "realized": 0,
            "unresolved": True, "unresolved_reason": "missing quote",
        }
        view = account_from(broker)
        self.assertEqual(view.equity, 960)  # not 960 + last price
        decision = check_token_trade(valid_thesis(), view, 30_000, now=NOON)
        self.assertIn("daily loss", decision[2])
        self.assertEqual(broker.open_count(), 1)
        self.assertEqual(broker.s["cash"], 960)
        self.assertEqual(broker.realized_pnl(), 0)


class DailyBaseline(TempCase):
    def test_midnight_roll_and_restart_keep_cash_and_the_holding(self):
        state, now = clock_at(datetime(2026, 11, 8, 12, 0, tzinfo=UTC))
        broker = PaperBroker(self.tmp / "ledger.json", now=now, tz="UTC")
        self.assertEqual(broker.s["timezone"], "UTC")
        self.assertTrue(broker.buy("M", "TEST", 40, 1.0, 100_000)["filled"])
        self.assertEqual(broker.attempt_exit("M", None)["status"], "failed")
        self.assertEqual(broker.s["cash"], 960)
        self.assertEqual(broker.day_pnl(), -40)
        self.assertEqual(broker.s["day"], "2026-11-08")

        state["i"] = 1
        state["stamps"].append(datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC))
        self.assertEqual(broker.day_pnl(), -40)
        self.assertEqual(broker.s["day"], "2026-11-08")

        state["stamps"].append(datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC))
        state["i"] = 2
        # No quote refresh yet: do not reset the counter off a stale book.
        self.assertFalse(broker.roll())
        self.assertEqual(broker.s["day"], "2026-11-08")
        self.assertEqual(broker.day_pnl(), -40)

        broker.note_quotes_refreshed()
        self.assertTrue(broker.roll())
        self.assertEqual(broker.s["day"], "2026-11-09")
        self.assertEqual(broker.s["day_start"], 960)
        self.assertEqual(broker.day_pnl(), 0)
        self.assertEqual(broker.s["cash"], 960)
        self.assertEqual(broker.realized_pnl(), 0)
        self.assertEqual(broker.exposure()["loss_risk_usd"], 40)

        restarted = PaperBroker(self.tmp / "ledger.json", now=lambda: state["stamps"][2], tz="UTC")
        self.assertEqual(restarted.s["day"], "2026-11-09")
        self.assertEqual(restarted.s["day_start"], 960)
        self.assertEqual(restarted.s["cash"], 960)
        self.assertTrue(restarted.s["positions"]["M"]["unresolved"])
        self.assertEqual(restarted.day_pnl(), 0)
        self.assertEqual(restarted.exposure()["loss_risk_usd"], 40)

    def test_same_day_restart_keeps_the_loss_counter(self):
        stamp = datetime(2026, 11, 8, 15, tzinfo=UTC)
        broker = PaperBroker(self.tmp / "ledger.json", now=lambda: stamp, tz="UTC")
        broker.buy("M", "TEST", 40, 1.0, 100_000)
        broker.attempt_exit("M", None)
        self.assertEqual(broker.day_pnl(), -40)
        restarted = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 8, 18, tzinfo=UTC))
        self.assertEqual(restarted.s["day_start"], 1_000)
        self.assertEqual(restarted.day_pnl(), -40)
        view = account_from(restarted)
        self.assertIn("daily loss", check_token_trade(valid_thesis(), view, 30_000, now=NOON)[2])
        self.assertEqual(restarted.open_count(), 1)

    def test_empty_book_rolls_at_midnight_without_a_quote(self):
        broker = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        broker.s["cash"] = 900
        broker._save()
        restarted = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 9, 0, tzinfo=UTC))
        self.assertEqual(restarted.s["day"], "2026-11-09")
        self.assertEqual(restarted.s["day_start"], 900)
        self.assertEqual(restarted.day_pnl(), 0)

    def test_timezone_is_explicit(self):
        stamp = datetime(2026, 11, 9, 0, 30, tzinfo=UTC)
        utc_book = PaperBroker(self.tmp / "utc.json", now=lambda: stamp, tz="UTC")
        ny_book = PaperBroker(self.tmp / "ny.json", now=lambda: stamp, tz="America/New_York")
        self.assertEqual(utc_book.s["day"], "2026-11-09")
        self.assertEqual(ny_book.s["day"], "2026-11-08")
        self.assertEqual(utc_book.s["timezone"], "UTC")
        self.assertEqual(ny_book.s["timezone"], "America/New_York")

    def test_cycle_refreshes_marks_before_the_midnight_baseline(self):
        broker = PaperBroker(self.tmp / "ledger.json", now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        broker.s["cash"] = 900
        broker.s["day_start"] = 1_000
        broker.s["positions"]["M"] = {
            "symbol": "TEST", "qty": 1, "entry": 100, "last": 100, "opened": 0,
            "cost": 100, "half_taken": False, "proceeds": 0, "realized": 0,
            "unresolved": False, "unresolved_reason": None,
        }
        broker._save()
        cycle(
            broker, _bench(self.tmp), shadow=False, tokens=[],
            quote_for=lambda _mint: None,
            now=datetime(2026, 11, 9, 0, 0, tzinfo=UTC),
        )
        self.assertTrue(broker.s["positions"]["M"]["unresolved"])
        self.assertEqual(broker.s["cash"], 900)
        self.assertEqual(broker.s["day"], "2026-11-09")
        self.assertEqual(broker.s["day_start"], 900)
        self.assertEqual(broker.realized_pnl(), 0)


class ModelBudgetTests(TempCase):
    def test_paid_calls_are_disabled_until_configured(self):
        self.assertFalse(MODEL["enabled"])
        self.assertIsNone(MODEL["daily_budget_usd"])
        self.assertIsNone(MODEL["price_input_usd_per_million"])
        self.assertIsNone(MODEL["price_output_usd_per_million"])
        self.assertEqual(MODEL["model"], "")
        os.chdir(self.tmp)
        called = []

        def parse(**kwargs):
            called.append(kwargs)
            raise AssertionError("client should not be called")

        fake = SimpleNamespace(messages=SimpleNamespace(parse=parse, create=parse))
        with self.assertRaises(PaidCallsDisabled):
            analyst.analyze_token(sample_token(), {}, {}, client=fake)
        with self.assertRaises(PaidCallsDisabled):
            claude_review({"cycles": 0}, client=fake)
        self.assertEqual(called, [])
        self.assertFalse((self.tmp / "model_budget.json").exists())

    def test_exhaustion_failure_and_restart_do_not_refund(self):
        cfg = budget_cfg()
        path = self.tmp / "budget.json"
        budget = ModelBudget(path, cfg, now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        first = budget.reserve("analyze_token")
        self.assertEqual(first["status"], "reserved")
        with self.assertRaises(BudgetExhausted):
            budget.reserve("analyze_token")
        restarted = ModelBudget(path, cfg, now=lambda: datetime(2026, 11, 8, 13, tzinfo=UTC))
        with self.assertRaises(BudgetExhausted):
            restarted.reserve("analyze_token")
        state = json.loads(path.read_text())
        self.assertEqual(state["spent_micro"], worst_case_micro(cfg))
        self.assertEqual(state["attempts"][0]["status"], "reserved")

        calls = []

        def explode(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("provider may have billed")

        retry_cfg = budget_cfg(max_billable_retries=1, daily_budget_usd=0.004)
        retry_path = self.tmp / "retry.json"
        retry = ModelBudget(retry_path, retry_cfg)
        with self.assertRaises(RuntimeError):
            analyst.analyze_token(
                sample_token(), {}, {},
                client=SimpleNamespace(messages=SimpleNamespace(parse=explode)),
                budget=retry,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["max_tokens"], 1000)
        self.assertEqual(retry.spent_micro(), worst_case_micro(retry_cfg))
        with self.assertRaises(BudgetExhausted):
            ModelBudget(retry_path, retry_cfg).reserve("analyze_token")

    def test_successful_call_keeps_the_worst_case_reservation(self):
        cfg = budget_cfg(max_billable_retries=1, daily_budget_usd=0.004)
        budget = ModelBudget(self.tmp / "budget.json", cfg)
        calls = []

        def parse(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(parsed_output=valid_thesis())

        analyst.analyze_token(
            sample_token(), {}, {},
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse)),
            budget=budget,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.spent_micro(), worst_case_micro(cfg))

    def test_input_is_truncated_to_the_reserved_cap(self):
        cfg = budget_cfg(max_input_tokens=5, max_output_tokens=32, daily_budget_usd=1)
        budget = ModelBudget(self.tmp / "budget.json", cfg)
        seen = {}

        def parse(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(parsed_output=valid_thesis())

        analyst.analyze_token(
            sample_token(symbol="Y" * 400), {}, {},
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse)),
            budget=budget,
        )
        content = seen["messages"][0]["content"]
        self.assertLessEqual(len(content), 5 * 4)
        self.assertNotIn("Y" * 400, content)

    def test_concurrent_reserves_cannot_overspend(self):
        cfg = budget_cfg()
        budget = ModelBudget(self.tmp / "budget.json", cfg)
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            try:
                budget.reserve("analyze_token")
                results.append("ok")
            except BudgetExhausted:
                results.append("no")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["no", "ok"])
        self.assertEqual(budget.spent_micro(), worst_case_micro(cfg))

    def test_budget_rolls_at_midnight_in_its_timezone(self):
        cfg = budget_cfg()
        state, now = clock_at(datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC))
        budget = ModelBudget(self.tmp / "budget.json", cfg, now=now)
        budget.reserve("analyze_token")
        with self.assertRaises(BudgetExhausted):
            budget.reserve("again")
        state["stamps"].append(datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
        state["i"] = 1
        budget.reserve("next-day")
        self.assertEqual(budget.spent_micro(), worst_case_micro(cfg))

    def test_one_microdollar_short_is_refused(self):
        cfg = budget_cfg(daily_budget_usd=0.001999)
        budget = ModelBudget(self.tmp / "budget.json", cfg)
        self.assertLess(_micro_budget(cfg), worst_case_micro(cfg))
        with self.assertRaises(BudgetExhausted):
            budget.reserve("analyze_token")
        self.assertEqual(budget.spent_micro(), 0)


class ShadowAndLogs(TempCase):
    def test_shadow_does_not_touch_the_paper_ledger(self):
        os.chdir(self.tmp)
        ledger = self.tmp / "paper_ledger.json"
        broker = PaperBroker(ledger, now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
        broker.buy("M", "TEST", 20, 1.0, 100_000)
        before = ledger.read_bytes()
        cash = broker.s["cash"]
        positions = json.loads(json.dumps(broker.s["positions"]))

        def explode(*_args, **_kwargs):
            raise AssertionError("shadow touched the paper broker")

        broker.equity = explode
        broker.day_pnl = explode
        broker.buy = explode
        broker.attempt_exit = explode
        broker.roll = explode
        cycle(broker, _bench(self.tmp), shadow=True, tokens=[], now=NOON)
        self.assertEqual(ledger.read_bytes(), before)
        reloaded = json.loads(ledger.read_text())
        self.assertEqual(reloaded["cash"], cash)
        self.assertEqual(set(reloaded["positions"]), set(positions))

        isolated = open_broker(True)
        isolated.buy("Z", "ZZ", 20, 1.0, 100_000)
        self.assertIsNone(isolated.path)
        saved = json.loads(ledger.read_text())
        self.assertIn("M", saved["positions"])
        self.assertNotIn("Z", saved["positions"])

    def test_quiet_hours_and_cash_skip_the_model(self):
        cfg = budget_cfg(daily_budget_usd=5)
        events = []
        budget = ModelBudget(self.tmp / "budget.json", cfg, on_event=events.append)
        calls = []

        def analyze(token, facts, links):
            calls.append(token["mint"])
            return analyst.analyze_token(
                token, facts, links,
                client=SimpleNamespace(messages=SimpleNamespace(
                    parse=lambda **_kwargs: SimpleNamespace(parsed_output=valid_thesis()))),
                budget=budget,
            )

        broker = PaperBroker(self.tmp / "ledger.json")
        nowhere = lambda _token: (_ for _ in ()).throw(AssertionError("chain or model input ran"))
        cycle(broker, _bench(self.tmp), shadow=False, tokens=[sample_token()],
              now=NIGHT, analyze=analyze, budget=budget, facts_for=nowhere)
        self.assertEqual(calls, [])
        self.assertEqual(events, [])
        self.assertTrue(any(row["kind"] == "reject" and "quiet" in row["reason"] for row in self.rows()))

        import time as time_mod
        broker.s["cash"] = 5
        broker.s["positions"]["MARK"] = {
            "symbol": "MARK", "qty": 995, "entry": 1, "last": 1,
            "opened": time_mod.time(), "cost": 995, "half_taken": False,
            "proceeds": 0, "realized": 0, "unresolved": False, "unresolved_reason": None,
        }
        cycle(broker, _bench(self.tmp), shadow=False, tokens=[sample_token(mint="CASH")],
              now=NOON, analyze=analyze, budget=budget, facts_for=nowhere,
              quote_for=lambda _mint: {"price_usd": 1.0, "liquidity_usd": 100_000,
                                       "volume_h6": 1000, "volume_h24": 4000})
        self.assertEqual(calls, [])
        self.assertEqual(events, [])
        self.assertTrue(any(row.get("reason") == "insufficient cash" for row in self.rows()))

    def test_rejections_failed_exits_and_model_attempts_are_logged(self):
        broker = PaperBroker(self.tmp / "ledger.json")
        broker.buy("HELD", "HELD", 20, 1.0, 100_000)
        cfg = budget_cfg(daily_budget_usd=0.0001)
        budget = ModelBudget(self.tmp / "budget.json", cfg, on_event=lambda event: journal("model_cost", **event))
        with self.assertRaises(BudgetExhausted):
            analyst.analyze_token(
                sample_token(), {}, {},
                client=SimpleNamespace(messages=SimpleNamespace(parse=lambda **_kwargs: None)),
                budget=budget,
            )
        cycle(
            broker, _bench(self.tmp), shadow=False,
            tokens=[sample_token(mint="YOUNG", age_minutes=1)],
            quote_for=lambda _mint: None, now=NOON,
        )
        kinds = {row["kind"] for row in self.rows()}
        self.assertIn("reject", kinds)
        self.assertIn("exit_failed", kinds)
        self.assertIn("model_cost", kinds)
        self.assertTrue(any(row["kind"] == "model_cost" and row["status"] == "refused" for row in self.rows()))
        self.assertTrue(any(row["kind"] == "exit_failed" and row["proceeds_usd"] == 0 for row in self.rows()))
        self.assertTrue(any(row["kind"] == "reject" and row["stage"] == "scanner" for row in self.rows()))


class ScannerSample(unittest.TestCase):
    def test_two_pages_and_a_hard_cap(self):
        pages = {1: _page("A"), 2: _page("B"), 3: _page("C")}
        session = _Session(pages)
        sample = scanner.new_tokens(session)
        self.assertEqual(session.calls, [1, 2])
        self.assertEqual(sample.pages_requested, 2)
        self.assertEqual(sample.pages_fetched, 2)
        self.assertEqual([token["mint"] for token in sample.tokens], ["A", "B"])
        self.assertIn("Not every launch", sample.claim)
        self.assertFalse(sample.truncated)

        limited = _Session({1: _page("A"), 2: {"status": 429}})
        sample = scanner.new_tokens(limited)
        self.assertEqual(limited.calls, [1, 2])
        self.assertTrue(sample.truncated)
        self.assertTrue(sample.rate_limited)
        self.assertEqual(sample.pages_fetched, 1)
        self.assertEqual([token["mint"] for token in sample.tokens], ["A"])

        original = (SCAN["pages"], SCAN["max_pages"])
        SCAN["pages"], SCAN["max_pages"] = 50, 50
        try:
            wide = _Session({1: _page("A"), 2: _page("B"), 3: _page("SHOULD-NOT-FETCH")})
            sample = scanner.new_tokens(wide)
            self.assertEqual(wide.calls, [1, 2])
            self.assertEqual(sample.pages_requested, scanner.MAX_NEW_POOL_PAGES)
        finally:
            SCAN["pages"], SCAN["max_pages"] = original

    def test_readme_claims_match_the_code(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(scanner.SAMPLE_CLAIM, readme)
        self.assertIn(chain.HOLDER_CLAIM, readme)
        self.assertIn("A missing or unexecutable quote never books proceeds.", readme)
        self.assertIn(EXECUTION_ASSUMPTIONS, readme)
        self.assertIn(
            "There is no evidence in this repository that the desk is profitable or fit for real money.",
            readme,
        )
        self.assertNotIn("Every launch scanned", readme)
        self.assertNotIn("Every dev wallet checked", readme)
        for name in ("run_crypto.py", "run_stocks.py", "desk/broker.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("All paper positions closed", text)
            self.assertNotIn("no quote, cannot measure, close", text)


class AlertsAndStocks(TempCase):
    def test_alerts_are_off_by_default(self):
        calls = []
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=False):
            os.environ.pop("ALERTS_ENABLED", None)
            self.assertFalse(alerts_enabled())
            result = alert("hello", http_post=lambda *_args, **_kwargs: calls.append(True))
        self.assertFalse(result["sent"])
        self.assertEqual(calls, [])
        with patch.dict(os.environ, {
            "ALERTS_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat",
        }):
            result = alert("hello", http_post=lambda *_args, **_kwargs: calls.append(True) or SimpleNamespace())
        self.assertTrue(result["sent"])
        self.assertEqual(calls, [True])

    def test_stocks_refuse_live_clients_and_precheck_before_the_model(self):
        class Paper:
            _paper = True
            _base_url = "https://paper-api.alpaca.markets"

            def close_all_positions(self, cancel_orders=False):
                self.closed = True
                return "paper-flatten"

        class Live:
            _paper = False
            _base_url = "https://api.alpaca.markets"

            def close_all_positions(self, cancel_orders=False):
                raise AssertionError("live flatten")

            def submit_order(self, **_kwargs):
                raise AssertionError("live order")

        class Liar:
            _paper = True
            _base_url = "https://api.alpaca.markets"

        def factory(key, secret, paper):
            self.assertTrue(paper)
            return Paper()

        def evil(key, secret, paper):
            return Live()

        self.assertIsInstance(build_clients(factory, "k", "s"), Paper)
        with self.assertRaises(PaperOnlyError):
            build_clients(evil, "k", "s")
        with self.assertRaises(PaperOnlyError):
            build_clients(lambda *_args, **_kwargs: Liar(), "k", "s")
        live = Live()
        with self.assertRaises(PaperOnlyError):
            flatten_stocks(live)
        self.assertFalse(hasattr(live, "closed"))

        sent = []
        thesis_calls = []

        def thesis_fn():
            thesis_calls.append(True)
            return _stock_thesis()

        def submit(_thesis, qty):
            sent.append(qty)
            return "paper-order"

        quiet = consider_symbol(
            account(equity=100_000, cash=100_000, day_start=100_000),
            "NVDA", 180, thesis_fn, submit, Paper(), now=NIGHT,
        )
        self.assertFalse(quiet["ai_called"])
        self.assertEqual(thesis_calls, [])
        self.assertEqual(sent, [])

        consider_symbol(
            account(equity=100_000, cash=100_000, day_start=100_000),
            "NVDA", 180, thesis_fn, submit, Paper(), now=NOON,
        )
        self.assertEqual(sent, [27])

        sent.clear()
        with self.assertRaises(PaperOnlyError):
            consider_symbol(
                account(equity=100_000, cash=100_000, day_start=100_000),
                "NVDA", 180, thesis_fn, submit, Live(), now=NOON,
            )
        self.assertEqual(sent, [])

    def test_stock_cash_and_aggregate_boundaries(self):
        thesis = _stock_thesis(stop_loss=90)
        rich = account(equity=2_000, cash=100, day_start=2_000)
        ok, qty, _ = check_stock_trade(thesis, rich, 100, now=NOON)
        self.assertEqual((ok, qty), (True, 1))
        poor = account(equity=2_000, cash=99.99, day_start=2_000)
        self.assertIn("insufficient cash", check_stock_trade(thesis, poor, 100, now=NOON)[2])
        limit = RISK["max_aggregate_open_risk_pct"] * 2_000
        new_risk = 10  # qty 1 * $10 stop distance
        allowed = account(equity=2_000, cash=100, day_start=2_000, open_risk_usd=limit - new_risk)
        self.assertTrue(check_stock_trade(thesis, allowed, 100, now=NOON)[0])
        blocked = account(equity=2_000, cash=100, day_start=2_000, open_risk_usd=limit - new_risk + 0.01)
        self.assertIn("aggregate", check_stock_trade(thesis, blocked, 100, now=NOON)[2])


def _micro_budget(cfg) -> int:
    return int(round(float(cfg["daily_budget_usd"]) * 1_000_000))


def _bench(tmp: Path):
    from desk.ops import Bench
    return Bench(str(tmp / "bench.db"))


def _stock_thesis(**kwargs):
    base = dict(symbol="nvda", action="BUY", confidence="High", entry_reason="fixture",
                stop_loss=170, invalidation="x", sources=[])
    base.update(kwargs)
    return analyst.StockThesis.model_construct(**base)


def _page(mint: str) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    return {
        "data": [{
            "attributes": {
                "address": "P", "name": f"{mint} / SOL", "base_token_price_usd": "0.001",
                "fdv_usd": "150000", "reserve_in_usd": "20000",
                "volume_usd": {"h1": "50000"},
                "transactions": {"h1": {"buys": 300, "sells": 200, "buyers": 180}},
                "price_change_percentage": {"h1": "40"}, "pool_created_at": created,
            },
            "relationships": {
                "base_token": {"data": {"id": f"solana_{mint}"}},
                "dex": {"data": {"id": "pump-fun"}},
            },
        }],
        "included": [{"type": "token", "id": f"solana_{mint}", "attributes": {"symbol": mint}}],
    }


class _Session:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, **kwargs):
        page = params["page"]
        self.calls.append(page)
        body = self.pages[page]
        if isinstance(body, dict) and body.get("status") == 429:
            return _Response(429, {})
        return _Response(200, body)


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


if __name__ == "__main__":
    unittest.main()
