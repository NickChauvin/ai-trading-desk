"""Hard daily dollar budget for model calls.

A reservation is written and flushed before the caller may send the request.
The reserved amount is the configured worst case: input cap + output cap,
times one plus the billable retries. Failures are not refunded. A retry that
might have been billed is already inside that worst case.

Nothing in this module knows which models exist or what they cost today.
Prices come only from explicit config. The default config disables calls.
"""
from __future__ import annotations

import fcntl
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import MODEL
from desk.persist import atomic_write

BUDGET_PATH = Path("model_budget.json")


class BudgetError(RuntimeError):
    pass


class PaidCallsDisabled(BudgetError):
    pass


class BudgetExhausted(BudgetError):
    pass


def _micro(usd: float) -> int:
    return round(float(usd) * 1_000_000)


def worst_case_micro(cfg: dict) -> int:
    """Conservative reservation in microdollars (1e-6 USD).

    usd = tokens / 1e6 * price_per_million, so microdollars = tokens * price.
    """
    retries = int(cfg.get("max_billable_retries") or 0)
    calls = 1 + retries
    per_call = (
        int(cfg["max_input_tokens"]) * float(cfg["price_input_usd_per_million"])
        + int(cfg["max_output_tokens"]) * float(cfg["price_output_usd_per_million"])
    )
    return round(calls * per_call)


def config_problem(cfg: dict) -> str | None:
    if not cfg.get("enabled"):
        return "paid model calls are disabled"
    if not str(cfg.get("model") or "").strip():
        return "model id is empty; this desk does not pick one for you"
    for key in ("price_input_usd_per_million", "price_output_usd_per_million", "daily_budget_usd"):
        if cfg.get(key) is None:
            return f"{key} is unset; this desk does not invent prices or a budget"
        try:
            if float(cfg[key]) <= 0:
                return f"{key} must be a positive number you set from provider docs"
        except (TypeError, ValueError):
            return f"{key} is not a number"
    try:
        if int(cfg.get("max_output_tokens") or 0) < 1:
            return "max_output_tokens must be at least 1"
        if int(cfg.get("max_input_tokens") or 0) < 1:
            return "max_input_tokens must be at least 1"
        if int(cfg.get("max_billable_retries") or 0) < 0:
            return "max_billable_retries cannot be negative"
    except (TypeError, ValueError):
        return "token caps must be integers"
    return None


class ModelBudget:
    def __init__(self, path: Path | str = BUDGET_PATH, cfg: dict | None = None,
                 now=None, on_event=None):
        self.path = Path(path)
        self.cfg = dict(MODEL if cfg is None else cfg)
        self.now = now or (lambda: datetime.now(ZoneInfo(self.cfg.get("timezone") or "UTC")))
        self.on_event = on_event

    @classmethod
    def from_config(cls, path: Path | str = BUDGET_PATH, now=None, on_event=None) -> "ModelBudget":
        return cls(path, cfg=dict(MODEL), now=now, on_event=on_event)

    @property
    def model(self) -> str:
        review = str(self.cfg.get("review_model") or "").strip()
        return str(self.cfg.get("model") or "").strip() or review

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("timezone") or "UTC")

    def _today(self) -> str:
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._tz())
        return now.astimezone(self._tz()).date().isoformat()

    def _emit(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path.with_name(self.path.name + ".lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _load(self) -> dict:
        if not self.path.exists():
            return {"timezone": self.cfg.get("timezone") or "UTC", "day": self._today(),
                    "spent_micro": 0, "attempts": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _roll(self, state: dict) -> None:
        today = self._today()
        if state.get("day") != today:
            state["yesterday"] = {"day": state.get("day"), "spent_micro": state.get("spent_micro", 0)}
            state["day"] = today
            state["spent_micro"] = 0
            state["attempts"] = []
        state["timezone"] = self.cfg.get("timezone") or "UTC"

    def _save(self, state: dict) -> None:
        attempts = state.get("attempts") or []
        if len(attempts) > 400:
            state["attempts"] = attempts[-400:]
        atomic_write(self.path, json.dumps(state, indent=1))

    def reserve(self, purpose: str) -> dict:
        """Durably reserve worst-case cost. Raises before the caller may hit the network.

        The lock is held across the read-modify-flush so two processes cannot
        both pass the check. The reservation stays spent if the process dies
        before complete() — the provider may already have billed.
        """
        problem = config_problem(self.cfg)
        if problem:
            event = {"status": "disabled", "purpose": purpose, "reason": problem,
                     "usd": 0.0, "micro": 0, "spent_usd": 0.0, "model": self.cfg.get("model") or ""}
            self._emit(event)
            raise PaidCallsDisabled(problem)

        cost = worst_case_micro(self.cfg)
        budget = _micro(self.cfg["daily_budget_usd"])
        lock = self._lock()
        try:
            state = self._load()
            self._roll(state)
            spent = int(state.get("spent_micro") or 0)
            if spent + cost > budget:
                event = {"status": "refused", "purpose": purpose, "reason": "daily model budget exhausted",
                         "usd": cost / 1_000_000, "micro": cost, "spent_usd": spent / 1_000_000,
                         "spent_micro": spent, "model": self.cfg.get("model")}
                state.setdefault("attempts", []).append({**event, "at": self._today()})
                self._save(state)
                self._emit(event)
                raise BudgetExhausted("daily model budget exhausted")
            rec = {
                "id": uuid.uuid4().hex,
                "status": "reserved",
                "purpose": purpose,
                "micro": cost,
                "usd": cost / 1_000_000,
                "model": self.cfg.get("model"),
                "max_output_tokens": int(self.cfg["max_output_tokens"]),
                "max_input_tokens": int(self.cfg["max_input_tokens"]),
                "max_billable_retries": int(self.cfg.get("max_billable_retries") or 0),
                "at": self._today(),
            }
            state["spent_micro"] = spent + cost
            state.setdefault("attempts", []).append(rec)
            self._save(state)  # flushed before the caller is allowed to send
            event = {"status": "reserved", "purpose": purpose, "id": rec["id"],
                     "usd": rec["usd"], "micro": cost, "spent_usd": state["spent_micro"] / 1_000_000,
                     "spent_micro": state["spent_micro"], "model": rec["model"]}
            self._emit(event)
            return rec
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def complete(self, reservation: dict, *, ok: bool, error: str | None = None, attempts: int = 1) -> None:
        """Annotate the reservation. Does not refund it."""
        lock = self._lock()
        try:
            state = self._load()
            for row in state.get("attempts", []):
                if row.get("id") == reservation.get("id"):
                    row["status"] = "committed" if ok else "failed"
                    row["ok"] = ok
                    row["error"] = error
                    row["attempts"] = attempts
                    break
            self._save(state)
            spent = int(state.get("spent_micro") or 0)
            self._emit({
                "status": "committed" if ok else "failed",
                "purpose": reservation.get("purpose"),
                "id": reservation.get("id"),
                "usd": reservation.get("usd"),
                "micro": reservation.get("micro"),
                "spent_usd": spent / 1_000_000,
                "spent_micro": spent,
                "model": reservation.get("model"),
                "ok": ok,
                "error": error,
                "attempts": attempts,
            })
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def spent_micro(self) -> int:
        if not self.path.exists():
            return 0
        lock = self._lock()
        try:
            state = self._load()
            self._roll(state)
            return int(state.get("spent_micro") or 0)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
