# Changelog

## Unreleased

- Paper exits fail closed when a quote is missing or unexecutable. No proceeds are invented from the last price. Unresolved holdings keep their cash and realized P&L, and show a separate zero-recovery loss risk. `KILL` reports holdings that are still open.
- Holder checks sample at most 20 token accounts, aggregate owner balances inside that sample, and exclude only known pool/curve programs. Unknown or missing metadata fails closed. This is not a developer-wallet audit.
- Daily loss baseline uses an explicit timezone (UTC by default), persists across restart, and rolls during a long run only after quotes are refreshed.
- Cash and aggregate open-risk limits are enforced. The daily-loss gate is not a guaranteed maximum loss.
- Model calls reserve a worst-case daily dollar budget before the request. Failures and retries that may bill are not refunded. Paid calls are off until model, prices, and budget are set. Quiet hours and free risk checks run first.
- The scanner's two GeckoTerminal pages are described as a sample, not every launch.
- Shadow mode does not open or mutate the paper ledger. Telegram alerts are off by default. Stocks refuse non-paper clients.
- A holder sample that does not cover supply is not an approval. Missing prices stay missing. The paper ledger reloads under a file lock before a second writer mutates it. A quote timestamp is checked only when the quote carries one.
- `docs/READINESS.md` records the 8 November 2026 bounded public snapshot: zero free-filter passes, and the public RPC blocked largest-account reads.

## v1.0.0 · 2026-10-26

- Five seats: scanner, chain check, Claude analyst, risk engine, paper execution
- Real-wallet concentration: bonding curve and pool accounts excluded by owner program
- Typed `TokenThesis` and `StockThesis` via structured outputs, untrusted-input wrapping
- Rule-based exits: -35% stop, half at 2x, volume decay, 24h time out, 6h no-revenge bench
- Stocks mode on Alpaca paper with OTO stop orders
- Journal, Telegram alerts, kill switch, weekly review
- 17 offline tests, CI on Python 3.10 to 3.13
