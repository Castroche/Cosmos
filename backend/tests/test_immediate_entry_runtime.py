from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.domain import Order, OrderStatus, TradingMode
from cosmos_quant.entry_runtime import EntryOrderService
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    ActiveThesis,
    DecisionEpisode,
    DeltaContext,
    EvidenceRef,
    PlanState,
    TradePlan,
    TradingModeV2,
)
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from cosmos_quant.v2_execution import V2ExecutionEngine
from test_v2_execution import allocation, market, market_state, trade_plan


class Venue:
    def __init__(self):
        self.orders = {}
        self.intents = []
        self.mark = Decimal("100.5")
        self.fill_on_cancel = False
        self.fail_submit = False
        self.protected = []

    async def place_protected_order(self, plan, intent):
        self.intents.append(intent)
        if self.fail_submit:
            raise TimeoutError("submission acknowledgement lost")
        order = Order(
            id=f"order-{len(self.intents)}",
            client_order_id=intent.idempotency_key + "1",
            plan_id=plan.id,
            symbol=plan.symbol,
            side=intent.side,
            position_side=intent.position_side,
            order_type=intent.order_type,
            status=OrderStatus.OPEN,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            mode=TradingMode.SANDBOX,
        )
        self.orders[order.id] = order
        return [order]

    async def market_snapshot(self, symbol):
        return market().model_copy(
            update={"symbol": symbol, "last": self.mark, "timestamp": datetime.now(UTC)}
        )

    async def fetch_order(self, order_id, symbol):
        return self.orders[order_id]

    async def find_order_by_client_id(self, client_id, symbol):
        return next((o for o in self.orders.values() if o.client_order_id == client_id), None)

    async def cancel_order(self, order_id, symbol):
        order = self.orders[order_id]
        updates = {"status": OrderStatus.CANCELED}
        if self.fill_on_cancel:
            updates = {
                "status": OrderStatus.FILLED,
                "filled_quantity": order.quantity,
                "average_fill_price": Decimal("100"),
            }
        self.orders[order_id] = order.model_copy(update=updates)
        return self.orders[order_id]

    async def sync_position_protection(self, plan, orders, quantity):
        self.protected.append((plan.id, quantity, plan.exit.stop_loss))
        return True

    def fill(self, order_id, quantity, price="100"):
        order = self.orders[order_id]
        quantity = Decimal(quantity)
        self.orders[order_id] = order.model_copy(
            update={
                "filled_quantity": quantity,
                "average_fill_price": Decimal(price),
                "status": OrderStatus.FILLED
                if quantity == order.quantity
                else OrderStatus.PARTIALLY_FILLED,
            }
        )


def install(store, *, name="first", kind="LIMIT", side="LONG", target=None):
    now = datetime.now(UTC)
    store.save_thesis(
        ActiveThesis(
            thesis_id=name,
            symbol="BTC/USDT:USDT",
            direction=side,
            statement="explicit trade",
            supporting_evidence=[EvidenceRef(kind="state", reference="test")],
            invalidation_rules=["price leaves validity"],
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    data = trade_plan().model_dump()
    data.update(
        plan_id=name,
        thesis_id=name,
        state=PlanState.ACTIVE,
        side=side,
        order_type=kind,
        entry_conditions=[],
        created_at=now,
        expires_at=now + timedelta(hours=1),
        limit_price="100" if kind == "LIMIT" else None,
        reference_price="100.5",
        valid_price_range={"lower": "99", "upper": "103"},
    )
    if side == "SHORT":
        data["management"]["hard_stop"] = "104"
        data["management"]["take_profits"][0]["price"] = "98"
        data["management"]["take_profits"][1]["price"] = "96"
    if target:
        data.update(position_id=target.position_id, position_version=target.version)
    plan = TradePlan.model_validate(data)
    store.save_plan(plan)
    episode = DecisionEpisode(
        symbol=plan.symbol,
        plan_id=name,
        market_state_hash="test",
        thesis_id=name,
        status="DECIDED",
        delta=DeltaContext(current_state_hash="test"),
    )
    store.save_episode(episode)
    risk = allocation().model_copy(update={"plan_id": name, "allocation_id": f"allocation-{name}"})
    return plan, episode, risk


async def submit(store, venue, installed):
    plan, episode, risk = installed
    engine = V2ExecutionEngine(
        venue,
        store,
        margin_mode="cross",
        execution_leverage=Decimal("3"),
        demo_credentials_available=True,
    )
    return await engine.execute(
        plan=plan,
        episode=episode,
        allocation=risk,
        market=await venue.market_snapshot(plan.symbol),
        market_state=market_state(),
        requested_mode=TradingModeV2.OKX_DEMO,
        configuration_fingerprint="test",
        live_grant=None,
        quant=None,
        preflight_mode=TradingModeV2.OKX_DEMO,
    )


@pytest.fixture
def store(tmp_path):
    return TradingIntelligenceStore(SqliteStore(tmp_path / "entries.db"))


@pytest.mark.parametrize("kind", ["MARKET", "LIMIT"])
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_immediate_order_exact_type_price_and_restart_idempotency(store, kind, side):
    venue = Venue()
    installed = install(store, kind=kind, side=side)
    first = asyncio.run(submit(store, venue, installed))
    second = asyncio.run(submit(store, venue, installed))
    assert first.order_ids == second.order_ids
    assert len(venue.intents) == 1
    intent = venue.intents[0]
    assert intent.order_type.value == kind.lower()
    assert intent.limit_price == (Decimal("100") if kind == "LIMIT" else None)
    assert intent.position_side.value == side
    assert store.list_positions() == []


def test_partial_add_fills_merge_once_and_recall_only_terminal(store):
    async def run():
        venue = Venue()
        terminal = []

        async def notify(symbol, event):
            terminal.append(event)

        service = EntryOrderService(store, on_terminal=notify)
        first = await submit(store, venue, install(store))
        venue.fill(first.order_ids[0], "5")
        await service.reconcile(venue)
        await service.reconcile(venue)
        position = store.list_positions()[0]
        assert position.remaining_quantity == 5
        assert terminal == []
        venue.fill(first.order_ids[0], "20")
        await service.reconcile(venue)
        assert len(terminal) == 1
        position = store.list_positions()[0]
        added = await submit(store, venue, install(store, name="add", target=position))
        venue.fill(added.order_ids[0], "3", "101")
        await service.reconcile(venue)
        await service.reconcile(venue)
        pooled = store.list_positions()[0]
        assert pooled.remaining_quantity == 23
        assert pooled.original_quantity == 23
        assert pooled.management_quantity == 23
        assert pooled.plan_id == "add"
        assert set(pooled.plan_ids) == {"first", "add"}
        assert len(terminal) == 1
        assert store.position_for_plan("first").position_id == pooled.position_id

    asyncio.run(run())


def test_invalid_limit_cancel_race_books_fill_and_no_duplicate_notification(store):
    async def run():
        venue = Venue()
        events = []

        async def notify(symbol, event):
            events.append(event)

        await submit(store, venue, install(store))
        venue.mark = Decimal("104")
        venue.fill_on_cancel = True
        service = EntryOrderService(store, on_terminal=notify)
        await service.reconcile(venue)
        await EntryOrderService(store, on_terminal=notify).reconcile(venue)
        entry = store.entry_for_plan("first")
        assert entry.status == "FILLED"
        assert store.list_positions()[0].remaining_quantity == 20
        assert entry.reserved_risk == 0
        assert len(events) == 1

    asyncio.run(run())


def test_pending_direction_blocks_duplicate_but_opposite_direction_allowed(store):
    async def run():
        venue = Venue()
        await submit(store, venue, install(store))
        with pytest.raises(ValueError, match="unfinished entry"):
            await submit(store, venue, install(store, name="duplicate"))
        result = await submit(store, venue, install(store, name="short", side="SHORT"))
        assert result.status == "OPEN"
        assert len(venue.intents) == 2

    asyncio.run(run())


def test_uncertain_submission_keeps_reservation_and_never_resubmits(store):
    venue = Venue()
    venue.fail_submit = True
    installed = install(store)
    assert asyncio.run(submit(store, venue, installed)).status == "UNCERTAIN"
    assert asyncio.run(submit(store, venue, installed)).status == "UNCERTAIN"
    assert len(venue.intents) == 1
    assert store.entry_for_plan("first").reserved_risk > 0
