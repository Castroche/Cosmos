import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter


class SpotAccountClient:
    markets = {"BTC/USDT": {"id": "BTCUSDT"}}

    async def fetch_tickers(self, symbols):
        assert symbols == ["BTC/USDT"]
        return {"BTC/USDT": {"last": 50000}}


class SwapAccountClient:
    async def fetch_positions(self):
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "short",
                "contracts": 100,
                "contractSize": 0.001,
                "entryPrice": 51000,
                "markPrice": 50000,
                "unrealizedPnl": 100,
                "notional": -5000,
            }
        ]


class FundingClient:
    has = {"fetchFundingRate": True}
    markets = {
        "BTC/USDT:USDT": {
            "id": "BTC-USDT-SWAP",
            "swap": True,
            "contractSize": 0.01,
        }
    }

    async def load_markets(self):
        return self.markets

    async def fetch_funding_rate(self, symbol):
        assert symbol == "BTC/USDT:USDT"
        return {
            "fundingRate": 0.0001,
            "fundingTimestamp": 1_800_000_000_000,
            "interval": "8h",
        }


def account_adapter(exchange_id: str, market_type: str, client):
    instance = CcxtExchangeAdapter.__new__(CcxtExchangeAdapter)
    instance.exchange_id = exchange_id
    instance.market_type = market_type
    instance.store = None
    instance.account_id = "test"
    instance.client = client
    return instance


def test_spot_portfolio_values_inventory_in_quote_currency():
    async def scenario():
        exchange = account_adapter("binance", "spot", SpotAccountClient())
        portfolio = await exchange._spot_portfolio(
            {
                "total": {"USDT": 1000, "BTC": 0.1},
                "free": {"USDT": 800},
            }
        )

        assert portfolio.equity == Decimal("6000")
        assert portfolio.gross_exposure == Decimal("5000")
        assert portfolio.open_positions[0].quantity == Decimal("0.1")

    asyncio.run(scenario())


def test_swap_portfolio_uses_exchange_total_equity_and_contract_size():
    async def scenario():
        exchange = account_adapter("okx", "swap", SwapAccountClient())
        portfolio = await exchange._swap_portfolio(
            {
                "total": {"USDT": 1400},
                "free": {"USDT": 900},
                "info": {
                    "data": [
                        {
                            "totalEq": "1500",
                            "details": [{"ccy": "USDT", "availEq": "850"}],
                        }
                    ]
                },
            }
        )

        assert portfolio.equity == Decimal("1500")
        assert portfolio.gross_exposure == Decimal("5000")
        assert portfolio.open_positions[0].side == "short"
        assert portfolio.open_positions[0].symbol == "BTC/USDT"
        assert portfolio.open_positions[0].quantity == Decimal("0.1")
        assert portfolio.available_margin == Decimal("850")

    asyncio.run(scenario())


def test_swap_funding_rate_is_normalized_and_cached():
    async def scenario():
        exchange = account_adapter("okx", "swap", FundingClient())
        result = await exchange.funding_snapshot("BTC/USDT")

        assert result is not None
        assert result["funding_rate"] == Decimal("0.0001")
        assert result["funding_interval"] == "8h"
        assert result["funding_time"] == datetime.fromtimestamp(1_800_000_000, tz=UTC)

    asyncio.run(scenario())
