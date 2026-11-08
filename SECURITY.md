# Security

## What this repo will never ask for

- Private keys or seed phrases
- Wallet signing permissions
- Exchange API keys with withdrawal rights

If anything claiming to be this project asks for one, it's a scam.

## Keys it does use

- `ANTHROPIC_API_KEY` only if you enable paid model calls in `config.py`
- Optional: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, which do nothing unless `ALERTS_ENABLED=true`
- Optional: `SOLANA_RPC_URL`, Alpaca **paper** keys

The stock seat has no live-trading switch. A client that is not Alpaca paper is refused before an order is sent.

Keep them in `.env` (already in `.gitignore`). Never commit them, never paste them in issues.

## Prompt injection

Token names, descriptions, links and headlines are written by strangers. The analyst receives them wrapped as untrusted data, and the architecture makes sure a fooled analyst still cannot size or place a trade.

## Reporting

Found a vulnerability? Don't open a public issue. Message the maintainers on [Telegram](https://t.me/+Ykc8YKaJR6Q3NDNk) first.
