"""On-chain whale intelligence: tracks whale token balances over RPC polling and/or WebSocket
account subscriptions and classifies the phase as ACCUMULATION / DISTRIBUTION / NEUTRAL."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

from agents.base_agent import BaseAgent
from config import Settings, WatchToken
from core.state import AgentHealth, OnChainState
from database import repository as repo
from database.models import WhaleTransaction
from exchange_connector import ConnectorError, FatalConnectorError, SolanaRPC

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PTsBQMfj45L4pcDzr")


class HolderDiscoveryError(FatalConnectorError):
    """Configuration problem: no way to discover whale accounts (not retried)."""


@dataclass
class _Tracked:
    owner: str
    balance: float
    updated: float


@dataclass
class _TokenBook:
    accounts: dict[str, _Tracked] = field(default_factory=dict)   # token account → holder
    history: deque[tuple[float, dict[str, float]]] = field(default_factory=lambda: deque(maxlen=720))
    refreshed_at: float = 0.0
    ws_subs: dict[int, str] = field(default_factory=dict)


class OnChainAgent(BaseAgent[OnChainState]):
    name = "onchain"
    HOLDER_REFRESH_SEC = 3600
    WINDOW_SEC = 3600
    MIN_WINDOW_SEC = 600
    WHALE_EVENT_MIN_USD = 25_000

    def __init__(self, settings: Settings, rpc: SolanaRPC) -> None:
        super().__init__(timeout=120)
        self.settings = settings
        self.rpc = rpc
        self.books: dict[str, _TokenBook] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._prices: dict[str, float] = {}
        self._symbols: dict[str, str] = {}

    # ------------------------------------------------------------ discovery
    async def _refresh_holders(self, token: WatchToken, book: _TokenBook) -> None:
        candidates: list[str] = []
        try:
            largest = await self.rpc.call("getTokenLargestAccounts", [token.mint, {"commitment": "confirmed"}])
            candidates = [a["address"] for a in (largest or {}).get("value", [])]
        except ConnectorError as exc:
            # The public mainnet RPC rejects this method (HTTP 429); Helius/QuickNode serve it.
            if not self.settings.whale_wallets:
                raise HolderDiscoveryError(
                    f"getTokenLargestAccounts refused by RPC ({str(exc)[:80]}); "
                    "set HELIUS_API_KEY (or a private RPC) or WHALE_WALLETS") from exc
            self.log.warning("{}: top-holder discovery refused by RPC; tracking WHALE_WALLETS only", token.symbol)
        for wallet in self.settings.whale_wallets:
            res = await self.rpc.call("getTokenAccountsByOwner",
                                      [wallet, {"mint": token.mint}, {"encoding": "jsonParsed"}])
            candidates += [a["pubkey"] for a in (res or {}).get("value", [])]
        infos = await self._parsed_accounts(list(dict.fromkeys(candidates)))
        explicit = set(self.settings.whale_wallets)
        tracked: dict[str, _Tracked] = {}
        now = time.time()
        for addr, info in infos.items():
            owner = info["owner"]
            # Skip PDAs (AMM vaults, CEX program accounts): off-curve owners are programs, not whales.
            if owner not in explicit and not Pubkey.from_string(owner).is_on_curve():
                continue
            tracked[addr] = _Tracked(owner=owner, balance=info["amount"], updated=now)
            if len(tracked) >= self.settings.whale_top_holders + len(explicit):
                break
        book.accounts, book.refreshed_at = tracked, now
        self.log.info("{}: tracking {} whale token accounts", token.symbol, len(tracked))
        if self._ws is not None and not self._ws.closed:
            await self._subscribe(book)

    async def _parsed_accounts(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(addresses), 100):
            chunk = addresses[i:i + 100]
            res = await self.rpc.call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
            for addr, acc in zip(chunk, (res or {}).get("value", [])):
                parsed = self._parse_token_account(acc)
                if parsed:
                    out[addr] = parsed
        return out

    @staticmethod
    def _parse_token_account(acc: dict[str, Any] | None) -> dict[str, Any] | None:
        try:
            if not acc or acc.get("owner") not in TOKEN_PROGRAMS:
                return None
            info = acc["data"]["parsed"]["info"]
            return {"owner": info["owner"], "amount": float(info["tokenAmount"]["uiAmountString"] or 0)}
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------ websocket stream
    async def startup(self) -> None:
        if self.settings.enable_ws_stream and self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_loop(), name="onchain-ws")

    async def shutdown(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_task = None

    async def _subscribe(self, book: _TokenBook) -> None:
        assert self._ws is not None
        for sub_id in list(book.ws_subs):
            await self._ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "accountUnsubscribe",
                                      "params": [sub_id]})
        book.ws_subs.clear()
        for addr in book.accounts:
            # request id carries the address so the subscription id can be mapped back
            await self._ws.send_json({"jsonrpc": "2.0", "id": f"sub:{addr}", "method": "accountSubscribe",
                                      "params": [addr, {"encoding": "jsonParsed", "commitment": "confirmed"}]})

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.settings.solana_ws_url, heartbeat=30) as ws:
                        self._ws, backoff = ws, 1.0
                        self.log.info("WebSocket connected: {}", self.settings.solana_ws_url.split("?")[0])
                        for book in self.books.values():
                            await self._subscribe(book)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._on_ws_message(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("WebSocket error: {!r}; reconnecting in {:.0f}s", exc, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _on_ws_message(self, msg: dict[str, Any]) -> None:
        if isinstance(msg.get("id"), str) and msg["id"].startswith("sub:") and "result" in msg:
            addr = msg["id"][4:]
            for book in self.books.values():
                if addr in book.accounts:
                    book.ws_subs[int(msg["result"])] = addr
            return
        if msg.get("method") != "accountNotification":
            return
        params = msg["params"]
        sub_id = params["subscription"]
        for mint, book in self.books.items():
            addr = book.ws_subs.get(sub_id)
            if not addr:
                continue
            parsed = self._parse_token_account(params["result"]["value"])
            if not parsed:
                return
            tracked = book.accounts[addr]
            delta = parsed["amount"] - tracked.balance
            tracked.balance, tracked.updated = parsed["amount"], time.time()
            await self._maybe_record_event(mint, tracked.owner, delta, method="ws_account")
            return

    # ------------------------------------------------------------ analysis
    async def _maybe_record_event(self, mint: str, owner: str, delta_units: float, method: str) -> None:
        usd = delta_units * self._prices.get(mint, 0.0)
        if abs(usd) < self.WHALE_EVENT_MIN_USD:
            return
        await repo.record(WhaleTransaction(
            token=self._symbols.get(mint, mint[:8]), net_flow_usd=usd,
            sentiment="ACCUMULATION" if usd > 0 else "DISTRIBUTION",
            confidence=1.0, wallet=owner, method=method))
        self.log.info("🐋 {} {} {:+,.0f} USD ({})", self._symbols.get(mint, mint[:6]), owner[:6], usd, method)

    async def _poll_balances(self, token: WatchToken, book: _TokenBook) -> None:
        stale = [a for a, t in book.accounts.items()
                 if not self.settings.enable_ws_stream or time.time() - t.updated > 120]
        if not stale:
            return
        infos = await self._parsed_accounts(stale)
        now = time.time()
        for addr, info in infos.items():
            tracked = book.accounts[addr]
            delta = info["amount"] - tracked.balance
            tracked.balance, tracked.updated = info["amount"], now
            if delta:
                await self._maybe_record_event(token.mint, tracked.owner, delta, method="rpc_poll")

    async def process(self, token: WatchToken, price: float) -> OnChainState:  # type: ignore[override]
        self._prices[token.mint], self._symbols[token.mint] = price, token.symbol
        book = self.books.setdefault(token.mint, _TokenBook())
        if not book.accounts or time.time() - book.refreshed_at > self.HOLDER_REFRESH_SEC:
            await self._refresh_holders(token, book)
        else:
            await self._poll_balances(token, book)

        now = time.time()
        snapshot = {a: t.balance for a, t in book.accounts.items()}
        book.history.append((now, snapshot))
        base_ts, base = next(((ts, s) for ts, s in book.history if now - ts <= self.WINDOW_SEC),
                             book.history[-1])
        common = snapshot.keys() & base.keys()
        base_total = sum(base[a] for a in common)
        delta_units = sum(snapshot[a] - base[a] for a in common)
        span = now - base_ts

        if span < self.MIN_WINDOW_SEC or base_total <= 0:
            state = OnChainState(token=token.symbol, net_flow=0.0, whale_sentiment="UNKNOWN", confidence=0.0,
                                 tracked_wallets=len(book.accounts), observations=len(book.history),
                                 method="holders_delta")
            await self.set_health(AgentHealth.HEALTHY,
                                  f"warming up {token.symbol}: {span / 60:.0f}/{self.MIN_WINDOW_SEC / 60:.0f} min")
            return state

        pct = delta_units / base_total * 100
        thr = self.settings.onchain_flow_threshold_pct
        sentiment = "ACCUMULATION" if pct >= thr else "DISTRIBUTION" if pct <= -thr else "NEUTRAL"
        confidence = min(1.0, abs(pct) / (3 * thr))
        state = OnChainState(token=token.symbol, net_flow=delta_units * price, net_flow_pct=pct,
                             whale_sentiment=sentiment, confidence=confidence,
                             tracked_wallets=len(book.accounts), observations=len(book.history),
                             method="ws_account" if self._ws is not None else "holders_delta")
        await repo.record(WhaleTransaction(
            timestamp=datetime.now(timezone.utc), token=token.symbol, net_flow_usd=state.net_flow,
            sentiment=sentiment, confidence=confidence, method=f"window_{int(span // 60)}m"))
        return state
