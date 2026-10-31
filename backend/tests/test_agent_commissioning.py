import asyncio
from decimal import Decimal

import pytest
from app_fixture import services as api
from cosmos_quant.api_contracts import CommissioningRequest
from cosmos_quant.configuration import CommissioningResult, UserConfiguration
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import (
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioState,
    Position,
    Side,
    TradingMode,
)
from cosmos_quant.exchanges.base import PositionProtectionError
from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter
from fastapi import HTTPException


class FakeConfigurationService:
    def __init__(self):
        self.configuration = UserConfiguration(
            exchange_id="okx",
            trading_mode=TradingMode.SANDBOX,
            market_type="swap",
            margin_mode="cross",
            contract_leverage=Decimal("3"),
            default_symbol="BTC/USDT",
            default_timeframe="1h",
        )
        self.commissioning = CommissioningResult(
            exchange="okx",
            market_type="swap",
            symbol="BTC/USDT",
            credential_fingerprint="credential-test",
            protection_verified=True,
            cancellation_confirmed=True,
            order_ids=["prior-order"],
            message="entry protection passed",
        )

    def current_configuration(self):
        return self.configuration

    def commissioning_for(self, _configuration):
        return self.commissioning

    def credential_fingerprint(self, _configuration):
        return "credential-test"


class FakeStore:
    saved = None

    def save_commissioning(self, exchange_id, market_type, result):
        self.saved = (exchange_id, market_type, result)


class FakeRiskState:
    reason = ""
    halted = False

    def get(self):
        return type("RiskState", (), {"halted": self.halted})()

    def halt(self, reason, source):
        self.reason = f"{source}:{reason}"
        return type("RiskState", (), {"reason": reason})()


class FakeCommissioningExchange(CcxtExchangeAdapter):
    def __init__(self, *, fail_reduction: bool = False):
        self.position_quantity = Decimal("0")
        self.fail_reduction = fail_reduction
        self.reduce_calls = 0
        self.emergency_calls = 0
        self.orders: dict[str, Order] = {}

    async def open_orders(self):
        return []

    async def validate_account_configuration(self):
        return {"status": "ok"}

    async def minimum_commission_quantity(self, symbol, entry_price):
        del symbol, entry_price
        return Decimal("0.08")

    @staticmethod
    def _filled_order(
        order_id: str,
        plan_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType,
    ) -> Order:
        return Order(
            id=order_id,
            client_order_id=f"client-{order_id}",
            plan_id=plan_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            status=OrderStatus.FILLED,
            quantity=quantity,
            filled_quantity=quantity,
            average_fill_price=Decimal("50000"),
            mode=TradingMode.SANDBOX,
        )

    async def place_protected_order(self, plan, intent):
        quantity = intent.quantity / Decimal("2")
        result = [
            self._filled_order(
                f"entry-{index}",
                plan.id,
                plan.symbol,
                OrderSide.BUY,
                quantity,
                OrderType.LIMIT,
            )
            for index in (1, 2)
        ]
        self.position_quantity = sum((order.filled_quantity for order in result), Decimal("0"))
        self.orders.update({order.id: order for order in result})
        return result

    async def fetch_order(self, order_id, symbol):
        order = self.orders[order_id]
        assert order.symbol == symbol
        return order

    async def cancel_order(self, order_id, symbol):
        return await self.fetch_order(order_id, symbol)

    async def reduce_position_safely(self, plan, entry_orders, intent, current_quantity):
        del entry_orders
        self.reduce_calls += 1
        assert current_quantity == self.position_quantity
        order = self._filled_order(
            f"reduce-{self.reduce_calls}",
            plan.id,
            plan.symbol,
            OrderSide.SELL,
            intent.quantity,
            OrderType.MARKET,
        )
        self.orders[order.id] = order
        if self.fail_reduction:
            if self.reduce_calls == 1:
                self.position_quantity -= intent.quantity
            raise PositionProtectionError("injected protection failure", order)
        self.position_quantity -= intent.quantity
        return order

    async def verify_position_protection(self, plan, entry_orders, current_quantity):
        del plan, entry_orders
        return current_quantity == self.position_quantity

    async def place_order(self, intent: OrderIntent):
        self.emergency_calls += 1
        order = self._filled_order(
            f"emergency-{self.emergency_calls}",
            intent.plan_id,
            intent.symbol,
            intent.side,
            intent.quantity,
            intent.order_type,
        )
        self.position_quantity -= intent.quantity
        self.orders[order.id] = order
        return order

    async def clear_position_protection(self, plan, entry_orders):
        del plan, entry_orders
        return self.position_quantity == 0


def install_commissioning_runtime(monkeypatch, exchange):
    configuration = FakeConfigurationService()
    store = FakeStore()
    risk = FakeRiskState()
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)

    async def initialize(*, force=False):
        assert force is True

    async def market_context():
        return snapshot, candles

    async def portfolio():
        positions = []
        if exchange.position_quantity > 0:
            positions.append(
                Position(
                    symbol="BTC/USDT",
                    side=Side.LONG,
                    quantity=exchange.position_quantity,
                    average_entry=Decimal("50000"),
                    mark_price=Decimal("50000"),
                )
            )
        return PortfolioState(
            equity=Decimal("10000"),
            cash=Decimal("10000"),
            daily_start_equity=Decimal("10000"),
            gross_exposure=exchange.position_quantity * Decimal("50000"),
            open_positions=positions,
        )

    monkeypatch.setattr(api, "configuration_service", configuration)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "risk_state_service", risk)
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.runtime, "execution", exchange)
    monkeypatch.setattr(api.runtime, "initialize", initialize)
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    return configuration, store, risk


def test_agent_position_management_commissioning_persists_full_success(monkeypatch):
    exchange = FakeCommissioningExchange()
    _, store, risk = install_commissioning_runtime(monkeypatch, exchange)

    result = asyncio.run(
        api.commission_agent_position_management(
            CommissioningRequest(confirmation="TEST AGENT REDUCTION BTC/USDT")
        )
    )

    assert result.agent_position_management_passed
    assert exchange.reduce_calls == 2
    assert exchange.position_quantity == 0
    assert store.saved[2] == result
    assert risk.reason == ""


def test_failed_commissioning_emergency_flattens_only_test_position(monkeypatch):
    exchange = FakeCommissioningExchange(fail_reduction=True)
    _, store, risk = install_commissioning_runtime(monkeypatch, exchange)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            api.commission_agent_position_management(
                CommissioningRequest(confirmation="TEST AGENT REDUCTION BTC/USDT")
            )
        )

    assert error.value.status_code == 502
    assert exchange.reduce_calls == 2
    assert exchange.emergency_calls == 1
    assert exchange.position_quantity == 0
    assert "自动清理未完全确认" in risk.reason
    assert "protected cleanup" in risk.reason
    assert store.saved is None


def test_halted_platform_rejects_position_commissioning_before_any_order(
    monkeypatch,
):
    exchange = FakeCommissioningExchange()
    _, store, risk = install_commissioning_runtime(monkeypatch, exchange)
    risk.halted = True

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            api.commission_agent_position_management(
                CommissioningRequest(confirmation="TEST AGENT REDUCTION BTC/USDT")
            )
        )

    assert error.value.status_code == 409
    assert "HALT" in error.value.detail
    assert exchange.orders == {}
    assert store.saved is None
