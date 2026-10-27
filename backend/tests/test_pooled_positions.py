"""Pooled management and attribution use only local SQLite and fake venue data."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.decision_attribution import DecisionOutcomeV2, build_decision_attribution
from cosmos_quant.domain import Fill, OrderSide, OrderStatus, Position, Side
from cosmos_quant.entry_runtime import EntryOrderRuntime
from cosmos_quant.position_manager_v2 import DeterministicPositionManager, PositionActionType
from cosmos_quant.trade_history import build_trade_history_v2
from cosmos_quant.trading_intelligence import (
    DecisionEpisode,
    DeltaContext,
    PositionRuntimeV2,
    PositionStateV2,
    RiskAllocation,
    TakeProfitRule,
)
from cosmos_quant.trading_learning import refresh_learning
from cosmos_quant.v2_position_runtime import V2PositionReconciler
from test_v2_automation_runtime import FakeDemoExchange, activate_plan, make_store, state


class PooledExchange(FakeDemoExchange):
    def __init__(self, orders, fills):
        super().__init__()
        self.orders = {order.id: order for order in orders}
        self.fills = fills
        self.quantity = Decimal("15")
        self.pending = []
        self.synced = []
        self.protected = True

    async def fetch_order(self, identifier, _symbol):
        return self.orders[identifier].model_copy(update={"plan_id": "unknown"})

    async def portfolio(self):
        result = await super().portfolio()
        result.open_positions = (
            [
                Position(
                    symbol="BTC/USDT:USDT",
                    side=Side.LONG,
                    quantity=self.quantity,
                    average_entry=Decimal("1550") / 15,
                    mark_price="103",
                )
            ]
            if self.quantity
            else []
        )
        return result

    async def open_orders(self):
        return self.pending

    async def fetch_fills(self, *_args, **_kwargs):
        return self.fills

    async def verify_position_protection(self, _plan, _orders, _quantity):
        return self.protected

    async def sync_position_protection(self, plan, orders, quantity):
        self.synced.append((plan, orders, quantity))
        self.protected = True
        return True


def campaign(tmp_path):
    store = make_store(tmp_path)
    first = activate_plan(store)
    active = first.model_copy(update={"plan_id": "plan_add"})
    store.save_plan(active)
    observed = datetime.now(UTC)
    store.save_market_state(state(at=observed, last="103"))
    orders = []
    episodes = []
    fills = []
    for index, (plan, quantity, price, intended, risk) in enumerate(
        [
            (first, "10", "100", "10", "20"),
            (active, "5", "110", "10", "30"),
        ]
    ):
        order = FakeDemoExchange().entry.model_copy(
            update={
                "id": f"entry-{index}",
                "plan_id": plan.plan_id,
                "quantity": Decimal(quantity),
                "filled_quantity": Decimal(quantity),
                "average_fill_price": Decimal(price),
            }
        )
        orders.append(order)
        store.save_artifact(order.id, "ExchangeOrder", order)
        allocation = RiskAllocation(
            allocation_id=f"risk-{index}",
            plan_id=plan.plan_id,
            equity="10000",
            risk_amount=risk,
            quantity=intended,
            notional=Decimal(intended) * Decimal(price),
            leverage="1",
            constraints=["fixture"],
            configuration_fingerprint="fixture",
        )
        store.save_artifact(allocation.allocation_id, "RiskAllocation", allocation)
        episode = DecisionEpisode(
            episode_id=f"episode-{index}",
            symbol=plan.symbol,
            plan_id=plan.plan_id,
            market_state_hash=state().state_hash,
            delta=DeltaContext(current_state_hash=state().state_hash),
            risk_allocation_id=allocation.allocation_id,
            order_ids=[order.id],
        )
        store.save_episode(episode)
        episodes.append(episode)
        fills.append(
            Fill(
                id=f"fill-{index}",
                order_id=order.id,
                plan_id=plan.plan_id,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity=quantity,
                price=price,
                fee="1",
                timestamp=observed,
            )
        )
    position = PositionRuntimeV2(
        position_id="pool",
        symbol=active.symbol,
        side="LONG",
        state=PositionStateV2.OPEN,
        plan_id=active.plan_id,
        episode_id=episodes[-1].episode_id,
        plan_ids=[first.plan_id, active.plan_id],
        episode_ids=[e.episode_id for e in episodes],
        entry_order_ids=[o.id for o in orders],
        entry_filled_quantities={"one": "10", "two": "5"},
        original_quantity="15",
        remaining_quantity="15",
        average_entry=Decimal("1550") / 15,
        management_quantity="15",
        management_started_at=observed,
        initial_stop="98",
        current_stop="98",
        opened_at=observed,
    )
    store.save_position(position)
    exchange = PooledExchange(orders, fills)
    completed = []

    async def callback(symbol, event_id):
        completed.append((symbol, event_id))

    reconciler = V2PositionReconciler(
        store,
        halt=lambda *_: None,
        on_trade_completed=callback,
        margin_mode="cross",
        commissioned_max_leverage=Decimal("3"),
    )
    return store, position, active, exchange, reconciler, completed


def test_management_uses_new_activation_time_and_quantity(tmp_path):
    _store, position, plan, _exchange, _reconciler, _completed = campaign(tmp_path)
    position = position.model_copy(
        update={
            "original_quantity": Decimal("30"),
            "opened_at": datetime.now(UTC) - timedelta(days=1),
        }
    )
    manager = DeterministicPositionManager()
    assert (
        manager.evaluate(
            plan=plan, position=position, mark_price=Decimal("103"), atr_value=None
        ).action
        == PositionActionType.HOLD
    )
    plan.management.take_profits = [
        TakeProfitRule(price="104", close_fraction="0.5", rationale="half")
    ]
    action = manager.evaluate(
        plan=plan, position=position, mark_price=Decimal("104"), atr_value=None
    )
    assert action.action == PositionActionType.REDUCE
    assert action.quantity == Decimal("7.5")


def test_reconcile_preserves_entry_ownership_and_rebalances_pool(tmp_path):
    store, position, _plan, exchange, reconciler, _completed = campaign(tmp_path)
    exchange.protected = False
    assert asyncio.run(reconciler.reconcile(exchange))["failed"] == 0
    assert store.get_artifact("entry-0")["payload"]["plan_id"] == position.plan_ids[0]
    assert store.get_artifact("entry-1")["payload"]["plan_id"] == position.plan_id
    adapter, orders, quantity = exchange.synced[0]
    assert adapter.id == position.plan_id
    assert adapter.exit.stop_loss == position.current_stop
    assert len(orders) == 2 and quantity == 15


@pytest.mark.parametrize("venue_quantity", ["0", "10"])
def test_lagging_portfolio_cannot_erase_an_add_or_close_pool(tmp_path, venue_quantity):
    store, position, _plan, exchange, reconciler, completed = campaign(tmp_path)
    exchange.quantity = Decimal(venue_quantity)
    result = asyncio.run(reconciler.reconcile(exchange))
    assert result["closed"] == 0 and not completed
    assert store.get_position(position.position_id).remaining_quantity == 15
    assert store.get_position(position.position_id).version == position.version


def test_direction_matching_is_independent_and_normalizes_symbol(tmp_path):
    _store, position, _plan, _exchange, reconciler, _completed = campaign(tmp_path)
    long = Position(
        symbol="BTC/USDT", side=Side.LONG, quantity="15", average_entry="103", mark_price="104"
    )
    short = long.model_copy(
        update={"symbol": "BTC/USDT:USDT", "side": Side.SHORT, "quantity": Decimal("2")}
    )
    assert reconciler._actual_position([short, long], position) is long
    assert (
        reconciler._actual_position([long, short], position.model_copy(update={"side": "SHORT"}))
        is short
    )


def test_pending_entry_without_position_is_known_to_order_integrity_check(tmp_path):
    store, position, plan, exchange, reconciler, _completed = campaign(tmp_path)
    pending = EntryOrderRuntime(
        entry_id="pending",
        plan_id="pending-plan",
        episode_id="pending-episode",
        symbol=position.symbol,
        side="SHORT",
        mode="OKX_DEMO",
        plan_version=plan.version,
        order_type="LIMIT",
        quantity="1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allocation_id="pending-risk",
        client_order_ids=["pending-client"],
        status="SUBMITTING",
    )
    store.create_entry_order(pending)
    exchange.pending = [
        exchange.entry.model_copy(
            update={
                "id": "unacknowledged-order",
                "client_order_id": "pending-client",
                "status": OrderStatus.OPEN,
                "filled_quantity": Decimal("0"),
            }
        )
    ]
    assert asyncio.run(reconciler.reconcile(exchange))["failed"] == 0
    assert len(store.list_positions()) == 1


def close_fills(exchange):
    exchange.quantity = Decimal("0")
    exchange.fills.extend(
        [
            Fill(
                id="exit",
                order_id="native-exit",
                symbol="BTC/USDT",
                side=OrderSide.SELL,
                quantity="15",
                price="120",
                fee="1",
                timestamp=datetime.now(UTC),
            ),
            exchange.fills[1],  # Venue pagination overlap cannot duplicate cash flows.
            Fill(
                id="hedge",
                order_id="other-campaign",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity="50",
                price="120",
                fee="100",
                timestamp=datetime.now(UTC),
            ),
        ]
    )


def test_close_campaign_attributes_actual_fills_once_across_episodes(tmp_path):
    store, position, _plan, exchange, reconciler, completed = campaign(tmp_path)
    close_fills(exchange)
    assert asyncio.run(reconciler.reconcile(exchange))["closed"] == 1
    closed = store.get_position(position.position_id)
    episodes = {e.episode_id: e for e in store.list_episodes()}
    outcomes = [DecisionOutcomeV2.model_validate(e.outcome) for e in episodes.values()]
    primary = next(item for item in outcomes if item.attribution_role == "PRIMARY")
    assert primary.notional == 1550
    assert primary.realized_pnl == 247 and primary.fees == 3
    assert primary.return_on_risk == Decimal("247") / 35
    assert primary.evidence_complete
    assert all(e.status == "COMPLETED" for e in episodes.values())
    assert closed.average_entry == Decimal("1550") / 15
    report = build_decision_attribution(outcomes + [primary])
    assert report.trade_count == 1 and report.total_fees == 3
    assert refresh_learning(store).sample_count == 1
    history = build_trade_history_v2([closed, closed], episodes, {}, days=0)
    assert history.summary.trade_count == 1 and history.summary.net_pnl == 247
    assert history.items[0].episode_ids == position.episode_ids
    assert history.items[0].plan_ids == position.plan_ids
    assert len(completed) == 1
    assert asyncio.run(reconciler.reconcile(exchange))["closed"] == 0


def test_episode_write_failure_leaves_closure_retryable(tmp_path, monkeypatch):
    store, position, _plan, exchange, reconciler, completed = campaign(tmp_path)
    close_fills(exchange)
    original_save = store.save_episode

    def fail(_episode):
        raise OSError("injected local write failure")

    monkeypatch.setattr(store, "save_episode", fail)
    with pytest.raises(RuntimeError, match="failed reconciliation"):
        asyncio.run(reconciler.reconcile(exchange))
    assert store.get_position(position.position_id).state == PositionStateV2.CLOSING
    monkeypatch.setattr(store, "save_episode", original_save)
    assert asyncio.run(reconciler.reconcile(exchange))["closed"] == 1
    assert len(completed) == 1


def test_callback_failure_retains_durable_completed_event(tmp_path):
    store, position, _plan, exchange, reconciler, _completed = campaign(tmp_path)
    close_fills(exchange)

    async def fail(*_args):
        raise OSError("injected dispatch failure")

    reconciler.on_trade_completed = fail
    with pytest.raises(RuntimeError, match="failed reconciliation"):
        asyncio.run(reconciler.reconcile(exchange))
    assert store.get_position(position.position_id).state == PositionStateV2.CLOSED
    events = store.list_events(aggregate_id=position.position_id)
    assert len([e for e in events if e["event_type"] == "TRADE_COMPLETED"]) == 1
