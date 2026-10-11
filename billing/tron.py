"""Read incoming USDT (TRC20) transfers to one TRON address from TronGrid.

Only the official USDT contract is accepted: anyone can deploy a TRC20 token called "USDT", so checking the
token symbol or decimals (as earlier drafts did) lets fake tokens pay invoices.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID = "https://api.trongrid.io"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_tron_address(addr: str) -> bool:
    """Base58Check with the 0x41 mainnet prefix (34 characters, starts with T)."""
    if not isinstance(addr, str) or len(addr) != 34 or not addr.startswith("T"):
        return False
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    raw = n.to_bytes(25, "big")
    payload, checksum = raw[:21], raw[21:]
    return payload[0] == 0x41 and hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


@dataclass(frozen=True)
class Transfer:
    tx_id: str
    to: str
    amount_units: int          # USDT × 10^6
    at: datetime


def parse_transfers(payload: dict, address: str) -> list[Transfer]:
    out: list[Transfer] = []
    for tx in payload.get("data", []):
        info = tx.get("token_info") or {}
        if (info.get("address") != USDT_TRC20_CONTRACT or tx.get("type") != "Transfer"
                or tx.get("to") != address or int(info.get("decimals", -1)) != 6):
            continue
        try:
            value = int(tx["value"])
            at = datetime.fromtimestamp(int(tx["block_timestamp"]) / 1000, tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Transfer(tx_id=str(tx["transaction_id"]), to=address, amount_units=value, at=at))
    return out


class TronGridClient:
    def __init__(self, address: str, api_key: str | None = None, base_url: str = TRONGRID, timeout: float = 15):
        if not is_tron_address(address):
            raise ValueError(f"not a valid TRON address: {address!r}")
        self.address = address
        self.base_url = base_url.rstrip("/")
        self._headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def incoming_usdt(self, since: datetime, max_pages: int = 20) -> list[Transfer]:
        """Confirmed (solidified) USDT transfers to the address since `since`, oldest pages included."""
        url = f"{self.base_url}/v1/accounts/{self.address}/transactions/trc20"
        params: dict[str, str | int] = {
            "only_confirmed": "true", "only_to": "true", "limit": 200, "order_by": "block_timestamp,asc",
            "contract_address": USDT_TRC20_CONTRACT, "min_timestamp": int(since.timestamp() * 1000)}
        transfers: list[Transfer] = []
        async with aiohttp.ClientSession(timeout=self._timeout, headers=self._headers) as http:
            for _ in range(max_pages):
                payload = await self._get(http, url, params)
                if not payload.get("success", True):
                    raise RuntimeError(f"TronGrid error: {payload.get('error')}")
                transfers += parse_transfers(payload, self.address)
                fingerprint = (payload.get("meta") or {}).get("fingerprint")
                if not fingerprint:
                    break
                params["fingerprint"] = fingerprint
                await asyncio.sleep(0.5)                  # keyless TronGrid allows only a few requests/second
        return transfers

    @staticmethod
    async def _get(http: aiohttp.ClientSession, url: str, params: dict, retries: int = 4) -> dict:
        for attempt in range(retries + 1):
            async with http.get(url, params=params) as resp:
                if resp.status == 429 and attempt < retries:
                    await asyncio.sleep(2 ** attempt)     # rate limited: 1, 2, 4, 8 s
                    continue
                resp.raise_for_status()
                return await resp.json()
        raise RuntimeError("unreachable")
