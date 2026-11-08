"""Stocks mode: same desk, US equities, Alpaca PAPER account only.

    python run_stocks.py          every 15 minutes while the market is open
    python run_stocks.py --once

There is no live-trading switch. The trading client is constructed with
paper=True and refused unless its base URL is Alpaca's paper host.
Alerts are off unless ALERTS_ENABLED is set. Model calls go through the
daily dollar budget and are disabled until that budget is configured.
Quiet hours, the daily-loss gate, and cash are checked before the model call.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

from config import STOCK_CYCLE_SECONDS
from desk.analyst import analyze_stock
from desk.budget import BudgetError, ModelBudget
from desk.ops import alert, journal, killed, load_env, log_model_cost, log_reject
from desk.risk import ALLOWED_SYMBOLS, Account
from desk.stocks import PaperOnlyError, build_clients, consider_symbol, flatten_stocks, kill_message


def clients():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.news import NewsClient
    from alpaca.trading.client import TradingClient
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    trading = build_clients(TradingClient, key, secret)
    return trading, StockHistoricalDataClient(key, secret), NewsClient(key, secret)


def inputs(symbol: str, data, news) -> tuple[str, list[str], float]:
    from alpaca.data.requests import NewsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.now(timezone.utc) - timedelta(days=5)
    bars = data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start,
    ))
    rows = bars.data.get(symbol, [])[-30:]
    market = "\n".join(
        f"{bar.timestamp:%m-%d %H:%M} o{bar.open} h{bar.high} l{bar.low} c{bar.close} v{int(bar.volume)}"
        for bar in rows
    )
    price = float(rows[-1].close) if rows else 0.0
    try:
        items = news.get_news(NewsRequest(symbols=symbol, start=start, limit=10)).data.get("news", [])
        headlines = [f"{item.created_at:%m-%d %H:%M} {item.headline}. {item.summary or ''}" for item in items]
    except Exception as exc:
        headlines = []
        journal("error", stage="news", symbol=symbol, error=str(exc))
    return market, headlines, price


def execute(trading, thesis, qty: int):
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
    from desk.stocks import gated_submit
    def _send():
        order = MarketOrderRequest(
            symbol=thesis.symbol.upper(), qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=round(thesis.stop_loss, 2)),
        )
        return trading.submit_order(order_data=order)
    return gated_submit(trading, _send)


def cycle(trading, data, news, *, now=None, budget: ModelBudget | None = None):
    if not trading.get_clock().is_open:
        return
    acct = trading.get_account()
    positions = trading.get_all_positions()
    cash = float(getattr(acct, "cash", None) or acct.equity)
    # Existing positions rarely tell us their stop. Count full market value so
    # unknown open risk fails closed instead of looking like zero.
    open_risk = 0.0
    for pos in positions:
        open_risk += abs(float(getattr(pos, "market_value", 0) or 0))
    account = Account(
        equity=float(acct.equity),
        day_pnl=float(acct.equity) - float(acct.last_equity),
        open_positions=len(positions),
        cash=cash,
        open_risk_usd=open_risk,
        day_start=float(acct.last_equity),
    )
    from desk.risk import precheck_stock
    early = precheck_stock(account, price=None, now=now)
    if early:
        log_reject("risk", early, symbol="*")
        return
    model_budget = budget or ModelBudget.from_config(on_event=log_model_cost)
    for symbol in sorted(ALLOWED_SYMBOLS):
        market, headlines, price = inputs(symbol, data, news)
        if not price:
            continue

        def thesis_fn(symbol=symbol, market=market, headlines=headlines):
            return analyze_stock(symbol, market, headlines, budget=model_budget)

        def submit(thesis, qty, _trading=trading):
            order = execute(_trading, thesis, qty)
            return str(order.id)

        try:
            result = consider_symbol(account, symbol, price, thesis_fn, submit, trading, now=now)
        except BudgetError as exc:
            log_reject("budget", str(exc), symbol=symbol)
            break
        except PaperOnlyError as exc:
            journal("error", stage="stock_order", symbol=symbol, error=str(exc))
            alert(f"STOCK ORDER REFUSED {symbol}: {exc}")
            break
        if result.get("approved"):
            account.open_positions += 1
            account.cash -= result["qty"] * price
            account.open_risk_usd += result["qty"] * (price - result["thesis"].stop_loss)
            alert(
                f"PAPER BUY {result['qty']} {symbol} @~{price} "
                f"stop {result['thesis'].stop_loss} | {result['thesis'].entry_reason}"
            )
        elif not result.get("ai_called"):
            log_reject("risk", result["reason"], symbol=symbol)
        journal("stock_decision", thesis=result["thesis"].model_dump() if result.get("thesis") else None,
                price=price, approved=result["approved"], qty=result.get("qty") or 0,
                reason=result["reason"], order_id=result.get("order_id"),
                ai_called=result["ai_called"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    load_env()
    trading, data, news = clients()
    while True:
        if killed():
            try:
                flatten_stocks(trading)
                alert(kill_message(sent=True))
                journal("kill", sent=True, message=kill_message(sent=True))
            except PaperOnlyError as exc:
                alert(kill_message(sent=False))
                journal("kill", sent=False, error=str(exc), message=kill_message(sent=False))
            return
        try:
            cycle(trading, data, news)
        except Exception as exc:
            journal("error", stage="stock_cycle", error=str(exc))
            print(f"cycle failed: {exc}")
        if args.once:
            return
        time.sleep(STOCK_CYCLE_SECONDS)


if __name__ == "__main__":
    main()
