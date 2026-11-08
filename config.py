"""Every number the desk uses lives here. Retune the desk by editing this file only.

These are STARTING values, not advice. Nothing here is evidence of profitability
or a claim that a named model is currently offered at a known price.

Paid model calls stay off until MODEL["enabled"] is set True and the model id,
token prices, and daily dollar budget are filled in from your provider's docs.
This file does not ship those prices.
"""

# Daily-loss dates and quiet hours use this zone. It is not the host's local zone.
DESK_TIMEZONE = "UTC"

# ---------- Seat 1: scanner (free cuts on a bounded GeckoTerminal sample) ----------
# scanner.MAX_NEW_POOL_PAGES is a hard cap of 2. Raising `pages` cannot fetch more.
SCAN = {
    "pages": 2,                    # new_pools pages per cycle (API page size is not a census)
    "max_pages": 2,                # clamped again by the scanner hard cap
    "pump_only": True,             # only pump.fun curve + PumpSwap pools
    "pump_dexes": {"pump-fun", "pumpswap"},
    "min_age_minutes": 10,         # younger: the data is noise
    "max_age_hours": 6,            # older with no new move: not a launch any more
    "min_liquidity_usd": 8_000,
    "min_volume_h1_usd": 15_000,
    "min_mcap_usd": 25_000,
    "max_mcap_usd": 5_000_000,
    "min_trades_h1": 120,
    "min_sells_ratio_h1": 0.15,    # sells / buys. Below this nobody can sell = honeypot smell
    "watch_limit": 200,            # retained discoveries, not a census of launches
    "revisit_per_cycle": 3,        # refreshes of tokens that aged off the two-page feed
    "revisit_pause_seconds": 1.0,  # pacing between those refreshes; 429 stops the batch
}

# Owner programs treated as liquidity venues. An account is excluded from holder
# concentration only when its owner is positively owned by one of these.
# Any other program, or missing owner metadata, fails closed. It is not counted
# as a pool and it is not counted as a safe wallet.
KNOWN_POOL_PROGRAMS = frozenset({
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
})

# ---------- Seat 2: chain facts (Solana RPC) ----------
CHAIN = {
    "max_top_wallet": 0.05,        # biggest sampled wallet, after known pools are removed
    "max_top_10": 0.25,            # top 10 sampled wallets combined
    "allow_mint_authority": False, # supply can be printed -> hard no
    "allow_freeze_authority": False,
    "largest_accounts_checked": 20,  # sample size, not a holder census
    "known_pool_programs": KNOWN_POOL_PROGRAMS,
    "facts_ttl_seconds": 120,      # cached chain reads expire; stale cache is not a pass
    # Default public endpoint. It is not assumed to allow getTokenLargestAccounts.
    # A usable endpoint is one you provision that allows getAccountInfo,
    # getTokenLargestAccounts, and getMultipleAccounts. Set SOLANA_RPC_URL.
    # Do not put an API key in this file.
    "public_rpc": "https://api.mainnet-beta.solana.com",
}

# ---------- Seat 3: analyst (paid calls OFF until you fill this in) ----------
# Prices are USD per million tokens, from the provider's current price card.
# Leave them None. Do not paste a remembered rate and treat it as current.
MODEL = {
    "enabled": False,
    "model": "",                   # explicit model id; empty means "not configured"
    "review_model": "",            # falls back to model; same price pair is reserved
    "max_output_tokens": 1024,     # hard cap sent on every call
    "max_input_tokens": 4000,      # reservation input cap; prompts are truncated to it
    "max_billable_retries": 1,     # extra attempts that may still be billed
    "daily_budget_usd": None,
    "price_input_usd_per_million": None,
    "price_output_usd_per_million": None,
    "timezone": DESK_TIMEZONE,
}

# Older names. Empty model means the seat is not configured.
ANALYST_MODEL = MODEL["model"]
REVIEW_MODEL = MODEL["review_model"]
ANALYST_MAX_TOKENS = MODEL["max_output_tokens"]
MIN_SCORE = 7                        # 0-10, the analyst's score must clear this

# ---------- Seat 4: risk engine (pure code) ----------
RISK = {
    "max_risk_per_trade": 0.01,    # lose at most 1% of equity if the stop fills
    "max_position_pct": 0.02,      # no position above 2% of equity (memecoins)
    "max_pool_share": 0.02,        # sizing cap: 2% of quoted pool liquidity
    "max_open_positions": 3,
    "max_aggregate_open_risk_pct": 0.03,  # sum of open modeled risk, including full unresolved cost
    "max_daily_loss_pct": 0.03,    # blocks new entries; NOT a guaranteed maximum loss
    "min_ticket_usd": 10,          # below this, fees eat the edge
    "quiet_hours": (1, 7),         # hours in DESK_TIMEZONE with no new entries [1, 7)
    "timezone": DESK_TIMEZONE,
}

# ---------- Seat 5: exits (paper broker applies these, no model involved) ----------
EXITS = {
    "stop_pct": 0.35,              # -35% from entry -> try to close, if a quote is executable
    "take_half_at": 2.0,           # 2x -> sell half, let the rest ride
    "volume_decay_ratio": 0.20,    # h6 volume / (h24/4) under this -> close
    "volume_decay_min_minutes": 30,
    "max_hold_hours": 24,
}

PAPER = {
    "starting_equity_usd": 1_000,
    "fee_pct": 0.01,               # assumed fee, taken out of notional. Not an observed fill.
    "slippage_pct": 0.02,          # assumed 2% worse than the quoted price. Not an observed fill.
    "max_liquidity_fraction": 0.02,  # one simulated fill may use at most this share of quoted liquidity
    "max_quote_age_seconds": 120,    # applied only when the quote itself carries quoted_at
}

CYCLE_SECONDS = 60                 # crypto scan cycle
STOCK_CYCLE_SECONDS = 15 * 60      # stocks: every 15 min while the market is open

# How long a rejected token sits out, by the check that failed (minutes)
BENCH_MINUTES = {
    "exited": 360,                 # stopped out or closed: no revenge re-entry for 6h
    "mint_authority": 100_000, "freeze_authority": 100_000,
    "top_wallet": 120, "top_10": 120, "no_sells": 240,
    "unknown_holder": 30, "unavailable_concentration": 30, "incomplete_sample": 30,
    "missing_price": 10, "price": 10,
    "skip": 60, "watch": 15, "score": 20,
    "age": 10, "liquidity": 10, "volume": 10, "mcap": 15, "trades": 10,
}
DEFAULT_BENCH = 30
