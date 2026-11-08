"""Seat 4: the risk engine. Plain Python. No model touches this file. Ever.

The daily-loss check blocks new entries. It is not a guaranteed maximum loss:
open positions can gap through the stop, a quote can vanish, and the counter
resets on the next desk day after marks are refreshed. Aggregate open risk
counts unresolved holdings at their full remaining cost.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DESK_TIMEZONE, EXITS, MIN_SCORE, RISK


@dataclass
class Account:
    equity: float
    day_pnl: float
    open_positions: int
    cash: float = 0.0
    open_risk_usd: float = 0.0
    day_start: float | None = None


def _in_zone(now: datetime | None) -> datetime:
    tz = ZoneInfo(RISK.get("timezone") or DESK_TIMEZONE)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def in_quiet_hours(now: datetime | None = None) -> bool:
    start, end = RISK["quiet_hours"]
    hour = _in_zone(now).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def sized_ticket(equity: float, liquidity_usd: float) -> float:
    if equity <= 0 or liquidity_usd <= 0:
        return 0.0
    by_risk = RISK["max_risk_per_trade"] * equity / EXITS["stop_pct"]
    by_size = RISK["max_position_pct"] * equity
    by_pool = RISK["max_pool_share"] * liquidity_usd
    return round(min(by_risk, by_size, by_pool), 2)


def _baseline(account: Account) -> float | None:
    if account.day_start is None:
        return None
    return float(account.day_start)


def precheck_token(account: Account, liquidity_usd: float, now: datetime | None = None) -> str | None:
    """Free checks that do not need a model thesis. None means they passed."""
    baseline = _baseline(account)
    if baseline is None:
        return "daily loss baseline missing"
    if account.equity <= 0 or baseline <= 0:
        return "no equity"
    # Half a cent of slack so a ledger rounded to cents cannot sneak past the printed limit.
    if account.day_pnl <= -RISK["max_daily_loss_pct"] * baseline + 0.005:
        return ("daily loss limit hit; new entries stopped until the next desk day. "
                "This is not a guaranteed max loss")
    if in_quiet_hours(now):
        return "quiet hours, no new entries"
    if account.open_positions >= RISK["max_open_positions"]:
        return "max open positions"
    ticket = sized_ticket(account.equity, liquidity_usd)
    if ticket < RISK["min_ticket_usd"]:
        return f"ticket ${ticket} below fee floor ${RISK['min_ticket_usd']}"
    if ticket > account.cash + 1e-9:
        return "insufficient cash"
    new_risk = ticket * EXITS["stop_pct"]
    limit = RISK["max_aggregate_open_risk_pct"] * account.equity
    if account.open_risk_usd + new_risk > limit + 1e-6:
        return "aggregate open-risk limit"
    return None


def check_token_trade(thesis, account: Account, liquidity_usd: float,
                       now: datetime | None = None) -> tuple[bool, float, str]:
    """Return (approved, ticket_usd, reason)."""
    if (reason := precheck_token(account, liquidity_usd, now)):
        return False, 0.0, reason
    if thesis.verdict != "valid_setup":
        return False, 0.0, f"analyst verdict: {thesis.verdict}"
    if thesis.score < MIN_SCORE:
        return False, 0.0, f"score {thesis.score} < {MIN_SCORE}"
    ticket = sized_ticket(account.equity, liquidity_usd)
    return True, ticket, "approved"


def precheck_stock(account: Account, price: float | None = None,
                    now: datetime | None = None) -> str | None:
    """Free stock checks. Aggregate size still needs the thesis stop, so it is not here."""
    baseline = _baseline(account)
    if baseline is None:
        return "daily loss baseline missing"
    if account.equity <= 0 or baseline <= 0:
        return "no equity"
    if account.day_pnl <= -RISK["max_daily_loss_pct"] * baseline + 0.005:
        return ("daily loss limit hit; new entries stopped until the next desk day. "
                "This is not a guaranteed max loss")
    if in_quiet_hours(now):
        return "quiet hours, no new entries"
    if account.open_positions >= 5:
        return "max open positions"
    if price is not None and account.cash + 1e-9 < price:
        return "insufficient cash"
    return None


# ---------- stocks (Alpaca paper) ----------
MAX_STOCK_POSITION_PCT = 0.05
ALLOWED_SYMBOLS = {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"}


def check_stock_trade(thesis, account: Account, price: float,
                       now: datetime | None = None) -> tuple[bool, int, str]:
    if (reason := precheck_stock(account, price, now)):
        return False, 0, reason
    if thesis.symbol.upper() not in ALLOWED_SYMBOLS:
        return False, 0, f"{thesis.symbol} not on the allowed list"
    if thesis.action.lower() == "hold":
        return False, 0, "model recommends no trade"
    if thesis.action.lower() == "sell":
        return False, 0, "long-only desk"
    if thesis.confidence.lower() != "high":   # enum casing is not guaranteed
        return False, 0, "only high-confidence theses trade"
    if thesis.stop_loss >= price or thesis.stop_loss <= 0:
        return False, 0, "stop must sit below entry for a long"
    stop_distance = price - thesis.stop_loss
    qty = int(min(RISK["max_risk_per_trade"] * account.equity / stop_distance,
                  MAX_STOCK_POSITION_PCT * account.equity / price))
    if qty < 1:
        return False, 0, "position too small after sizing"
    cost = qty * price
    if cost > account.cash + 1e-9:
        return False, 0, "insufficient cash"
    new_risk = qty * stop_distance
    limit = RISK["max_aggregate_open_risk_pct"] * account.equity
    if account.open_risk_usd + new_risk > limit + 1e-6:
        return False, 0, "aggregate open-risk limit"
    return True, qty, "approved"
