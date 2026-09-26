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
