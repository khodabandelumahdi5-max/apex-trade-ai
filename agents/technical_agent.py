"""Multi-timeframe quantitative analysis: EMA 50/200, Wilder RSI 14, ATR 14, order-book imbalance,
combined into a single buy-confidence score in [0, 1]."""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from agents.base_agent import BaseAgent
from config import Settings, WatchToken
from core.market_data import GeckoTerminalClient
from core.state import MarketDataState, TechnicalState, TimeframeAnalysis
from exchange_connector import ConnectorError, ExchangeConnector, MexcSpotConnector, SolanaDEXConnector

TIMEFRAMES = ("15m", "1h", "4h")
WEIGHTS = {"trend_4h": 0.30, "trend_1h": 0.30, "trend_15m": 0.10, "rsi": 0.15, "obi": 0.15}
OBI_PROBE_USD = 5_000.0
OBI_NOISE_FLOOR = 5e-4  # Jupiter priceImpactPct units (fraction)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    flat = (loss == 0) & (gain == 0)
    return rsi.mask(loss == 0, 100.0).mask(flat, 50.0)


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def analyse_frame(df: pd.DataFrame, timeframe: str) -> TimeframeAnalysis:
    if len(df) < 60:
        raise ValueError(f"{timeframe}: only {len(df)} bars, need ≥ 60")
    close = df["close"].astype(float)
    e50, e200 = ema(close, 50), ema(close, 200)
    last_close, last50, last200 = float(close.iloc[-1]), float(e50.iloc[-1]), float(e200.iloc[-1])
    rsi = float(rsi_wilder(close).iloc[-1])
    atr = float(atr_wilder(df.astype({"high": float, "low": float, "close": float})).iloc[-1])
    full = len(df) >= 200
    score = (0.5 if last_close > last50 else -0.5)
    # EMA200 is unreliable with < 200 bars: only half weight
    score += (0.5 if last50 > last200 else -0.5) * (1.0 if full else 0.5)
    return TimeframeAnalysis(timeframe=timeframe, close=last_close, ema_50=last50, ema_200=last200,
                             rsi_14=min(100.0, max(0.0, rsi)), atr_14=max(0.0, atr),
                             trend_score=float(np.clip(score, -1, 1)), bars=len(df))


def rsi_pullback_score(rsi: float) -> float:
    """1.0 inside the 40–55 pullback zone, tapering to 0 at 30 and 70."""
    if 40 <= rsi <= 55:
        return 1.0
    if rsi < 40:
        return max(0.0, (rsi - 30) / 10)
    return max(0.0, (70 - rsi) / 15)


def confidence_score(frames: dict[str, TimeframeAnalysis], obi: float) -> float:
    to_unit = lambda s: (s + 1) / 2  # noqa: E731
    score = (WEIGHTS["trend_4h"] * to_unit(frames["4h"].trend_score)
             + WEIGHTS["trend_1h"] * to_unit(frames["1h"].trend_score)
             + WEIGHTS["trend_15m"] * to_unit(frames["15m"].trend_score)
             + WEIGHTS["rsi"] * rsi_pullback_score(frames["1h"].rsi_14)
             + WEIGHTS["obi"] * to_unit(obi))
    return float(np.clip(score, 0, 1))


class TechnicalAgent(BaseAgent[TechnicalState]):
    name = "technical"

    def __init__(self, settings: Settings, candles: GeckoTerminalClient, dex: SolanaDEXConnector,
                 cex: ExchangeConnector | None = None, mexc: MexcSpotConnector | None = None) -> None:
        super().__init__(timeout=120)
        self.settings, self.candles, self.dex, self.cex, self.mexc = settings, candles, dex, cex, mexc

    async def _frame(self, token: WatchToken, tf: str) -> pd.DataFrame:
        if self.mexc is not None and await self.mexc.has_symbol(token.symbol):
            rows = await self.mexc.fetch_ohlcv(token.symbol, tf, 500)
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df
        try:
            return await self.candles.ohlcv(token.mint, tf, limit=500)
        except ConnectorError as exc:
            if self.cex and token.cex_symbol:
                self.log.warning("GeckoTerminal {} {} failed ({}); using {}", token.symbol, tf, exc, self.cex.name)
                rows = await self.cex.fetch_ohlcv(token.cex_symbol, tf, 500)
                return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            raise

    async def _dex_imbalance(self, token: WatchToken, price: float) -> float:
        """Liquidity-asymmetry proxy for DEX tokens with no order book: a deeper bid side means
        selling moves the price less than buying the same notional."""
        try:
            buy_q, _, _ = await self.dex.quote_usd(token.mint, "BUY", OBI_PROBE_USD)
            sell_q, _, _ = await self.dex.quote_usd(token.mint, "SELL", OBI_PROBE_USD / price)
        except ConnectorError as exc:
            self.log.debug("OBI probe failed for {}: {}", token.symbol, exc)
            return 0.0
        buy_imp = abs(float(buy_q.get("priceImpactPct") or 0))
        sell_imp = abs(float(sell_q.get("priceImpactPct") or 0))
        # Both impacts near zero = deep book on both sides: no information, avoid ±1 from rounding noise.
        return float(np.clip((buy_imp - sell_imp) / (buy_imp + sell_imp + OBI_NOISE_FLOOR), -1, 1))

    async def process(self, token: WatchToken, market: MarketDataState) -> TechnicalState:  # type: ignore[override]
        dfs = await asyncio.gather(*(self._frame(token, tf) for tf in TIMEFRAMES))
        frames = {tf: analyse_frame(df, tf) for tf, df in zip(TIMEFRAMES, dfs)}
        obi = (market.order_book_imbalance if market.order_book_imbalance is not None
               else await self._dex_imbalance(token, market.price))
        conf = confidence_score(frames, obi)
        h1, h4 = frames["1h"], frames["4h"]

        reasons: list[str] = [f"conf={conf:.2f}", f"4h trend={h4.trend_score:+.2f}",
                              f"1h trend={h1.trend_score:+.2f}", f"1h RSI={h1.rsi_14:.1f}", f"OBI={obi:+.2f}"]
        if conf >= self.settings.technical_buy_threshold and h4.trend_score > 0 \
                and h1.ema_50 > h1.ema_200 and h1.rsi_14 < 70:
            signal = "BUY"
        elif h1.close < h1.ema_50 and h1.ema_50 < h1.ema_200:
            signal = "SELL"
            reasons.append("1h trend broken (close < EMA50 < EMA200)")
        else:
            signal = "HOLD"

        stop_distance = max(self.settings.atr_stop_multiplier * h1.atr_14, market.price * 0.005)
        stop = market.price - stop_distance
        from agents.risk_agent import take_profit_price
        tp1 = take_profit_price(market.price, stop, self.settings.tp1_r) if stop > 0 else None
        tp2 = take_profit_price(market.price, stop, self.settings.tp2_r) if stop > 0 else None
        return TechnicalState(tp1=tp1, tp2=tp2, symbol=token.symbol, ema_50=h1.ema_50, ema_200=h1.ema_200, rsi_14=h1.rsi_14,
                              atr_14=h1.atr_14, order_book_imbalance=obi, confidence=conf, signal=signal,
                              suggested_stop=stop if stop > 0 else None, timeframes=frames, reasons=reasons)
