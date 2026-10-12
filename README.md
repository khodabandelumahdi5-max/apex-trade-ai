# Apex Trade AI

Multi-agent crypto trading engine for Solana tokens (Jupiter) with optional CEX futures (Binance/Bybit via CCXT).
**Default mode is paper trading**: fills are priced from live Jupiter quotes, no funds move.

## Architecture

```
               ┌────────────── SwarmCoordinator (every LOOP_INTERVAL_SEC) ──────────────┐
 Jupiter price │  OnChainAgent ─┐                                                       │
 GeckoTerminal │                ├─ consensus gate ─► RiskAgent ─► ExecutionAgent ─► DB  │
 Solana RPC/WS │  TechnicalAgent┘   (both must agree)  (Kelly, VaR,   (paper/Jupiter/    │
 CCXT (opt.)   │                                        breakers)      Jito/CEX)        │
               └────────────────────────────────────────────────────────────────────────┘
               Guard loop (every 5 s): marks positions, Zero-Loss breakeven at +1.5 %,
               trailing stop, stop exits, dashboard emergency halt.
```

| Path | Purpose |
|---|---|
| `config.py` | Settings from env / `.env` (validated; live mode needs an explicit confirmation phrase) |
| `core/state.py` | Pydantic v2 schemas shared by agents |
| `core/coordinator.py` | Orchestrator, consensus gate, guard loop, circuit breakers |
| `core/market_data.py` | Multi-timeframe OHLCV from GeckoTerminal |
| `exchange_connector.py` | `ExchangeConnector` (CCXT async futures), `SolanaDEXConnector` (Jupiter + Jito), `SolanaRPC` |
| `agents/onchain_agent.py` | Whale balance tracking (RPC polling or WebSocket `accountSubscribe`) → ACCUMULATION / DISTRIBUTION |
| `agents/technical_agent.py` | EMA 50/200, Wilder RSI 14, ATR 14, order-book imbalance on 15m/1h/4h → confidence score |
| `agents/risk_agent.py` | Fractional Kelly (capped 1–2 %), VaR, drawdown/daily-loss breakers, breakeven protocol |
| `agents/execution_agent.py` | Paper / live Jupiter swaps / CEX orders with exchange-side stops |
| `database/` | SQLAlchemy 2.0 async models; TimescaleDB hypertables auto-created on Postgres |
| `dashboard.py` | Streamlit monitoring + emergency halt + risk/Kelly sliders |

## MEXC

`EXECUTION_VENUE=mexc` switches prices, candles, order-book imbalance and fills to MEXC spot (`SOL/USDT`, `JUP/USDT`).

* **Paper on MEXC** (`TRADING_MODE=paper`): fills are simulated by walking the live MEXC order book; no keys needed.
* **Live on MEXC** (`TRADING_MODE=live`, `LIVE_TRADING_CONFIRM=...`, `MEXC_API_KEY/SECRET`): real market orders.
  `INITIAL_CAPITAL_USD` is the budget the bot may use and must be ≤ `MAX_LIVE_CAPITAL_USD`; every buy also checks
  free USDT. Create the API key with spot-trading permission only (no withdrawals) and bind it to your IP.
* **MEXC spot has no stop orders in its API.** Stops, breakeven and trailing are enforced by the engine's 5-second
  guard loop, so they only protect you while `main.py` is running and online.

## Recording videos of the dashboard

`record.bat` (or `python recorder.py live 5` / `python recorder.py timelapse 24`) records **only the dashboard
page** in its own hidden browser: no desktop, other windows, tabs or address bar, so private things never end up in
the video. The Streamlit toolbar and the system-log panel are hidden. `live` = real-time video with a slow scroll;
`timelapse` = one frame every 30 s, played back fast. Output: `recordings/*.mp4` (1920×1080, H.264).
First run installs `requirements-record.txt` and Playwright's Chromium (~200 MB).

## TradingView

`tradingview/apex_strategy.pine` is the same technical + risk logic as a Pine Script v5 strategy: BUY labels,
stop/TP1/TP2 lines, a live trade-plan table, `alert()` messages for BUY / TP1 / exit, and TradingView's Strategy
Tester for backtests. On-chain flow and order-book imbalance are not available there (treated as neutral), so its
confidence can differ slightly from the bot's. Use a 1h chart, e.g. `MEXC:SOLUSDT`.

## Run on a VPS (24/7, independent of your own internet)

On a fresh Ubuntu 22.04 or 24.04 server, as root:
```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/khodabandelumahdi5-max/apex-trade-ai/main/deploy/install.sh
bash install.sh
```
- **Prompts.** It asks for the dashboard user/password, the Telegram bot token, your Telegram id (for the admin
  panel), the Stars price and a support contact. These are typed on the server only, into `/opt/alphaduel/.env`
  (chmod 600). It generates `DB_ENCRYPTION_KEY`.
- **What it installs:** system updates, Python venv, firewall (SSH/80/443 only), and HTTPS through Caddy on
  `<ip>.sslip.io` and `app.<ip>.sslip.io`.
- **Services** (systemd: start on boot, restart on crash):
  - `alphaduel-engine`: the trading engine, paper mode by default;
  - `alphaduel-dashboard`: the Streamlit dashboard;
  - `alphaduel-bot`: the Telegram bot and the Mini App.
- **Health check.** At the end it checks every service, its restarts, its logs and the local ports, and prints
  OK/FAIL.
- **Day to day:**
  - Logs: `journalctl -u alphaduel-bot -f`.
  - Update: `bash /opt/alphaduel/deploy/update.sh`.
  - Back up `.env` and `apex.db`.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # set HELIUS_API_KEY at minimum
python main.py                # engine
streamlit run dashboard.py    # dashboard (same DATABASE_URL)
python signals.py             # current trade plan: signal, entry, stop, TP1, TP2, size (no orders)
pytest -q                     # unit tests
```

## Accounts and encrypted API keys (groundwork for multi-user)

`database/models.py` has `User` and `ExchangeAPIKey` tables. They are not used by the single-user bot yet.
- API key and secret are encrypted with Fernet (`database/secure.py`) using `DB_ENCRYPTION_KEY` from `.env`.
  There is no random-key fallback: a missing key is an error, because a new random key on each start would make
  every stored secret unreadable.
- Generate a key with `python -m database.secure genkey`. Rotate it with `python -m database.secure rotate`
  (new key first in the comma-separated list).
- Passwords are hashed with scrypt (`hash_password` / `verify_password`), never stored in plain text.
- Keys that declare withdrawal permission are refused. MEXC cannot report a key's permissions, so create
  trade-only keys with an IP whitelist.

## USDT subscriptions (billing/)

`python -m billing adduser EMAIL` · `invoice EMAIL [DAYS]` · `check` · `status EMAIL` (settings in `.env.example`).
- Only the official USDT TRC20 contract (`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`) counts. A fake token named "USDT"
  is ignored. Only confirmed (solidified) transfers to your address are read.
- TRC20 has no memo field, so each invoice gets a unique amount (price + 0.01 … 0.99). The customer must send
  exactly that amount. An amount stays reserved for 3 days, so a late payment cannot pay someone else's invoice.
- Each transaction id can pay one invoice only, once. Paying early adds to the remaining time.
- Unmatched payments (wrong amount, after expiry) are logged for manual handling.
- An expired subscription pauses trading (`subscription_allows_trading`). It does not disable the account.
- Not built on purpose: running bots on customers' exchange accounts. See "Things to know".

## AlphaDuel: 21-day discipline challenge (competition/, tgbot/, webapp/)

Paper trading with the same virtual balance for everyone at live MEXC prices: BUY at the ask, SELL at the bid,
plus fees. No customer exchange keys, and no deposits mid-contest to inflate ROI.

**Rules** (set per challenge; 0 turns a rule off):

| Rule | Default |
|---|---|
| Stop-loss on every buy; max risk per trade, fees included | 1 % of equity |
| Daily loss (UTC day) above the limit | 3 %, disqualified |
| Drawdown from the peak above the limit | 10 %, disqualified |
| Losing round trips in a row before a pause | 3, then a 24 h pause |
| Entries per day | 5 |

- Stops can only be raised. Disqualification closes all positions at the bid.
- A refused rule-breaking order counts as a rule break. Discipline score = 100 − 10 × rule breaks.
- Ranking: discipline, then ROI, then lower drawdown, then who joined first. The first tick after the end time is final.
- Joining needs an active subscription. Prizes are a fixed text set by the organiser. There is deliberately no
  entry-fee prize pool: paid entry plus a prize pool counts as gambling in many countries.

**Run:**
- On Windows, double-click `bot.bat`. Elsewhere, run `python -m tgbot` (long polling). Either starts the Telegram
  bot (@alphaduellivebot; `BOT_USERNAME` makes it refuse any other bot's token).
  - At start-up it sets the bot's command menu, description and Mini App button by itself.
  - Commands: /subscribe, /status, /challenges, /join, /buy ID SYMBOL USD STOP, /sell, /stop, /me, /board.
  - Every minute it matches USDT payments and ticks every live challenge (stops, rules, ranking).
  - It sends stop-hit, near-stop, pause and disqualification messages.
  - Run one copy only. Do not also run `python -m competition run`.
- The Mini App is a full app inside Telegram, served by the same `python -m tgbot` process on
  127.0.0.1:8080. It shares the bot's write lock and price cache.
  - **Trade tab:** join a challenge, see live prices and stats, and see your equity chart.
  - **Buying:** a stop is required. Buttons set a −2/−3/−5 % stop. A live preview shows the loss if the stop is hit.
    "max size" calculates the largest buy that stays within the risk rule.
  - **Open positions:** move the stop to breakeven, raise it, or sell all.
  - **Leaderboard tab** and **Account tab** (USDT invoice with copy buttons and an expiry countdown).
  - Users are verified with Telegram's signed initData.
  - It needs HTTPS. The VPS installer serves it at `https://app.<your-host>`. In @BotFather set the menu button to
    that URL.
  - `python -m webapp` runs the page alone in read-only mode (no trading), for testing.
- **Telegram Stars** (`STARS_PRICE` in .env): inside Telegram the subscription is paid with Stars. Telegram
  requires Stars for digital goods in bots and Mini Apps.
  - Flow: `/subscribe` in the bot or "Pay ⭐" in the app → Telegram's own payment sheet → pre-checkout check
    (payer, price, payload) → the subscription is extended once per charge id.
  - The admin panel lists Stars payments and can refund through Telegram (the paid days are taken back).
  - `/paysupport` and `/terms` are built in. Withdrawing Stars is done by the bot owner through Telegram/Fragment
    under their rules and eligibility.
- **Outside Telegram**, the same app works in any browser and can be installed to the home screen as a PWA.
  - E-mail and password sign-in: scrypt hashes, hashed bearer tokens, lock-out after 5 failed attempts.
  - In-app account deletion. `/privacy` and `/terms` are templates to complete.
  - `mobile/` wraps the app with Capacitor for Android and iOS builds. Read `mobile/README.md` before submitting
    to a store: payments, contests and developer-account rules.
- **Admin panel** (tab visible only to ADMIN_TELEGRAM_IDS / ADMIN_EMAILS from .env):
  - Overview: users, subscriptions, revenue, pending payments.
  - Challenges: create with all rules, edit the prize, end now.
  - Users: search, add or remove subscription days, disable.
  - Payments: mark an unmatched payment paid with its Tronscan tx hash (each tx can pay once).
  - An audit log records every admin action.
- `python -m competition create "AlphaDuel 21-day" SOL,JUP 21 "prize text"` creates a challenge (admin CLI).

## Things to know before trusting it

- Selling access to a strategy, or trading other people's accounts, is regulated as investment advice or
  portfolio management in most countries. Tether can freeze USDT on any address. Check your own legal
  situation before taking payments.

- **No strategy is loss-free.** "Zero-Loss" moves the stop to entry after +1.5 %; gaps, slippage and fees can
  still produce a loss (set `BREAKEVEN_BUFFER_PCT` to cover fees). Before breakeven, each trade risks up to 1 %.
- **Nothing here is backtested yet.** The 70 % win rate is a target shown on the dashboard, not a measured result.
- **Whale detection needs a private RPC.** The public mainnet RPC rejects `getTokenLargestAccounts` (HTTP 429).
  Without `HELIUS_API_KEY` or `WHALE_WALLETS` the on-chain agent reports an error and, in `strict` mode,
  no trade can pass the consensus gate. The agent also needs ~10 minutes of history after each start.
- **Paper/DEX stops are software stops** enforced by the guard loop — they do not protect you while the engine
  is stopped. CEX positions get exchange-side reduce-only stops.
- Jupiter's old `quote-api.jup.ag/v6` host no longer resolves; the default is `lite-api.jup.ag/swap/v1`.
- Binance returns HTTP 451 from restricted regions; Bybit may return 403 — check your jurisdiction.
- Live mode requires `TRADING_MODE=live` **and** `LIVE_TRADING_CONFIRM=I_UNDERSTAND_REAL_FUNDS_AT_RISK`.
  Use a dedicated, low-balance wallet.

## Backtest

### One year on MEXC (2025-09-30 → 2026-09-30, 1h, bearish year)

`python -m backtest.engine --source mexc --days 365` (first ~35 days are indicator warm-up):

| Symbol | Variant | Trades | Win rate | Return | Max DD | Buy & hold (same window) |
|---|---|---|---|---|---|---|
| SOL | any (no target was ever reached) | 3 | 0 % | −1.28 % | 1.3 % | −23.4 % (DD 64 %) |
| JUP | no targets (old) | 7 | 14 % | −2.12 % | 3.6 % | −4.3 % (DD 63 %) |
| JUP | TP1 1.5R 50 % + TP2 3R (default) | 5 | 20 % | −2.06 % | 3.2 % | |
| JUP | TP 2R full exit | 6 | 17 % | −1.96 % | 3.2 % | |

The trend filter kept the bot almost entirely out of a 60 %+ crash (exposure < 1 % of the time), which is what
the risk layer is for, but it still has **no profitable edge**, and 3–7 trades a year are far too few to tell
the target variants apart. Treat take-profit targets as risk management, not as a source of profit.

### Six months on GeckoTerminal (earlier run)

```bash
python -m backtest.data 180     # cache 180 days of 1h/15m candles (GeckoTerminal public limit)
python -m backtest.engine       # runs the technical + risk logic through the live agent functions
```

Result on 2026-03-31 → 2026-09-26 (1h bars, 10 bps fee + 5 bps slippage per side, $10k per symbol):

| Symbol | Variant | Trades | Win rate | Profit factor | Return | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|
| SOL | default (thr .65, breakeven on) | 20 | 35 % | 0.79 | −0.89 % | 3.9 % | +39.9 % (DD 38 %) |
| SOL | no breakeven | 5 | 20 % | 0.30 | −1.61 % | 4.8 % | |
| JUP | default | 11 | 55 % | 0.43 | −2.49 % | 3.6 % | +39.6 % (DD 46 %) |
| JUP | no breakeven | 3 | 0 % | 0.00 | −2.82 % | 4.2 % | |

**The technical + risk layer has no edge on this sample**: it loses slightly while buy-and-hold gained ~40 %.
Risk control works as designed (max drawdown ≤ 5 %, losses ≈ the 1 % budget), and the breakeven protocol
reduces losses versus a fixed stop, but it does not create profit. Do not trade this live. On-chain and
order-book signals are not in the backtest (no history), and 180 days is a short, single-regime sample.
