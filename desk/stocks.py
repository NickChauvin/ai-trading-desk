"""Paper-only guard for the stock seat.

There is no switch that turns this into a live broker. A client that is not
explicitly paper, or whose base URL is not Alpaca's paper host, is refused
before any order method is called.
"""
from __future__ import annotations

from desk.risk import check_stock_trade, precheck_stock

PAPER_HOST = "paper-api.alpaca.markets"


class PaperOnlyError(RuntimeError):
    pass


def assert_paper_only(trading) -> None:
    paper = getattr(trading, "_paper", None)
    if paper is None:
        paper = getattr(trading, "paper", None)
    base = str(getattr(trading, "_base_url", "") or getattr(trading, "base_url", "") or "")
    if paper is not True:
        raise PaperOnlyError("stock trading is paper-only; refusing a live or unknown client")
    if base and PAPER_HOST not in base:
        raise PaperOnlyError("stock client base URL is not Alpaca paper; refusing")
    if "api.alpaca.markets" in base and PAPER_HOST not in base:
        raise PaperOnlyError("stock client points at Alpaca live; refusing")


def build_clients(trading_client, key: str, secret: str):
    """Construct the trading client with paper=True and refuse anything else."""
    trading = trading_client(key, secret, paper=True)
    assert_paper_only(trading)
    return trading


def gated_submit(trading, submit):
    assert_paper_only(trading)
    return submit()


def consider_symbol(account, symbol: str, price: float, thesis_fn, submit, trading, now=None) -> dict:
    """Run free checks before the thesis function. Orders go through the paper guard."""
    reason = precheck_stock(account, price, now=now)
    if reason:
        return {"symbol": symbol, "approved": False, "ai_called": False, "reason": reason,
                "qty": 0, "order_id": None}
    thesis = thesis_fn()
    ok, qty, reason = check_stock_trade(thesis, account, price, now=now)
    order_id = None
    if ok:
        order_id = gated_submit(trading, lambda: submit(thesis, qty))
    return {"symbol": symbol, "approved": ok, "ai_called": True, "reason": reason,
            "qty": qty if ok else 0, "order_id": order_id, "thesis": thesis}


def kill_message(sent: bool) -> str:
    if sent:
        return ("KILL file found. Paper flatten was requested on the Alpaca paper endpoint. "
                "Desk stopped. A request is not proof that every fill succeeded.")
    return "KILL file found. No stock order was sent. Desk stopped."


def flatten_stocks(trading):
    assert_paper_only(trading)
    return trading.close_all_positions(cancel_orders=True)
