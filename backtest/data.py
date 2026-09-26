"""Download and cache historical OHLCV from GeckoTerminal (paginated with before_timestamp)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pandas as pd
from loguru import logger

from core.market_data import TIMEFRAMES, GeckoTerminalClient
from exchange_connector import FatalConnectorError, HttpClient

MAX_PUBLIC_DAYS = 179  # GeckoTerminal public API returns 401 beyond 180 days

CACHE_DIR = Path(os.getenv("BACKTEST_CACHE", Path(__file__).parent / "data"))


async def fetch_history(client: GeckoTerminalClient, mint: str, timeframe: str, days: int) -> pd.DataFrame:
    """Walk backwards 1000 bars at a time until `days` of history (or the API's limit) is reached."""
    pool = await client.top_pool(mint)
    endpoint, agg = TIMEFRAMES[timeframe]
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=min(days, MAX_PUBLIC_DAYS))
    frames: list[pd.DataFrame] = []
    before: int | None = None
    while True:
        params: dict[str, object] = {"aggregate": agg, "limit": 1000, "currency": "usd", "token": mint}
        if before is not None:
            params["before_timestamp"] = before
        try:
            data = await client._http.request(
                "GET", f"{client.base}/networks/solana/pools/{pool}/ohlcv/{endpoint}", params=params)
        except FatalConnectorError as exc:
            if "401" in str(exc) and frames:
                logger.warning("history limit reached: {}", str(exc)[:120])
                break
            raise
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            break
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        frames.append(df)
        oldest = int(df["timestamp"].min())
        if pd.Timestamp(oldest, unit="s", tz="UTC") <= cutoff or len(rows) < 1000 or oldest == before:
            break
        before = oldest
    if not frames:
        raise RuntimeError(f"no history for {mint} {timeframe}")
    out = pd.concat(frames).drop_duplicates("timestamp")
    out["timestamp"] = pd.to_datetime(out["timestamp"], unit="s", utc=True)
    out = out[out["timestamp"] >= cutoff].sort_values("timestamp").reset_index(drop=True)
    return out.astype({c: float for c in ("open", "high", "low", "close", "volume")})


async def load(mint: str, symbol: str, timeframe: str, days: int, refresh: bool = False) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{symbol}_{timeframe}_{days}d.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["timestamp"])
    client = GeckoTerminalClient("https://api.geckoterminal.com/api/v2")
    # bulk download: stay well under the public ~30 req/min limit, retry patiently on 429
    client._http = HttpClient("geckoterminal-bulk", rate=1, per=6.0, retries=8)
    try:
        df = await fetch_history(client, mint, timeframe, days)
    finally:
        await client.close()
    df.to_csv(path, index=False)
    logger.info("{} {}: {} bars {} → {}", symbol, timeframe, len(df), df.timestamp.iloc[0], df.timestamp.iloc[-1])
    return df


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    async def main() -> None:
        for sym, mint in (("SOL", "So11111111111111111111111111111111111111112"),
                          ("JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN")):
            for tf in ("15m", "1h"):
                await load(mint, sym, tf, int(sys.argv[1]) if len(sys.argv) > 1 else 180, refresh=False)
    asyncio.run(main())
