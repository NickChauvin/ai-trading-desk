"""Sunday review: the journal tells you the truth, then Claude says it bluntly.

    python review.py              stats only, no API call
    python review.py --claude     stats + a blunt written review (cheaper model)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desk.analyst import _call
from desk.budget import ModelBudget, PaidCallsDisabled
from desk.ops import load_env

REVIEW_PROMPT = """You review a trading desk's week from its journal stats.
Be blunt. No encouragement. Use the numbers given, invent none.
1. What is the desk actually rejecting most, and is that filter too tight or too loose?
2. Win rate and P&L on closed paper trades, and what the losers had in common.
3. One rule to add and one rule to cut, each justified with a number from the stats."""


def load(days: int = 7) -> list[dict]:
    p = Path("journal.jsonl")
    if not p.exists():
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return [r for r in rows if datetime.fromisoformat(r["time"]) >= since]


def stats(rows: list[dict]) -> dict:
    decisions = [r for r in rows if r["kind"] == "decision"]
    exits = [r for r in rows if r["kind"] == "exit" and "pnl_usd" in r]
    pnl = [r["pnl_usd"] for r in exits]
    return {
        "cycles": sum(1 for r in rows if r["kind"] == "cycle"),
        "chain_rejects": Counter(r["reason"] for r in rows
                                 if r["kind"] == "reject" and r.get("stage") == "chain"),
        "analyst_verdicts": Counter(r["thesis"]["verdict"] for r in decisions),
        "risk_rejects": Counter(r["reason"] for r in decisions if not r["approved"]),
        "approved": sum(1 for r in decisions if r["approved"]),
        "closed_trades": len(pnl),
        "wins": sum(1 for x in pnl if x > 0),
        "total_pnl_usd": round(sum(pnl), 2),
        "worst_trades": sorted(exits, key=lambda r: r["pnl_usd"])[:3],
        "exit_reasons": Counter(r["reason"] for r in exits),
        "errors": Counter(r.get("stage") for r in rows if r["kind"] == "error"),
    }


def claude_review(rows: dict, budget: ModelBudget | None = None, client=None) -> str:
    """Paid written review. Disabled until model, prices, and a budget are set.

    The same worst-case reservation covers this call. Prices are whatever you
    configured; they are not looked up and not assumed to match a cheaper model.
    """
    budget = budget or ModelBudget.from_config()
    response = _call(
        budget, client, purpose="weekly_review", system=REVIEW_PROMPT,
        content=json.dumps(rows, default=str), output_format=None,
    )
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    summary = stats(load(args.days))
    print(json.dumps(summary, indent=1, default=str))
    if args.claude:
        load_env()
        try:
            print("\n" + claude_review(summary))
        except PaidCallsDisabled as exc:
            print(f"\npaid review disabled: {exc}")


if __name__ == "__main__":
    main()
