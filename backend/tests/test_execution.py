import asyncio
from decimal import Decimal

import pytest
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import OrderStatus, PlanStatus
from cosmos_quant.exchanges.paper import PaperExchange
from cosmos_quant.execution import ExecutionEngine, ProtectionVerificationError
from cosmos_quant.risk import RiskEngine
from cosmos_quant.strategy import ExplainableTrendStrategy


def test_execution_is_idempotent():
    async def scenario():
        candles = demo_candles()
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
        plan.status = PlanStatus.ACTIVE
        exchange = PaperExchange()
        exchange.update_market(market)
        engine = ExecutionEngine(exchange, RiskEngine())

        first = await engine.submit_plan(plan)
        second = await engine.submit_plan(plan)

        assert first.order is not None
        assert second.order is not None
        assert first.order.id == second.order.id
        assert len(exchange.orders) == 1

    asyncio.run(scenario())


def test_failed_post_submit_protection_verification_cancels_and_raises():
    class UnverifiedPaper(PaperExchange):
        def __init__(self):
            super().__init__()
            self.cancel_attempts = 0

        async def verify_protection(self, order):
            del order
            return False

        async def cancel_order(self, order_id, symbol):
            self.cancel_attempts += 1
            return await super().cancel_order(order_id, symbol)

    async def scenario():
        candles = demo_candles()
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
        plan.status = PlanStatus.ACTIVE
        exchange = UnverifiedPaper()
        exchange.update_market(market)
        engine = ExecutionEngine(exchange, RiskEngine())

        with pytest.raises(ProtectionVerificationError) as raised:
            await engine.submit_plan(plan)

        assert exchange.cancel_attempts == 1
        assert len(raised.value.orders) == 1
        assert raised.value.orders[0].status in {
            OrderStatus.CANCELED,
            OrderStatus.FILLED,
        }

    asyncio.run(scenario())


def test_transient_protection_query_is_retried_before_success():
    class EventuallyVerifiedPaper(PaperExchange):
        def __init__(self):
            super().__init__()
            self.verification_attempts = 0

        async def verify_protection(self, order):
            del order
            self.verification_attempts += 1
            return self.verification_attempts >= 2

    async def scenario():
        candles = demo_candles()
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
        plan.status = PlanStatus.ACTIVE
        exchange = EventuallyVerifiedPaper()
        exchange.update_market(market)
        result = await ExecutionEngine(exchange, RiskEngine()).submit_plan(plan)

        assert result.order is not None
        assert exchange.verification_attempts == 2

    asyncio.run(scenario())


def test_execution_reuses_complete_market_snapshot_when_provided():
    class CountMarketFetchPaper(PaperExchange):
        def __init__(self):
            super().__init__()
            self.market_fetches = 0

        async def market_snapshot(self, symbol):
            self.market_fetches += 1
            return await super().market_snapshot(symbol)

    async def scenario():
        candles = demo_candles()
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
        plan.status = PlanStatus.ACTIVE
        exchange = CountMarketFetchPaper()
        exchange.update_market(market)
        portfolio = await exchange.portfolio()

        result = await ExecutionEngine(exchange, RiskEngine()).submit_plan(
            plan,
            portfolio,
            market,
        )

        assert result.order is not None
        # Paper order matching reads its venue price once. The risk engine must
        # not perform an additional bare snapshot fetch before that.
        assert exchange.market_fetches == 1

    asyncio.run(scenario())
