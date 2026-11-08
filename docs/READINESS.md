# Readiness — 8 November 2026

This is a bounded check of `cursor/paper-sim-safety-fixes-5a4f`. It is not a certification, not a backtest, and not evidence of profit. No wallet, no model call, no Telegram message, and no broker order was sent. Nothing was left running.

**Verdict: do not use this for real money, and do not treat a green unit run as permission to leave it trading.** A responsible paper trial is not available on the default public RPC, and this snapshot did not produce a single candidate that reached a model.

## What ran

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -q` | **58 tests, OK** |
| `python demos/offline_demo.py` | finished, invented fixtures only |
| Public probe `scripts/bounded_public_probe.py` | 2026-11-08T20:54:29.242611Z to 2026-11-08T20:54:31.956051Z, 8 HTTP calls |
| Follow-up ages and one RPC method | 2026-11-08T20:55:00.507460Z to 2026-11-08T20:55:12.797185Z |

The raw counts are in `docs/readiness-snapshot.json`. Mints in that file are examples of what the providers returned. They are not recommendations.

Model scoring was off (`MODEL["enabled"]` is false, model id empty, prices unset). No score below is a market judgment. Offline tests that need a thesis use a fixture labeled as one.

## Blocking

1. **This snapshot produced zero candidates.** The scanner fetched 2 GeckoTerminal pages (40 pools, HTTP 200, not rate-limited). After the pump.fun filter, 30 tokens remained. Free filters rejected 29 for age and 1 for missing liquidity. **0 passed.** A second two-page fetch a minute later parsed 40 pools, ages **0.5 to 3.2 minutes** (median 2.6). The desk's minimum age is 10 minutes, so the newest two pages and the age window did not overlap. That is not "the strategy lost." It is "this source, capped at two pages, did not contain an in-window token."

2. **The default public Solana RPC cannot complete a holder check.** `getAccountInfo` returned HTTP 200 (method limit 50, 49 remaining). The next call, `getTokenLargestAccounts`, returned HTTP 429 with `retry-after: 10`, `x-ratelimit-method-limit: 0`, and `Too many requests for a specific RPC call`. Waiting 11 seconds and calling that method once more returned the same 429 and method limit 0. The mint was not approved. Holder coverage for live tokens was **not measured**. A private RPC would be a credential or a paid service; this evaluation did not use one.

3. **There is no live model-scored strategy to judge.** Paid calls stay disabled until an id, prices, and a daily budget are set. This run did not spend. Turning that on, or watching for days, was out of scope. Multi-day paper observation and any scored pass/fail remain undone.

4. **A passing quote would still not be a fill.** DexScreener returned HTTP 200 for three sample mints and the desk formed **0** quotes. One inspected pair had `priceUsd` and `pairCreatedAt`, and **no** `liquidity.usd` key. The desk returned no quote rather than inventing liquidity. Quotes that do carry a price and liquidity still have no trade timestamp unless the payload includes `quoted_at`. The desk does not stamp "now" onto them. A stop is not a guaranteed exit: an offline gap from the entry price to 20% of entry realized a loss larger than the 35% stop. Fees and slippage are config assumptions.

5. **Holder math cannot certify a token.** Unknown programs, missing owner metadata, a missing amount, and zero supply fail closed. A sample that does not add up to supply now fails closed too (`incomplete_sample`), which is the normal case for a 20-account read of a widely held mint. `developer_wallet_audited` is always false. The seven pool-program ids on the allowlist were present on-chain and executable (BPF upgradeable loader) at 20:54:31Z. That checks the ids exist. It does not audit their bytecode, and it does not identify a developer wallet.

6. **Not real-money ready.** No signing code, and the stock path refuses a non-paper client, but none of that is a track record. There is no evidence in this repository of profitability.

## What held up

- Missing or stale-timestamped quotes do not book cash. A $20 holding with no quote stays at cash 980, realized P&L 0, loss risk 20.
- `KILL` reports holdings still open. Shadow mode does not write `paper_ledger.json`. Telegram stays local unless `ALERTS_ENABLED=true`. A live Alpaca base URL is refused before submit.
- Two in-process brokers buying together finished at cash 960 and both positions. The ledger lock reloads the file before the second write.
- A missing `price_usd` is `missing_price_usd`, not a zero that can pass. The live sample's one pool without liquidity was rejected, not filled in.
- An RPC HTTP 429 is raised and is not turned into a concentration pass.
- The probe's hosts were only `api.geckoterminal.com`, `api.dexscreener.com`, and `api.mainnet-beta.solana.com`.

## Cost and coverage

Dollar cost of this evaluation: **0**. No model request was reserved. GeckoTerminal allowed both pages (about 20 pools each). It did not send a rate-limit header on these calls. The public RPC's free tier allowed `getAccountInfo` and `getMultipleAccounts` for the program list, and blocked `getTokenLargestAccounts`. DexScreener allowed the three GETs and did not supply liquidity on the pair that was inspected. Dependencies in `requirements.txt` are minimum versions, not hash-pinned.

## Revisit queue and holder method — same evening

Too-young pools are now kept in `candidate_queue.json` with the discovery time and the provider's `pool_created_at`. They are refreshed only once they reach 10 minutes, even if they have left the two-page feed. A refresh replaces market fields from one new response; a missing price or liquidity stays missing and fails. HTTP 429 stops that batch and waits for `Retry-After`. The queue is capped, deduped, and reloaded under a file lock. It is still not every launch: anything that never appeared in a two-page sample is never retained.

Holder concentration was not loosened. `getTokenLargestAccounts` is required. On the public endpoint it is not available:

| | |
| --- | --- |
| Probe | 2026-11-08T21:03:20.019575Z to 2026-11-08T21:03:22.616412Z |
| GeckoTerminal | 2 pages, 40 pools, 19 after the pump filter, **0** past the free filters (all 19 rejected for age) |
| `getTokenLargestAccounts` | HTTP 429 at 21:03:20.538512Z, `retry-after: 10`, method limit **0** |
| Chain reads after that | none; the block is not treated as a pass |
| DexScreener quotes formed | 0 |

The one-cycle shadow command then ran 2026-11-08T21:03:22.777502Z to 2026-11-08T21:03:22.854506Z. It made 2 HTTP calls (the feed only; the holder method was still inside its backoff), retained 19 waiting mints, revisited 0 because none were due, opened no paper ledger, and made no model call. Caps in the script are 1 cycle, 30 seconds, and 8 HTTP calls, and arguments cannot raise them.

```bash
python scripts/bounded_shadow_trial.py
```

Do not start a longer trial from this. Reliable holder data needs a Solana JSON-RPC endpoint you provision that allows `getAccountInfo`, `getTokenLargestAccounts`, and `getMultipleAccounts`. Put that URL in `SOLANA_RPC_URL`. Do not commit the key. This run did not create an account and did not ask for one. Public `https://api.mainnet-beta.solana.com` is not that endpoint today.

Even with a working provider, the gate still reads at most 20 accounts and fails closed unless those balances account for total supply. A widely held mint will not pass. `developer_wallet_audited` remains false. That is a property of the sample, not something a faster RPC removes.

`python -m unittest discover -s tests -q` after these changes: **70 tests, OK.**

## What remains, and the only plan that stays honest

Do not start `run_crypto.py` in a loop and do not enable the model or a private RPC just to manufacture a result.

Re-run, once, when you want another snapshot:

```bash
python -m unittest discover -s tests -q
python demos/offline_demo.py
python scripts/bounded_public_probe.py
```

Read `docs/readiness-snapshot.json` for that run's UTC times, page counts, free-filter reasons, and HTTP statuses. Expect the newest two pages to be younger than 10 minutes unless the market is quiet. Expect the public RPC to keep refusing largest-account reads.

Anything past that needs a decision this evaluation did not make: a private RPC you are willing to operate, an explicit model id and price card, and several days of paper journaling with the model still unable to move size. Those steps can still not prove a live fill or an annual return. Stop here.
