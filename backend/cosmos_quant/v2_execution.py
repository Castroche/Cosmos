from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    EntryRule,
    ExitPlan,
    MarketRegime,
    MarketSnapshot,
    Order,
    OrderIntent,
    OrderSide,
    OrderType,
    PlanStatus,
    PositionSide,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
    TradingMode,
)
from .exchanges.base import ExchangeAdapter
from .trading_intelligence import (
    DecisionEpisode,
    HumanLiveGrant,
    MarketStateV2,
    PositionRuntimeV2,
    PositionStateV2,
    QuantPrediction,
    RiskAllocation,
    TradePlan,
    TradingModeV2,
    content_hash,
    validate_market_state_time,
)
from .trading_intelligence_engine import resolve_trading_mode
from .trading_intelligence_store import TradingIntelligenceStore


class V2ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TradingModeV2
    status: Literal[
        "SHADOW_RECORDED",
        "SUBMITTED",
        "REJECTED",
        "SUBMITTING",
        "OPEN",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED",
        "UNCERTAIN",
    ]
    plan_id: str
    allocation_id: str
    order_ids: list[str] = Field(default_factory=list)
    protection_verified: bool = False
    reason: str = ""


class EntryRiskBlocked(ValueError):
    """Expected fail-closed entry rejection before any durable execution claim."""


def _legacy_targets(plan: TradePlan) -> list[ProfitTarget]:
    targets = [
        ProfitTarget(
            price=item.price,
            close_fraction=item.close_fraction,
            rationale=item.rationale,
        )
        for item in plan.management.take_profits
    ]
    total = sum((item.close_fraction for item in targets), Decimal("0"))
    if total < 1:
        last = targets[-1]
        targets[-1] = last.model_copy(
            update={"close_fraction": last.close_fraction + (Decimal("1") - total)}
        )
    return targets


def compile_execution_adapter_plan(
    plan: TradePlan,
    allocation: RiskAllocation,
    market: MarketSnapshot,
    *,
    margin_mode: str,
    execution_leverage: Decimal,
    trading_mode: TradingModeV2,
) -> StrategyPlan:
    """Translate v2 intent at the exchange boundary; no research gate is reintroduced."""

    if allocation.plan_id != plan.plan_id:
        raise ValueError("risk allocation references another plan")
    if allocation.leverage > execution_leverage:
        raise ValueError("allocated leverage exceeds commissioned execution leverage")
    legacy_mode = {
        TradingModeV2.SHADOW: TradingMode.PAPER,
        TradingModeV2.OKX_DEMO: TradingMode.SANDBOX,
        TradingModeV2.LIVE_AUTHORIZED: TradingMode.LIVE,
    }[trading_mode]
    symbol = plan.symbol.removesuffix(":USDT")
    snapshot = market.model_copy(update={"symbol": symbol})
    maximum_bars = (
        max(plan.management.maximum_holding_minutes // 5, 1)
        if plan.management.maximum_holding_minutes
        else None
    )
    return StrategyPlan(
        id=plan.plan_id,
        lineage_id=plan.thesis_id,
        name=f"Trading Intelligence v2 {plan.side}",
        version=plan.version,
        algorithm="trading_intelligence_v2",
        source_prompt="",
        exchange="okx",
        trading_mode=legacy_mode,
        market_type="swap",
        margin_mode=margin_mode,
        leverage=allocation.leverage,
        symbol=symbol,
        timeframe="5m",
        side=Side.LONG if plan.side == "LONG" else Side.SHORT,
        status=PlanStatus.ACTIVE,
        signal_candle_at=market.timestamp,
        data_as_of=market.timestamp,
        regime=MarketRegime.UNCERTAIN,
        confidence=Decimal("0"),
        entry=EntryRule(
            order_type=OrderType(plan.order_type.lower()) if plan.order_type else OrderType.LIMIT,
            zone=(
                PriceZone(lower=plan.execution_price, upper=plan.execution_price)
                if plan.order_type
                else PriceZone(lower=plan.entry_zone.lower, upper=plan.entry_zone.upper)
            ),
            trigger="immediate LLM instruction"
            if plan.order_type
            else "validated WakeCondition v2",
            expires_at=plan.expires_at,
        ),
        exit=ExitPlan(
            stop_loss=plan.management.hard_stop,
            take_profits=_legacy_targets(plan),
            trailing_stop_atr=plan.management.atr_trailing_multiple,
            break_even_after_first_target=plan.management.break_even_after_target == 1,
            maximum_holding_bars=maximum_bars,
            invalidation="deterministic Position Manager v2",
        ),
        sizing=PositionSizing(
            risk_fraction=allocation.risk_amount / allocation.equity,
            risk_amount=allocation.risk_amount,
            suggested_quantity=allocation.quantity,
            notional=allocation.notional,
        ),
        minimum_reward_risk=plan.minimum_reward_risk,
        thesis=plan.thesis_id,
        reasons=plan.rationale,
        snapshot=snapshot,
        decision_origin="trading_intelligence_v2",
    )


class V2ExecutionEngine:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        store: TradingIntelligenceStore,
        *,
        margin_mode: str,
        execution_leverage: Decimal,
        demo_credentials_available: bool,
    ):
        self.exchange = exchange
        self.store = store
        self.margin_mode = margin_mode
        self.execution_leverage = execution_leverage
        self.demo_credentials_available = demo_credentials_available

    async def preflight(
        self,
        *,
        plan: TradePlan,
        market_state: MarketStateV2,
        requested_mode: TradingModeV2,
        configuration_fingerprint: str,
        live_grant: HumanLiveGrant | None,
        quant: QuantPrediction | None,
    ) -> TradingModeV2:
        """Reject known risk blockers before a plan is marked as triggered."""

        if requested_mode == TradingModeV2.LIVE_AUTHORIZED:
            raise EntryRiskBlocked(
                "Live execution is disabled until Live reconciliation is available"
            )
        try:
            validate_market_state_time(market_state)
        except ValueError as exc:
            raise EntryRiskBlocked(str(exc)) from exc
        mode = resolve_trading_mode(
            requested=requested_mode,
            symbol=plan.symbol,
            configuration_fingerprint_value=configuration_fingerprint,
            live_grant=live_grant,
            demo_credentials_available=self.demo_credentials_available,
        )
        if not market_state.healthy_for_new_risk:
            raise EntryRiskBlocked("MarketState data quality blocks new risk")
        if bool(market_state.events.values.get("new_risk_blocked")):
            raise EntryRiskBlocked("official event risk window blocks new risk")
        if mode == TradingModeV2.LIVE_AUTHORIZED and (
            quant is None or not quant.supports(plan.side)
        ):
            raise EntryRiskBlocked(
                "fresh direction-aligned positive Quant edge is required for Live"
            )
        if mode == TradingModeV2.LIVE_AUTHORIZED and (
            not live_grant
            or not live_grant.permits(
                symbol=plan.symbol,
                configuration_fingerprint=configuration_fingerprint,
            )
        ):
            raise EntryRiskBlocked("LIVE API denied by HumanLiveGrant")
        if mode != TradingModeV2.SHADOW:
            protection_check = self.exchange.protection_available
            # Keep third-party/test adapters implementing the original one-arg
            # interface usable while native adapters receive the hedge leg.
            if len(inspect.signature(protection_check).parameters) >= 2:
                available = await protection_check(
                    plan.symbol.removesuffix(":USDT"), PositionSide(plan.side)
                )
            else:
                available = await protection_check(plan.symbol.removesuffix(":USDT"))
            if not available:
                raise RuntimeError("native protection is unavailable")
        return mode

    async def execute(
        self,
        *,
        plan: TradePlan,
        allocation: RiskAllocation,
        market_state: MarketStateV2,
        market: MarketSnapshot,
        episode: DecisionEpisode,
        requested_mode: TradingModeV2,
        configuration_fingerprint: str,
        live_grant: HumanLiveGrant | None,
        quant: QuantPrediction | None,
        preflight_mode: TradingModeV2 | None = None,
    ) -> V2ExecutionResult:
        if TradingModeV2.LIVE_AUTHORIZED in (requested_mode, preflight_mode):
            raise EntryRiskBlocked(
                "Live execution is disabled until Live reconciliation is available"
            )
        if plan.order_type is not None:
            return await self._execute_immediate(
                plan=plan,
                allocation=allocation,
                market_state=market_state,
                market=market,
                episode=episode,
                requested_mode=requested_mode,
                configuration_fingerprint=configuration_fingerprint,
                live_grant=live_grant,
                quant=quant,
                preflight_mode=preflight_mode,
            )
        mode = preflight_mode or await self.preflight(
            plan=plan,
            market_state=market_state,
            requested_mode=requested_mode,
            configuration_fingerprint=configuration_fingerprint,
            live_grant=live_grant,
            quant=quant,
        )
        self.store.save_artifact(
            allocation.allocation_id,
            "RiskAllocation",
            allocation,
        )
        if mode == TradingModeV2.SHADOW:
            episode.status = "SHADOW_RECORDED"
            episode.risk_allocation_id = allocation.allocation_id
            self.store.save_episode(episode)
            return V2ExecutionResult(
                mode=mode,
                status="SHADOW_RECORDED",
                plan_id=plan.plan_id,
                allocation_id=allocation.allocation_id,
                reason="No exchange API was called",
            )
        adapter_plan = compile_execution_adapter_plan(
            plan,
            allocation,
            market,
            margin_mode=self.margin_mode,
            execution_leverage=self.execution_leverage,
            trading_mode=mode,
        )
        raw_key = f"{plan.plan_id}:{plan.version}:{content_hash(allocation)}:entry:v2"
        intent = OrderIntent(
            idempotency_key=f"ti{sha256(raw_key.encode()).hexdigest()[:20]}",
            plan_id=plan.plan_id,
            plan_fingerprint=content_hash(plan)[:16],
            symbol=adapter_plan.symbol,
            side=OrderSide.BUY if plan.side == "LONG" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=allocation.quantity,
            limit_price=(plan.entry_zone.lower + plan.entry_zone.upper) / Decimal("2"),
        )
        orders: list[Order] = await self.exchange.place_protected_order(adapter_plan, intent)
        try:
            for order in orders:
                verified = False
                for attempt in range(3):
                    verified = await self.exchange.verify_protection(order)
                    if verified:
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (attempt + 1))
                if not verified:
                    raise RuntimeError(f"native protection not verified for {order.id}")
                self.store.save_artifact(order.id, "ExchangeOrder", order)
        except Exception:
            for order in reversed(orders):
                with suppress(Exception):
                    await self.exchange.cancel_order(order.id, order.symbol)
            raise
        episode.status = "ORDERS_SUBMITTED"
        episode.risk_allocation_id = allocation.allocation_id
        episode.order_ids = [item.id for item in orders]
        position = self.store.position_for_plan(plan.plan_id)
        if position is None:
            position = PositionRuntimeV2(
                plan_id=plan.plan_id,
                episode_id=episode.episode_id,
                entry_order_ids=episode.order_ids,
                symbol=plan.symbol,
                side=plan.side,
                state=PositionStateV2.OPENING,
                original_quantity=allocation.quantity,
                remaining_quantity=Decimal("0"),
                average_entry=(plan.entry_zone.lower + plan.entry_zone.upper) / Decimal("2"),
                initial_stop=plan.management.hard_stop,
                current_stop=plan.management.hard_stop,
            )
            self.store.save_position(position)
        episode.position_id = position.position_id
        episode.updated_at = datetime.now(UTC)
        self.store.save_episode(episode)
        self.store.append_event(
            dedupe_key=f"orders:{plan.plan_id}:{allocation.allocation_id}",
            event_type="ORDERS_SUBMITTED",
            aggregate_type="plan",
            aggregate_id=plan.plan_id,
            aggregate_version=plan.version,
            payload={
                "mode": str(mode),
                "order_ids": episode.order_ids,
                "native_protection_verified": True,
            },
        )
        return V2ExecutionResult(
            mode=mode,
            status="SUBMITTED",
            plan_id=plan.plan_id,
            allocation_id=allocation.allocation_id,
            order_ids=episode.order_ids,
            protection_verified=True,
        )

    async def _execute_immediate(
        self,
        *,
        plan,
        allocation,
        market_state,
        market,
        episode,
        requested_mode,
        configuration_fingerprint,
        live_grant,
        quant,
        preflight_mode,
    ) -> V2ExecutionResult:
        from .entry_runtime import EntryOrderRuntime
        from .trading_intelligence import PlanState

        existing = self.store.entry_for_plan(plan.plan_id)
        if existing:
            return self._entry_result(existing)
        mode = preflight_mode or await self.preflight(
            plan=plan,
            market_state=market_state,
            requested_mode=requested_mode,
            configuration_fingerprint=configuration_fingerprint,
            live_grant=live_grant,
            quant=quant,
        )
        stable_id = sha256(f"{mode}:{plan.plan_id}".encode()).hexdigest()[:20]
        entry = EntryOrderRuntime(
            entry_id=f"entry_{stable_id}",
            plan_id=plan.plan_id,
            episode_id=episode.episode_id,
            symbol=plan.symbol,
            side=plan.side,
            mode=str(mode),
            plan_version=plan.version,
            order_type=plan.order_type,
            requested_price=plan.limit_price,
            reference_price=plan.reference_price or market.last,
            valid_price_range=plan.valid_price_range,
            expires_at=plan.expires_at,
            quantity=allocation.quantity,
            allocation_id=allocation.allocation_id,
            reserved_risk=allocation.risk_amount,
            reserved_margin=allocation.notional / allocation.leverage,
            reserved_stop_risk=getattr(allocation, "existing_stop_risk_increase", Decimal("0")),
            position_id=plan.position_id,
            client_order_ids=[
                f"ti{stable_id}{i}" for i in range(1, len(plan.management.take_profits) + 1)
            ],
        )
        reason = ""
        if datetime.now(UTC) >= plan.expires_at:
            reason = "decision expired before submission"
        elif plan.valid_price_range and not (
            plan.valid_price_range.lower <= market.last <= plan.valid_price_range.upper
        ):
            reason = "market left limit validity range before submission"
        elif episode.plan_id != plan.plan_id:
            reason = "decision episode does not own this plan"
        elif plan.position_id:
            position = self.store.get_position(plan.position_id)
            if (
                not position
                or position.version != plan.position_version
                or position.side != plan.side
            ):
                reason = "target position changed while deciding"
        if reason:
            entry = entry.model_copy(
                update={
                    "status": "REJECTED",
                    "reason": reason,
                    "reserved_risk": Decimal("0"),
                    "reserved_margin": Decimal("0"),
                    "reserved_stop_risk": Decimal("0"),
                }
            )
        self.store.save_artifact(allocation.allocation_id, "RiskAllocation", allocation)
        episode.risk_allocation_id = allocation.allocation_id
        self.store.save_episode(episode)
        entry, created = self.store.create_entry_order(entry)
        if not created or reason:
            return self._entry_result(entry)
        if mode == TradingModeV2.SHADOW:
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={
                    "status": "SHADOW_RECORDED",
                    "reason": "No exchange API was called",
                    "reserved_risk": Decimal("0"),
                    "reserved_margin": Decimal("0"),
                    "reserved_stop_risk": Decimal("0"),
                },
            )
            episode.status = "SHADOW_RECORDED"
            self.store.save_episode(episode)
            return self._entry_result(entry)
        adapter_plan = compile_execution_adapter_plan(
            plan,
            allocation,
            market,
            margin_mode=self.margin_mode,
            execution_leverage=self.execution_leverage,
            trading_mode=mode,
        )
        intent = OrderIntent(
            idempotency_key=f"ti{stable_id}",
            plan_id=plan.plan_id,
            plan_fingerprint=content_hash(plan)[:16],
            symbol=adapter_plan.symbol,
            side=OrderSide.BUY if plan.side == "LONG" else OrderSide.SELL,
            position_side=PositionSide(plan.side),
            order_type=OrderType(plan.order_type.lower()),
            quantity=allocation.quantity,
            limit_price=plan.limit_price,
        )
        try:
            # Submitted plans remain separate from the (possibly already existing) direction pool.
            current = self.store.get_plan(plan.plan_id)
            if current.state == PlanState.ACTIVE:
                current = self.store.transition_plan(
                    current.plan_id, expected_version=current.version, target=PlanState.ARMED
                )
            if current.state == PlanState.ARMED:
                self.store.transition_plan(
                    current.plan_id, expected_version=current.version, target=PlanState.TRIGGERED
                )
            orders = await self.exchange.place_protected_order(adapter_plan, intent)
            for order in orders:
                self.store.save_artifact(order.id, "ExchangeOrder", order)
            if not orders:
                raise RuntimeError("exchange returned no order acknowledgement")
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={
                    "order_ids": [o.id for o in orders],
                    "client_order_ids": [o.client_order_id for o in orders],
                    "submitted_price": orders[0].limit_price,
                    "status": "OPEN",
                },
            )
            episode.order_ids = entry.order_ids
            episode.status = "ORDERS_SUBMITTED"
            self.store.save_episode(episode)
            # Full/partial fills are accounted and protection verified by the durable lifecycle.
            self.store.append_event(
                dedupe_key=f"immediate-orders:{entry.entry_id}",
                event_type="ORDERS_SUBMITTED",
                aggregate_type="entry_order",
                aggregate_id=entry.entry_id,
                aggregate_version=entry.version,
                payload={"order_ids": entry.order_ids, "mode": entry.mode},
            )
        except Exception as exc:
            entry = self.store.get_entry_order(entry.entry_id)
            entry = self.store.update_entry_order(
                entry.entry_id,
                expected_version=entry.version,
                updates={"status": "UNCERTAIN", "reason": str(exc)[:500]},
            )
        return self._entry_result(entry)

    @staticmethod
    def _entry_result(entry):
        return V2ExecutionResult(
            mode=TradingModeV2(entry.mode),
            status=entry.status,
            plan_id=entry.plan_id,
            allocation_id=entry.allocation_id,
            order_ids=entry.order_ids,
            protection_verified=entry.protection_verified,
            reason=entry.reason,
        )
