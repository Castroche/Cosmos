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
        source="test",
    )


def test_spot_accounting_and_bracket_targets_close_position():
    async def scenario():
        candles = demo_candles()
        snapshot = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(
            candles,
            snapshot,
            Decimal("100000"),
            risk_fraction=Decimal("0.005"),
        )
        exchange = PaperExchange(slippage_bps=Decimal("0"), market_type="spot")
        exchange.update_market(snapshot)
        entry = await exchange.place_order(
            OrderIntent(
                idempotency_key="paper-entry",
                plan_id=plan.id,
                plan_fingerprint=plan.fingerprint,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=plan.sizing.suggested_quantity,
            )
        )
        await exchange.attach_bracket(plan, entry)
        portfolio = await exchange.portfolio()
        assert len(portfolio.open_positions) == 1
        assert portfolio.cash < Decimal("100000")

        exchange.update_market(market(plan.exit.take_profits[0].price + Decimal("1")))
        after_first_target = await exchange.portfolio()
        assert after_first_target.open_positions[0].quantity < plan.sizing.suggested_quantity

        exchange.update_market(market(plan.exit.take_profits[1].price + Decimal("1")))
        completed = await exchange.portfolio()
        assert completed.open_positions == []
        assert exchange.brackets[plan.id].completed
        fills = await exchange.fetch_fills(plan.symbol, entry.created_at, limit=20)
        assert len(fills) == 3
        assert all(fill.plan_id == plan.id for fill in fills)
        assert sum(fill.quantity for fill in fills if fill.side == OrderSide.SELL) == (
            entry.filled_quantity
        )

    asyncio.run(scenario())


def test_paper_safe_reduce_keeps_bracket_equal_to_remaining_position():
    async def scenario():
        candles = demo_candles()
        snapshot = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(
            candles, snapshot, Decimal("100000"), risk_fraction=Decimal("0.005")
        )
        exchange = PaperExchange(slippage_bps=Decimal("0"), market_type="spot")
        exchange.update_market(snapshot)
        entry_intent = OrderIntent(
            idempotency_key="paper-safe-entry",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=plan.sizing.suggested_quantity,
        )
        entries = await exchange.place_protected_order(plan, entry_intent)
        current = entries[0].filled_quantity
        reduce_quantity = (current / Decimal("4")).quantize(Decimal("0.00000001"))
        reduction = OrderIntent(
            idempotency_key="paper-safe-reduce",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=reduce_quantity,
            reduce_only=True,
        )

        order = await exchange.reduce_position_safely(plan, entries, reduction, current)
        remaining = current - order.filled_quantity

        assert await exchange.verify_position_protection(plan, entries, remaining)
        assert exchange.brackets[plan.id].remaining_quantity == remaining

    asyncio.run(scenario())


def test_paper_account_state_survives_restart(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        first = PaperExchange(store=store, account_id="paper:test", market_type="spot")
        snapshot = market(Decimal("50000"))
        first.update_market(snapshot)
        await first.place_order(
            OrderIntent(
                idempotency_key="persistent-entry",
                plan_id="plan-1",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )

        restored = PaperExchange(store=store, account_id="paper:test", market_type="spot")
        restored.update_market(snapshot)
        portfolio = await restored.portfolio()
        assert portfolio.open_positions[0].quantity == Decimal("0.1")
        assert portfolio.cash < Decimal("100000")
        assert len(restored.orders) == 1

    asyncio.run(scenario())


def test_spot_account_rejects_naked_short_sale():
    async def scenario():
        exchange = PaperExchange(market_type="spot")
        exchange.update_market(market(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="naked-short",
                plan_id="plan-short",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        assert order.status == "rejected"
        assert (await exchange.portfolio()).open_positions == []

    asyncio.run(scenario())


def test_swap_account_enforces_and_reports_initial_margin():
    async def scenario():
        exchange = PaperExchange(
            initial_cash=Decimal("1000"),
            market_type="swap",
            contract_leverage=Decimal("2"),
            slippage_bps=Decimal("0"),
        )
        exchange.update_market(market(Decimal("50000")))
        rejected = await exchange.place_order(
            OrderIntent(
                idempotency_key="swap-too-large",
                plan_id="plan-swap-large",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.05"),
            )
        )
        assert rejected.status == "rejected"

        accepted = await exchange.place_order(
            OrderIntent(
                idempotency_key="swap-with-margin",
                plan_id="plan-swap",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.02"),
            )
        )
        assert accepted.status == "filled"
        portfolio = await exchange.portfolio()
        assert portfolio.used_margin == Decimal("500")
        assert portfolio.available_margin < Decimal("500")
        assert portfolio.margin_ratio == Decimal("0.5003")
        assert portfolio.open_positions[0].leverage == Decimal("2")
        assert portfolio.open_positions[0].margin_mode == "cross"
        assert portfolio.open_positions[0].liquidation_price == Decimal("30000.06")

    asyncio.run(scenario())
