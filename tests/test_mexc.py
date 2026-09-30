import pytest

from config import LIVE_CONFIRM_PHRASE, Settings, WatchToken
from exchange_connector import FatalConnectorError, MexcSpotConnector

SOL = WatchToken(symbol="SOL", mint="So11111111111111111111111111111111111111112")


class FakeMexc:
    """Minimal stand-in for ccxt.mexc with deterministic fills."""

    def __init__(self, usdt=100.0, sol=0.0, price=120.0):
        self.markets = {"SOL/USDT": {}}
        self.bal = {"USDT": usdt, "SOL": sol}
        self.price, self.orders, self.calls = price, {}, []

    async def load_markets(self):
        return self.markets

    async def fetch_balance(self):
        return {k: {"free": v} for k, v in self.bal.items()}

    def cost_to_precision(self, sym, cost):
        return f"{cost:.2f}"

    def amount_to_precision(self, sym, amt):
        return f"{int(amt * 1000) / 1000:.3f}"

    async def create_market_buy_order_with_cost(self, sym, cost):
        self.calls.append(("buy", cost))
        filled = cost / self.price
        fee = filled * 0.0005                       # MEXC takes the taker fee in the received asset
        self.bal["USDT"] -= cost
        self.bal["SOL"] += filled - fee
        self.orders["b1"] = {"id": "b1", "status": "closed", "filled": filled, "remaining": 0,
                             "average": self.price, "cost": cost, "fees": [{"currency": "SOL", "cost": fee}]}
        return {"id": "b1"}

    async def create_order(self, sym, typ, side, qty, price=None, params=None):
        self.calls.append((side, qty))
        cost = qty * self.price
        fee = cost * 0.0005
        self.bal["SOL"] -= qty
        self.bal["USDT"] += cost - fee
        self.orders["s1"] = {"id": "s1", "status": "closed", "filled": qty, "remaining": 0,
                             "average": self.price, "cost": cost, "fees": [{"currency": "USDT", "cost": fee}]}
        return {"id": "s1"}

    async def fetch_order(self, oid, sym):
        return self.orders[oid]

    async def close(self):
        pass


def connector(fake):
    c = MexcSpotConnector(Settings(watchlist=[SOL], execution_venue="mexc"))
    c.exchange = fake
    return c


@pytest.mark.asyncio
async def test_buy_nets_out_base_fee():
    fake = FakeMexc(usdt=100)
    fill = await connector(fake).market_buy_usd("SOL", 60.0)
    assert fill["units"] == pytest.approx(0.5 * (1 - 0.0005))
    assert fill["price"] == 120.0 and fill["fee_usd"] == pytest.approx(0.03)
    assert fake.bal["SOL"] == pytest.approx(fill["units"])


@pytest.mark.asyncio
async def test_buy_rejects_when_usdt_insufficient():
    with pytest.raises(FatalConnectorError, match="insufficient USDT"):
        await connector(FakeMexc(usdt=20)).market_buy_usd("SOL", 60.0)


@pytest.mark.asyncio
async def test_sell_never_exceeds_free_balance():
    fake = FakeMexc(sol=0.4996)
    fill = await connector(fake).market_sell("SOL", 0.5)       # DB says 0.5, account holds less
    assert fake.calls[-1] == ("sell", 0.499)
    assert fill["usd"] == pytest.approx(0.499 * 120) and fill["fee_usd"] == pytest.approx(0.499 * 120 * 0.0005)


@pytest.mark.asyncio
async def test_sell_with_nothing_held_fails_loudly():
    with pytest.raises(FatalConnectorError, match="nothing to sell"):
        await connector(FakeMexc(sol=0)).market_sell("SOL", 0.5)


def test_live_mexc_requires_keys_and_budget_cap():
    base = dict(watchlist=[SOL], trading_mode="live", live_trading_confirm=LIVE_CONFIRM_PHRASE, execution_venue="mexc")
    with pytest.raises(ValueError, match="MEXC_API_KEY"):
        Settings(**base)
    with pytest.raises(ValueError, match="MAX_LIVE_CAPITAL_USD"):
        Settings(**base, mexc_api_key="k", mexc_api_secret="s", initial_capital_usd=500, max_live_capital_usd=100)
    ok = Settings(**base, mexc_api_key="k", mexc_api_secret="s", initial_capital_usd=50, max_live_capital_usd=100)
    assert ok.is_live


def test_walk_book_buy_and_sell():
    from core.state import OrderBookLevel as L
    from exchange_connector import walk_book
    asks = [L(price=100, size=1), L(price=101, size=1)]
    assert walk_book(asks, usd=100) == pytest.approx(100)
    assert walk_book(asks, usd=201) == pytest.approx(201 / 2)                 # 1 @100 + 1 @101
    bids = [L(price=99, size=1), L(price=98, size=1)]
    assert walk_book(bids, units=2) == pytest.approx(98.5)
    assert walk_book(bids, units=3) == pytest.approx((99 + 98 + 98) / 3)      # thin book: rest at last level
