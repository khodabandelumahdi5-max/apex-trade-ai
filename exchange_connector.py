"""Venue connectivity: CEX futures via CCXT (async) and Solana DEX via Jupiter + Jito."""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import aiohttp
import ccxt.async_support as ccxt_async
from ccxt.base.errors import (AuthenticationError, BadSymbol, DDoSProtection, ExchangeError,
                              ExchangeNotAvailable, InsufficientFunds, InvalidOrder, NetworkError,
                              RequestTimeout)
from loguru import logger

from config import Settings
from core.state import MarketDataState, OrderBookLevel

LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = "So11111111111111111111111111111111111111112"


class ConnectorError(RuntimeError):
    """Recoverable venue error."""


class FatalConnectorError(ConnectorError):
    """Non-retryable error (auth, bad symbol, insufficient funds, rejected order)."""


class SlippageError(ConnectorError):
    pass


def order_book_imbalance(bids: list[OrderBookLevel], asks: list[OrderBookLevel]) -> float:
    """(bid_notional - ask_notional) / (bid_notional + ask_notional), in [-1, 1]."""
    bid_n = sum(l.price * l.size for l in bids)
    ask_n = sum(l.price * l.size for l in asks)
    total = bid_n + ask_n
    return 0.0 if total <= 0 else (bid_n - ask_n) / total


def walk_book(levels: list[OrderBookLevel], usd: float | None = None, units: float | None = None) -> float:
    """Average fill price of a market order that consumes `usd` (buy, walk asks) or `units` (sell, walk bids).
    If the snapshot is too thin, the remainder fills at the last visible level."""
    if not levels or (usd is None) == (units is None):
        raise ValueError("walk_book needs levels and exactly one of usd / units")
    spent = got = 0.0
    for lvl in levels:
        if usd is not None:
            take = min(lvl.price * lvl.size, usd - spent)
            spent, got = spent + take, got + take / lvl.price
            if spent >= usd:
                break
        else:
            take = min(lvl.size, units - got)
            got, spent = got + take, spent + take * lvl.price
            if got >= units:
                break
    last = levels[-1].price
    if usd is not None and spent < usd:
        got, spent = got + (usd - spent) / last, usd
    if units is not None and got < units:
        spent, got = spent + (units - got) * last, units
    return spent / got


class RateLimiter:
    """Simple async limiter: at most `rate` calls per `per` seconds."""

    def __init__(self, rate: int, per: float) -> None:
        self._rate, self._per = rate, per
        self._stamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._stamps = [t for t in self._stamps if now - t < self._per]
                if len(self._stamps) < self._rate:
                    self._stamps.append(now)
                    return
                await asyncio.sleep(self._per - (now - self._stamps[0]) + 0.01)


class HttpClient:
    """aiohttp wrapper with retries on network errors, 429 and 5xx."""

    def __init__(self, name: str, rate: int = 10, per: float = 1.0, retries: int = 4,
                 timeout: float = 20.0, headers: dict[str, str] | None = None) -> None:
        self.name = name
        self._limiter = RateLimiter(rate, per)
        self._retries = retries
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = headers or {}
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers)
        return self._session

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            await self._limiter.acquire()
            try:
                sess = await self.session()
                async with sess.request(method, url, **kwargs) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                        raise ConnectorError(f"{self.name} HTTP {resp.status}: {(await resp.text())[:200]}"
                                             ) if attempt == self._retries else _Retry(retry_after)
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise FatalConnectorError(f"{self.name} HTTP {resp.status}: {str(body)[:300]}")
                    return body
            except _Retry as r:
                last_exc = r
                await asyncio.sleep(max(r.delay, 0.5 * 2 ** attempt))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt == self._retries:
                    break
                await asyncio.sleep(0.5 * 2 ** attempt)
        raise ConnectorError(f"{self.name} request failed after {self._retries} attempts: {last_exc!r}")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class _Retry(Exception):
    def __init__(self, delay: float) -> None:
        super().__init__(f"retry after {delay}s")
        self.delay = delay


class SolanaRPC:
    """JSON-RPC client for Solana (public RPC, Helius or QuickNode)."""

    def __init__(self, url: str, rate: int = 8) -> None:
        self.url = url
        self._http = HttpClient("solana-rpc", rate=rate, per=1.0)
        self._id = 0

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._id += 1
        body = await self._http.request("POST", self.url, json={
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []})
        if "error" in body:
            err = body["error"]
            code = err.get("code") if isinstance(err, dict) else None
            # -32005 node behind, -32004 block not available, -32429 rate limited → transient
            if code in (-32005, -32004, -32429, 429):
                raise ConnectorError(f"RPC {method} transient error: {err}")
            raise FatalConnectorError(f"RPC {method} error: {err}")
        return body.get("result")

    async def close(self) -> None:
        await self._http.close()


# --------------------------------------------------------------------------- CEX
class ExchangeConnector:
    """Binance / Bybit USDT-margined perpetuals via ccxt.async_support."""

    RETRYABLE = (NetworkError, RequestTimeout, ExchangeNotAvailable, DDoSProtection)
    FATAL = (AuthenticationError, InsufficientFunds, InvalidOrder, BadSymbol)

    def __init__(self, settings: Settings, max_retries: int = 4) -> None:
        if not settings.cex_exchange:
            raise ValueError("CEX_EXCHANGE is not configured")
        self.settings = settings
        self.name = "binanceusdm" if settings.cex_exchange == "binance" else settings.cex_exchange
        self.max_retries = max_retries
        self.exchange: ccxt_async.Exchange = self._build()
        self._markets_loaded = False
        self._leverage_set: set[str] = set()

    def _build(self) -> ccxt_async.Exchange:
        klass = getattr(ccxt_async, self.name)
        ex: ccxt_async.Exchange = klass({
            "apiKey": self.settings.cex_api_key or "",
            "secret": self.settings.cex_api_secret or "",
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": "swap" if self.name == "bybit" else "future",
                        "adjustForTimeDifference": True},
        })
        if self.settings.cex_testnet:
            ex.set_sandbox_mode(True)
        return ex

    async def _reconnect(self) -> None:
        logger.warning("[{}] reconnecting exchange client", self.name)
        try:
            await self.exchange.close()
        except Exception as exc:
            logger.debug("close during reconnect failed: {}", exc)
        self.exchange = self._build()
        self._markets_loaded = False
        await self.connect()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(1, self.max_retries + 1):
            try:
                return await getattr(self.exchange, method)(*args, **kwargs)
            except self.FATAL as exc:
                raise FatalConnectorError(f"{self.name}.{method}: {exc}") from exc
            except self.RETRYABLE as exc:
                if attempt == self.max_retries:
                    raise ConnectorError(f"{self.name}.{method} failed after retries: {exc}") from exc
                delay = min(30.0, 1.0 * 2 ** attempt)
                logger.warning("[{}] {} attempt {}/{} failed: {} (retry in {:.0f}s)",
                               self.name, method, attempt, self.max_retries, exc, delay)
                await asyncio.sleep(delay)
                if attempt >= 2:
                    await self._reconnect()
            except ExchangeError as exc:
                raise ConnectorError(f"{self.name}.{method}: {exc}") from exc
        raise ConnectorError("unreachable")

    async def connect(self) -> None:
        if not self._markets_loaded:
            for attempt in range(1, self.max_retries + 1):
                try:
                    await self.exchange.load_markets()
                    self._markets_loaded = True
                    break
                except self.RETRYABLE as exc:
                    if attempt == self.max_retries:
                        raise ConnectorError(f"{self.name} load_markets failed: {exc}") from exc
                    await asyncio.sleep(2 ** attempt)
                except ExchangeError as exc:
                    raise ConnectorError(f"{self.name} load_markets failed: {exc}") from exc
            logger.info("[{}] connected ({} markets, testnet={})", self.name,
                        len(self.exchange.markets), self.settings.cex_testnet)

    async def _ensure_leverage(self, symbol: str) -> None:
        if symbol in self._leverage_set:
            return
        try:
            await self._call("set_leverage", self.settings.cex_leverage, symbol)
        except ConnectorError as exc:
            if "not modified" not in str(exc).lower():
                raise
        self._leverage_set.add(symbol)

    async def fetch_ticker(self, symbol: str, depth: int = 20) -> MarketDataState:
        await self.connect()
        ticker, book = await asyncio.gather(self._call("fetch_ticker", symbol),
                                            self._call("fetch_order_book", symbol, depth))
        bids = [OrderBookLevel(price=p, size=s) for p, s, *_ in book["bids"][:depth]]
        asks = [OrderBookLevel(price=p, size=s) for p, s, *_ in book["asks"][:depth]]
        return MarketDataState(
            symbol=symbol, price=float(ticker["last"]), bid=ticker.get("bid") or None,
            ask=ticker.get("ask") or None, bids=bids, asks=asks,
            depth_usd=sum(l.price * l.size for l in bids + asks),
            order_book_imbalance=order_book_imbalance(bids, asks),
            price_change_24h_pct=ticker.get("percentage"), source=self.name)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]:
        await self.connect()
        return await self._call("fetch_ohlcv", symbol, timeframe, None, limit)

    def _amount(self, symbol: str, amount: float) -> float:
        value = float(self.exchange.amount_to_precision(symbol, amount))
        if value <= 0:
            raise FatalConnectorError(f"amount {amount} rounds to zero for {symbol}")
        return value

    async def place_stop(self, symbol: str, side: str, amount: float, stop_price: float) -> dict[str, Any]:
        """Reduce-only stop-market order (exchange-side protection)."""
        price = float(self.exchange.price_to_precision(symbol, stop_price))
        return await self._call("create_order", symbol, "market", side, self._amount(symbol, amount), None,
                                {"stopLossPrice": price, "reduceOnly": True})

    async def create_order_with_stop(self, symbol: str, side: str, amount: float, stop_loss: float,
                                     order_type: str = "market", limit_price: float | None = None
                                     ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Entry + protective stop. If the stop cannot be placed the position is flattened."""
        await self.connect()
        await self._ensure_leverage(symbol)
        qty = self._amount(symbol, amount)
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            entry = await self._call("create_order", symbol, "limit", side, qty,
                                     float(self.exchange.price_to_precision(symbol, limit_price)))
        else:
            entry = await self._call("create_order", symbol, "market", side, qty)
        exit_side = "sell" if side == "buy" else "buy"
        try:
            stop = await self.place_stop(symbol, exit_side, qty, stop_loss)
        except ConnectorError as exc:
            logger.error("[{}] stop placement failed ({}); flattening {} immediately", self.name, exc, symbol)
            if order_type == "limit":
                await self._call("cancel_order", entry["id"], symbol)
            await self._call("create_order", symbol, "market", exit_side, qty, None, {"reduceOnly": True})
            raise
        logger.info("[{}] {} {} {} filled, stop {} @ {}", self.name, side, qty, symbol, stop.get("id"), stop_loss)
        return entry, stop

    async def replace_stop(self, symbol: str, old_stop_id: str | None, amount: float,
                           new_stop: float) -> dict[str, Any]:
        """Place the new stop first, then cancel the old one, so the position is never unprotected."""
        new = await self.place_stop(symbol, "sell", amount, new_stop)
        if old_stop_id:
            try:
                await self._call("cancel_order", old_stop_id, symbol)
            except ConnectorError as exc:
                logger.warning("[{}] could not cancel old stop {}: {}", self.name, old_stop_id, exc)
        return new

    async def reduce_position(self, symbol: str, amount: float) -> dict[str, Any]:
        return await self._call("create_order", symbol, "market", "sell", self._amount(symbol, amount), None,
                                {"reduceOnly": True})

    async def close_position(self, symbol: str, amount: float, stop_id: str | None) -> dict[str, Any]:
        order = await self._call("create_order", symbol, "market", "sell", self._amount(symbol, amount),
                                 None, {"reduceOnly": True})
        if stop_id:
            try:
                await self._call("cancel_order", stop_id, symbol)
            except ConnectorError as exc:
                logger.warning("[{}] stale stop {} not cancelled: {}", self.name, stop_id, exc)
        return order

    async def close(self) -> None:
        await self.exchange.close()


# --------------------------------------------------------------------------- DEX
class SolanaDEXConnector:
    """Jupiter aggregator quotes/swaps with Jito-routed, pre-simulated transactions."""

    def __init__(self, settings: Settings, rpc: SolanaRPC) -> None:
        self.settings = settings
        self.rpc = rpc
        headers = {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else {}
        # Jupiter lite tier: ~60 req/min
        self._http = HttpClient("jupiter", rate=1, per=1.0, headers=headers)
        self._jito = HttpClient("jito", rate=1, per=1.0)
        base = settings.jupiter_api_base.rstrip("/")
        self._swap_base = f"{base}/v6" if "quote-api" in base else f"{base}/swap/v1"
        self._price_url = f"{base}/price/v3"
        self._decimals: dict[str, int] = {WSOL_MINT: 9, settings.quote_mint: settings.quote_decimals}
        self._keypair: Any = None
        if settings.solana_private_key:
            from solders.keypair import Keypair
            self._keypair = Keypair.from_base58_string(settings.solana_private_key)

    @property
    def wallet(self) -> str | None:
        return str(self._keypair.pubkey()) if self._keypair else None

    async def get_prices(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        data = await self._http.request("GET", self._price_url, params={"ids": ",".join(mints)})
        return {m: v for m, v in (data or {}).items() if v and v.get("usdPrice")}

    async def token_decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = await self.rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            try:
                self._decimals[mint] = int(info["value"]["data"]["parsed"]["info"]["decimals"])
            except (TypeError, KeyError) as exc:
                raise FatalConnectorError(f"cannot read decimals for mint {mint}") from exc
        return self._decimals[mint]

    async def get_quote(self, input_mint: str, output_mint: str, amount_raw: int,
                        slippage_bps: int | None = None) -> dict[str, Any]:
        if amount_raw <= 0:
            raise ValueError("amount_raw must be positive")
        quote = await self._http.request("GET", f"{self._swap_base}/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "slippageBps": str(slippage_bps or self.settings.max_slippage_bps),
            "restrictIntermediateTokens": "true"})
        if not isinstance(quote, dict) or "outAmount" not in quote:
            raise ConnectorError(f"unexpected Jupiter quote: {str(quote)[:300]}")
        impact = abs(float(quote.get("priceImpactPct") or 0)) * 100
        if impact > self.settings.max_price_impact_pct:
            raise SlippageError(f"price impact {impact:.3f}% > limit {self.settings.max_price_impact_pct}%")
        return quote

    async def quote_usd(self, mint: str, side: str, usd_or_units: float) -> tuple[dict[str, Any], float, float]:
        """Quote a BUY (spend USD) or SELL (sell token units). Returns (quote, fill_price, out_units)."""
        tdec = await self.token_decimals(mint)
        qdec = self.settings.quote_decimals
        if side == "BUY":
            quote = await self.get_quote(self.settings.quote_mint, mint, int(usd_or_units * 10 ** qdec))
            out_units = int(quote["outAmount"]) / 10 ** tdec
            return quote, usd_or_units / out_units, out_units
        quote = await self.get_quote(mint, self.settings.quote_mint, int(usd_or_units * 10 ** tdec))
        out_usd = int(quote["outAmount"]) / 10 ** qdec
        return quote, out_usd / usd_or_units, out_usd

    async def _build_swap_tx(self, quote: dict[str, Any]) -> str:
        body: dict[str, Any] = {
            "quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "dynamicSlippage": False,
        }
        if self.settings.use_jito and self.settings.jito_tip_lamports > 0:
            body["prioritizationFeeLamports"] = {"jitoTipLamports": self.settings.jito_tip_lamports}
        else:
            body["prioritizationFeeLamports"] = {"priorityLevelWithMaxLamports": {
                "priorityLevel": "high", "maxLamports": 1_000_000}}
        resp = await self._http.request("POST", f"{self._swap_base}/swap", json=body)
        tx = resp.get("swapTransaction") if isinstance(resp, dict) else None
        if not tx:
            raise ConnectorError(f"Jupiter swap build failed: {str(resp)[:300]}")
        return tx

    def _sign(self, tx_b64: str) -> tuple[str, str]:
        from solders.transaction import VersionedTransaction
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self._keypair])
        return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])

    async def simulate(self, signed_b64: str) -> dict[str, Any]:
        """Pre-flight simulation; aborts before anything reaches a leader if the swap would fail."""
        result = await self.rpc.call("simulateTransaction", [signed_b64, {
            "encoding": "base64", "sigVerify": True, "commitment": "processed"}])
        value = (result or {}).get("value", {})
        if value.get("err") is not None:
            logs = "\n".join((value.get("logs") or [])[-8:])
            raise FatalConnectorError(f"swap simulation failed: {value['err']}\n{logs}")
        return value

    async def _send(self, signed_b64: str) -> str:
        if self.settings.use_jito:
            # Private submission to the Jito block engine as a revert-protected bundle:
            # the tx is never gossiped through public RPC, which removes the sandwich surface.
            url = f"{self.settings.jito_block_engine_url.rstrip('/')}/api/v1/transactions"
            resp = await self._jito.request("POST", url, params={"bundleOnly": "true"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [signed_b64, {"encoding": "base64"}]})
            if "error" in resp:
                raise ConnectorError(f"Jito sendTransaction error: {resp['error']}")
            return str(resp["result"])
        return str(await self.rpc.call("sendTransaction", [signed_b64, {
            "encoding": "base64", "skipPreflight": True, "maxRetries": 3}]))

    async def confirm(self, signature: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = await self.rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
            status = (res or {}).get("value", [None])[0]
            if status:
                if status.get("err") is not None:
                    raise FatalConnectorError(f"transaction {signature} failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(2)
        raise ConnectorError(f"transaction {signature} not confirmed within {timeout:.0f}s")

    async def _balance_delta(self, signature: str, mint: str) -> float:
        """Actual token delta for our wallet from the confirmed transaction's balance metadata."""
        tx = await self.rpc.call("getTransaction", [signature, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        meta = (tx or {}).get("meta") or {}
        owner = self.wallet
        if mint == WSOL_MINT:
            keys = [k["pubkey"] if isinstance(k, dict) else k
                    for k in tx["transaction"]["message"]["accountKeys"]]
            i = keys.index(owner)
            return (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS_PER_SOL

        def total(rows: list[dict[str, Any]]) -> float:
            return sum(float(r["uiTokenAmount"]["uiAmountString"] or 0)
                       for r in rows if r.get("mint") == mint and r.get("owner") == owner)
        return total(meta.get("postTokenBalances") or []) - total(meta.get("preTokenBalances") or [])

    async def execute_swap(self, mint: str, side: str, usd_or_units: float) -> dict[str, Any]:
        """Live swap: quote → build (Jito tip) → sign → simulate → private send → confirm → reconcile."""
        if self._keypair is None:
            raise FatalConnectorError("SOLANA_PRIVATE_KEY not configured")
        quote, quoted_price, _ = await self.quote_usd(mint, side, usd_or_units)
        signed_b64, signature = self._sign(await self._build_swap_tx(quote))
        await self.simulate(signed_b64)
        sent_sig = await self._send(signed_b64)
        logger.info("[jupiter] {} {} submitted: {}", side, mint[:6], sent_sig)
        await self.confirm(signature)
        token_delta = abs(await self._balance_delta(signature, mint))
        quote_delta = abs(await self._balance_delta(signature, self.settings.quote_mint))
        if token_delta <= 0 or quote_delta <= 0:
            raise ConnectorError(f"could not reconcile fill for {signature}")
        return {"signature": signature, "price": quote_delta / token_delta, "units": token_delta,
                "usd": quote_delta, "quoted_price": quoted_price,
                "price_impact_pct": abs(float(quote.get("priceImpactPct") or 0)) * 100}

    async def close(self) -> None:
        await self._http.close()
        await self._jito.close()


# --------------------------------------------------------------------------- MEXC spot
class MexcSpotConnector:
    """MEXC spot (USDT pairs) via ccxt.async_support.

    MEXC's spot API has no stop orders (only LIMIT / MARKET / LIMIT_MAKER), so stops are enforced by
    the engine's guard loop and protect positions only while the engine is running."""

    RETRYABLE = (NetworkError, RequestTimeout, ExchangeNotAvailable, DDoSProtection)
    FATAL = (AuthenticationError, InsufficientFunds, InvalidOrder, BadSymbol)
    QUOTE = "USDT"

    def __init__(self, settings: Settings, max_retries: int = 4) -> None:
        self.settings = settings
        self.name = "mexc"
        self.max_retries = max_retries
        self.exchange: ccxt_async.Exchange = self._build()
        self._markets_loaded = False

    def _build(self) -> ccxt_async.Exchange:
        return ccxt_async.mexc({
            "apiKey": self.settings.mexc_api_key or "",
            "secret": self.settings.mexc_api_secret or "",
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        })

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(1, self.max_retries + 1):
            try:
                return await getattr(self.exchange, method)(*args, **kwargs)
            except self.FATAL as exc:
                raise FatalConnectorError(f"mexc.{method}: {exc}") from exc
            except self.RETRYABLE as exc:
                if attempt == self.max_retries:
                    raise ConnectorError(f"mexc.{method} failed after retries: {exc}") from exc
                await asyncio.sleep(min(20.0, 1.0 * 2 ** attempt))
            except ExchangeError as exc:
                raise ConnectorError(f"mexc.{method}: {exc}") from exc
        raise ConnectorError("unreachable")

    async def connect(self) -> None:
        if not self._markets_loaded:
            await self._call("load_markets")
            self._markets_loaded = True
            logger.info("[mexc] connected ({} markets, keys={})", len(self.exchange.markets),
                        bool(self.settings.mexc_api_key))

    def symbol(self, token_symbol: str) -> str:
        return f"{token_symbol.upper()}/{self.QUOTE}"

    async def has_symbol(self, token_symbol: str) -> bool:
        await self.connect()
        return self.symbol(token_symbol) in self.exchange.markets

    async def fetch_ticker(self, token_symbol: str, depth: int = 20) -> MarketDataState:
        await self.connect()
        sym = self.symbol(token_symbol)
        ticker, book = await asyncio.gather(self._call("fetch_ticker", sym),
                                            self._call("fetch_order_book", sym, depth))
        bids = [OrderBookLevel(price=p, size=s) for p, s, *_ in book["bids"][:depth]]
        asks = [OrderBookLevel(price=p, size=s) for p, s, *_ in book["asks"][:depth]]
        return MarketDataState(
            # MEXC's 24h ticker bid/ask lags the book; take the top of book instead
            symbol=token_symbol, price=float(ticker["last"]), bid=bids[0].price if bids else None,
            ask=asks[0].price if asks else None, bids=bids, asks=asks,
            depth_usd=sum(l.price * l.size for l in bids + asks),
            order_book_imbalance=order_book_imbalance(bids, asks),
            price_change_24h_pct=ticker.get("percentage"), source="mexc")

    async def last_prices(self, token_symbols: list[str]) -> dict[str, float]:
        await self.connect()
        syms = [self.symbol(t) for t in token_symbols if self.symbol(t) in self.exchange.markets]
        tickers = await self._call("fetch_tickers", syms)
        return {s.split("/")[0]: float(t["last"]) for s, t in tickers.items() if t.get("last")}

    async def fetch_ohlcv(self, token_symbol: str, timeframe: str, limit: int = 500) -> list[list[float]]:
        await self.connect()
        return await self._call("fetch_ohlcv", self.symbol(token_symbol), timeframe, None, limit)

    async def free_balance(self, asset: str) -> float:
        bal = await self._call("fetch_balance")
        return float((bal.get(asset) or {}).get("free") or 0.0)

    async def _wait_filled(self, order_id: str, sym: str, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        order: dict[str, Any] = {}
        while time.monotonic() < deadline:
            order = await self._call("fetch_order", order_id, sym)
            if order.get("status") == "closed" or float(order.get("remaining") or 0) == 0 and order.get("filled"):
                return order
            await asyncio.sleep(1.0)
        raise ConnectorError(f"mexc order {order_id} not filled within {timeout:.0f}s (last: {order.get('status')})")

    @staticmethod
    def parse_fill(order: dict[str, Any], base: str) -> dict[str, float]:
        """Net base units received/sold, average price, USD notional and fee in USD."""
        filled = float(order.get("filled") or 0)
        avg = float(order.get("average") or 0) or (float(order.get("cost") or 0) / filled if filled else 0.0)
        if filled <= 0 or avg <= 0:
            raise ConnectorError(f"mexc order {order.get('id')} has no fill data: {order}")
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_base = sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") == base)
        fee_quote = sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") != base)
        return {"units": filled, "fee_base": fee_base, "price": avg, "usd": float(order.get("cost") or filled * avg),
                "fee_usd": fee_quote + fee_base * avg}

    async def market_buy_usd(self, token_symbol: str, usd: float) -> dict[str, Any]:
        await self.connect()
        sym = self.symbol(token_symbol)
        free_usdt = await self.free_balance(self.QUOTE)
        if usd > free_usdt * 0.995:
            raise FatalConnectorError(f"insufficient USDT: need {usd:.2f}, free {free_usdt:.2f}")
        base = token_symbol.upper()
        held_before = await self.free_balance(base)
        cost = float(self.exchange.cost_to_precision(sym, usd))
        order = await self._call("create_market_buy_order_with_cost", sym, cost)
        try:
            fill = self.parse_fill(await self._wait_filled(str(order["id"]), sym), base)
        except ConnectorError as exc:
            # The order was accepted; never drop it on the floor (that would leave an unprotected
            # position). Reconcile from the balance change instead.
            received = await self.free_balance(base) - held_before
            if received <= 0:
                raise ConnectorError(f"buy {order['id']} state unknown and no {base} arrived: {exc}") from exc
            logger.warning("[mexc] buy {} not confirmed ({}); reconciled {} {} from balance", order["id"], exc,
                           received, base)
            return {"order_id": str(order["id"]), "units": received, "price": cost / received, "usd": cost,
                    "fee_usd": 0.0}
        fill["units"] -= fill.pop("fee_base")          # a base-asset fee reduces what we hold
        return {"order_id": str(order["id"]), **fill}

    async def market_sell(self, token_symbol: str, units: float) -> dict[str, Any]:
        await self.connect()
        sym = self.symbol(token_symbol)
        held = await self.free_balance(token_symbol.upper())
        qty = float(self.exchange.amount_to_precision(sym, min(units, held)))
        if qty <= 0:
            raise FatalConnectorError(f"nothing to sell: position {units}, free {held}")
        order = await self._call("create_order", sym, "market", "sell", qty)
        try:
            fill = self.parse_fill(await self._wait_filled(str(order["id"]), sym), token_symbol.upper())
        except ConnectorError as exc:
            sold = held - await self.free_balance(token_symbol.upper())
            if sold <= 0:
                raise ConnectorError(f"sell {order['id']} state unknown and balance unchanged: {exc}") from exc
            last = float((await self._call("fetch_ticker", sym))["last"])
            logger.warning("[mexc] sell {} not confirmed ({}); reconciled {} sold at ~{}", order["id"], exc, sold, last)
            return {"order_id": str(order["id"]), "units": sold, "price": last, "usd": sold * last, "fee_usd": 0.0}
        fill.pop("fee_base")
        return {"order_id": str(order["id"]), **fill}

    async def close(self) -> None:
        await self.exchange.close()
