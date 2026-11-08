"""Seat 2: chain facts. Straight from a Solana RPC node, no model involved.

The sample is the largest token accounts the RPC returns, capped in config
(default 20). It is not a holder census and it is not a developer-wallet audit.
Only accounts whose owner is positively owned by a known pool or curve program
are removed. Missing or unknown owner metadata fails closed.
"""
from __future__ import annotations

import os

import requests

from config import CHAIN

RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SYSTEM_PROGRAM = "11111111111111111111111111111111"

HOLDER_CLAIM = (
    "Holder concentration uses at most the largest 20 token accounts, "
    "aggregates balances only inside that sample, and excludes an account only when its owner "
    "is positively identified as a known pool or curve program. "
    "Unknown or missing owner metadata fails closed. "
    "If the sampled balances do not account for total supply, the token is not approved. "
    "This is not a complete holder census and not a developer-wallet audit."
)


def rpc(method: str, params: list, session: requests.Session | None = None):
    s = session or requests.Session()
    response = s.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


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


def facts(mint: str, session: requests.Session | None = None) -> dict:
    """Mint/freeze authority plus a sampled concentration figure.

    `developer_wallet_audited` is always false. Passing the numeric caps does
    not mean the holder set is safe.
    """
    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], session)
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
    return {
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
