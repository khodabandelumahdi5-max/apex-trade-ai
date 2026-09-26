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
_TTL = {"15m": 120, "1h": 300, "4h": 900, "1d": 3600}


class GeckoTerminalClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._http = HttpClient("geckoterminal", rate=1, per=3.0, retries=5)
        self._pools: dict[str, str] = {}
        self._pool_lock = asyncio.Lock()
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    async def top_pool(self, mint: str) -> str:
        """Most liquid pool for a token (by USD reserve)."""
        async with self._pool_lock:
            return await self._top_pool_locked(mint)

    async def _top_pool_locked(self, mint: str) -> str:
        if mint not in self._pools:
            data = await self._http.request("GET", f"{self.base}/networks/solana/tokens/{mint}/pools",
                                            params={"page": 1})
            pools: list[dict[str, Any]] = (data or {}).get("data") or []
            if not pools:
                raise ConnectorError(f"no GeckoTerminal pools for {mint}")
            best = max(pools, key=lambda p: float(p["attributes"].get("reserve_in_usd") or 0))
            self._pools[mint] = best["attributes"]["address"]
            logger.info("GeckoTerminal pool for {}: {} ({})", mint[:6], self._pools[mint],
                        best["attributes"].get("name"))
        return self._pools[mint]

    async def ohlcv(self, mint: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe}")
        key = (mint, timeframe)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < _TTL[timeframe]:
            return cached[1]
        pool = await self.top_pool(mint)
        endpoint, agg = TIMEFRAMES[timeframe]
        data = await self._http.request(
            "GET", f"{self.base}/networks/solana/pools/{pool}/ohlcv/{endpoint}",
            params={"aggregate": agg, "limit": min(limit, 1000), "currency": "usd", "token": mint})
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            raise ConnectorError(f"empty OHLCV for {mint} {timeframe}")
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        self._cache[key] = (time.monotonic(), df)
        return df

    async def close(self) -> None:
        await self._http.close()
