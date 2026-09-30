"""OHLCV candles for Solana tokens from GeckoTerminal (free tier: ~30 req/min, throttled to 20)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pandas as pd
from loguru import logger

from exchange_connector import ConnectorError, HttpClient

# our timeframe → (GeckoTerminal endpoint, aggregate)
TIMEFRAMES: dict[str, tuple[str, int]] = {
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "4h": ("hour", 4),
    "1d": ("day", 1),
}
MIN_BARS = 250        # enough for EMA200 plus a margin
MAX_POOL_TRIES = 3
_TTL = {"15m": 120, "1h": 300, "4h": 900, "1d": 3600}


class GeckoTerminalClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._http = HttpClient("geckoterminal", rate=1, per=3.0, retries=5)
        self._pools: dict[str, str] = {}
        self._ranked: dict[str, list[tuple[str, str]]] = {}
        self._pool_lock = asyncio.Lock()
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    async def ranked_pools(self, mint: str) -> list[tuple[str, str]]:
        """Pools for a token ranked by 24h USD volume: [(address, name), ...].

        Ranking by reserves alone picks freshly launched pools with almost no candle history."""
        async with self._pool_lock:
            if mint not in self._ranked:
                data = await self._http.request("GET", f"{self.base}/networks/solana/tokens/{mint}/pools",
                                                params={"page": 1})
                pools: list[dict[str, Any]] = (data or {}).get("data") or []
                if not pools:
                    raise ConnectorError(f"no GeckoTerminal pools for {mint}")
                pools.sort(key=lambda p: float((p["attributes"].get("volume_usd") or {}).get("h24") or 0),
                           reverse=True)
                self._ranked[mint] = [(p["attributes"]["address"], p["attributes"].get("name") or "?")
                                      for p in pools]
            return self._ranked[mint]

    async def top_pool(self, mint: str) -> str:
        """Pool currently used for `mint` (the highest-volume pool with enough history)."""
        return self._pools.get(mint) or (await self.ranked_pools(mint))[0][0]

    async def _fetch(self, pool: str, mint: str, timeframe: str, limit: int) -> pd.DataFrame:
        endpoint, agg = TIMEFRAMES[timeframe]
        data = await self._http.request(
            "GET", f"{self.base}/networks/solana/pools/{pool}/ohlcv/{endpoint}",
            params={"aggregate": agg, "limit": min(limit, 1000), "currency": "usd", "token": mint})
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    async def ohlcv(self, mint: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe}")
        key = (mint, timeframe)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < _TTL[timeframe]:
            return cached[1]
        ranked = await self.ranked_pools(mint)
        preferred = self._pools.get(mint)
        order = ([p for p in ranked if p[0] == preferred] + [p for p in ranked if p[0] != preferred])[:MAX_POOL_TRIES]
        best: pd.DataFrame | None = None
        for address, name in order:
            df = await self._fetch(address, mint, timeframe, limit)
            if best is None or len(df) > len(best):
                best = df
            if len(df) >= min(limit, MIN_BARS):
                if self._pools.get(mint) != address:
                    self._pools[mint] = address
                    logger.info("GeckoTerminal pool for {}: {} ({})", mint[:6], address, name)
                break
            logger.debug("pool {} ({}) has only {} {} bars; trying next", address[:6], name, len(df), timeframe)
        if best is None or best.empty:
            raise ConnectorError(f"empty OHLCV for {mint} {timeframe}")
        self._cache[key] = (time.monotonic(), best)
        return best

    async def close(self) -> None:
        await self._http.close()
