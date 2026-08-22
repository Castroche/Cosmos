import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import (
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from cosmos_quant.exchanges.base import PositionProtectionError
from cosmos_quant.exchanges.ccxt_adapter import (
    CcxtExchangeAdapter,
    ExchangeConfigurationError,
)
from cosmos_quant.strategy import ExplainableTrendStrategy


class FakeClient:
    def __init__(self, exchange_id: str, market_type: str):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.calls: list[dict] = []
        self.created_symbols: list[str] = []
        self.position_mode = "net_mode"
        self.account_level = "2"
        market_symbol = "BTC/USDT:USDT" if market_type == "swap" else "BTC/USDT"
        self.markets = {
            market_symbol: {
                "id": "BTC-USDT-SWAP" if market_type == "swap" else "BTCUSDT",
                "contractSize": 0.01 if market_type == "swap" else None,
                "swap": market_type == "swap",
            }
        }

    async def load_markets(self):
        return self.markets

    def market(self, symbol: str):
        return self.markets[symbol]

    def price_to_precision(self, symbol: str, value: float):
        del symbol
        return f"{value:.2f}"

    def amount_to_precision(self, symbol: str, value: float):
        del symbol
        return f"{value:.6f}"

    async def create_order(self, symbol, order_type, side, amount, price, params):
        self.created_symbols.append(symbol)
        self.calls.append(params)
        if params.get("reduceOnly"):
            return {
                "id": "reduce-1",
                "clientOrderId": params["clientOrderId"],
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "status": "closed",
                "amount": amount,
                "filled": amount,
                "price": price,
                "average": "50000",
                "fee": {"cost": "0.10", "currency": "USDT"},
                "info": {},
            }
        return {
            "id": str(len(self.calls)),
            "clientOrderId": params["clOrdId"],
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "status": "open",
            "amount": amount,
            "filled": 0,
            "price": price,
            "average": None,
            "fee": None,
            "info": {"attachAlgoOrds": params["attachAlgoOrds"]},
        }

    async def private_post_orderlist_otoco(self, params):
        self.calls.append(params)
        return {
            "orderReports": [
                {
                    "orderId": len(self.calls),
                    "clientOrderId": params["workingClientOrderId"],
                    "side": params["workingSide"],
                    "status": "NEW",
                    "origQty": params["workingQuantity"],
                    "executedQty": "0",
                    "price": params["workingPrice"],
                }
            ]
        }

    async def private_get_account_config(self):
        return {"data": [{"posMode": self.position_mode, "acctLv": self.account_level}]}

    async def set_leverage(self, leverage, symbol, params):
        self.leverage_call = (leverage, symbol, params)

    async def cancel_order(self, order_id, symbol):
        return {"id": order_id, "symbol": symbol, "info": {}}

    async def fetch_order(self, order_id, symbol):
        return {
            "id": order_id,
            "clientOrderId": "cxaccepted1",
            "symbol": symbol,
            "side": "buy",
            "type": "limit",
            "status": "canceled",
            "amount": "0.001",
            "filled": "0",
            "price": "50000",
            "average": None,
            "fee": None,
            "info": {},
        }

    async def fetch_my_trades(self, symbol, since, limit):
        self.trade_history_call = (symbol, since, limit)
        return [
            {
                "id": "fill-1",
                "order": "order-1",
                "clientOrderId": "cxagentplan1p",
                "symbol": symbol,
                "side": "sell",
                "amount": "3",
                "price": "51000",
                "fee": {"cost": "0.25", "currency": "USDT"},
                "timestamp": 1784505600000,
                "info": {"clOrdId": "cxagentplan1p"},
            }
        ]


class SafeReduceClient(FakeClient):
    def __init__(self, *, confirm_amendment: bool = True):
        super().__init__("okx", "swap")
        self.position_contracts = Decimal("10")
        self.confirm_amendment = confirm_amendment
        self.algos = {
            "algo-1": {
                "algoId": "algo-1",
                "algoClOrdId": "entry1p",
                "state": "live",
                "sz": "6",
            },
            "algo-2": {
                "algoId": "algo-2",
                "algoClOrdId": "entry2p",
                "state": "live",
                "sz": "4",
            },
        }
        self.amendments: list[dict] = []
        self.cancellations: list[dict] = []

    async def fetch_balance(self):
        return {
            "total": {"USDT": "10000"},
            "free": {"USDT": "9000"},
            "info": {
                "data": [
                    {
                        "totalEq": "10000",
                        "details": [{"ccy": "USDT", "availEq": "9000"}],
                    }
                ]
            },
        }

    async def fetch_positions(self):
        if self.position_contracts <= 0:
            return []
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": str(self.position_contracts),
                "contractSize": "0.01",
                "entryPrice": "50000",
                "markPrice": "50100",
                "notional": str(self.position_contracts * Decimal("501")),
                "unrealizedPnl": "10",
                "leverage": "3",
                "initialMargin": "1670",
                "marginMode": "cross",
            }
        ]

    async def create_order(self, symbol, order_type, side, amount, price, params):
        response = await super().create_order(symbol, order_type, side, amount, price, params)
        if params.get("reduceOnly"):
            self.position_contracts -= Decimal(str(amount))
        return response

    async def private_get_trade_order(self, params):
        suffix = "1" if params["ordId"] == "entry-1" else "2"
        return {
            "code": "0",
            "data": [
                {
                    "attachAlgoOrds": [
                        {
                            "attachAlgoId": f"algo-{suffix}",
                            "attachAlgoClOrdId": f"entry{suffix}p",
                        }
                    ]
                }
            ],
        }

    async def private_get_trade_order_algo(self, params):
        algo = self.algos.get(params.get("algoId")) or next(
            (
                item
                for item in self.algos.values()
                if item["algoClOrdId"] == params.get("algoClOrdId")
            ),
            None,
        )
        return {"code": "0", "data": [dict(algo)] if algo else []}

    async def private_post_trade_amend_algos(self, params):
        self.amendments.append(dict(params))
        if self.confirm_amendment:
            self.algos[params["algoId"]]["sz"] = params["newSz"]
        return {
            "code": "0",
            "data": [{"algoId": params["algoId"], "sCode": "0", "sMsg": ""}],
        }

    async def private_post_trade_cancel_algos(self, payload):
        self.cancellations.extend(dict(item) for item in payload)
        for item in payload:
            self.algos[item["algoId"]]["state"] = "canceled"
        return {
            "code": "0",
            "data": [{"algoId": item["algoId"], "sCode": "0", "sMsg": ""} for item in payload],
        }


class OkxHistoryFallbackClient(SafeReduceClient):
    def __init__(self):
        super().__init__()
        self.history_calls = 0
        self.history_record = {
            "ordId": "entry-1",
            "clOrdId": "entry1",
            "state": "filled",
            "side": "buy",
            "ordType": "limit",
            "sz": "6",
            "accFillSz": "6",
            "px": "50000",
            "avgPx": "50010",
            "attachAlgoOrds": [
                {
                    "attachAlgoId": "algo-1",
                    "attachAlgoClOrdId": "entry1p",
                    "failCode": "",
                    "tpTriggerPx": "55000",
                    "slTriggerPx": "48000",
                }
            ],
        }

    async def fetch_order(self, order_id, symbol):
        del order_id, symbol
        raise RuntimeError('okx {"code":"51603","msg":"Order does not exist"}')

    async def private_get_trade_order(self, params):
        del params
        raise RuntimeError('okx {"code":"51603","msg":"Order does not exist"}')

    async def private_get_trade_orders_history(self, params):
        assert params["instType"] == "SWAP"
        assert params["instId"] == "BTC-USDT-SWAP"
        self.history_calls += 1
        return {"code": "0", "data": [dict(self.history_record)]}

    def parse_order(self, record, market):
        del market
        return {
            "id": record["ordId"],
            "clientOrderId": record["clOrdId"],
            "symbol": "BTC/USDT:USDT",
            "side": record["side"],
            "type": record["ordType"],
            "status": "closed",
            "amount": record["sz"],
            "filled": record["accFillSz"],
            "price": record["px"],
            "average": record["avgPx"],
            "fee": None,
            "info": record,
        }

    async def private_get_trade_order_algo(self, params):
        del params
        raise RuntimeError('okx {"code":"51603","msg":"Order does not exist"}')

    async def private_get_trade_orders_algo_pending(self, params):
        assert params["ordType"] in {"oco", "conditional"}
        active = [dict(item) for item in self.algos.values() if item["state"] == "live"]
        return {"code": "0", "data": active}


class TriggeredProtectionClient(SafeReduceClient):
    def __init__(self):
        super().__init__()
        self.algos = {
            "triggered-algo-1": {
                "algoId": "triggered-algo-1",
                "algoClOrdId": "entry1p",
                "state": "effective",
                "sz": "6",
                "ordId": "exit-1",
                "ordIdList": ["exit-1"],
            },
            "triggered-algo-2": {
                "algoId": "triggered-algo-2",
                "algoClOrdId": "entry2p",
                "state": "effective",
                "sz": "4",
                "ordId": "exit-2",
                "ordIdList": ["exit-2"],
            },
        }


def adapter(exchange_id: str, market_type: str) -> CcxtExchangeAdapter:
    instance = CcxtExchangeAdapter.__new__(CcxtExchangeAdapter)
    instance.exchange_id = exchange_id
    instance.market_type = market_type
    instance.sandbox = True
    instance.live_enabled = False
    instance.public_only = False
    instance.margin_mode = "cross"
    instance.contract_leverage = Decimal("3")
    instance.store = None
    instance.client = FakeClient(exchange_id, market_type)
    return instance


def safe_reduce_adapter(*, confirm_amendment: bool = True) -> CcxtExchangeAdapter:
    instance = adapter("okx", "swap")
    instance.client = SafeReduceClient(confirm_amendment=confirm_amendment)
    return instance


def history_fallback_adapter() -> CcxtExchangeAdapter:
    instance = adapter("okx", "swap")
    instance.client = OkxHistoryFallbackClient()
    return instance


def triggered_protection_adapter() -> CcxtExchangeAdapter:
    instance = adapter("okx", "swap")
    instance.client = TriggeredProtectionClient()
    return instance


def plan_and_intent():
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, snapshot, Decimal("100000"))
    plan.market_type = "swap"
    plan.margin_mode = "cross"
    plan.leverage = Decimal("3")
    intent = OrderIntent(
        idempotency_key="cx_protocol_test",
        plan_id=plan.id,
        plan_fingerprint=plan.fingerprint,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=plan.sizing.suggested_quantity,
        limit_price=(plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2"),
    )
    return plan, intent


def test_okx_protected_entries_attach_native_tp_and_sl_atomically():
    async def scenario():
        exchange = adapter("okx", "swap")
        plan, intent = plan_and_intent()
        orders = await exchange.place_protected_order(plan, intent)

        assert len(orders) == len(plan.exit.take_profits)
        assert exchange.client.leverage_call == (
            3,
            "BTC/USDT:USDT",
            {"mgnMode": "cross"},
        )
        assert exchange.client.created_symbols == ["BTC/USDT:USDT"] * len(plan.exit.take_profits)
        assert len(exchange.client.calls) == len(plan.exit.take_profits)
        for call in exchange.client.calls:
            assert call["clOrdId"].isalnum()
            protection = call["attachAlgoOrds"][0]
            assert protection["tpTriggerPx"]
            assert protection["slTriggerPx"]
            assert protection["tpOrdPx"] == "-1"
            assert protection["slOrdPx"] == "-1"
            assert call["tdMode"] == "cross"

    asyncio.run(scenario())


def test_okx_filled_order_and_algo_terminal_state_fall_back_after_51603():
    async def scenario():
        exchange = history_fallback_adapter()
        fetched = await exchange.fetch_order("entry-1", "BTC/USDT")
        entry = Order(
            id="entry-1",
            client_order_id="entry1",
            plan_id="plan-history",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            status=OrderStatus.FILLED,
            quantity=Decimal("0.06"),
            filled_quantity=Decimal("0.06"),
        )
        plan, _ = plan_and_intent()

        assert fetched.status == OrderStatus.FILLED
        assert fetched.filled_quantity == Decimal("0.06")
        assert await exchange.verify_position_protection(plan, [entry], Decimal("0.06"))

        exchange.client.algos["algo-1"]["state"] = "canceled"
        assert await exchange.verify_position_protection(plan, [entry], Decimal("0"))
        assert exchange.client.history_calls >= 3

    asyncio.run(scenario())


def test_okx_order_and_fill_values_are_quantized_to_domain_precision():
    async def scenario():
        exchange = adapter("okx", "swap")
        mapped = exchange._map_order(
            {
                "id": "precision-order",
                "clientOrderId": "precisionorder",
                "symbol": "BTC/USDT:USDT",
                "side": "buy",
                "type": "limit",
                "status": "closed",
                "amount": "0.123456789",
                "filled": "0.123456789",
                "price": "65000.123456789",
                "average": "65001.987654329",
                "fee": {"cost": "0.019495065", "currency": "USDT"},
                "timestamp": 1784505600000,
                "lastTradeTimestamp": 1784505660000,
                "info": {},
            },
            "precision-plan",
        )

        async def precise_trades(symbol, since, limit):
            del since, limit
            return [
                {
                    "id": "precision-fill",
                    "order": mapped.id,
                    "clientOrderId": mapped.client_order_id,
                    "symbol": symbol,
                    "side": "buy",
                    "amount": "0.123456789",
                    "price": "65000.123456789",
                    "fee": {"cost": "0.019495065", "currency": "USDT"},
                    "timestamp": 1784505600000,
                    "info": {},
                }
            ]

        exchange.client.fetch_my_trades = precise_trades
        fills = await exchange.fetch_fills("BTC/USDT", datetime.now(UTC) - timedelta(minutes=5))

        assert mapped.fee == Decimal("0.01949506")
        assert mapped.quantity == Decimal("0.00123457")
        assert mapped.limit_price == Decimal("65000.12345679")
        assert mapped.created_at == datetime(2026, 7, 20, tzinfo=UTC)
        assert mapped.updated_at == datetime(2026, 7, 20, 0, 1, tzinfo=UTC)
        assert fills[0].fee == Decimal("0.01949506")
        assert fills[0].quantity == Decimal("0.00123457")
        assert fills[0].price == Decimal("65000.12345679")

    asyncio.run(scenario())


def test_binance_spot_entries_use_otoco_with_pending_oco_children():
    async def scenario():
        exchange = adapter("binance", "spot")
        plan, intent = plan_and_intent()
        orders = await exchange.place_protected_order(plan, intent)

        assert len(orders) == len(plan.exit.take_profits)
        for call in exchange.client.calls:
            assert call["workingType"] == "LIMIT"
            assert call["pendingAboveType"] == "TAKE_PROFIT"
            assert call["pendingBelowType"] == "STOP_LOSS"
            assert call["pendingAboveStopPrice"]
            assert call["pendingBelowStopPrice"]

    asyncio.run(scenario())


def test_unverified_binance_swap_protection_stays_blocked():
    async def scenario():
        exchange = adapter("binance", "swap")
        assert not await exchange.protection_available("BTC/USDT")

    asyncio.run(scenario())


def test_okx_swap_requires_net_position_mode():
    async def scenario():
        exchange = adapter("okx", "swap")
        exchange.client.position_mode = "long_short_mode"
        with pytest.raises(ExchangeConfigurationError, match="net_mode") as error:
            await exchange.protection_available("BTC/USDT")
        assert error.value.code == "okx_position_mode_not_net"

    asyncio.run(scenario())


def test_okx_swap_rejects_spot_account_mode_with_actionable_code():
    async def scenario():
        exchange = adapter("okx", "swap")
        exchange.client.account_level = "1"
        with pytest.raises(ExchangeConfigurationError, match="现货模式") as error:
            await exchange.protection_available("BTC/USDT")
        assert error.value.code == "okx_account_mode_spot"
        assert "Futures mode" in error.value.required_action

    asyncio.run(scenario())


def test_okx_swap_accepts_net_position_mode():
    async def scenario():
        exchange = adapter("okx", "swap")
        assert await exchange.protection_available("BTC/USDT")

    asyncio.run(scenario())


def test_private_fill_history_preserves_client_tag_and_base_quantity():
    async def scenario():
        exchange = adapter("okx", "swap")
        since = datetime.now(UTC) - timedelta(days=1)
        fills = await exchange.fetch_fills("BTC/USDT", since)

        assert exchange.client.trade_history_call[0] == "BTC/USDT:USDT"
        assert fills[0].client_order_id == "cxagentplan1p"
        assert fills[0].quantity == Decimal("0.03")
        assert fills[0].price == Decimal("51000")
        assert fills[0].fee == Decimal("0.25")

    asyncio.run(scenario())


def test_sparse_cancel_response_is_confirmed_with_order_query():
    async def scenario():
        exchange = adapter("okx", "spot")
        canceled = await exchange.cancel_order("venue-order-1", "BTC/USDT")
        assert canceled.status == "canceled"
        assert canceled.id == "venue-order-1"

    asyncio.run(scenario())


def test_okx_reduce_only_exit_cancels_excess_protection_and_verifies_position():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id="entry-1",
                client_order_id="entry1",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=Decimal("0.06"),
                filled_quantity=Decimal("0.06"),
            ),
            Order(
                id="entry-2",
                client_order_id="entry2",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=Decimal("0.04"),
                filled_quantity=Decimal("0.04"),
            ),
        ]
        intent = OrderIntent(
            idempotency_key="cx_reduce_verified",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.04"),
            reduce_only=True,
        )

        order = await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert order.status == OrderStatus.FILLED
        assert exchange.client.position_contracts == Decimal("6")
        assert exchange.client.cancellations == [{"algoId": "algo-2", "instId": "BTC-USDT-SWAP"}]
        assert exchange.client.algos["algo-1"]["state"] == "live"
        assert exchange.client.algos["algo-2"]["state"] == "canceled"

    asyncio.run(scenario())


def test_okx_reduce_only_partial_reduction_amends_and_requeries_protection():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]
        intent = OrderIntent(
            idempotency_key="cx_reduce_amended",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.03"),
            reduce_only=True,
        )

        await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert exchange.client.position_contracts == Decimal("7")
        assert len(exchange.client.amendments) == 1
        assert exchange.client.amendments[0]["algoId"] == "algo-2"
        assert Decimal(exchange.client.amendments[0]["newSz"]) == Decimal("1")
        assert Decimal(exchange.client.algos["algo-2"]["sz"]) == Decimal("1")

    asyncio.run(scenario())


def test_okx_amend_ack_without_final_state_confirmation_fails_closed():
    async def scenario():
        exchange = safe_reduce_adapter(confirm_amendment=False)
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]
        intent = OrderIntent(
            idempotency_key="cx_reduce_unconfirmed",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.03"),
            reduce_only=True,
        )

        with pytest.raises(PositionProtectionError, match="did not confirm") as error:
            await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert error.value.order is not None
        assert exchange.client.position_contracts == Decimal("7")

    asyncio.run(scenario())


def test_okx_amend_transport_error_accepts_independently_confirmed_final_state():
    async def scenario():
        exchange = safe_reduce_adapter()
        original_amend = exchange.client.private_post_trade_amend_algos

        async def amend_then_raise(params):
            await original_amend(params)
            raise RuntimeError(
                'okx {"code":"1","data":[{"sCode":"51400","sMsg":"Order cancellation failed"}]}'
            )

        exchange.client.private_post_trade_amend_algos = amend_then_raise
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]
        intent = OrderIntent(
            idempotency_key="cx_reduce_ambiguous_ack",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.03"),
            reduce_only=True,
        )

        order = await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert order.status == OrderStatus.FILLED
        assert exchange.client.position_contracts == Decimal("7")
        assert Decimal(exchange.client.algos["algo-2"]["sz"]) == Decimal("1")

    asyncio.run(scenario())


def test_okx_cancel_transport_error_accepts_independently_confirmed_terminal_state():
    async def scenario():
        exchange = safe_reduce_adapter()
        original_cancel = exchange.client.private_post_trade_cancel_algos

        async def cancel_then_raise(payload):
            await original_cancel(payload)
            raise RuntimeError(
                'okx {"code":"1","data":[{"sCode":"51400","sMsg":"Order cancellation failed"}]}'
            )

        exchange.client.private_post_trade_cancel_algos = cancel_then_raise
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]
        intent = OrderIntent(
            idempotency_key="cx_exit_ambiguous_ack",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.10"),
            reduce_only=True,
        )

        order = await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert order.status == OrderStatus.FILLED
        assert exchange.client.position_contracts == 0
        assert all(item["state"] == "canceled" for item in exchange.client.algos.values())

    asyncio.run(scenario())


def test_okx_full_exit_cancels_every_remaining_attached_protection():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]
        intent = OrderIntent(
            idempotency_key="cx_full_exit_verified",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.10"),
            reduce_only=True,
        )

        order = await exchange.reduce_position_safely(plan, entries, intent, Decimal("0.10"))

        assert order.status == OrderStatus.FILLED
        assert exchange.client.position_contracts == 0
        assert {item["algoId"] for item in exchange.client.cancellations} == {
            "algo-1",
            "algo-2",
        }
        assert all(item["state"] == "canceled" for item in exchange.client.algos.values())

    asyncio.run(scenario())


def test_okx_protection_verifier_requires_exact_active_coverage():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]

        assert await exchange.verify_position_protection(plan, entries, Decimal("0.10"))
        exchange.client.algos["algo-2"]["sz"] = "3"
        assert not await exchange.verify_position_protection(plan, entries, Decimal("0.10"))

    asyncio.run(scenario())


def test_okx_zero_position_verification_rejects_orphaned_protection():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id="entry-1",
                client_order_id="entry1",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=Decimal("0.06"),
                filled_quantity=Decimal("0.06"),
            )
        ]

        assert not await exchange.verify_position_protection(plan, entries, Decimal("0"))
        exchange.client.algos["algo-1"]["state"] = "effective"
        assert await exchange.verify_position_protection(plan, entries, Decimal("0"))

    asyncio.run(scenario())


def test_okx_triggered_protection_resolves_exchange_child_order_ids():
    async def scenario():
        exchange = triggered_protection_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]

        assert await exchange.protection_exit_order_ids(entries, plan.symbol) == {
            "exit-1",
            "exit-2",
        }
        assert await exchange.verify_position_protection(plan, entries, Decimal("0"))

    asyncio.run(scenario())


def test_okx_explicit_protection_cleanup_cancels_every_active_algo():
    async def scenario():
        exchange = safe_reduce_adapter()
        plan, _ = plan_and_intent()
        entries = [
            Order(
                id=f"entry-{index}",
                client_order_id=f"entry{index}",
                plan_id=plan.id,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.FILLED,
                quantity=quantity,
                filled_quantity=quantity,
            )
            for index, quantity in ((1, Decimal("0.06")), (2, Decimal("0.04")))
        ]

        assert await exchange.clear_position_protection(plan, entries)
        assert {item["algoId"] for item in exchange.client.cancellations} == {
            "algo-1",
            "algo-2",
        }

    asyncio.run(scenario())
