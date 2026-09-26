import numpy as np
import pandas as pd
import pytest

from agents.technical_agent import analyse_frame, confidence_score, rsi_pullback_score, rsi_wilder
from core.state import MarketDataState, OrderBookLevel
from exchange_connector import order_book_imbalance


def frame(trend=0.001, n=400, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(trend, 0.005, n)))
    return pd.DataFrame({"timestamp": range(n), "open": close, "high": close * 1.003, "low": close * 0.997,
                         "close": close, "volume": 1.0})


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi_wilder(up).iloc[-1] == 100.0
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert rsi_wilder(down).iloc[-1] == pytest.approx(0.0)
    r = rsi_wilder(frame()["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_trend_scores_sign():
    assert analyse_frame(frame(0.002), "1h").trend_score > 0
    assert analyse_frame(frame(-0.002), "1h").trend_score < 0


def test_confidence_range_and_order():
    bull = {tf: analyse_frame(frame(0.002), tf) for tf in ("15m", "1h", "4h")}
    bear = {tf: analyse_frame(frame(-0.002), tf) for tf in ("15m", "1h", "4h")}
    assert 0 <= confidence_score(bear, -1) < confidence_score(bull, 1) <= 1


def test_rsi_pullback_score():
    assert rsi_pullback_score(50) == 1 and rsi_pullback_score(75) == 0 and rsi_pullback_score(25) == 0


def test_obi_and_book_validation():
    bids = [OrderBookLevel(price=99, size=10), OrderBookLevel(price=98, size=10)]
    asks = [OrderBookLevel(price=101, size=1), OrderBookLevel(price=102, size=1)]
    assert order_book_imbalance(bids, asks) > 0.8
    with pytest.raises(ValueError):
        MarketDataState(symbol="X", price=100, bid=101, ask=100)
    with pytest.raises(ValueError):
        MarketDataState(symbol="X", price=100, bids=list(reversed(bids)))
