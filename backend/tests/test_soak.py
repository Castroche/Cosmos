import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import MarketSnapshot, OrderIntent, OrderSide, OrderType
from cosmos_quant.exchanges.paper import PaperExchange
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy import ExplainableTrendStrategy


def market(price: Decimal) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime.now(UTC),
        bid=price - Decimal("0.1"),
        ask=price + Decimal("0.1"),
        last=price,
        source="accelerated-soak",
    )


def test_accelerated_paper_execution_soak_with_periodic_restarts(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        account_id = "paper:soak"
        candles = demo_candles()
        snapshot = snapshot_from_candles(candles)
        exchange = PaperExchange(
            store=store,
            account_id=account_id,
            slippage_bps=Decimal("0"),
        )

        for cycle in range(50):
            if cycle and cycle % 10 == 0:
                exchange = PaperExchange(
                    store=store,
                    account_id=account_id,
                    slippage_bps=Decimal("0"),
                )
            exchange.update_market(snapshot)
            equity = (await exchange.portfolio()).equity
            plan = ExplainableTrendStrategy().generate(candles, snapshot, equity)
            await exchange.place_protected_order(
                plan,
                OrderIntent(
                    idempotency_key=f"soak-entry-{cycle}",
                    plan_id=plan.id,
                    plan_fingerprint=plan.fingerprint,
                    symbol=plan.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=plan.sizing.suggested_quantity,
                ),
            )
            exchange.update_market(market(plan.exit.take_profits[-1].price + Decimal("10")))
            assert (await exchange.portfolio()).open_positions == []

        for tick in range(10_000):
            exchange.update_market(snapshot.model_copy(update={"last": snapshot.last + tick % 3}))

        restored = PaperExchange(store=store, account_id=account_id)
        restored.update_market(snapshot)
        portfolio = await restored.portfolio()
        assert portfolio.open_positions == []
        assert portfolio.equity > Decimal("100000")
        assert len(restored.orders) == 150
        assert store.integrity_check() == "ok"

    asyncio.run(scenario())
