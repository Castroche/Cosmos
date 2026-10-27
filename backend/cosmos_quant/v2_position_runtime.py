from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

from .decision_attribution import DecisionOutcomeV2
from .domain import (
    MarketSnapshot,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    Side,
)
from .exchanges.base import ExchangeAdapter, PositionProtectionError
from .position_manager_v2 import (
    DeterministicPositionManager,
    PositionActionType,
    PositionActionV2,
)
from .trading_intelligence import (
    PlanState,
    PositionRuntimeV2,
    PositionStateV2,
    RiskAllocation,
    ThesisState,
    evaluate_wake_condition,
)
from .trading_intelligence_store import TradingIntelligenceStore
from .v2_execution import compile_execution_adapter_plan

TradeCompletedCallback = Callable[[str, str], Awaitable[None]]
HaltCallback = Callable[[str, str], Any]


class PositionReconciliationIntegrityError(RuntimeError):
    """Venue state exists outside the attributable Trading Intelligence ledger."""


class V2PositionReconciler:
    """Reconcile Demo venue truth and apply deterministic management without an LLM."""

    def __init__(
        self,
        store: TradingIntelligenceStore,
        *,
        halt: HaltCallback,
        on_trade_completed: TradeCompletedCallback,
        margin_mode: str,
        commissioned_max_leverage: Decimal,
    ) -> None:
        self.store = store
        self.halt = halt
        self.on_trade_completed = on_trade_completed
        self.margin_mode = margin_mode
        self.commissioned_max_leverage = commissioned_max_leverage
        self.manager = DeterministicPositionManager()

    async def reconcile(self, exchange: ExchangeAdapter) -> dict[str, int]:
        portfolio = await exchange.portfolio()
        positions = self.store.list_positions()
        positions_by_scope: dict[tuple[str, Side], list[str]] = {}
        for position in positions:
            scope = (
                position.symbol.removesuffix(":USDT"),
                Side.LONG if position.side == "LONG" else Side.SHORT,
            )
            positions_by_scope.setdefault(scope, []).append(position.position_id)
        duplicate_scopes = {
            scope: position_ids
            for scope, position_ids in positions_by_scope.items()
            if len(position_ids) > 1
        }
        if duplicate_scopes:
            raise PositionReconciliationIntegrityError(
                "multiple v2 positions share one aggregate Demo venue scope; "
                "automatic quantity attribution is unsafe"
            )
        open_orders = await exchange.open_orders()
        pending_entries = self.store.list_entry_orders(active_only=True)
        known_order_ids = (
            {
                order_id
                for episode in (
                    await asyncio.to_thread(self.store.list_episodes, limit=5000)
                    if open_orders else []
                )
                for order_id in episode.order_ids
            }
            | {
                order_id
                for position in positions
                for order_id in [*position.entry_order_ids, *position.exit_order_ids]
            }
            | {order_id for entry in pending_entries for order_id in entry.order_ids}
        )
        known_attributable_client_ids: set[str] = {
            client_id for entry in pending_entries for client_id in entry.client_order_ids
        }
        for position in positions:
            known_attributable_client_ids.update(self.close_client_ids(position))
            for order_id in position.entry_order_ids:
                artifact = self.store.get_artifact(order_id)
                if not artifact or artifact["artifact_type"] != "ExchangeOrder":
                    continue
                entry = Order.model_validate(artifact["payload"])
                if entry.client_order_id:
                    known_attributable_client_ids.add(f"{entry.client_order_id}p"[:32])
        if any(
            order.id not in known_order_ids
            and order.client_order_id not in known_attributable_client_ids
            for order in open_orders
        ):
            raise PositionReconciliationIntegrityError(
                "Demo account contains an order outside the v2 ledger"
            )
        managed_scopes = {
            (
                item.symbol.removesuffix(":USDT"),
                Side.LONG if item.side == "LONG" else Side.SHORT,
            )
            for item in positions
        }
        unknown_positions = [
            item
            for item in portfolio.open_positions
            if (item.symbol.removesuffix(":USDT"), item.side) not in managed_scopes
        ]
        if unknown_positions:
            raise PositionReconciliationIntegrityError(
                "Demo account contains a position outside the v2 ledger"
            )
        result = {"checked": 0, "opened": 0, "managed": 0, "closed": 0, "failed": 0}
        for position in positions:
            result["checked"] += 1
            try:
                outcome = await self._reconcile_position(exchange, portfolio, position)
                if outcome:
                    result[outcome] += 1
            except Exception as exc:
                result["failed"] += 1
                error_hash = sha256(str(exc).encode()).hexdigest()
                self.store.append_event(
                    dedupe_key=(
                        f"position-reconcile-failed:{position.position_id}:"
                        f"{position.version}:{error_hash}"
                    ),
                    event_type="POSITION_RECONCILIATION_FAILED",
                    aggregate_type="position",
                    aggregate_id=position.position_id,
                    aggregate_version=position.version,
                    payload={
                        "error_type": type(exc).__name__,
                        "error_hash": error_hash,
                        "error_message": str(exc)[:300],
                    },
                )
                self.halt(
                    "Trading Intelligence v2 position reconciliation failed; "
                    "manual Demo account verification is required",
                    "protection_reconciliation",
                )
        if result["failed"]:
            raise RuntimeError("one or more attributable Demo positions failed reconciliation")
        return result

    async def _reconcile_position(
        self,
        exchange: ExchangeAdapter,
        portfolio,
        position: PositionRuntimeV2,
    ) -> str | None:
        plan = self.store.get_plan(position.plan_id)
        if not plan:
            raise RuntimeError("position references an unknown TradePlan")
        episode = next(
            (
                item
                for item in self.store.list_episodes(symbol=position.symbol, limit=1000)
                if item.episode_id == position.episode_id
            ),
            None,
        )
        if not episode or not episode.risk_allocation_id:
            raise RuntimeError("position has no complete DecisionEpisode allocation")
        allocation_artifact = self.store.get_artifact(episode.risk_allocation_id)
        if not allocation_artifact:
            raise RuntimeError("RiskAllocation artifact is unavailable")
        allocation = RiskAllocation.model_validate(allocation_artifact["payload"])
        entry_orders = await self._entry_orders(exchange, position)
        actual = self._actual_position(portfolio.open_positions, position)
        actual_quantity = actual.quantity if actual else Decimal("0")
        if (
            getattr(position, "entry_filled_quantities", {})
            and actual_quantity != position.remaining_quantity
        ):
            # Entry reconciliation may have committed newer fills than this venue snapshot.
            refreshed = await exchange.portfolio()
            actual = self._actual_position(refreshed.open_positions, position)
            actual_quantity = actual.quantity if actual else Decimal("0")
            if not await self._pooled_quantity_confirmed(
                exchange, position, entry_orders, actual_quantity
            ):
                pending_event_id, _ = self.store.append_event(
                    dedupe_key=f"position-snapshot-lag:{position.position_id}:{position.version}",
                    event_type="POSITION_SNAPSHOT_PENDING",
                    aggregate_type="position",
                    aggregate_id=position.position_id,
                    aggregate_version=position.version,
                    payload={
                        "ledger_quantity": str(position.remaining_quantity),
                        "venue_quantity": str(actual_quantity),
                    },
                )
                pending_event = self.store.get_event(pending_event_id)
                first_pending_at = datetime.fromisoformat(pending_event["created_at"])
                if datetime.now(UTC) - first_pending_at > timedelta(minutes=2):
                    raise PositionReconciliationIntegrityError(
                        "venue quantity cannot be attributed to position fills after two minutes"
                    )
                return None
            # Confirming fills may have recovered a previously unrecorded close.
            position = self.store.get_position(position.position_id)
        market = await self._market_snapshot(exchange, position)
        adapter_plan = compile_execution_adapter_plan(
            plan,
            allocation,
            market,
            margin_mode=self.margin_mode,
            execution_leverage=self.commissioned_max_leverage,
            trading_mode=self._demo_mode(),
        )
        adapter_plan = adapter_plan.model_copy(
            update={
                "exit": adapter_plan.exit.model_copy(update={"stop_loss": position.current_stop})
            }
        )
        if hasattr(exchange, "contract_leverage"):
            exchange.contract_leverage = allocation.leverage

        if actual_quantity <= 0:
            if position.state == PositionStateV2.OPENING:
                terminal = all(
                    order.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
                    for order in entry_orders
                )
                if terminal and not any(order.filled_quantity > 0 for order in entry_orders):
                    self.store.transition_position(
                        position.position_id,
                        expected_version=position.version,
                        target=PositionStateV2.FLAT,
                        updates={"remaining_quantity": Decimal("0")},
                    )
                    self._finish_unfilled(plan, episode)
                    return None
                if terminal and any(order.filled_quantity > 0 for order in entry_orders):
                    position = self.store.transition_position(
                        position.position_id,
                        expected_version=position.version,
                        target=PositionStateV2.OPEN,
                        updates={
                            "remaining_quantity": sum(
                                (order.filled_quantity for order in entry_orders), Decimal("0")
                            )
                        },
                    )
                else:
                    return None
            await self._finish_closed_position(
                exchange,
                adapter_plan,
                entry_orders,
                position,
                plan,
                episode,
                allocation,
            )
            return "closed"

        average_entry = actual.average_entry if actual else position.average_entry
        mark = actual.mark_price if actual else market.last
        high_mark = max(position.highest_mark or mark, mark)
        low_mark = min(position.lowest_mark or mark, mark)
        targets_reached = self._targets_reached(
            plan, actual_quantity, position.management_quantity or position.original_quantity
        )
        updates = {
            "remaining_quantity": actual_quantity,
            "average_entry": average_entry,
            "highest_mark": high_mark,
            "lowest_mark": low_mark,
            "targets_reached": max(position.targets_reached, targets_reached),
        }
        epsilon = Decimal("0.00000001")
        if position.state == PositionStateV2.OPENING:
            target = (
                PositionStateV2.OPEN
                if actual_quantity + epsilon >= position.original_quantity
                else PositionStateV2.PARTIALLY_FILLED
            )
            position = self.store.transition_position(
                position.position_id,
                expected_version=position.version,
                target=target,
                updates=updates,
            )
            opened = True
        elif actual_quantity + epsilon < position.remaining_quantity and position.state in {
            PositionStateV2.OPEN,
            PositionStateV2.PARTIALLY_FILLED,
        }:
            position = self.store.transition_position(
                position.position_id,
                expected_version=position.version,
                target=PositionStateV2.PARTIAL_EXIT,
                updates=updates,
            )
            opened = False
        else:
            position = self.store.update_position(
                position.position_id,
                expected_version=position.version,
                updates=updates,
            )
            opened = False

        protected = await exchange.verify_position_protection(
            adapter_plan,
            entry_orders,
            actual_quantity,
        )
        if not protected:
            await self._rebalance_protection(exchange, adapter_plan, entry_orders, actual_quantity)
        if position.state == PositionStateV2.CLOSING:
            return None
        state = self.store.latest_market_state(position.symbol)
        atr_value = (
            Decimal(str(state.volatility.values["atr_14"]))
            if state and state.volatility.values.get("atr_14") is not None
            else None
        )
        exit_wake = None
        if state and plan.management.exit_wake_conditions:
            current_payload = state.model_dump(mode="json", exclude_none=True)
            current_payload["price"] = state.market.values.get("last")
            current_payload["candles"] = state.timeframe_features
            previous_state = self.store.previous_market_state(
                state.symbol,
                before_state_hash=state.state_hash,
                before_data_as_of=state.data_as_of,
            )
            previous_payload = None
            if previous_state:
                previous_payload = previous_state.model_dump(mode="json", exclude_none=True)
                previous_payload["price"] = previous_state.market.values.get("last")
                previous_payload["candles"] = previous_state.timeframe_features
            exit_wake = next(
                (
                    condition
                    for condition in plan.management.exit_wake_conditions
                    if evaluate_wake_condition(
                        condition,
                        current_payload,
                        previous_payload,
                        now=state.data_as_of,
                    )
                ),
                None,
            )
        if position.exit_requested_at or exit_wake:
            action = PositionActionV2(
                action=PositionActionType.CLOSE,
                quantity=actual_quantity,
                reason=(
                    position.exit_reason
                    or (f"exit wake fired: {exit_wake.condition_id}" if exit_wake else "exit")
                ),
            )
        else:
            action = self.manager.evaluate(
                plan=plan,
                position=position,
                mark_price=mark,
                atr_value=atr_value,
            )
        if action.action == PositionActionType.MOVE_STOP:
            tightened_plan = adapter_plan.model_copy(
                update={"exit": adapter_plan.exit.model_copy(update={"stop_loss": action.new_stop})}
            )
            await self._rebalance_protection(
                exchange, tightened_plan, entry_orders, actual_quantity
            )
            self.store.update_position(
                position.position_id,
                expected_version=position.version,
                updates={"current_stop": action.new_stop},
            )
            return "managed"
        if action.action in {PositionActionType.REDUCE, PositionActionType.CLOSE}:
            close_quantity = action.quantity or actual_quantity
            close_key = f"{position.position_id}:{position.version}:{action.action}:v2"
            close_client_id = f"tic{sha256(close_key.encode()).hexdigest()[:24]}"
            existing_closes = [
                item
                for item in await exchange.open_orders()
                if item.client_order_id in self.close_client_ids(position)
            ]
            if existing_closes:
                latest_close = max(existing_closes, key=lambda item: item.created_at)
                close_client_id = latest_close.client_order_id
            close_intent = OrderIntent(
                idempotency_key=close_client_id,
                plan_id=plan.plan_id,
                plan_fingerprint=sha256(plan.model_dump_json().encode()).hexdigest()[:16],
                symbol=plan.symbol.removesuffix(":USDT"),
                side=OrderSide.SELL if position.side == "LONG" else OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=close_quantity,
                reduce_only=True,
                **(
                    {"position_side": position.side}
                    if "position_side" in OrderIntent.model_fields
                    else {}
                ),
            )
            try:
                order = await exchange.reduce_position_safely(
                    adapter_plan,
                    entry_orders,
                    close_intent,
                    actual_quantity,
                )
            except PositionProtectionError as exc:
                # The close can fill before native-protection cleanup fails.
                # Preserve its acknowledgement so subsequent reconciliation can
                # attribute fills and finish cleanup without submitting again.
                if exc.order is not None:
                    self.store.save_artifact(exc.order.id, "ExchangeOrder", exc.order)
                    self.store.update_position(
                        position.position_id,
                        expected_version=position.version,
                        updates={
                            "exit_order_ids": list(dict.fromkeys([
                                *position.exit_order_ids, exc.order.id,
                            ])),
                        },
                    )
                raise
            self.store.save_artifact(order.id, "ExchangeOrder", order)
            if action.action == PositionActionType.CLOSE:
                self.store.transition_position(
                    position.position_id,
                    expected_version=position.version,
                    target=PositionStateV2.CLOSING,
                    updates={"exit_order_ids": [*position.exit_order_ids, order.id]},
                )
            else:
                self.store.update_position(
                    position.position_id,
                    expected_version=position.version,
                    updates={"exit_order_ids": [*position.exit_order_ids, order.id]},
                )
            return "managed"
        return "opened" if opened else None

    @staticmethod
    def close_client_ids(position: PositionRuntimeV2) -> set[str]:
        identifiers: set[str] = set()
        for version in range(position.version + 1):
            for action in (PositionActionType.REDUCE, PositionActionType.CLOSE):
                raw = f"{position.position_id}:{version}:{action}:v2"
                identifiers.add(f"tic{sha256(raw.encode()).hexdigest()[:24]}")
        return identifiers

    async def _entry_orders(
        self,
        exchange: ExchangeAdapter,
        position: PositionRuntimeV2,
    ) -> list[Order]:
        orders = []
        linked_plans = {*position.plan_ids, position.plan_id}
        episode_plans = {
            order_id: episode.plan_id
            for episode in self.store.list_episodes(symbol=position.symbol, limit=5000)
            if episode.episode_id in {*position.episode_ids, position.episode_id}
            for order_id in episode.order_ids
            if episode.plan_id in linked_plans
        }
        for order_id in dict.fromkeys(position.entry_order_ids):
            artifact = self.store.get_artifact(order_id)
            try:
                order = await exchange.fetch_order(
                    order_id,
                    position.symbol.removesuffix(":USDT"),
                )
            except Exception:
                if not artifact:
                    raise
                order = Order.model_validate(artifact["payload"])
            # Venue responses often omit our plan id. Restore the original owner;
            # the latest management plan must never relabel earlier entry fills.
            owner = episode_plans.get(order_id)
            if not owner and artifact and artifact["artifact_type"] == "ExchangeOrder":
                owner = artifact["payload"].get("plan_id")
            if owner not in linked_plans:
                owner = order.plan_id if order.plan_id in linked_plans else None
            if not owner and len(linked_plans) == 1:
                owner = position.plan_id
            if not owner:
                raise PositionReconciliationIntegrityError(
                    "pooled entry order has no attributable plan"
                )
            order = order.model_copy(update={"plan_id": owner})
            orders.append(order)
            self.store.save_artifact(order.id, "ExchangeOrder", order)
        if not orders:
            raise RuntimeError("position has no attributable entry orders")
        return orders

    async def _pooled_quantity_confirmed(self, exchange, position, entry_orders, quantity) -> bool:
        if abs(quantity - position.remaining_quantity) <= Decimal("0.00000001"):
            return True
        native_ids = await exchange.protection_exit_order_ids(
            entry_orders, position.symbol.removesuffix(":USDT")
        )
        exit_ids = {*position.exit_order_ids, *native_ids}
        fills = await exchange.fetch_fills(
            position.symbol.removesuffix(":USDT"), position.opened_at, limit=500
        )
        exit_side = OrderSide.SELL if position.side == "LONG" else OrderSide.BUY
        close_client_ids = self.close_client_ids(position)
        exits = {
            item.id: item
            for item in fills
            if (item.order_id in exit_ids or item.client_order_id in close_client_ids)
            and item.side == exit_side
            and item.symbol.removesuffix(":USDT") == position.symbol.removesuffix(":USDT")
        }
        exited = sum((item.quantity for item in exits.values()), Decimal("0"))
        confirmed = abs(position.original_quantity - exited - quantity) <= Decimal("0.00000001")
        recovered_ids = sorted({item.order_id for item in exits.values()} - exit_ids)
        if confirmed and recovered_ids:
            updated = self.store.update_position(
                position.position_id,
                expected_version=position.version,
                updates={
                    "exit_order_ids": list(dict.fromkeys([
                        *position.exit_order_ids, *recovered_ids,
                    ])),
                },
            )
            self.store.append_event(
                dedupe_key=f"position-close-recovered:{position.position_id}:{updated.version}",
                event_type="POSITION_CLOSE_ORDERS_RECOVERED",
                aggregate_type="position",
                aggregate_id=position.position_id,
                aggregate_version=updated.version,
                payload={"exit_order_ids": recovered_ids, "source": "attributed_venue_fills"},
            )
        return confirmed

    @staticmethod
    async def _rebalance_protection(exchange, adapter_plan, entry_orders, quantity) -> None:
        rebalance = getattr(exchange, "sync_position_protection", None) or getattr(
            exchange, "rebalance_position_protection", None
        )
        if rebalance is None or not await rebalance(adapter_plan, entry_orders, quantity):
            raise RuntimeError("native protection does not exactly cover the Demo position")
        if not await exchange.verify_position_protection(adapter_plan, entry_orders, quantity):
            raise RuntimeError("rebalanced native protection could not be verified")

    @staticmethod
    def _actual_position(open_positions, position: PositionRuntimeV2):
        symbol = position.symbol.removesuffix(":USDT")
        expected_side = Side.LONG if position.side == "LONG" else Side.SHORT
        matches = [
            item
            for item in open_positions
            if item.symbol.removesuffix(":USDT") == symbol and item.side == expected_side
        ]
        if len(matches) > 1:
            raise RuntimeError("multiple venue positions match one v2 position")
        return matches[0] if matches else None

    async def _market_snapshot(
        self,
        exchange: ExchangeAdapter,
        position: PositionRuntimeV2,
    ) -> MarketSnapshot:
        state = self.store.latest_market_state(position.symbol)
        if state:
            values = state.market.values
            return MarketSnapshot(
                symbol=position.symbol.removesuffix(":USDT"),
                timestamp=state.data_as_of,
                bid=values.get("bid", values["last"]),
                ask=values.get("ask", values["last"]),
                last=values["last"],
                source="okx-demo-reconciliation",
            )
        return await exchange.market_snapshot(position.symbol.removesuffix(":USDT"))

    async def _finish_closed_position(
        self,
        exchange: ExchangeAdapter,
        adapter_plan,
        entry_orders: list[Order],
        position: PositionRuntimeV2,
        plan,
        episode,
        allocation: RiskAllocation,
    ) -> None:
        native_exit_ids = await exchange.protection_exit_order_ids(
            entry_orders,
            plan.symbol.removesuffix(":USDT"),
        )
        if not await exchange.verify_position_protection(
            adapter_plan,
            entry_orders,
            Decimal("0"),
        ) and not await exchange.clear_position_protection(adapter_plan, entry_orders):
            raise RuntimeError("closed Demo position still has active native protection")
        # Fetch before committing CLOSED so a transient fill outage can be retried
        # by reconciliation rather than permanently losing the learning outcome.
        fills = await exchange.fetch_fills(
            plan.symbol.removesuffix(":USDT"),
            position.opened_at,
            limit=500,
        )
        closing = position
        if position.state != PositionStateV2.CLOSING:
            closing = self.store.transition_position(
                position.position_id,
                expected_version=position.version,
                target=PositionStateV2.CLOSING,
            )
        # Keep CLOSING retryable until outcomes and the durable completion event exist.
        close_updates = {
            "remaining_quantity": Decimal("0"),
            "exit_order_ids": list(dict.fromkeys([*position.exit_order_ids, *native_exit_ids])),
            "closed_at": datetime.now(UTC),
        }
        closed = closing.model_copy(update={**close_updates, "version": closing.version + 1})
        plan_ids = list(dict.fromkeys([*position.plan_ids, position.plan_id]))
        episode_ids = list(dict.fromkeys([*position.episode_ids, episode.episode_id]))
        linked_episodes = {
            item.episode_id: item
            for item in self.store.list_episodes(symbol=position.symbol, limit=5000)
            if item.episode_id in episode_ids
        }
        if set(episode_ids) - linked_episodes.keys():
            raise PositionReconciliationIntegrityError(
                "pooled position has a missing decision episode"
            )
        order_ids = {*position.entry_order_ids, *closed.exit_order_ids}
        attributable = list(
            {
                item.id: item
                for item in fills
                if item.order_id in order_ids
                and item.symbol.removesuffix(":USDT") == position.symbol.removesuffix(":USDT")
            }.values()
        )
        gross_cashflow = sum(
            (
                item.quantity * item.price
                if item.side == OrderSide.SELL
                else -(item.quantity * item.price)
                for item in attributable
            ),
            Decimal("0"),
        )
        fees = sum((item.fee for item in attributable), Decimal("0"))
        pnl = gross_cashflow - fees
        entry_fills = [item for item in attributable if item.order_id in position.entry_order_ids]
        entry_quantity = sum((item.quantity for item in entry_fills), Decimal("0"))
        notional = sum((item.quantity * item.price for item in entry_fills), Decimal("0"))
        risk_amount = Decimal("0")
        order_plans = {order.id: order.plan_id for order in entry_orders}
        seen_allocations: set[str] = set()
        for linked in linked_episodes.values():
            allocation_id = linked.risk_allocation_id
            if not allocation_id or allocation_id in seen_allocations:
                continue
            seen_allocations.add(allocation_id)
            artifact = self.store.get_artifact(allocation_id)
            if not artifact:
                raise PositionReconciliationIntegrityError(
                    "pooled position allocation is unavailable"
                )
            linked_allocation = RiskAllocation.model_validate(artifact["payload"])
            filled_quantity = sum(
                (
                    item.quantity
                    for item in entry_fills
                    if order_plans.get(item.order_id) == linked_allocation.plan_id
                ),
                Decimal("0"),
            )
            risk_amount += (
                linked_allocation.risk_amount * filled_quantity / linked_allocation.quantity
            )
        exit_order_ids = set(closed.exit_order_ids)
        exit_fills = [item for item in attributable if item.order_id in exit_order_ids]
        exit_quantity = sum((item.quantity for item in exit_fills), Decimal("0"))
        exit_price = (
            sum((item.quantity * item.price for item in exit_fills), Decimal("0")) / exit_quantity
            if exit_quantity > 0
            else None
        )
        stop_distance = abs(position.average_entry - position.initial_stop)
        bought = sum((f.quantity for f in attributable if f.side == OrderSide.BUY), Decimal(0))
        sold = sum((f.quantity for f in attributable if f.side == OrderSide.SELL), Decimal(0))
        complete = (
            bought > 0
            and sold > 0
            and abs(bought - sold) <= Decimal("0.00000001")
            and abs(entry_quantity - position.original_quantity) <= Decimal("0.00000001")
        )
        if complete and entry_quantity > 0:
            close_updates["average_entry"] = notional / entry_quantity
        close_updates["accumulated_fees"] = max(fees, Decimal("0"))
        entry_state = None
        with self.store.store._lock, self.store.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM ti_market_states_v2 WHERE state_hash = ? LIMIT 1",
                (episode.market_state_hash,),
            ).fetchone()
        if row:
            from .trading_intelligence import MarketStateV2

            entry_state = MarketStateV2.model_validate_json(row["payload"])
        if position.side == "LONG":
            mfe = (position.highest_mark or position.average_entry) - position.average_entry
            mae = (position.lowest_mark or position.average_entry) - position.average_entry
        else:
            mfe = position.average_entry - (position.lowest_mark or position.average_entry)
            mae = position.average_entry - (position.highest_mark or position.average_entry)
        outcome = DecisionOutcomeV2(
            episode_id=episode.episode_id,
            campaign_id=position.position_id,
            plan_ids=plan_ids,
            episode_ids=episode_ids,
            evidence_complete=complete,
            trading_mode="OKX_DEMO",
            action=plan.side,
            setup=plan.setup,
            regime=(str(entry_state.regime) if entry_state else "unknown"),
            direction=plan.side,
            notional=notional,
            gross_pnl=gross_cashflow,
            realized_pnl=pnl,
            exit_price=exit_price,
            return_after_cost=pnl / notional if notional else Decimal("0"),
            return_on_risk=pnl / risk_amount if risk_amount else Decimal("0"),
            mfe=mfe / stop_distance if stop_distance else Decimal("0"),
            mae=mae / stop_distance if stop_distance else Decimal("0"),
            fees=max(fees, Decimal("0")),
            slippage=Decimal("0"),
            wake_fired=True,
            wake_actionable=True,
            target_hit=closed.targets_reached > 0,
            stopped=(
                position.side == "LONG"
                and (position.lowest_mark or position.average_entry) <= position.initial_stop
                or position.side == "SHORT"
                and (position.highest_mark or position.average_entry) >= position.initial_stop
            ),
        )
        for linked in linked_episodes.values():
            linked.status = "COMPLETED"
            linked.position_id = position.position_id
            linked.outcome = outcome.model_copy(
                update={
                    "episode_id": linked.episode_id,
                    "attribution_role": "PRIMARY"
                    if linked.episode_id == episode.episode_id
                    else "CONTRIBUTOR",
                }
            ).model_dump(mode="json")
            linked.order_ids = list(dict.fromkeys([*linked.order_ids, *closed.exit_order_ids]))
            self.store.save_episode(linked)
        from .trading_learning import refresh_learning

        try:
            refresh_learning(self.store)
        except Exception:
            logging.getLogger(__name__).exception(
                "Learning refresh failed; completed outcome retained for replay"
            )
        for plan_id in plan_ids:
            current_plan = self.store.get_plan(plan_id)
            if current_plan and current_plan.state == PlanState.TRIGGERED:
                self.store.transition_plan(
                    plan_id,
                    expected_version=current_plan.version,
                    target=PlanState.COMPLETED,
                )
            if not current_plan:
                continue
            thesis = self.store.get_thesis(current_plan.thesis_id)
            other_positions = [
                item
                for item in self.store.list_positions()
                if item.position_id != position.position_id
            ]
            other_plans = {
                key for item in other_positions for key in [*item.plan_ids, item.plan_id]
            }
            thesis_in_use = any(
                other and other.thesis_id == current_plan.thesis_id
                for other in (self.store.get_plan(key) for key in other_plans)
            )
            if thesis and thesis.state == ThesisState.ACTIVE and not thesis_in_use:
                self.store.transition_thesis(
                    thesis.thesis_id,
                    expected_version=thesis.version,
                    target=ThesisState.COMPLETED,
                )
        event_id, _ = self.store.append_event(
            dedupe_key=f"trade-completed:{closed.position_id}",
            event_type="TRADE_COMPLETED",
            aggregate_type="position",
            aggregate_id=closed.position_id,
            aggregate_version=closed.version,
            payload={
                "episode_id": episode.episode_id,
                "plan_id": plan.plan_id,
                "campaign_id": position.position_id,
                "plan_ids": plan_ids,
                "episode_ids": episode_ids,
                "outcome_hash": sha256(outcome.model_dump_json().encode()).hexdigest(),
            },
        )
        self.store.transition_position(
            closing.position_id,
            expected_version=closing.version,
            target=PositionStateV2.CLOSED,
            updates=close_updates,
        )
        await self.on_trade_completed(position.symbol, event_id)

    def _finish_unfilled(self, plan, episode) -> None:
        current_plan = self.store.get_plan(plan.plan_id)
        if current_plan and current_plan.state in {
            PlanState.ARMED,
            PlanState.TRIGGERED,
            PlanState.WAITING_TRIGGER,
        }:
            self.store.transition_plan(
                current_plan.plan_id,
                expected_version=current_plan.version,
                target=PlanState.CANCELLED,
            )
        episode.status = "ENTRY_NOT_FILLED"
        episode.outcome = {"reason": "all entry orders terminated without a fill"}
        self.store.save_episode(episode)

    @staticmethod
    def _targets_reached(plan, remaining: Decimal, original: Decimal) -> int:
        closed_fraction = Decimal("1") - remaining / original if original > 0 else Decimal("0")
        cumulative = Decimal("0")
        reached = 0
        for target in plan.management.take_profits:
            cumulative += target.close_fraction
            if closed_fraction + Decimal("0.000001") >= cumulative:
                reached += 1
        return reached

    @staticmethod
    def _demo_mode():
        from .trading_intelligence import TradingModeV2

        return TradingModeV2.OKX_DEMO
