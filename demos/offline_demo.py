"""Offline demo of the paper-only safety fixes.

Every number below is an invented fixture. This script does not read API keys,
does not open a socket, and does not touch a live broker.

    python demos/offline_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import MODEL  # noqa: E402
from desk import analyst, chain, scanner  # noqa: E402
from desk.broker import EXECUTION_ASSUMPTIONS, PaperBroker  # noqa: E402
from desk.budget import BudgetExhausted, ModelBudget, PaidCallsDisabled  # noqa: E402
from desk.ops import alert, set_journal_path  # noqa: E402
from desk.stocks import PaperOnlyError, assert_paper_only  # noqa: E402
from run_crypto import kill_message, open_broker, stop_for_kill  # noqa: E402

UTC = ZoneInfo("UTC")
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def section(title: str) -> None:
    print(f"\n== {title}")


def main() -> None:
    print("Offline paper-simulator demo")
    print("Fixtures are invented. No API keys. No network. No live orders.")
    print(EXECUTION_ASSUMPTIONS)

    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        previous = os.getcwd()
        os.chdir(tmp)
        set_journal_path(tmp / "journal.jsonl")
        try:
            vanished_quote(tmp)
            kill_switch(tmp)
            holders()
            midnight(tmp)
            model_budget(tmp)
            scanner_sample()
            shadow(tmp)
            alerts_and_stocks()
        finally:
            os.chdir(previous)
            set_journal_path(None)

    section("Limitations")
    print(scanner.SAMPLE_CLAIM)
    print(chain.HOLDER_CLAIM)
    print("The daily-loss gate blocks new entries only. It is not a guaranteed max loss.")
    print("There is no evidence in this repository that the desk is profitable or fit for real money.")
    print("\nDemo finished without a network call.")


def vanished_quote(tmp: Path) -> None:
    section("Vanished quote on a $20 simulated holding")
    broker = PaperBroker(tmp / "vanished.json", now=lambda: datetime(2026, 11, 8, 12, tzinfo=UTC))
    assert broker.buy("MINT", "FIXTURE", 20, 1.0, 100_000)["filled"]
    broker.s["positions"]["MINT"]["last"] = 50
    result = broker.attempt_exit("MINT", None)
    exposure = broker.exposure()
    print(f"status={result['status']} proceeds={result['proceeds_usd']} cash={exposure['cash']}")
    print(f"realized={exposure['realized_pnl_usd']} loss_risk={exposure['loss_risk_usd']} "
          f"zero_recovery_mark={exposure['unresolved'][0]['zero_recovery_mark_usd']}")
    print(f"conservative_equity={exposure['conservative_equity_usd']} still_open={broker.open_count()}")
    assert result["status"] == "failed"
    assert exposure["cash"] == 980
    assert exposure["realized_pnl_usd"] == 0
    assert exposure["loss_risk_usd"] == 20
    assert exposure["conservative_equity_usd"] == 980
    assert broker.equity({"MINT": 50}) == 980


def kill_switch(tmp: Path) -> None:
    section("KILL with one missing quote")
    broker = PaperBroker(tmp / "kill.json")
    assert broker.buy("QUOTED", "YES", 20, 1.0, 100_000)["filled"]
    assert broker.buy("DARK", "NO", 20, 1.0, 100_000)["filled"]
    broker.s["positions"]["DARK"]["last"] = 80
    cash_before_dark_could_have_paid = broker.s["cash"]
    report = stop_for_kill(broker, {
        "QUOTED": {"price_usd": 1.0, "liquidity_usd": 100_000},
        "DARK": None,
    })
    message = kill_message(report)
    print(message)
    print(f"all_sold={report['all_sold']} still_open={report['still_open']} cash={broker.s['cash']}")
    assert report["entries_stopped"] is True
    assert report["all_sold"] is False
    assert report["still_open"] == 1
    assert "DARK" in broker.s["positions"]
    assert broker.s["positions"]["DARK"]["unresolved"] is True
    assert broker.s["cash"] != cash_before_dark_could_have_paid + 80
    assert "All paper positions closed" not in message


def holders() -> None:
    section("Holder sample: known pool excluded, unknown owner fails closed")

    def fake(method, params, session=None):
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {
                "decimals": 6, "supply": str(1_000_000_000 * 10 ** 6),
                "mintAuthority": None, "freezeAuthority": None}}}}}
        if method == "getTokenLargestAccounts":
            return {"value": [
                {"address": "pool", "uiAmount": 500_000_000},
                {"address": "a1", "uiAmount": 20_000_000},
                {"address": "a2", "uiAmount": 20_000_000},
                {"address": "mystery", "uiAmount": 30_000_000},
            ]}
        if method == "getMultipleAccounts" and params[1]["encoding"] == "jsonParsed":
            owners = {"pool": "poolPDA", "a1": "wallet", "a2": "wallet", "mystery": "ghost"}
            return {"value": [{"data": {"parsed": {"info": {"owner": owners[addr]}}}} for addr in params[0]]}
        programs = {"poolPDA": PUMP, "wallet": chain.SYSTEM_PROGRAM, "ghost": None}
        return {"value": [None if programs[owner] is None else {"owner": programs[owner]} for owner in params[0]]}

    original = chain.rpc
    chain.rpc = fake
    try:
        facts = chain.facts("FIXTURE")
    finally:
        chain.rpc = original
    print(f"available={facts['concentration_available']} failure={facts['concentration_failure']}")
    print(f"developer_wallet_audited={facts['developer_wallet_audited']} "
          f"coverage_complete={facts['coverage_complete']}")
    print(facts["limitations"])
    assert facts["concentration_available"] is False
    assert chain.chain_kill(facts) == "unknown_holder"
    assert facts["developer_wallet_audited"] is False
    assert facts["excluded_as_pool"]
    assert all(row["program"] == PUMP for row in facts["excluded_as_pool"])
    print("known pump pool was excluded with evidence; the missing owner still failed closed")


def midnight(tmp: Path) -> None:
    section("Daily loss baseline across midnight UTC")
    state = {"now": datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC)}
    broker = PaperBroker(tmp / "day.json", now=lambda: state["now"], tz="UTC")
    assert broker.buy("MINT", "FIXTURE", 40, 1.0, 100_000)["filled"]
    assert broker.attempt_exit("MINT", None)["status"] == "failed"
    print(f"before midnight day={broker.s['day']} pnl={broker.day_pnl()} cash={broker.s['cash']}")
    assert broker.day_pnl() == -40
    state["now"] = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    assert broker.roll() is False
    print(f"restart-style hold: day stays {broker.s['day']} until quotes are refreshed")
    broker.note_quotes_refreshed()
    assert broker.roll() is True
    print(f"after refresh day={broker.s['day']} day_start={broker.s['day_start']} "
          f"pnl={broker.day_pnl()} loss_risk={broker.exposure()['loss_risk_usd']}")
    assert broker.s["day"] == "2026-11-09"
    assert broker.s["cash"] == 960
    assert broker.exposure()["loss_risk_usd"] == 40
    assert broker.realized_pnl() == 0


def model_budget(tmp: Path) -> None:
    section("Model budget: disabled by default, then a fixture budget")
    print(f"default enabled={MODEL['enabled']} model={MODEL['model']!r} "
          f"prices={MODEL['price_input_usd_per_million']}")
    called = []
    fake = SimpleNamespace(messages=SimpleNamespace(
        parse=lambda **_kwargs: called.append(True),
        create=lambda **_kwargs: called.append(True),
    ))
    try:
        analyst.analyze_token({"symbol": "FIXTURE"}, {}, {}, client=fake)
    except PaidCallsDisabled as exc:
        print(f"default call refused: {exc}")
    assert called == []

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
    budget = ModelBudget(tmp / "budget.json", cfg)
    budget.reserve("analyze_token")
    try:
        budget.reserve("analyze_token")
    except BudgetExhausted as exc:
        print(f"second fixture request refused: {exc}")
    else:
        raise AssertionError("budget allowed a second reservation")
    print("Fixture prices are not a claim about any provider's current rates.")


def scanner_sample() -> None:
    section("Scanner sample, two pages")

    def page(mint: str) -> dict:
        return {"data": [{
            "attributes": {
                "address": "P", "name": f"{mint} / SOL", "base_token_price_usd": "0.001",
                "fdv_usd": "150000", "reserve_in_usd": "20000",
                "volume_usd": {"h1": "50000"},
                "transactions": {"h1": {"buys": 1, "sells": 1}},
                "pool_created_at": "2026-11-08T12:00:00Z",
            },
            "relationships": {
                "base_token": {"data": {"id": f"solana_{mint}"}},
                "dex": {"data": {"id": "pump-fun"}},
            },
        }], "included": []}

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, **kwargs):
            self.calls.append(params["page"])
            body = page("ONE") if params["page"] == 1 else page("TWO")
            return SimpleNamespace(status_code=200, json=lambda body=body: body)

    session = Session()
    sample = scanner.new_tokens(session)
    print(f"pages_fetched={sample.pages_fetched} tokens={[row['mint'] for row in sample.tokens]}")
    print(sample.claim)
    assert session.calls == [1, 2]
    assert sample.pages_fetched == 2
    assert "Not every launch" in sample.claim


def shadow(tmp: Path) -> None:
    section("Shadow mode leaves the paper ledger alone")
    ledger = tmp / "paper_ledger.json"
    broker = PaperBroker(ledger)
    broker.buy("MINT", "FIXTURE", 20, 1.0, 100_000)
    before = ledger.read_bytes()
    isolated = open_broker(True)
    isolated.buy("OTHER", "NO", 20, 1.0, 100_000)
    assert ledger.read_bytes() == before
    assert isolated.path is None
    saved = json.loads(ledger.read_text())
    assert "MINT" in saved["positions"]
    assert "OTHER" not in saved["positions"]
    print("paper ledger bytes unchanged; shadow buy stayed in memory")


def alerts_and_stocks() -> None:
    section("Alerts off, live stock client refused")
    os.environ.pop("ALERTS_ENABLED", None)
    os.environ["TELEGRAM_BOT_TOKEN"] = "fixture-token"
    os.environ["TELEGRAM_CHAT_ID"] = "fixture-chat"
    sent = []
    result = alert("fixture alert", http_post=lambda *_args, **_kwargs: sent.append(True))
    print(f"alert sent={result['sent']} http_calls={len(sent)}")
    assert result["sent"] is False and sent == []

    class Live:
        _paper = False
        _base_url = "https://api.alpaca.markets"

    try:
        assert_paper_only(Live())
    except PaperOnlyError as exc:
        print(f"live stock client refused: {exc}")
    else:
        raise AssertionError("live client was accepted")


if __name__ == "__main__":
    main()
