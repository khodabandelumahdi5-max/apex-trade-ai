"""Check that the MEXC API key in .env works. Read-only: shows balances, places no orders.

    python check_mexc.py
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from config import get_settings
from exchange_connector import ConnectorError, MexcSpotConnector


async def main() -> int:
    logger.remove()
    s = get_settings()
    if not (s.mexc_api_key and s.mexc_api_secret):
        print("MEXC_API_KEY / MEXC_API_SECRET are empty in .env")
        return 1
    m = MexcSpotConnector(s)
    try:
        await m.connect()
        bal = await m._call("fetch_balance")
        print("✅ MEXC key works (read access).")
        for asset in ["USDT"] + [t.symbol for t in s.watchlist]:
            b = bal.get(asset) or {}
            print(f"   {asset:<5} free {float(b.get('free') or 0):,.6f}   in orders {float(b.get('used') or 0):,.6f}")
        usdt = float((bal.get("USDT") or {}).get("free") or 0)
        if usdt < s.initial_capital_usd:
            print(f"⚠️  free USDT ({usdt:,.2f}) is below INITIAL_CAPITAL_USD ({s.initial_capital_usd:,.2f}); "
                  "buys larger than your balance will be rejected")
        print(f"   mode: {s.trading_mode}   venue: {s.execution_venue}   budget cap: ${s.max_live_capital_usd:,.2f}")
        return 0
    except ConnectorError as exc:
        print(f"❌ MEXC rejected the key: {exc}")
        print("   Check: key/secret copied fully, no spaces; IP whitelist matches your current IP (VPN changes it).")
        return 2
    finally:
        await m.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
