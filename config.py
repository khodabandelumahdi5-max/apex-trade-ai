"""Runtime configuration loaded from environment variables (.env supported)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_FUNDS_AT_RISK"


class WatchToken(BaseModel):
    """A Solana token the swarm watches. `cex_symbol` is optional (e.g. SOL/USDT:USDT)."""

    symbol: str
    mint: str
    cex_symbol: str | None = None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _parse_watchlist(raw: str) -> list[WatchToken]:
    # Format: SYMBOL:MINT[:CEX_SYMBOL],SYMBOL:MINT...
    tokens: list[WatchToken] = []
    for item in filter(None, (p.strip() for p in raw.split(","))):
        parts = item.split(":", 2)
        if len(parts) < 2:
            raise ValueError(f"Invalid WATCHLIST entry '{item}', expected SYMBOL:MINT[:CEX_SYMBOL]")
        tokens.append(WatchToken(symbol=parts[0].upper(), mint=parts[1],
                                 cex_symbol=parts[2] if len(parts) == 3 else None))
    return tokens


class Settings(BaseModel):
    # --- mode ---
    trading_mode: Literal["paper", "live"] = "paper"
    live_trading_confirm: str = ""
    initial_capital_usd: float = Field(10_000.0, gt=0)
    quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    quote_decimals: int = 6
    watchlist: list[WatchToken]
    loop_interval_sec: float = Field(60.0, ge=5)

    # --- infrastructure ---
    database_url: str = "sqlite+aiosqlite:///./apex.db"
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"
    helius_api_key: str | None = None
    enable_ws_stream: bool = False
    jupiter_api_base: str = "https://lite-api.jup.ag"
    jupiter_api_key: str | None = None
    geckoterminal_base: str = "https://api.geckoterminal.com/api/v2"
    jito_block_engine_url: str = "https://mainnet.block-engine.jito.wtf"
    use_jito: bool = True
    jito_tip_lamports: int = Field(10_000, ge=0)
    solana_private_key: str | None = None  # base58; required only in live DEX mode

    # --- CEX (optional) ---
    cex_exchange: str | None = None  # binance | bybit
    cex_api_key: str | None = None
    cex_api_secret: str | None = None
    cex_testnet: bool = True
    cex_leverage: int = Field(1, ge=1, le=5)

    # --- on-chain ---
    whale_wallets: list[str] = Field(default_factory=list)
    whale_top_holders: int = Field(15, ge=1, le=20)
    onchain_flow_threshold_pct: float = Field(0.5, gt=0)
    onchain_mode: Literal["strict", "veto"] = "strict"

    # --- technical ---
    technical_buy_threshold: float = Field(0.65, ge=0, le=1)

    # --- risk ---
    max_risk_per_trade_pct: float = Field(1.0, gt=0, le=2.0)
    kelly_fraction: float = Field(0.25, gt=0, le=1)
    max_position_pct: float = Field(25.0, gt=0, le=100)
    max_open_positions: int = Field(3, ge=1)
    max_portfolio_var_pct: float = Field(3.0, gt=0)
    var_confidence: float = Field(0.95, gt=0.5, lt=1)
    daily_loss_limit_pct: float = Field(3.0, gt=0)
    max_drawdown_pct: float = Field(10.0, gt=0)
    breakeven_trigger_pct: float = Field(1.5, gt=0)
    breakeven_buffer_pct: float = Field(0.0, ge=0)
    trailing_stop_pct: float = Field(2.0, gt=0)
    atr_stop_multiplier: float = Field(2.0, gt=0)
    max_slippage_bps: int = Field(50, ge=1, le=1000)
    max_price_impact_pct: float = Field(1.0, gt=0)
    paper_fee_bps: float = Field(10.0, ge=0)
    max_consecutive_failures: int = Field(5, ge=1)

    @field_validator("cex_exchange")
    @classmethod
    def _check_cex(cls, v: str | None) -> str | None:
        if v is not None and v.lower() not in {"binance", "binanceusdm", "bybit"}:
            raise ValueError("CEX_EXCHANGE must be binance, binanceusdm or bybit")
        return v.lower() if v else v

    @model_validator(mode="after")
    def _check_live(self) -> "Settings":
        if self.trading_mode == "live":
            if self.live_trading_confirm != LIVE_CONFIRM_PHRASE:
                raise ValueError(f"TRADING_MODE=live requires LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE}")
            if not self.solana_private_key and not (self.cex_exchange and self.cex_api_key):
                raise ValueError("Live mode needs SOLANA_PRIVATE_KEY and/or CEX credentials")
        if self.helius_api_key:
            self.solana_rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
            self.solana_ws_url = f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return self

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    raw: dict[str, object] = {
        "watchlist": _parse_watchlist(_env(
            "WATCHLIST",
            "SOL:So11111111111111111111111111111111111111112:SOL/USDT:USDT,"
            "JUP:JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        ) or ""),
        "whale_wallets": [w.strip() for w in (_env("WHALE_WALLETS", "") or "").split(",") if w.strip()],
        "enable_ws_stream": _bool("ENABLE_WS_STREAM", False),
        "use_jito": _bool("USE_JITO", True),
        "cex_testnet": _bool("CEX_TESTNET", True),
    }
    for field in Settings.model_fields:
        if field in raw:
            continue
        value = _env(field.upper())
        if value is not None:
            raw[field] = value
    return Settings(**raw)
