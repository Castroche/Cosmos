import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cosmos_quant.domain import (
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioState,
)
from cosmos_quant.exchanges.paper import PaperExchange
from cosmos_quant.order_ledger import OrderLedger
from cosmos_quant.storage import SqliteStore


def snapshot(price: Decimal) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime.now(UTC),
        bid=price - Decimal("1"),
        ask=price + Decimal("1"),
        last=price,
        source="test",
    )


def test_order_ledger_survives_restart_and_reconciles_terminal_state(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="ledger-entry",
                plan_id="plan-1",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                limit_price=Decimal("49000"),
            )
        )
        first = OrderLedger(store, "paper:test")
        first.record(order, protection_verified=True)

        restored = OrderLedger(store, "paper:test")
        assert restored.records[order.id].protection_verified
        exchange.orders[order.id].status = OrderStatus.CANCELED
        report = await restored.reconcile(exchange)

        assert report.confirmed == 1
        assert restored.records[order.id].order.status == "canceled"
        assert restored.records[order.id].reconciliation_status == "confirmed"

    asyncio.run(scenario())


def test_reconciliation_detects_exchange_order_missing_from_local_ledger(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="external-entry",
                plan_id="external",
                plan_fingerprint="external",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                limit_price=Decimal("49000"),
            )
        )
        ledger = OrderLedger(store, "paper:test")
        report = await ledger.reconcile(exchange)

        assert report.unknown_open_order_ids == [order.id]
        assert ledger.records[order.id].protection_verified is False

    asyncio.run(scenario())


def test_protection_query_error_is_transport_failure_not_confirmed_absence(
    tmp_path,
):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="filled-protected-entry",
                plan_id="plan-1",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        ledger = OrderLedger(store, "paper:test")
        ledger.record(order, protection_verified=True)

        async def unavailable(_order):
            raise TimeoutError("temporary protection endpoint timeout")

        exchange.verify_protection = unavailable
        with pytest.raises(
            RuntimeError,
            match="protection verification is temporarily unavailable",
        ):
            await ledger.reconcile(exchange)

        record = ledger.records[order.id]
        assert record.protection_verified is True
        assert record.reconciliation_status == "uncertain"
        assert "temporary protection endpoint timeout" in record.last_error

        async def verified(_order):
            return True

        exchange.verify_protection = verified
        report = await ledger.reconcile(exchange)

        assert report.unprotected_order_ids == []
        assert record.reconciliation_status == "confirmed"
        assert record.last_error == ""

    asyncio.run(scenario())


def test_confirmed_missing_protection_remains_an_integrity_failure(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="confirmed-unprotected-entry",
                plan_id="plan-1",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        ledger = OrderLedger(store, "paper:test")
        ledger.record(order, protection_verified=True)

        async def missing(_order):
            return False

        exchange.verify_protection = missing
        report = await ledger.reconcile(exchange)

        assert report.unprotected_order_ids == [order.id]
        assert ledger.records[order.id].protection_verified is False
        assert ledger.records[order.id].reconciliation_status == "confirmed"

    asyncio.run(scenario())


def test_reconciliation_skips_filled_orders_from_inactive_closed_plans(
    tmp_path,
):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="historical-closed-entry",
                plan_id="historical-plan",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        ledger = OrderLedger(store, "paper:test")
        ledger.record(order, protection_verified=True)
        protection_queries = 0

        async def flat_portfolio():
            return PortfolioState(
                equity=Decimal("100000"),
                cash=Decimal("100000"),
                daily_start_equity=Decimal("100000"),
                gross_exposure=Decimal("0"),
            )

        async def verify(_order):
            nonlocal protection_queries
            protection_queries += 1
            return True

        exchange.portfolio = flat_portfolio
        exchange.verify_protection = verify
        report = await ledger.reconcile(
            exchange,
            protected_plan_ids=set(),
        )

        assert protection_queries == 0
        assert report.unknown_position_symbols == []
        assert report.unprotected_order_ids == []

    asyncio.run(scenario())


def test_reconciliation_verifies_filled_order_for_active_position_plan(
    tmp_path,
):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        order = await exchange.place_order(
            OrderIntent(
                idempotency_key="active-protected-entry",
                plan_id="active-plan",
                plan_fingerprint="fingerprint",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        ledger = OrderLedger(store, "paper:test")
        ledger.record(order, protection_verified=True)
        protection_queries = 0

        async def verify(_order):
            nonlocal protection_queries
            protection_queries += 1
            return True

        exchange.verify_protection = verify
        report = await ledger.reconcile(
            exchange,
            protected_plan_ids={"active-plan"},
        )

        assert protection_queries == 1
        assert report.unknown_position_symbols == []
        assert report.unprotected_order_ids == []

    asyncio.run(scenario())


def test_reconciliation_detects_position_without_active_plan_attribution(
    tmp_path,
):
    async def scenario():
        store = SqliteStore(tmp_path / "cosmos.db")
        exchange = PaperExchange()
        exchange.update_market(snapshot(Decimal("50000")))
        await exchange.place_order(
            OrderIntent(
                idempotency_key="external-filled-entry",
                plan_id="external",
                plan_fingerprint="external",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.1"),
            )
        )
        ledger = OrderLedger(store, "paper:test")

        report = await ledger.reconcile(
            exchange,
            protected_plan_ids=set(),
        )

        assert report.unknown_position_symbols == ["BTC/USDT"]
        assert report.unprotected_order_ids == []

    asyncio.run(scenario())
