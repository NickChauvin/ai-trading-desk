"""One bounded, read-only look at public market data.

Two GeckoTerminal new-pool pages, at most two Solana holder samples, at most
three DexScreener quotes, and one account lookup for the known pool programs.
No model calls, no orders, no Telegram, no credentials, no retry loop.

    python scripts/bounded_public_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Public endpoints only. Ignore any keys or private RPC the environment has.
for _name in ("SOLANA_RPC_URL", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHAT_ID", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALERTS_ENABLED"):
    os.environ.pop(_name, None)

import requests  # noqa: E402

from config import KNOWN_POOL_PROGRAMS, MODEL  # noqa: E402
from desk import chain, scanner  # noqa: E402

OUT = ROOT / "docs" / "readiness-snapshot.json"
MAX_CHAIN = 2
MAX_QUOTES = 3
PAUSE_SECONDS = 0.4


class RecordingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.events = []

    def request(self, method, url, **kwargs):
        started = datetime.now(timezone.utc).isoformat()
        host = url.split("/")[2] if "://" in url else url
        try:
            response = super().request(method, url, **kwargs)
        except Exception as exc:
            self.events.append({"at": started, "method": method, "host": host,
                                "error": f"{type(exc).__name__}: {exc}"})
            raise
        interesting = {}
        for key, value in response.headers.items():
            lowered = key.lower()
            if "rate" in lowered or "retry" in lowered:
                interesting[key] = value
        self.events.append({
            "at": started, "method": method, "host": host, "status": response.status_code,
            "rate_headers": interesting,
        })
        return response


def main() -> None:
    started = datetime.now(timezone.utc)
    session = RecordingSession()
    snapshot = {
        "started_at": started.isoformat(),
        "model_enabled": bool(MODEL["enabled"]),
        "model_id": MODEL["model"],
        "prices_configured": MODEL["price_input_usd_per_million"] is not None,
        "note": "Read-only public sample. Not a strategy result and not a fill.",
    }
    sample = scanner.new_tokens(session)
    time.sleep(PAUSE_SECONDS)
    reasons = Counter()
    passed = []
    missing_price = 0
    for token in sample.tokens:
        if token.get("price_usd") is None:
            missing_price += 1
        reason = scanner.free_kill(token)
        reasons[reason or "passed_free_filters"] += 1
        if reason is None:
            passed.append(token["mint"])
    snapshot["gecko"] = {
        "pages_requested": sample.pages_requested,
        "pages_fetched": sample.pages_fetched,
        "pools_seen": sample.pools_seen,
        "tokens_after_pump_filter": len(sample.tokens),
        "truncated": sample.truncated,
        "rate_limited": sample.rate_limited,
        "error_status": sample.error_status,
        "claim": sample.claim,
        "free_kill": dict(reasons),
        "missing_price": missing_price,
        "passed_free_filters": len(passed),
    }

    # Integration probes are not approvals. Prefer filter survivors, else the
    # first parsed mints, so a quiet market still shows the provider shape.
    chain_targets = passed[:MAX_CHAIN] or [token["mint"] for token in sample.tokens[:1]]
    chain_rows = []
    for mint in chain_targets:
        row = {"mint": mint, "passed_free_filters": mint in passed}
        try:
            facts = chain.facts(mint, session)
            row["kill"] = chain.chain_kill(facts)
            row["supply"] = facts.get("supply")
            row["sampled_accounts"] = facts.get("sampled_accounts")
            row["coverage_pct"] = facts.get("coverage_pct")
            row["coverage_complete"] = facts.get("coverage_complete")
            row["concentration_available"] = facts.get("concentration_available")
            row["concentration_failure"] = facts.get("concentration_failure")
            row["developer_wallet_audited"] = facts.get("developer_wallet_audited")
            row["unknown_accounts"] = facts.get("unknown_accounts")
            row["excluded_pool_programs"] = sorted({
                item.get("program") for item in facts.get("excluded_as_pool") or []
            })
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        chain_rows.append(row)
        time.sleep(PAUSE_SECONDS)
    snapshot["chain"] = chain_rows

    quote_targets = [token["mint"] for token in sample.tokens[:MAX_QUOTES]]
    quotes = []
    for mint in quote_targets:
        row = {"mint": mint}
        try:
            quoted = scanner.quote(mint, session)
            if quoted is None:
                row["quote"] = None
            else:
                row["quote"] = {
                    "price_usd": quoted.get("price_usd"),
                    "liquidity_usd": quoted.get("liquidity_usd"),
                    "volume_h6": quoted.get("volume_h6"),
                    "volume_h24": quoted.get("volume_h24"),
                    "quoted_at_present": "quoted_at" in quoted,
                }
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        quotes.append(row)
        time.sleep(PAUSE_SECONDS)
    snapshot["quotes"] = quotes

    programs = sorted(KNOWN_POOL_PROGRAMS)
    try:
        accounts = chain.rpc("getMultipleAccounts", [programs, {"encoding": "base64"}], session)["value"]
        snapshot["known_pool_programs"] = [
            {"program": program, "found": account is not None,
             "executable": None if account is None else bool(account.get("executable")),
             "owner": None if account is None else account.get("owner")}
            for program, account in zip(programs, accounts or [])
        ]
    except Exception as exc:
        snapshot["known_pool_programs_error"] = f"{type(exc).__name__}: {exc}"

    finished = datetime.now(timezone.utc)
    snapshot["finished_at"] = finished.isoformat()
    snapshot["elapsed_seconds"] = round((finished - started).total_seconds(), 3)
    snapshot["http"] = session.events
    snapshot["http_calls"] = len(session.events)
    snapshot["http_errors"] = [event for event in session.events if event.get("status", 200) >= 400 or event.get("error")]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "wrote": str(OUT),
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "gecko": snapshot["gecko"],
        "chain_kills": [row.get("kill") or row.get("error") for row in chain_rows],
        "quotes_present": sum(1 for row in quotes if row.get("quote")),
        "http_calls": snapshot["http_calls"],
        "http_errors": snapshot["http_errors"],
        "programs_not_executable": [
            row["program"] for row in snapshot.get("known_pool_programs", [])
            if row.get("executable") is not True
        ],
    }, indent=1))


if __name__ == "__main__":
    main()
