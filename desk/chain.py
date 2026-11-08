"""Seat 2: chain facts. Straight from a Solana RPC node, no model involved.

The sample is the largest token accounts the RPC returns, capped in config
(default 20). It is not a holder census and it is not a developer-wallet audit.
Only accounts whose owner is positively owned by a known pool or curve program
are removed. Missing or unknown owner metadata fails closed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import CHAIN
from desk.persist import atomic_write

SYSTEM_PROGRAM = "11111111111111111111111111111111"
READINESS_PATH = "holder_readiness.json"


def rpc_url() -> str:
    """Endpoint from SOLANA_RPC_URL, otherwise the public default.

    The value is not logged. URLs from vendors often embed an API key.
    """
    configured = os.getenv("SOLANA_RPC_URL", "").strip()
    return configured or CHAIN["public_rpc"]

HOLDER_CLAIM = (
    "Holder concentration uses at most the largest 20 token accounts, "
    "aggregates balances only inside that sample, and excludes an account only when its owner "
    "is positively identified as a known pool or curve program. "
    "Unknown or missing owner metadata fails closed. "
    "If the sampled balances do not account for total supply, the token is not approved. "
    "If the RPC cannot return largest accounts, concentration is unavailable and the token is not approved. "
    "This is not a complete holder census and not a developer-wallet audit. "
    "Even a working provider only returns the largest accounts, so coverage of a widely held mint stays incomplete."
)


def rpc(method: str, params: list, session: requests.Session | None = None):
    client = session or requests.Session()
    response = client.post(
        rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    if response.status_code == 429:
        error = requests.HTTPError(f"HTTP 429 for {method}", response=response)
        raise error
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        message = body["error"].get("message", "rpc error") if isinstance(body["error"], dict) else str(body["error"])
        raise RuntimeError(f"{method}: {message}")
    return body["result"]


def _http_diagnostic(method: str, response, now: datetime) -> dict:
    headers = {} if response is None else {k.lower(): v for k, v in response.headers.items()}
    retry_raw = headers.get("retry-after")
    try:
        retry_seconds = max(1, int(float(retry_raw)))
    except (TypeError, ValueError):
        retry_seconds = 60
    status = None if response is None else response.status_code
    return {
        "method": method,
        "supported": False,
        "http_status": status,
        "retry_after": retry_raw,
        "retry_seconds": retry_seconds,
        "method_limit": headers.get("x-ratelimit-method-limit"),
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "unavailable_until": (now.astimezone(timezone.utc) + timedelta(seconds=retry_seconds)).isoformat(),
        "detail": f"{method} is unavailable (HTTP {status}). Concentration is not a pass.",
    }


class HolderReadiness:
    """Remembers that getTokenLargestAccounts is blocked, and caches fresh reads.

    The backoff is the provider's Retry-After, not a second endpoint. A cached
    read past its TTL is ignored. Nothing here treats a miss as safe.
    """

    def __init__(self, path: str | os.PathLike = READINESS_PATH):
        self.path = Path(path)
        self.state = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"block": None, "facts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"block": None, "facts": {}}
        if not isinstance(data, dict):
            return {"block": None, "facts": {}}
        data.setdefault("block", None)
        data.setdefault("facts", {})
        return data

    def _save(self) -> None:
        atomic_write(self.path, json.dumps(self.state, indent=1))

    def blocked(self, now: datetime | None = None) -> dict | None:
        block = self.state.get("block")
        if not block:
            return None
        now = now or datetime.now(timezone.utc)
        until = datetime.fromisoformat(block["unavailable_until"])
        if now.astimezone(timezone.utc) >= until.astimezone(timezone.utc):
            return None
        return block

    def note_block(self, method: str, response, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        block = _http_diagnostic(method, response, now)
        self.state["block"] = block
        self._save()
        return block

    def cached_facts(self, mint: str, now: datetime | None = None) -> dict | None:
        row = (self.state.get("facts") or {}).get(mint)
        if not row:
            return None
        now = now or datetime.now(timezone.utc)
        fetched = datetime.fromisoformat(row["fetched_at"])
        age = (now.astimezone(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds()
        if age > float(CHAIN["facts_ttl_seconds"]) or age < 0:
            return None
        return row["facts"]

    def store_facts(self, mint: str, facts_row: dict, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.state.setdefault("facts", {})[mint] = {
            "fetched_at": now.astimezone(timezone.utc).isoformat(),
            "facts": facts_row,
        }
        self._save()


def probe_largest_accounts(session: requests.Session | None = None, mint: str | None = None,
                            readiness: HolderReadiness | None = None,
                            now: datetime | None = None) -> dict:
    """One getTokenLargestAccounts call. Does not retry and does not switch endpoints.

    The mint defaults to wrapped SOL, a public account, only to see if the method
    is allowed. The result is a diagnostic, not a trading signal.
    """
    now = now or datetime.now(timezone.utc)
    target = mint or "So11111111111111111111111111111111111111112"
    if readiness and readiness.blocked(now):
        return readiness.blocked(now)
    client = session or requests.Session()
    response = client.post(
        rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [target]},
        timeout=20,
    )
    if response.status_code == 429:
        diagnostic = _http_diagnostic("getTokenLargestAccounts", response, now)
        if readiness:
            readiness.note_block("getTokenLargestAccounts", response, now)
        return diagnostic
    if response.status_code != 200:
        diagnostic = _http_diagnostic("getTokenLargestAccounts", response, now)
        if readiness:
            readiness.note_block("getTokenLargestAccounts", response, now)
        return diagnostic
    body = response.json()
    if "error" in body:
        diagnostic = _http_diagnostic("getTokenLargestAccounts", response, now)
        diagnostic["detail"] = "getTokenLargestAccounts returned an RPC error. Concentration is not a pass."
        if readiness:
            readiness.state["block"] = diagnostic
            readiness._save()
        return diagnostic
    return {
        "method": "getTokenLargestAccounts",
        "supported": True,
        "http_status": 200,
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "detail": "method responded. A response is not a holder audit.",
    }


def _unavailable(reason: str, detail: str = "", **fields) -> dict:
    out = {
        "mint_authority": None,
        "freeze_authority": None,
        "supply": None,
        "top_wallet_pct": None,
        "top_10_pct": None,
        "program_held_pct": None,
        "wallets_counted": 0,
        "sampled_accounts": 0,
        "sampled_balance": 0.0,
        "coverage_pct": None,
        "coverage_complete": False,
        "developer_wallet_audited": False,
        "concentration_available": False,
        "concentration_failure": reason,
        "detail": detail,
        "unknown_accounts": 0,
        "excluded_as_pool": [],
        "aggregated_owners": 0,
        "limitations": HOLDER_CLAIM,
    }
    out.update(fields)
    return out


def _note_and_unavailable(readiness, method: str, response, now: datetime) -> dict:
    diagnostic = _http_diagnostic(method, response, now)
    if readiness is not None:
        readiness.note_block(method, response, now)
    return _unavailable("holder_method_unavailable", diagnostic["detail"], diagnostic=diagnostic)


def facts(mint: str, session: requests.Session | None = None,
          readiness: HolderReadiness | None = None, now: datetime | None = None) -> dict:
    """Mint/freeze authority plus a sampled concentration figure.

    `developer_wallet_audited` is always false. Passing the numeric caps does
    not mean the holder set is safe.
    """
    now = now or datetime.now(timezone.utc)
    if readiness is not None:
        block = readiness.blocked(now)
        if block:
            return _unavailable("holder_method_unavailable", block["detail"], diagnostic=block)
        cached = readiness.cached_facts(mint, now)
        if cached is not None:
            return cached
    try:
        info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], session)
    except requests.HTTPError as exc:
        return _note_and_unavailable(readiness, "getAccountInfo", getattr(exc, "response", None), now)
    try:
        parsed = info["value"]["data"]["parsed"]["info"]
        decimals = int(parsed["decimals"])
        supply = int(parsed["supply"]) / 10 ** decimals
    except (KeyError, TypeError, ValueError) as exc:
        return _unavailable("unavailable_concentration", f"mint account unreadable: {exc}")

    authorities = {
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "supply": supply,
    }
    if supply <= 0:
        return _unavailable("unavailable_concentration", "supply missing or zero", **authorities)

    try:
        raw = rpc("getTokenLargestAccounts", [mint], session)["value"]
    except requests.HTTPError as exc:
        return _note_and_unavailable(
            readiness, "getTokenLargestAccounts", getattr(exc, "response", None), now)
    except (KeyError, TypeError, ValueError) as exc:
        return _unavailable("unavailable_concentration", f"largest accounts unreadable: {exc}",
                            **authorities)
    if not raw:
        return _unavailable("unavailable_concentration", "no token accounts returned", **authorities)

    limit = int(CHAIN["largest_accounts_checked"])
    largest = list(raw)[:limit]
    token_accts = [row.get("address") for row in largest]
    if any(not addr for addr in token_accts):
        return _unavailable("unknown_holder", "token account address missing", **authorities,
                            sampled_accounts=len(largest))

    accts = rpc("getMultipleAccounts", [token_accts, {"encoding": "jsonParsed"}], session)["value"]
    if not accts or len(accts) != len(largest):
        return _unavailable("unknown_holder", "token account metadata missing", **authorities,
                            sampled_accounts=len(largest))

    owners = []
    unknown = 0
    amounts = []
    for row, acct in zip(largest, accts):
        if row.get("uiAmount") is None:
            unknown += 1
            amounts.append(None)
        else:
            try:
                amounts.append(float(row["uiAmount"]))
            except (TypeError, ValueError):
                unknown += 1
                amounts.append(None)
        if not acct:
            owners.append(None)
            unknown += 1
            continue
        try:
            owner = acct["data"]["parsed"]["info"].get("owner")
        except (KeyError, TypeError, AttributeError):
            owner = None
        if not owner:
            unknown += 1
        owners.append(owner)

    uniq = [owner for owner in dict.fromkeys(owners) if owner]
    owner_accts = (
        rpc("getMultipleAccounts", [uniq, {"encoding": "base64"}], session)["value"]
        if uniq else []
    )
    if uniq and (owner_accts is None or len(owner_accts) != len(uniq)):
        return _unavailable("unknown_holder", "owner metadata missing", **authorities,
                            unknown_accounts=max(unknown, 1), sampled_accounts=len(largest))

    program_of = {}
    for owner, acct in zip(uniq, owner_accts or []):
        if not acct or not acct.get("owner"):
            program_of[owner] = None
            unknown += 1
        else:
            program_of[owner] = acct.get("owner")

    known = set(CHAIN["known_pool_programs"])
    wallets: dict[str, float] = {}
    excluded = 0.0
    excluded_rows = []
    for amount, owner in zip(amounts, owners):
        if amount is None or not owner or not program_of.get(owner):
            unknown += 1
            continue
        program = program_of[owner]
        if program in known:
            excluded += amount
            excluded_rows.append({"owner": owner, "program": program, "amount": amount})
            continue
        if program != SYSTEM_PROGRAM:
            unknown += 1
            continue
        wallets[owner] = wallets.get(owner, 0.0) + amount

    sampled_balance = 0.0
    for amount in amounts:
        if amount is not None:
            sampled_balance += amount
    coverage = sampled_balance / supply
    ranked = sorted(wallets.values(), reverse=True)
    available = unknown == 0 and all(amount is not None for amount in amounts)
    share = (lambda xs: (sum(xs) / supply) if supply else None) if available else (lambda xs: None)
    built = {
        **authorities,
        "top_wallet_pct": share(ranked[:1]),
        "top_10_pct": share(ranked[:10]),
        "program_held_pct": (excluded / supply) if available else None,
        "wallets_counted": len(ranked) if available else 0,
        "sampled_accounts": len(largest),
        "sampled_balance": sampled_balance,
        "sample_limit": limit,
        "sample_capped": len(raw) >= limit,
        "coverage_pct": coverage,
        "coverage_complete": available and abs(sampled_balance - supply) <= max(1e-6, supply * 1e-9),
        "developer_wallet_audited": False,
        "concentration_available": available,
        "concentration_failure": None if available else "unknown_holder",
        "unknown_accounts": unknown,
        "excluded_as_pool": excluded_rows,
        "aggregated_owners": len(wallets),
        "limitations": HOLDER_CLAIM,
    }
    if readiness is not None:
        readiness.store_facts(mint, built, now)
    return built


def chain_kill(facts_row: dict) -> str | None:
    """Plain facts against plain limits. First failure wins.

    A missing concentration figure does not pass. Numeric caps are applied only
    when every returned account was classified and the sample covers total supply.
    A lower-bound percentage from a partial sample is not an approval.
    """
    if facts_row.get("mint_authority") and not CHAIN["allow_mint_authority"]:
        return "mint_authority"
    if facts_row.get("freeze_authority") and not CHAIN["allow_freeze_authority"]:
        return "freeze_authority"
    if facts_row.get("concentration_available") is not True:
        return facts_row.get("concentration_failure") or "unavailable_concentration"
    if facts_row.get("coverage_complete") is not True:
        return "incomplete_sample"
    if facts_row.get("top_wallet_pct") is None or facts_row.get("top_10_pct") is None:
        return "unavailable_concentration"
    if facts_row["top_wallet_pct"] > CHAIN["max_top_wallet"]:
        return "top_wallet"
    if facts_row["top_10_pct"] > CHAIN["max_top_10"]:
        return "top_10"
    return None
