import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter
from cosmos_quant.market_evidence import build_market_evidence


def test_market_evidence_combines_candles_and_microstructure():
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)

    evidence = build_market_evidence(
        candles,
        market,
        "1h",
        {
            "status": "complete",
            "order_book_imbalance": Decimal("0.60"),
            "bid_depth_quote": Decimal("1600000"),
            "ask_depth_quote": Decimal("400000"),
            "trade_flow_imbalance": Decimal("0.40"),
            "open_interest": Decimal("12345"),
            "open_interest_value": Decimal("800000000"),
            "open_interest_change_4h": Decimal("0.05"),
            "open_interest_change_24h": Decimal("0.08"),
        },
    )

    assert evidence.microstructure_status == "complete"
    assert evidence.directional_bias == "long"
    assert evidence.directional_strength > Decimal("0.15")
    assert evidence.order_book_imbalance == Decimal("0.60")
    assert evidence.open_interest == Decimal("12345")
    assert evidence.derivatives_regime == "long_build"
    assert evidence.derivatives_confirmation == "long"
    assert evidence.derivatives_strength > Decimal("0")
    assert evidence.reasons


def test_ccxt_microstructure_normalizes_contract_depth_and_trade_flow():
    class Client:
        has = {
            "fetchOrderBook": True,
            "fetchTrades": True,
            "fetchOpenInterest": True,
        }
        markets = {"BTC/USDT:USDT": {"contractSize": 0.01}}

        async def fetch_order_book(self, _symbol, _limit):
            return {
                "bids": [[100, 100], [99, 50]],
                "asks": [[101, 20], [102, 20]],
            }

        async def fetch_trades(self, _symbol, limit):
            assert limit == 100
            return [
                {"side": "buy", "price": 100, "amount": 10},
                {"side": "sell", "price": 100, "amount": 2},
            ]

        async def fetch_open_interest(self, _symbol):
            return {
                "openInterestAmount": 5000,
                "openInterestValue": 500000,
            }

    adapter = CcxtExchangeAdapter.__new__(CcxtExchangeAdapter)
    adapter.client = Client()
    adapter.market_type = "swap"

    result = asyncio.run(adapter._fetch_market_microstructure("BTC/USDT:USDT"))

    assert result["status"] == "complete"
    assert result["bid_depth_quote"] > result["ask_depth_quote"]
    assert result["order_book_imbalance"] > 0
    assert result["trade_flow_imbalance"] == Decimal("0.6666666666666666666666666667")
    assert result["open_interest"] == Decimal("5000")


def test_ccxt_microstructure_adds_bounded_open_interest_changes_for_okx():
    now = datetime.now(UTC)

    class Client:
        has = {
            "fetchOrderBook": False,
            "fetchTrades": False,
            "fetchOpenInterest": True,
            "fetchOpenInterestHistory": True,
        }
        markets = {
            "BTC/USDT:USDT": {
                "base": "BTC",
                "contractSize": 0.01,
            }
        }

        async def fetch_open_interest(self, _symbol):
            return {
                "openInterestAmount": 1100,
                "openInterestValue": 77000000,
            }

        async def fetch_open_interest_history(
            self,
            symbol,
            *,
            timeframe,
            since,
            limit,
        ):
            assert symbol == "BTC"
            assert timeframe == "1h"
            assert since > 0
            assert limit == 30
            return [
                {
                    "timestamp": int((now - timedelta(hours=25)).timestamp() * 1000),
                    "openInterestValue": 800,
                },
                {
                    "timestamp": int((now - timedelta(hours=5)).timestamp() * 1000),
                    "openInterestValue": 1000,
                },
                {
                    "timestamp": int((now - timedelta(hours=1)).timestamp() * 1000),
                    "openInterestValue": 1080,
                },
            ]

    adapter = CcxtExchangeAdapter.__new__(CcxtExchangeAdapter)
    adapter.client = Client()
    adapter.market_type = "swap"
    adapter.exchange_id = "okx"

    result = asyncio.run(adapter._fetch_market_microstructure("BTC/USDT:USDT"))

    assert result["open_interest_history_points"] == 3
    assert result["open_interest_history_metric"] == "value"
    assert result["open_interest_change_4h"] == Decimal("0.08")
    assert result["open_interest_change_24h"] == Decimal("0.35")
