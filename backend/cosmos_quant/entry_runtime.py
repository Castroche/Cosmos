"""Durable immediate entry orders and their lifecycle, separate from positions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import Order, OrderStatus
from .trading_intelligence import PriceZone, RiskAllocation

ACTIVE_ENTRY_STATUSES = frozenset(
    {
        "SUBMITTING",
        "OPEN",
        "PARTIALLY_FILLED",
        "CANCEL_PENDING",
        "UNCERTAIN",
    }
)
TERMINAL_ENTRY_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "SHADOW_RECORDED"})


class EntryOrderRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    plan_id: str
    episode_id: str
    symbol: str
    side: Literal["LONG", "SHORT"]
    mode: str
    version: int = 1
    plan_version: int
    order_type: Literal["MARKET", "LIMIT"]
    requested_price: Decimal | None = None
    submitted_price: Decimal | None = None
    reference_price: Decimal | None = None
    valid_price_range: PriceZone | None = None
    expires_at: datetime
    order_ids: list[str] = Field(default_factory=list)
    client_order_ids: list[str] = Field(default_factory=list)
    quantity: Decimal = Field(gt=0)
    filled_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    average_fill_price: Decimal | None = None
    accounted_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    accounted_notional: Decimal = Field(default=Decimal("0"), ge=0)
    reserved_risk: Decimal = Field(default=Decimal("0"), ge=0)
    reserved_margin: Decimal = Field(default=Decimal("0"), ge=0)
    reserved_stop_risk: Decimal = Field(default=Decimal("0"), ge=0)
    allocation_id: str
    position_id: str | None = None
    status: Literal[
        "SUBMITTING",
        "OPEN",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED",
        "REJECTED",
        "UNCERTAIN",
        "SHADOW_RECORDED",
    ] = "SUBMITTING"
    reason: str = ""
    cancel_requested: bool = False
    protection_verified: bool = False
    terminal_notified: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EntryOrderService:
    def __init__(
        self,
        store,
        *,
        on_terminal: Callable[[str, str], Awaitable[None]],
        margin_mode: str = "cross",
        execution_leverage: Decimal = Decimal("3"),
        mode: str = "OKX_DEMO",
    ):
        self.store = store
        self.on_terminal = on_terminal
        self.margin_mode = margin_mode
        self.execution_leverage = execution_leverage
        self.mode = mode
        self._lock = asyncio.Lock()

    async def reconcile(self, exchange, *, cancel_all: bool = False, symbol: str | None = None):
        """Reconcile fills first, cancel only remainders, and durably notify terminals."""
        result = {"checked": 0, "cancelled": 0, "filled": 0, "uncertain": 0}
        async with self._lock:
            entries = await asyncio.to_thread(
                self.store.list_entry_orders,
                symbol=symbol,
                needs_reconciliation=True,
                mode=self.mode,
            )
            for entry in entries:
                if entry.status in TERMINAL_ENTRY_STATUSES and entry.terminal_notified:
                    continue
                if entry.mode != self.mode or entry.mode == "SHADOW":
                    continue
                result["checked"] += 1
                try:
                    current = await self._reconcile_one(exchange, entry, cancel_all)
                except Exception as exc:
                    current = self.store.get_entry_order(entry.entry_id)
                    if current:
                        current = self.store.update_entry_order(
                            current.entry_id,
                            expected_version=current.version,
                            updates={"status": "UNCERTAIN", "reason": str(exc)[:500]},
                        )
                    result["uncertain"] += 1
                    continue
                if current.status == "CANCELLED":
                    result["cancelled"] += 1
                if current.status == "FILLED":
                    result["filled"] += 1
                if current.status in TERMINAL_ENTRY_STATUSES and not current.terminal_notified:
                    event_id, _ = self.store.append_event(
                        dedupe_key=f"entry-terminal:{current.entry_id}",
                        event_type="ENTRY_ORDER_TERMINAL",
                        aggregate_type="entry_order",
                        aggregate_id=current.entry_id,
                        aggregate_version=current.version,
                        payload={
                            "symbol": current.symbol,
                            "plan_id": current.plan_id,
                            "status": current.status,
                            "filled_quantity": str(current.filled_quantity),
                        },
                    )
                    await self.on_terminal(current.symbol, event_id)
                    self.store.update_entry_order(
                        current.entry_id,
                        expected_version=current.version,
                        updates={"terminal_notified": True},
                    )
        return result

    async def _reconcile_one(self, exchange, entry: EntryOrderRuntime, cancel_all: bool):
        plan = self.store.get_plan(entry.plan_id)
        if plan is None:
            raise RuntimeError("entry references an unknown plan")
        if entry.status in {"REJECTED", "CANCELLED"} and not entry.order_ids:
            return entry
        orders = []
        venue_symbol = entry.symbol.removesuffix(":USDT")
        if not entry.order_ids:
            for client_id in entry.client_order_ids:
                order = await exchange.find_order_by_client_id(client_id, venue_symbol)
                if order:
                    orders.append(order)
            if not orders:
                raise RuntimeError("submission acknowledgement unknown; no proven venue order yet")
        else:
            orders = [
                await exchange.fetch_order(order_id, venue_symbol) for order_id in entry.order_ids
            ]
        # Persist every proven order before protection/further exchange calls.
        for order in orders:
            self.store.save_artifact(
                order.id, "ExchangeOrder", order.model_copy(update={"plan_id": plan.plan_id})
            )
        if not entry.order_ids:
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={"order_ids": [o.id for o in orders]},
            )
        market = await exchange.market_snapshot(venue_symbol)
        invalid = entry.valid_price_range and not (
            entry.valid_price_range.lower <= market.last <= entry.valid_price_range.upper
        )
        cancel = (
            cancel_all
            or entry.cancel_requested
            or invalid
            or datetime.now(UTC) >= entry.expires_at
            or str(plan.state) in {"CANCELLED", "INVALIDATED", "EXPIRED"}
        )
        if cancel and any(
            o.status in {OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}
            for o in orders
        ):
            reason = entry.reason or (
                "automation stopped" if cancel_all else "limit validity expired"
            )
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={"status": "CANCEL_PENDING", "cancel_requested": True, "reason": reason},
            )
            cancelled = []
            for order in orders:
                if order.status in {
                    OrderStatus.OPEN,
                    OrderStatus.PENDING,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    order = await exchange.cancel_order(order.id, venue_symbol)
                self.store.save_artifact(
                    order.id, "ExchangeOrder", order.model_copy(update={"plan_id": plan.plan_id})
                )
                cancelled.append(order)
            orders = cancelled
        filled = sum((o.filled_quantity for o in orders), Decimal("0"))
        if filled and any(o.filled_quantity and not o.average_fill_price for o in orders):
            raise RuntimeError("venue fill exists without a proven execution price")
        notional = sum(
            (o.filled_quantity * (o.average_fill_price or Decimal("0")) for o in orders),
            Decimal("0"),
        )
        terminal = all(
            o.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
            for o in orders
        )
        # A partially recovered multi-order submission must not be labelled complete.
        if len(orders) < len(entry.client_order_ids):
            raise RuntimeError("only part of the submitted order set has been recovered")
        status = (
            "FILLED"
            if all(o.status == OrderStatus.FILLED for o in orders)
            else "CANCELLED"
            if terminal
            else "PARTIALLY_FILLED"
            if filled
            else "OPEN"
        )
        entry = self.store.update_entry_order(
            entry.entry_id,
            expected_version=entry.version,
            updates={
                "filled_quantity": filled,
                "average_fill_price": notional / filled if filled else None,
                "submitted_price": orders[0].limit_price,
                "status": "PARTIALLY_FILLED" if filled else status,
                "quantity": sum((o.quantity for o in orders), Decimal("0")),
            },
        )
        if filled:
            entry, position = self.store.merge_entry_fills(entry.entry_id)
            from .trading_intelligence import TradingModeV2
            from .v2_execution import compile_execution_adapter_plan

            active_plan = self.store.get_plan(position.plan_id)
            episode = self.store.episode_for_plan(active_plan.plan_id)
            artifact = self.store.get_artifact(episode.risk_allocation_id)
            allocation = RiskAllocation.model_validate(artifact["payload"])
            adapter_plan = compile_execution_adapter_plan(
                active_plan,
                allocation,
                market,
                margin_mode=self.margin_mode,
                execution_leverage=self.execution_leverage,
                trading_mode=TradingModeV2(entry.mode),
            )
            adapter_plan.exit.stop_loss = position.current_stop
            all_orders = [
                Order.model_validate(self.store.get_artifact(oid)["payload"])
                for oid in position.entry_order_ids
            ]
            try:
                if not await exchange.sync_position_protection(
                    adapter_plan, all_orders, position.remaining_quantity
                ):
                    raise RuntimeError("combined position protection not verified")
            except Exception:
                for order in orders:
                    if order.status in {
                        OrderStatus.OPEN,
                        OrderStatus.PENDING,
                        OrderStatus.PARTIALLY_FILLED,
                    }:
                        await exchange.cancel_order(order.id, venue_symbol)
                raise
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={"protection_verified": True},
            )
        if terminal:
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={
                    "status": status,
                    "reserved_risk": Decimal("0"),
                    "reserved_margin": Decimal("0"),
                    "reserved_stop_risk": Decimal("0"),
                },
            )
            episode = self.store.episode_for_plan(plan.plan_id)
            if episode:
                episode.status = "ENTRY_FILLED" if filled else "ENTRY_NOT_FILLED"
                episode.order_ids = list(dict.fromkeys([*episode.order_ids, *entry.order_ids]))
                episode.position_id = entry.position_id
                self.store.save_episode(episode)
            if not filled:
                from .trading_intelligence import PlanState

                if str(plan.state) not in {"COMPLETED", "EXPIRED", "INVALIDATED", "CANCELLED"}:
                    self.store.transition_plan(
                        plan.plan_id, expected_version=plan.version, target=PlanState.CANCELLED
                    )
        return entry
