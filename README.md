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

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # set HELIUS_API_KEY at minimum
python main.py                # engine
streamlit run dashboard.py    # dashboard (same DATABASE_URL)
pytest -q                     # unit tests
```

## Things to know before trusting it

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
