"""Retained discoveries that were too young for the two-page new-pool sample.

The feed is not a record of every launch. A mint we actually saw is kept, with
the provider's pool_created_at, until it is old enough to recheck or until it
expires. Rechecks are paced, restart from this file, and stop on HTTP 429.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fcntl

from config import SCAN
from desk.persist import atomic_write
from desk.scanner import free_kill

QUEUE_PATH = Path("candidate_queue.json")


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def age_minutes(pool_created_at: str | None, now: datetime):
    created = _parse_time(pool_created_at)
    if created is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - created).total_seconds() / 60


class CandidateQueue:
    def __init__(self, path: Path | str = QUEUE_PATH):
        self.path = Path(path)
        self.state = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"candidates": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"candidates": {}}
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
            return {"candidates": {}}
        return data

    def _write(self) -> None:
        atomic_write(self.path, json.dumps(self.state, indent=1))

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path.with_name(self.path.name + ".lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            self.state = self._load()
            yield
            self._write()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _limit(self) -> int:
        try:
            return max(1, int(SCAN["watch_limit"]))
        except (TypeError, ValueError, KeyError):
            return 200

    def observe(self, tokens: list[dict], now: datetime) -> dict:
        """Merge a feed sample. Does not call the network."""
        added = 0
        duplicates = 0
        rejected = 0
        not_retained = 0
        with self._locked():
            for token in tokens:
                if not isinstance(token, dict) or not token.get("mint"):
                    rejected += 1
                    continue
                mint = str(token["mint"])
                existing = self.state["candidates"].get(mint)
                if existing:
                    duplicates += 1
                    existing["last_seen_at"] = _iso(now)
                    continue
                created = token.get("pool_created_at") if isinstance(token.get("pool_created_at"), str) else None
                if _parse_time(created) is None:
                    self.state["candidates"][mint] = {
                        "mint": mint,
                        "symbol": token.get("symbol"),
                        "status": "rejected",
                        "reason": "missing_pool_created_at",
                        "discovered_at": _iso(now),
                        "pool_created_at": None,
                        "next_check_at": None,
                    }
                    rejected += 1
                    continue
                if len(self.state["candidates"]) >= self._limit():
                    not_retained += 1
                    continue
                minutes = age_minutes(created, now)
                status, nxt, reason = _schedule(created, minutes, now)
                self.state["candidates"][mint] = {
                    "mint": mint,
                    "symbol": token.get("symbol"),
                    "dex": token.get("dex"),
                    "pool": token.get("pool"),
                    "discovered_at": _iso(now),
                    "pool_created_at": created,
                    "age_minutes_at_discovery": None if minutes is None else round(minutes, 1),
                    "status": status,
                    "reason": reason,
                    "next_check_at": None if nxt is None else _iso(nxt),
                    "last_seen_at": _iso(now),
                    "last_refresh_at": None,
                }
                added += 1
        return {"added": added, "duplicates": duplicates, "rejected": rejected,
                "not_retained": not_retained, "retained": len(self.state["candidates"])}

    def due(self, now: datetime) -> list[dict]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        rows = []
        for row in self.state["candidates"].values():
            if row.get("status") not in {"waiting", "rate_limited"}:
                continue
            nxt = _parse_time(row.get("next_check_at"))
            if nxt is None or now.astimezone(timezone.utc) < nxt:
                continue
            if age_minutes(row.get("pool_created_at"), now) is None:
                continue
            rows.append(row)
        rows.sort(key=lambda row: row.get("next_check_at") or "")
        return rows

    def refresh_due(self, refresh, now: datetime, session=None, sleep=None,
                    skip_mints: set[str] | None = None) -> dict:
        """Refresh only due mints, then stop on the first 429.

        `refresh(mint, session)` must not be called for a mint that is not due.
        Market fields on the returned tokens come only from that call. Age is
        recomputed from the stored launch time.
        """
        pause = float(SCAN.get("revisit_pause_seconds") or 0)
        try:
            budget = max(0, int(SCAN.get("revisit_per_cycle") or 0))
        except (TypeError, ValueError):
            budget = 0
        fetched = 0
        stopped = False
        ready = []
        with self._locked():
            skip = skip_mints or set()
            due = [row for row in self.due(now) if row["mint"] not in skip][:budget]
        for index, row in enumerate(due):
            if index and sleep is not None and pause:
                sleep(pause)
            result = refresh(row["mint"], session)
            fetched += 1
            with self._locked():
                current = self.state["candidates"].get(row["mint"])
                if current is None:
                    continue
                if result.get("rate_limited"):
                    delay = _retry_seconds(result.get("retry_after"))
                    current["status"] = "rate_limited"
                    current["reason"] = "rate_limited"
                    current["next_check_at"] = _iso(now.astimezone(timezone.utc) + timedelta(seconds=delay))
                    stopped = True
                else:
                    token = result.get("token")
                    updated = _apply_refresh(current, token, now)
                    if updated is None:
                        current["status"] = "rejected"
                        current["reason"] = result.get("reason") or "incomplete_refresh"
                        current["next_check_at"] = None
                    else:
                        reason = free_kill(updated)
                        current["last_refresh_at"] = _iso(now)
                        if reason is None:
                            current["status"] = "ready"
                            current["reason"] = None
                            current["next_check_at"] = None
                            ready.append(updated)
                        elif reason == "age" and _still_young(current, now):
                            minutes = age_minutes(current.get("pool_created_at"), now)
                            _status, nxt, why = _schedule(current["pool_created_at"], minutes, now)
                            current["status"] = _status
                            current["reason"] = why
                            current["next_check_at"] = None if nxt is None else _iso(nxt)
                        else:
                            current["status"] = "expired" if reason == "age" else "rejected"
                            current["reason"] = reason
                            current["next_check_at"] = None
            if stopped:
                break
        return {"fetched": fetched, "ready": ready, "stopped_for_rate_limit": stopped,
                "due_left": max(0, len(self.due(now)))}


def _retry_seconds(raw) -> int:
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return 60


def _still_young(row: dict, now: datetime) -> bool:
    minutes = age_minutes(row.get("pool_created_at"), now)
    if minutes is None:
        return False
    return minutes < float(SCAN["min_age_minutes"])


def _schedule(created: str, minutes, now: datetime):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if minutes is None:
        return "rejected", None, "missing_pool_created_at"
    if minutes > float(SCAN["max_age_hours"]) * 60:
        return "expired", None, "age"
    if minutes < float(SCAN["min_age_minutes"]):
        created_at = _parse_time(created)
        return "waiting", created_at + timedelta(minutes=float(SCAN["min_age_minutes"])), "age"
    return "waiting", now.astimezone(timezone.utc), "due"


def _apply_refresh(row: dict, token: dict | None, now: datetime) -> dict | None:
    """Use the fresh payload's market fields. Keep the original launch time.

    A missing fresh field stays missing. Discovery values are not copied forward
    to fill a hole.
    """
    if not isinstance(token, dict):
        return None
    minutes = age_minutes(row.get("pool_created_at"), now)
    rebuilt = {
        "mint": row["mint"],
        "symbol": token.get("symbol") or row.get("symbol"),
        "pool": token.get("pool"),
        "dex": token.get("dex") or row.get("dex"),
        "price_usd": token.get("price_usd"),
        "mcap_usd": token.get("mcap_usd"),
        "liquidity_usd": token.get("liquidity_usd"),
        "volume_h1": token.get("volume_h1"),
        "volume_h6": token.get("volume_h6"),
        "volume_h24": token.get("volume_h24"),
        "buys_h1": token.get("buys_h1"),
        "sells_h1": token.get("sells_h1"),
        "buyers_h1": token.get("buyers_h1"),
        "age_minutes": None if minutes is None else round(minutes, 1),
        "pool_created_at": row.get("pool_created_at"),
        "retained": True,
    }
    return rebuilt
