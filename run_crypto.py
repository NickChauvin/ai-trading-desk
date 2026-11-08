"""The shift: one loop that runs the five seats in order.

    python run_crypto.py            paper trading (default)
    python run_crypto.py --shadow   journal only, isolated from the paper ledger
    python run_crypto.py --once     one cycle, then exit (good for testing)

Shadow mode does not open, manage, or mutate the paper ledger.

Each pass is more expensive than the one before it, so each one has to kill harder:
  scanner (free) -> free risk checks -> chain facts -> analyst (paid, off by default)
  -> risk engine -> paper fill

The scanner reads at most two GeckoTerminal new-pool pages. That is a sample,
not every launch. Paid model calls reserve a worst-case dollar amount first and
are disabled until config says otherwise. Quiet hours, daily loss, cash, and
aggregate open risk are checked before any model call.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from config import CYCLE_SECONDS, DESK_TIMEZONE, PAPER
from desk import analyst, chain, scanner
from desk.broker import EXECUTION_ASSUMPTIONS, PaperBroker
from desk.budget import BudgetError, ModelBudget
from desk.ops import Bench, alert, journal, killed, load_env, log_model_cost, log_reject
from desk.risk import Account, check_token_trade, precheck_token

CHAIN_BUDGET = 8      # RPC fact checks per cycle
ANALYST_BUDGET = 3    # model calls per cycle, still inside the daily dollar budget


def open_broker(shadow: bool, path=None, **kwargs) -> PaperBroker:
    if shadow:
        return PaperBroker(isolated=True, **kwargs)
    from desk.broker import LEDGER
    return PaperBroker(LEDGER if path is None else path, **kwargs)


def account_from(broker: PaperBroker) -> Account:
    return Account(
        equity=broker.equity(),
        day_pnl=broker.day_pnl(),
        open_positions=broker.open_count(),
        cash=float(broker.s["cash"]),
        open_risk_usd=broker.open_risk_usd(),
        day_start=float(broker.s["day_start"]),
    )


def shadow_account() -> Account:
    """Hypothetical book. Not the paper ledger and not allowed to touch it."""
    equity = float(PAPER["starting_equity_usd"])
    return Account(equity, 0.0, 0, cash=equity, open_risk_usd=0.0, day_start=equity)


def _public(result: dict) -> dict:
    skip = {"execution"}
    return {key: value for key, value in result.items() if key not in skip}


def log_exit(result: dict, bench: Bench | None = None) -> None:
    if result.get("status") == "failed":
        journal("exit_failed", **_public(result))
        alert(
            f"EXIT FAILED {result.get('symbol')} | {result.get('reason')} | "
            f"unresolved loss risk ${result.get('loss_risk_usd')} | proceeds $0"
        )
        return
    if result.get("status") in {"filled", "partial"}:
        journal("exit", **_public(result))
        if result.get("closed") and bench is not None:
            bench.sit(result["mint"], "exited")
        extra = f" | pnl ${result['pnl_usd']}" if result.get("pnl_usd") is not None else ""
        alert(f"EXIT {result.get('symbol')} {result.get('status')} | {result.get('reason')}{extra}")


def manage_positions(broker: PaperBroker, bench: Bench, quote_for) -> None:
    for mint in list(broker.s["positions"]):
        try:
            quoted = quote_for(mint)
        except Exception as exc:
            journal("error", stage="quote", mint=mint, error=str(exc))
            quoted = None
        log_exit(broker.attempt_exit(mint, quoted), bench)
    broker.note_quotes_refreshed()


def kill_message(report: dict) -> str:
    left = report["still_open"]
    return (
        "KILL file found. New entries are stopped. "
        f"Simulated exits were attempted. Holdings still open: {left}. "
        "Unresolved holdings were not sold and no proceeds were invented."
    )


def stop_for_kill(broker: PaperBroker, quotes: dict) -> dict:
    """Stop entries and try simulated exits. Do not claim a sale without a fill."""
    started = broker.open_count()
    results = []
    for mint in list(broker.s["positions"]):
        result = broker.attempt_exit(mint, quotes.get(mint), force=True, reason="kill switch")
        results.append(result)
        log_exit(result, bench=None)
    broker.note_quotes_refreshed()
    report = {
        "entries_stopped": True,
        "started": started,
        "still_open": broker.open_count(),
        "all_sold": False,
        "unresolved": broker.exposure()["unresolved"],
        "results": results,
        "note": "unresolved or open holdings were not sold" if broker.open_count()
        else "no paper holdings remain open after simulated exits",
    }
    report["all_sold"] = started > 0 and report["still_open"] == 0 and all(
        row.get("status") == "filled" for row in results
    )
    return report


def cycle(broker: PaperBroker | None, bench: Bench, session: requests.Session | None = None,
          shadow: bool = False, *, now: datetime | None = None, tokens=None,
          quote_for=None, facts_for=None, analyze=None, budget: ModelBudget | None = None,
          links_for=None) -> dict:
    now = now or datetime.now(ZoneInfo(DESK_TIMEZONE))
    if shadow:
        # Do not read or write the passed broker. It may be the real paper book.
        book = None
    else:
        book = broker
        if quote_for is None:
            def quote_for(mint, _session=session):
                return scanner.quote(mint, _session)
        manage_positions(book, bench, quote_for)
        book.roll(now)

    if tokens is None:
        sample = scanner.new_tokens(session)
    elif isinstance(tokens, scanner.PoolSample):
        sample = tokens
    else:
        sample = scanner.PoolSample(tokens=list(tokens), pages_requested=0, pages_fetched=0)

    stats = {"seen": 0, "benched": 0, "free": 0, "chain": 0, "analyst": 0, "risk": 0,
             "entries": 0, "pages_requested": sample.pages_requested,
             "pages_fetched": sample.pages_fetched, "pools_seen": sample.pools_seen,
             "sample_truncated": sample.truncated, "sample_claim": sample.claim,
             "shadow": shadow}
    ranked = sorted(sample.tokens, key=lambda token: token["volume_h1"] / max(token["mcap_usd"], 1),
                    reverse=True)
    chain_left, analyst_left = CHAIN_BUDGET, ANALYST_BUDGET
    model_budget = budget
    if analyze is None:
        model_budget = model_budget or ModelBudget.from_config(on_event=log_model_cost)
        def analyze(token, facts_row, links):
            return analyst.analyze_token(token, facts_row, links, budget=model_budget)

    for token in ranked:
        stats["seen"] += 1
        if book is not None and token["mint"] in book.s["positions"]:
            stats["benched"] += 1
            continue
        if bench.benched(token["mint"]):
            stats["benched"] += 1
            continue
        if (reason := scanner.free_kill(token)):
            bench.sit(token["mint"], reason)
            log_reject("scanner", reason, mint=token["mint"], symbol=token["symbol"], shadow=shadow)
            stats["free"] += 1
            continue

        account = shadow_account() if book is None else account_from(book)
        if (reason := precheck_token(account, token["liquidity_usd"], now)):
            # Account-level. Do not bench the token, and do not pay for a model call.
            log_reject("risk", reason, mint=token["mint"], symbol=token["symbol"], shadow=shadow)
            stats["risk"] += 1
            continue
        if chain_left <= 0 or analyst_left <= 0:
            log_reject("budget", "cycle chain or analyst count exhausted",
                       mint=token["mint"], symbol=token["symbol"], shadow=shadow)
            break

        try:
            facts_row = facts_for(token) if facts_for else chain.facts(token["mint"], session)
        except Exception as exc:
            journal("error", stage="chain", mint=token["mint"], error=str(exc))
            continue
        chain_left -= 1
        if (reason := chain.chain_kill(facts_row)):
            bench.sit(token["mint"], reason)
            log_reject("chain", reason, mint=token["mint"], symbol=token["symbol"],
                       facts=facts_row, shadow=shadow)
            stats["chain"] += 1
            continue

        links = links_for(token) if links_for else scanner.socials(token["mint"], session)
        try:
            thesis = analyze(token, facts_row, links)
        except BudgetError as exc:
            log_reject("budget", str(exc), mint=token["mint"], symbol=token["symbol"], shadow=shadow)
            break
        except Exception as exc:
            journal("error", stage="analyst", mint=token["mint"], error=str(exc))
            continue
        analyst_left -= 1

        account = shadow_account() if book is None else account_from(book)
        ok, ticket, reason = check_token_trade(thesis, account, token["liquidity_usd"], now)
        journal("decision", mint=token["mint"], symbol=token["symbol"], token=token, facts=facts_row,
                links=links, thesis=thesis.model_dump(), approved=ok, ticket_usd=ticket,
                reason=reason, shadow=shadow)
        if not ok:
            bench.sit(token["mint"], thesis.verdict if thesis.verdict != "valid_setup" else "score")
            log_reject("risk" if thesis.verdict == "valid_setup" else "analyst", reason,
                       mint=token["mint"], symbol=token["symbol"], shadow=shadow)
            stats["analyst" if thesis.verdict != "valid_setup" else "risk"] += 1
            continue

        flags = "; ".join(thesis.red_flags[:2]) or "none"
        top10 = facts_row.get("top_10_pct")
        top10_txt = f"{top10:.1%}" if isinstance(top10, (int, float)) else "n/a"
        alert(
            f"VALID SETUP ${token['symbol']} score {thesis.score}/10 | ticket ${ticket}\n"
            f"mcap ${token['mcap_usd']:,.0f} liq ${token['liquidity_usd']:,.0f} "
            f"top10 {top10_txt} (sample, not a holder audit)\n"
            f"red flags: {flags}\ninvalidation: {thesis.invalidation}\n"
            f"https://pump.fun/coin/{token['mint']}\n"
            f"https://dexscreener.com/solana/{token['mint']}"
        )
        if book is not None:
            fill = book.buy(token["mint"], token["symbol"], ticket, token["price_usd"],
                            token.get("liquidity_usd"))
            if not fill.get("filled"):
                log_reject("risk", fill.get("reason", "entry rejected"),
                           mint=token["mint"], symbol=token["symbol"], shadow=False)
                stats["risk"] += 1
                continue
            journal("entry", **fill)
        stats["entries"] += 1

    equity = None if book is None else book.equity(now=now)
    day_pnl = None if book is None else book.day_pnl(now=now)
    journal("cycle", **stats, equity=equity, day_pnl=day_pnl)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    load_env()
    broker = open_broker(shadow=args.shadow)
    bench, session = Bench(), requests.Session()
    while True:
        if killed():
            if args.shadow:
                message = ("KILL file found. Shadow mode does not manage or close paper holdings. "
                           "Desk stopped.")
                alert(message)
                journal("kill", shadow=True, entries_stopped=True, managed_paper_ledger=False,
                        message=message)
                return
            quotes = {}
            for mint in list(broker.s["positions"]):
                try:
                    quotes[mint] = scanner.quote(mint, session)
                except Exception as exc:
                    journal("error", stage="quote", mint=mint, error=str(exc))
                    quotes[mint] = None
            report = stop_for_kill(broker, quotes)
            message = kill_message(report)
            alert(message)
            journal("kill", entries_stopped=True, all_sold=report["all_sold"],
                    still_open=report["still_open"], note=report["note"], message=message,
                    assumptions=EXECUTION_ASSUMPTIONS)
            return
        try:
            stats = cycle(broker, bench, session, args.shadow)
            equity = "shadow" if args.shadow else f"${broker.equity()}"
            print(f"cycle {stats} equity {equity}")
        except Exception as exc:
            journal("error", stage="cycle", error=str(exc))
            print(f"cycle failed: {exc}")
        if args.once:
            return
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
