"""Seat 1: the scanner. Pulls a bounded sample of fresh Solana pools.

GeckoTerminal is free and needs no key, but allows about 10 calls a minute.
One cycle requests at most MAX_NEW_POOL_PAGES pages (2). That is a sample of
the newest pools, not every launch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import math

import requests

from config import SCAN

GT = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
HEADERS = {"Accept": "application/json", "User-Agent": "ai-trading-desk/1.0"}
MAX_NEW_POOL_PAGES = 2

SAMPLE_CLAIM = (
    "Bounded sample of GeckoTerminal new pools: at most 2 pages per cycle, "
    "whatever page size the API returns (historically 20). Not every launch."
)


@dataclass
class PoolSample:
    tokens: list = field(default_factory=list)
    pages_requested: int = 0
    pages_fetched: int = 0
    pools_seen: int = 0
    truncated: bool = False
    rate_limited: bool = False
    error_status: int | None = None
    claim: str = SAMPLE_CLAIM


def _num(x):
    """Parse a provider number. Missing or non-numeric stays None. Never invent a zero."""
    if x is None or x == "":
        return None
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _f(x) -> float:
    """Only for fields where the caller has already decided absence means skip, not a price."""
    value = _num(x)
    return 0.0 if value is None else value


def _age_minutes(iso: str | None):
    if not iso:
        return None
    created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 60)


def page_budget() -> int:
    """How many pages this cycle may request. Never above MAX_NEW_POOL_PAGES."""
    try:
        requested = int(SCAN.get("pages", MAX_NEW_POOL_PAGES))
    except (TypeError, ValueError):
        requested = MAX_NEW_POOL_PAGES
    try:
        configured_cap = int(SCAN.get("max_pages", MAX_NEW_POOL_PAGES))
    except (TypeError, ValueError):
        configured_cap = MAX_NEW_POOL_PAGES
    return max(0, min(requested, configured_cap, MAX_NEW_POOL_PAGES))


def parse_pool(pool: dict, symbols: dict[str, str]) -> dict | None:
    """One GeckoTerminal pool -> the desk's token dict. Field names are fixed here, once."""
    attrs = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    if not base_id.startswith("solana_"):
        return None
    mint = base_id.split("_", 1)[1]
    tx_h1 = (attrs.get("transactions") or {}).get("h1")
    vol = attrs.get("volume_usd") if isinstance(attrs.get("volume_usd"), dict) else None
    chg = attrs.get("price_change_percentage") if isinstance(attrs.get("price_change_percentage"), dict) else None
    age = _age_minutes(attrs.get("pool_created_at"))
    mcap = _num(attrs.get("market_cap_usd"))
    if mcap is None:
        mcap = _num(attrs.get("fdv_usd"))
    return {
        "mint": mint,
        "symbol": symbols.get(base_id, attrs.get("name", "?").split(" / ")[0]),
        "pool": attrs.get("address"),
        "dex": dex_id,
        "price_usd": _num(attrs.get("base_token_price_usd")),
        "mcap_usd": mcap,
        "liquidity_usd": _num(attrs.get("reserve_in_usd")),
        "volume_m5": _keyed(vol, "m5"), "volume_h1": _keyed(vol, "h1"),
        "volume_h6": _keyed(vol, "h6"), "volume_h24": _keyed(vol, "h24"),
        "buys_h1": _count(tx_h1, "buys"), "sells_h1": _count(tx_h1, "sells"),
        "buyers_h1": _count(tx_h1, "buyers"),
        "change_m5": _keyed(chg, "m5"), "change_h1": _keyed(chg, "h1"),
        "change_h6": _keyed(chg, "h6"),
        "age_minutes": None if age is None else round(age, 1),
    }


def _keyed(container: dict | None, key: str):
    if not isinstance(container, dict) or key not in container:
        return None
    return _num(container.get(key))


def _count(container: dict | None, key: str):
    if not isinstance(container, dict) or key not in container or container.get(key) is None:
        return None
    try:
        return int(container[key])
    except (TypeError, ValueError):
        return None


def new_tokens(session: requests.Session | None = None) -> PoolSample:
    """Newest Solana pools, deduped by mint, from at most two pages.

    A 429 or any other non-200 stops the sample. The result says it is truncated.
    Nothing here retries and nothing here claims to have seen every launch.
    """
    client = session or requests.Session()
    sample = PoolSample(pages_requested=page_budget())
    seen: set[str] = set()
    for page in range(1, sample.pages_requested + 1):
        try:
            response = client.get(
                f"{GT}/networks/solana/new_pools",
                params={"page": page, "include": "base_token,dex"},
                headers=HEADERS,
                timeout=20,
            )
        except requests.RequestException:
            sample.truncated = True
            break
        if response.status_code == 429:
            sample.rate_limited = True
            sample.truncated = True
            sample.error_status = 429
            break
        if response.status_code != 200:
            sample.truncated = True
            sample.error_status = response.status_code
            break
        sample.pages_fetched += 1
        try:
            body = response.json()
        except ValueError:
            sample.truncated = True
            break
        symbols = {
            item["id"]: item["attributes"].get("symbol", "?")
            for item in body.get("included", []) if item.get("type") == "token"
        }
        pools = body.get("data", [])
        sample.pools_seen += len(pools)
        for pool in pools:
            token = parse_pool(pool, symbols)
            if not token or token["mint"] in seen:
                continue
            if SCAN["pump_only"] and token["dex"] not in SCAN["pump_dexes"]:
                continue
            seen.add(token["mint"])
            sample.tokens.append(token)
    return sample


def free_kill(token: dict) -> str | None:
    """Pass one. Pure arithmetic on data we already hold. Returns the failed check or None.

    A missing field is not a zero. It cannot pass.
    """
    for key in ("price_usd", "age_minutes", "liquidity_usd", "volume_h1", "mcap_usd", "buys_h1", "sells_h1"):
        if token.get(key) is None:
            return f"missing_{key}"
    price = float(token["price_usd"])
    if not math.isfinite(price) or price <= 0:
        return "price"
    if not SCAN["min_age_minutes"] <= token["age_minutes"] <= SCAN["max_age_hours"] * 60:
        return "age"
    if token["liquidity_usd"] < SCAN["min_liquidity_usd"]:
        return "liquidity"
    if token["volume_h1"] < SCAN["min_volume_h1_usd"]:
        return "volume"
    if not SCAN["min_mcap_usd"] <= token["mcap_usd"] <= SCAN["max_mcap_usd"]:
        return "mcap"
    if token["buys_h1"] + token["sells_h1"] < SCAN["min_trades_h1"]:
        return "trades"
    if token["buys_h1"] and token["sells_h1"] / token["buys_h1"] < SCAN["min_sells_ratio_h1"]:
        return "no_sells"
    return None


def socials(mint: str, session: requests.Session | None = None) -> dict:
    """X / Telegram / site links as the project published them (DexScreener token info).
    Taken from the listing, never from a search: a search finds fan accounts."""
    client = session or requests.Session()
    try:
        pairs = client.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return {"x": None, "telegram": None, "website": None}
    info = {}
    for pair in pairs:
        if pair.get("info"):
            info = pair["info"]
            break
    links = {item.get("type"): item.get("url") for item in info.get("socials", [])}
    sites = info.get("websites") or []
    return {
        "x": links.get("twitter"),
        "telegram": links.get("telegram"),
        "website": sites[0].get("url") if sites else None,
    }


def quote(mint: str, session: requests.Session | None = None) -> dict | None:
    """Quoted price and liquidity for an open position, from the deepest pair.

    A missing pair, a bad payload, or a non-positive price/liquidity comes back
    as None. Callers must treat that as unexecutable, not as the last price.
    """
    client = session or requests.Session()
    try:
        pairs = client.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return None
    if not pairs:
        return None
    pair = max(pairs, key=lambda item: (item.get("liquidity") or {}).get("usd") or 0)
    vol = pair.get("volume") or {}
    quoted = {
        "price_usd": _f(pair.get("priceUsd")),
        "liquidity_usd": _f((pair.get("liquidity") or {}).get("usd")),
        "volume_h6": _f(vol.get("h6")),
        "volume_h24": _f(vol.get("h24")),
    }
    if quoted["price_usd"] <= 0 or quoted["liquidity_usd"] <= 0:
        return None
    return quoted
