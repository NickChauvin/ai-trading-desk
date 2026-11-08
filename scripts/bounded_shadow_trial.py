"""One shadow cycle with hard ceilings. It cannot be turned into a multi-day run.

State stays in the directory you pass (default ./shadow_state). The paper ledger
is not opened. Paid scoring is refused. Holder reads stop when the RPC says
getTokenLargestAccounts is unavailable; this does not weaken that gate.

    python scripts/bounded_shadow_trial.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Ceilings are in code. Arguments cannot raise them.
CEILING_SECONDS = 30
CEILING_HTTP = 8
CEILING_CYCLES = 1

for _name in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALERTS_ENABLED"):
    os.environ.pop(_name, None)

import requests  # noqa: E402

from config import MODEL  # noqa: E402
from desk.chain import HolderReadiness, probe_largest_accounts  # noqa: E402
from desk.ops import Bench, set_journal_path  # noqa: E402
from desk.watch import CandidateQueue  # noqa: E402
from run_crypto import cycle  # noqa: E402


class LimitedSession(requests.Session):
    def __init__(self, ceiling: int):
        super().__init__()
        self.ceiling = ceiling
        self.calls = 0
        self.stopped = False

    def request(self, method, url, **kwargs):
        if self.calls >= self.ceiling:
            self.stopped = True
            raise RuntimeError("bounded shadow trial hit its HTTP ceiling")
        self.calls += 1
        return super().request(method, url, **kwargs)


def main() -> None:
    if MODEL["enabled"] or MODEL["model"] or MODEL["daily_budget_usd"] is not None:
        print("paid scoring is not disabled in config.py; refusing to run")
        raise SystemExit(2)
    state = ROOT / "shadow_state"
    state.mkdir(exist_ok=True)
    set_journal_path(state / "journal.jsonl")
    started = datetime.now(timezone.utc)
    session = LimitedSession(CEILING_HTTP)
    readiness = HolderReadiness(state / "holder_readiness.json")
    diagnostic = probe_largest_accounts(session, readiness=readiness, now=started)
    print("holder_probe", {k: diagnostic.get(k) for k in (
        "method", "supported", "http_status", "method_limit", "retry_after", "detail")})
    queue = CandidateQueue(state / "candidate_queue.json")
    bench = Bench(str(state / "bench.db"))
    deadline = started.timestamp() + CEILING_SECONDS

    def sleep(seconds):
        if datetime.now(timezone.utc).timestamp() + seconds > deadline:
            raise RuntimeError("bounded shadow trial hit its time ceiling")
        import time
        time.sleep(min(seconds, 1))

    stats = None
    error = None
    if not diagnostic.get("supported"):
        print("holder method unavailable; this cycle will not treat that as a pass")
    try:
        if datetime.now(timezone.utc).timestamp() <= deadline:
            stats = cycle(None, bench, session, shadow=True, queue=queue, readiness=readiness,
                          sleep=sleep)
    except Exception as exc:
        error = type(exc).__name__
    finished = datetime.now(timezone.utc)
    print({
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "cycles": CEILING_CYCLES,
        "http_calls": session.calls,
        "http_ceiling": CEILING_HTTP,
        "seconds_ceiling": CEILING_SECONDS,
        "holder_supported": bool(diagnostic.get("supported")),
        "stats": stats,
        "error": error,
        "state_dir": str(state),
        "paper_ledger_opened": False,
        "paid_scoring": False,
    })


if __name__ == "__main__":
    main()
