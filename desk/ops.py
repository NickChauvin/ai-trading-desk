"""Journal, bench, alerts, kill switch. The boring seats that keep the desk honest.

Telegram is off unless ALERTS_ENABLED is exactly true. Token and chat id alone
do not send. The default path prints a local line and returns.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import BENCH_MINUTES, DEFAULT_BENCH

JOURNAL = Path("journal.jsonl")
KILL_FILE = Path("KILL")
_journal_path = JOURNAL


def set_journal_path(path: Path | str | None) -> None:
    global _journal_path
    _journal_path = Path(path) if path else JOURNAL


def journal(kind: str, **fields) -> dict:
    row = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    with _journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    return row


def log_reject(stage: str, reason: str, **fields) -> dict:
    return journal("reject", stage=stage, reason=reason, **fields)


def log_exit_failed(**fields) -> dict:
    return journal("exit_failed", **fields)


def log_model_cost(**fields) -> dict:
    return journal("model_cost", **fields)


class Bench:
    def __init__(self, path: str = "desk.db"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS bench(mint TEXT PRIMARY KEY, reason TEXT, until REAL)"
        )
        self.db.commit()

    def benched(self, mint: str) -> bool:
        row = self.db.execute("SELECT until FROM bench WHERE mint=?", (mint,)).fetchone()
        return bool(row and row[0] > time.time())

    def sit(self, mint: str, reason: str):
        mins = BENCH_MINUTES.get(reason, DEFAULT_BENCH)
        self.db.execute(
            "INSERT OR REPLACE INTO bench VALUES (?,?,?)",
            (mint, reason, time.time() + mins * 60),
        )
        self.db.commit()


def alerts_enabled() -> bool:
    return os.getenv("ALERTS_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def alert(text: str, http_post=None) -> dict:
    """Local line always. HTTP only when alerts are explicitly enabled and addressed."""
    print(text)
    if not alerts_enabled():
        return {"sent": False, "channel": "stdout"}
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "channel": "stdout", "reason": "telegram address missing"}
    post = http_post or requests.post
    try:
        post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        print(f"telegram failed: {exc}")
        return {"sent": False, "channel": "telegram", "reason": str(exc)}
    return {"sent": True, "channel": "telegram"}


def killed() -> bool:
    return KILL_FILE.exists()


def load_env(path: str = ".env"):
    """Tiny .env loader so the desk has no extra dependency. Real env vars win."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
