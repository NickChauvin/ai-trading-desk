"""Seat 5a: paper execution only.

There is deliberately no live Solana execution in this repo: no private key,
no signing, no seed phrase anywhere. Fills below are a model with explicit
assumptions. A missing quote is a failed exit, never a sale.
"""
from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import fcntl

from config import DESK_TIMEZONE, EXITS, PAPER, RISK
from desk.persist import atomic_write

LEDGER = Path("paper_ledger.json")

EXECUTION_ASSUMPTIONS = (
    "Paper fills are a model, not a market. "
    "A missing or unexecutable quote never books proceeds. "
    "The holding stays open, cash and realized P&L stay unchanged, and the "
    "conservative mark is zero recovery, shown separately from realized loss. "
    "Entries are all-or-nothing and must fit in cash, the aggregate open-risk "
    "limit, and PAPER['max_liquidity_fraction'] of quoted liquidity. "
    "Exits assume PAPER['slippage_pct'] against the trader and PAPER['fee_pct'] "
    "of notional, and may fill only up to that liquidity fraction. "
    "The unfilled remainder stays open. None of these numbers is an observed execution."
)


def executable_quote(q) -> tuple[bool, str]:
    if not isinstance(q, dict):
        return False, "missing quote"
    try:
        price = float(q.get("price_usd"))
        liq = float(q.get("liquidity_usd"))
    except (TypeError, ValueError):
        return False, "unexecutable quote"
    if not math.isfinite(price) or not math.isfinite(liq):
        return False, "unexecutable quote"
    if price <= 0:
        return False, "unexecutable quote: price missing or non-positive"
    if liq <= 0:
        return False, "unexecutable quote: no liquidity"
    # Only a timestamp the quote itself carries can be checked. Absence is not
    # turned into "now"; callers must treat freshness as unknown.
    if "quoted_at" in q and q.get("quoted_at") is not None:
        try:
            quoted_at = float(q["quoted_at"])
        except (TypeError, ValueError):
            return False, "unexecutable quote: bad timestamp"
        if not math.isfinite(quoted_at):
            return False, "unexecutable quote: bad timestamp"
        age = time.time() - quoted_at
        if age > float(PAPER["max_quote_age_seconds"]) or age < -30:
            return False, "stale quote"
    return True, "executable"


class PaperBroker:
    def __init__(self, path: Path | None = LEDGER, *, now=None, tz: str | None = None,
                 isolated: bool = False):
        self.tz_name = tz or RISK.get("timezone") or DESK_TIMEZONE
        self.now = now or (lambda: datetime.now(ZoneInfo(self.tz_name)))
        self.isolated = isolated or path is None
        self.path = None if self.isolated else Path(path)
        self._quotes_refreshed = False
        self._held = 0
        self._mtime = None
        if self.path is not None and self.path.exists():
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = self.path.stat().st_mtime_ns
        else:
            self.s = self._fresh()
        self._normalize()
        self.roll()

    def _fresh(self) -> dict:
        eq = float(PAPER["starting_equity_usd"])
        return {
            "cash": eq,
            "positions": {},
            "day": self._today(),
            "day_start": eq,
            "closed": [],
            "realized_pnl_usd": 0.0,
            "timezone": self.tz_name,
        }

    def _normalize(self) -> None:
        self.s.setdefault("positions", {})
        self.s.setdefault("closed", [])
        self.s.setdefault("cash", 0.0)
        self.s.setdefault(
            "realized_pnl_usd",
            round(sum(c.get("pnl_usd", 0.0) for c in self.s["closed"]), 2),
        )
        self.s.setdefault("timezone", self.tz_name)
        self.s.setdefault("day", self._today())
        self.s.setdefault("day_start", round(float(self.s["cash"]), 2))
        for pos in self.s["positions"].values():
            pos.setdefault("proceeds", 0.0)
            pos.setdefault("realized", 0.0)
            pos.setdefault("unresolved", False)
            pos.setdefault("unresolved_reason", None)
            pos.setdefault("half_taken", False)
            pos.setdefault("cost", 0.0)
            pos.setdefault("qty", 0.0)
            pos.setdefault("last", 0.0)

    def _tz(self) -> ZoneInfo:
        name = self.tz_name
        state = getattr(self, "s", None)
        if state:
            name = state.get("timezone") or name
        return ZoneInfo(name)

    def _clock(self, now=None) -> datetime:
        current = self.now() if now is None else now
        if current.tzinfo is None:
            return current.replace(tzinfo=self._tz())
        return current.astimezone(self._tz())

    def _today(self, now=None) -> str:
        return self._clock(now).date().isoformat()

    @contextmanager
    def _locked(self):
        """Exclusive ledger lock. A second process reloads the file before it mutates.

        The lock is not held across network calls. Nested calls in this process
        reuse it so a save inside buy cannot deadlock.
        """
        if self.path is None:
            yield
            return
        if self._held:
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
            return
        handle = open(self.path.with_name(self.path.name + ".lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._held = 1
        try:
            self._refresh_from_disk()
            yield
        finally:
            self._held = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _refresh_from_disk(self) -> None:
        if self.path is None or not self.path.exists():
            return
        current = self.path.stat().st_mtime_ns
        if current == self._mtime:
            return
        self.s = json.loads(self.path.read_text(encoding="utf-8"))
        self._normalize()
        self._mtime = current

    def _write(self) -> None:
        atomic_write(self.path, json.dumps(self.s, indent=1))
        self._mtime = self.path.stat().st_mtime_ns

    def _save(self) -> None:
        if self.path is None:
            return
        if self._held:
            self._write()
            return
        with self._locked():
            self._write()

    def note_quotes_refreshed(self) -> None:
        """This process has attempted a fresh quote for every open position.

        The flag lives in memory. A restart cannot use it to roll the daily
        baseline off a stale last price.
        """
        self._quotes_refreshed = True

    def roll(self, now=None) -> bool:
        """Roll the daily baseline when the desk date changes.

        Open positions block the roll until this process has refreshed quotes.
        The new baseline is conservative equity after that refresh (unresolved
        holdings marked at zero). Same-day restarts keep the stored baseline.
        Returns True when the baseline moved.
        """
        with self._locked():
            return self._roll(now)

    def _roll(self, now=None) -> bool:
        today = self._today(now)
        if self.s.get("day") == today:
            return False
        if self.s["positions"] and not self._quotes_refreshed:
            return False
        self.s["day"] = today
        self.s["day_start"] = self._equity_now()
        self.s["timezone"] = self.tz_name
        self._save()
        return True

    def _mark_price(self, mint: str, pos: dict, marks: dict | None) -> float:
        if pos.get("unresolved"):
            return 0.0
        raw = (marks or {}).get(mint, pos.get("last") or 0)
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(price) or price <= 0:
            return 0.0
        return price

    def _equity_now(self, marks: dict | None = None) -> float:
        held = 0.0
        for mint, pos in self.s["positions"].items():
            held += pos.get("qty", 0.0) * self._mark_price(mint, pos, marks)
        return round(float(self.s["cash"]) + held, 2)

    def equity(self, marks: dict | None = None, now=None) -> float:
        self.roll(now)
        return self._equity_now(marks)

    def day_pnl(self, now=None) -> float:
        self.roll(now)
        return round(self._equity_now() - float(self.s["day_start"]), 2)

    def realized_pnl(self) -> float:
        return round(float(self.s.get("realized_pnl_usd") or 0.0), 2)

    def open_count(self) -> int:
        return len(self.s["positions"])

    def open_risk_usd(self) -> float:
        """Modeled loss if stops filled, or the full remaining cost when a stop cannot be sent.

        This is not a guarantee. A quote can vanish and the position can go to zero.
        """
        total = 0.0
        for pos in self.s["positions"].values():
            cost = float(pos.get("cost") or 0.0)
            if pos.get("unresolved"):
                total += cost
            else:
                total += cost * EXITS["stop_pct"]
        return total

    def exposure(self) -> dict:
        unresolved = []
        for mint, pos in self.s["positions"].items():
            if not pos.get("unresolved"):
                continue
            cost = round(float(pos.get("cost") or 0.0), 2)
            unresolved.append({
                "mint": mint,
                "symbol": pos.get("symbol"),
                "cost_usd": cost,
                "zero_recovery_mark_usd": 0.0,
                "loss_risk_usd": cost,
                "reason": pos.get("unresolved_reason"),
            })
        loss = round(sum(row["loss_risk_usd"] for row in unresolved), 2)
        return {
            "cash": round(float(self.s["cash"]), 2),
            "realized_pnl_usd": self.realized_pnl(),
            "conservative_equity_usd": self._equity_now(),
            "day": self.s.get("day"),
            "day_start": self.s.get("day_start"),
            "timezone": self.s.get("timezone"),
            "unresolved": unresolved,
            "loss_risk_usd": loss,
            "assumptions": EXECUTION_ASSUMPTIONS,
        }

    # ---------- orders ----------
    def buy(self, mint: str, symbol: str, ticket_usd: float, price: float,
            liquidity_usd: float | None = None) -> dict:
        """All-or-nothing simulated entry. Rejects without mutating state."""
        with self._locked():
            return self._buy(mint, symbol, ticket_usd, price, liquidity_usd)

    def _buy(self, mint: str, symbol: str, ticket_usd: float, price: float,
             liquidity_usd: float | None) -> dict:
        reject = self._reject_buy(ticket_usd, price, liquidity_usd)
        if reject:
            return {"filled": False, "status": "rejected", "side": "buy", "mint": mint,
                    "symbol": symbol, "reason": reject, "proceeds_usd": 0.0}
        fill = price * (1 + PAPER["slippage_pct"])
        fee = ticket_usd * PAPER["fee_pct"]
        qty = (ticket_usd - fee) / fill
        self.s["cash"] -= ticket_usd
        self.s["positions"][mint] = {
            "symbol": symbol, "qty": qty, "entry": fill, "last": fill,
            "opened": time.time(), "cost": ticket_usd, "half_taken": False,
            "proceeds": 0.0, "realized": 0.0, "unresolved": False,
            "unresolved_reason": None,
        }
        self._save()
        return {"filled": True, "status": "filled", "side": "buy", "mint": mint,
                "symbol": symbol, "qty": qty, "fill": fill, "fee": fee,
                "execution": "simulated entry with configured slippage and fee"}

    def _reject_buy(self, ticket_usd: float, price: float, liquidity_usd) -> str | None:
        try:
            price_f = float(price)
            ticket = float(ticket_usd)
            liq = float(liquidity_usd)
        except (TypeError, ValueError):
            return "unexecutable quote"
        if not math.isfinite(price_f) or not math.isfinite(liq) or price_f <= 0 or liq <= 0:
            return "unexecutable quote"
        cap = liq * PAPER["max_liquidity_fraction"]
        if ticket > cap + 1e-9:
            return "ticket exceeds simulated liquidity cap"
        if ticket > float(self.s["cash"]) + 1e-9:
            return "insufficient cash"
        equity = max(self._equity_now(), 0.0)
        new_risk = ticket * EXITS["stop_pct"]
        limit = RISK["max_aggregate_open_risk_pct"] * equity
        if self.open_risk_usd() + new_risk > limit + 1e-6:
            return "aggregate open-risk limit"
        return None

    def exit_signal(self, mint: str, q: dict | None, now: float | None = None):
        """Return (fraction, reason) or None. A missing quote is not a sell signal."""
        with self._locked():
            return self._exit_signal(mint, q, now)

    def _exit_signal(self, mint: str, q: dict | None, now: float | None = None):
        if mint not in self.s["positions"]:
            return None
        ok, why = executable_quote(q)
        if not ok:
            pos = self.s["positions"][mint]
            pos["unresolved"] = True
            pos["unresolved_reason"] = why
            self._save()
            return None
        signal = self._rules(mint, q, now)
        self._save()
        return signal

    def attempt_exit(self, mint: str, quote, now: float | None = None, *,
                     force: bool = False, reason: str | None = None) -> dict:
        """Try a simulated exit. Never fills at the stored last price."""
        with self._locked():
            return self._attempt_exit(mint, quote, now, force=force, reason=reason)

    def _attempt_exit(self, mint: str, quote, now, *, force: bool, reason: str | None) -> dict:
        if mint not in self.s["positions"]:
            return {"status": "failed", "filled": False, "mint": mint, "reason": "no position",
                    "proceeds_usd": 0.0, "unresolved": False}
        pos = self.s["positions"][mint]
        ok, why = executable_quote(quote)
        if not ok:
            pos["unresolved"] = True
            pos["unresolved_reason"] = why
            self._save()
            return self._failed(pos, mint, why)
        if force:
            return self._fill(mint, 1.0, quote, reason or "forced exit")
        before = pos["half_taken"]
        signal = self._rules(mint, quote, now)
        if signal is None:
            self._save()
            return {"status": "open", "filled": False, "mint": mint, "symbol": pos["symbol"],
                    "reason": "no exit rule", "proceeds_usd": 0.0, "unresolved": False,
                    "loss_risk_usd": round(float(pos["cost"]) * EXITS["stop_pct"], 2),
                    "cash": round(float(self.s["cash"]), 2)}
        fraction, exit_reason = signal
        flagged = pos["half_taken"] and not before
        result = self._fill(mint, fraction, quote, exit_reason)
        if flagged and result["status"] == "partial" and mint in self.s["positions"]:
            self.s["positions"][mint]["half_taken"] = False
            self._save()
        return result

    def _failed(self, pos: dict, mint: str, why: str) -> dict:
        return {
            "status": "failed",
            "filled": False,
            "side": "sell",
            "mint": mint,
            "symbol": pos.get("symbol"),
            "reason": why,
            "proceeds_usd": 0.0,
            "pnl_usd": None,
            "unresolved": True,
            "loss_risk_usd": round(float(pos.get("cost") or 0.0), 2),
            "zero_recovery_mark_usd": 0.0,
            "cash": round(float(self.s["cash"]), 2),
            "execution": "unexecutable quote; no proceeds booked; zero-recovery mark is not a fill",
        }

    def _rules(self, mint, quote, now):
        pos = self.s["positions"][mint]
        now = now or time.time()
        held_min = (now - pos["opened"]) / 60
        price = float(quote["price_usd"])
        pos["last"] = price
        pos["unresolved"] = False
        pos["unresolved_reason"] = None
        if price <= pos["entry"] * (1 - EXITS["stop_pct"]):
            return (1.0, f"stop -{int(EXITS['stop_pct'] * 100)}%")
        if not pos["half_taken"] and price >= pos["entry"] * EXITS["take_half_at"]:
            pos["half_taken"] = True
            return (0.5, f"take half at {EXITS['take_half_at']}x")
        volume_h24 = float(quote.get("volume_h24") or 0)
        if held_min >= EXITS["volume_decay_min_minutes"] and volume_h24 > 0:
            ratio = float(quote.get("volume_h6") or 0) / (volume_h24 / 4)
            if ratio < EXITS["volume_decay_ratio"]:
                return (1.0, f"volume decay {ratio:.2f}")
        if held_min >= EXITS["max_hold_hours"] * 60:
            return (1.0, "max hold time")
        return None

    def _fill(self, mint: str, fraction: float, quote: dict, reason: str) -> dict:
        pos = self.s["positions"][mint]
        price = float(quote["price_usd"])
        liq = float(quote["liquidity_usd"])
        fill_price = price * (1 - PAPER["slippage_pct"])
        cap = liq * PAPER["max_liquidity_fraction"]
        qty_before = float(pos["qty"])
        requested = qty_before * min(max(float(fraction), 0.0), 1.0)
        max_qty = cap / fill_price if fill_price > 0 else 0.0
        qty = min(requested, max_qty)
        if qty <= 1e-12:
            pos["unresolved"] = True
            pos["unresolved_reason"] = "liquidity cap is below a fill"
            self._save()
            return self._failed(pos, mint, pos["unresolved_reason"])
        gross = qty * fill_price
        fee = gross * PAPER["fee_pct"]
        proceeds = gross - fee
        cost_alloc = float(pos["cost"]) * (qty / qty_before) if qty_before else 0.0
        self.s["cash"] += proceeds
        pos["qty"] -= qty
        pos["cost"] -= cost_alloc
        pos["proceeds"] = float(pos.get("proceeds") or 0.0) + proceeds
        realized = proceeds - cost_alloc
        pos["realized"] = float(pos.get("realized") or 0.0) + realized
        self.s["realized_pnl_usd"] = float(self.s.get("realized_pnl_usd") or 0.0) + realized
        pos["last"] = price
        pos["unresolved"] = False
        pos["unresolved_reason"] = None
        partial = requested - qty > 1e-8
        out = {
            "status": "partial" if partial else "filled",
            "filled": True,
            "closed": False,
            "side": "sell",
            "mint": mint,
            "symbol": pos["symbol"],
            "qty": qty,
            "fill": fill_price,
            "fee": fee,
            "reason": reason,
            "proceeds_usd": proceeds,
            "unresolved": False,
            "cash": round(float(self.s["cash"]), 2),
            "quote_freshness": "checked" if quote.get("quoted_at") is not None else "unknown",
            "execution": "simulated fill at quoted price with configured slippage, fee, and liquidity cap",
        }
        if (not partial and fraction >= 1) or pos["qty"] <= 1e-12:
            pnl = round(float(pos["realized"]), 2)
            out["pnl_usd"] = pnl
            out["closed"] = True
            out["status"] = "filled"
            self.s["closed"].append({
                "mint": mint, "symbol": pos["symbol"], "pnl_usd": pnl,
                "reason": reason, "closed": time.time(),
            })
            del self.s["positions"][mint]
        else:
            out["remainder_qty"] = pos["qty"]
            out["remainder_cost_usd"] = pos["cost"]
        self._save()
        return out
