from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ai_gateway import AIGateway
from .trading_intelligence import (
    DecisionAction,
    DecisionEpisode,
    DeltaContext,
    HumanLiveGrant,
    MarketAnalysis,
    MarketStateV2,
    NoTradeBlocker,
    PlanState,
    PortfolioTraderDecision,
    QuantPrediction,
    RiskAllocation,
    TradePlan,
    TradingModeV2,
    WaitPlan,
    WaitRuntimeStatus,
    WakeAction,
    WakeCondition,
    WakeKind,
    content_hash,
    evaluate_wake_condition,
    iter_wake_condition_leaves,
    metric_wake_path,
    normalize_wait_plan_liveness,
    wait_plan_liveness_errors,
)
from .trading_intelligence_store import TradingIntelligenceStore


class ExistingPositionRisk(BaseModel):
    """Current gross position risk already counted in portfolio_risk_used."""

    model_config = ConfigDict(extra="forbid")

    position_id: str = Field(min_length=1)
    position_version: int = Field(ge=1)
    symbol: str
    side: Literal["LONG", "SHORT"]
    quantity: Decimal = Field(gt=0)
    mark_price: Decimal = Field(gt=0)
    risk_amount: Decimal = Field(ge=0)


class RiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equity: Decimal = Field(gt=0)
    available_margin: Decimal = Field(gt=0)
    portfolio_risk_used: Decimal = Field(default=Decimal("0"), ge=0)
    daily_drawdown: Decimal = Field(default=Decimal("0"), ge=0)
    open_positions: int = Field(default=0, ge=0)
    estimated_slippage_bps: Decimal = Field(default=Decimal("0"), ge=0)
    liquidity_notional_cap: Decimal = Field(gt=0)
    correlation_risk_multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    existing_position: ExistingPositionRisk | None = None


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_per_trade: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.03"))
    max_portfolio_risk: Decimal = Field(default=Decimal("0.05"), gt=0, le=Decimal("0.20"))
    max_daily_drawdown: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("0.10"))
    max_open_positions: int = Field(default=5, ge=1, le=50)
    max_leverage: Decimal = Field(default=Decimal("3"), ge=1, le=Decimal("20"))
    max_slippage_bps: Decimal = Field(default=Decimal("20"), gt=0)
    minimum_quantity: Decimal = Field(default=Decimal("0.0001"), gt=0)
    minimum_reward_risk: Decimal = Field(default=Decimal("1.8"), ge=1, le=10)


def configuration_fingerprint(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode()).hexdigest()


class DeterministicRiskEngine:
    """Size immediate or legacy intent against gross account risk."""

    def allocate(
        self,
        plan: TradePlan,
        context: RiskContext,
        limits: RiskLimits,
        *,
        grant: HumanLiveGrant | None = None,
        live: bool = False,
    ) -> RiskAllocation:
        effective_risk = limits.risk_per_trade
        effective_portfolio_risk = limits.max_portfolio_risk
        effective_drawdown = limits.max_daily_drawdown
        effective_positions = limits.max_open_positions
        effective_leverage = limits.max_leverage
        constraints = ["deterministic-risk-v2", "llm-confidence-not-used"]
        if live:
            if not grant:
                raise PermissionError("LIVE API denied: no active HumanLiveGrant")
            effective_risk = min(effective_risk, grant.max_risk_per_trade)
            effective_portfolio_risk = min(effective_portfolio_risk, grant.max_portfolio_risk)
            effective_drawdown = min(effective_drawdown, grant.max_daily_drawdown)
            effective_positions = min(effective_positions, grant.max_open_positions)
            effective_leverage = min(effective_leverage, grant.max_leverage)
            constraints.append(f"human-live-grant:{grant.grant_id}")
        if context.daily_drawdown >= effective_drawdown:
            raise ValueError("daily drawdown limit reached")
        existing = context.existing_position
        if plan.position_id is not None and existing is None:
            raise ValueError("target position risk context is required for an add")
        if existing is not None and (
            existing.position_id != plan.position_id
            or existing.position_version != plan.position_version
            or existing.symbol.removesuffix(":USDT") != plan.symbol.removesuffix(":USDT")
            or existing.side != plan.side
        ):
            raise ValueError("position risk target id/version/symbol/side mismatch")
        if context.open_positions + (0 if existing else 1) > effective_positions:
            raise ValueError("maximum open positions reached")
        if context.estimated_slippage_bps > limits.max_slippage_bps:
            raise ValueError("estimated slippage exceeds configured limit")

        entry = plan.execution_price
        stop_distance = abs(entry - plan.management.hard_stop)
        if stop_distance <= 0:
            raise ValueError("stop distance must be positive")
        rewards = [
            (target.price - entry if plan.side == "LONG" else entry - target.price)
            for target in plan.management.take_profits
        ]
        actual_reward_risk = min(rewards) / stop_distance
        # Reward/risk belongs to the LLM-authored setup. Keep it as evidence,
        # but do not use it as a second strategy-approval gate here. This layer
        # owns account loss limits, sizing and leverage.
        constraints.append(f"llm-reward-risk={actual_reward_risk:.4f}")
        used_risk = context.portfolio_risk_used
        added_stop_risk = Decimal("0")
        if existing is not None:
            old_risk_fraction = existing.risk_amount / context.equity
            if old_risk_fraction > used_risk or context.open_positions < 1:
                raise ValueError("existing position risk must be included in portfolio context")
            distance = max(
                existing.mark_price - plan.management.hard_stop
                if plan.side == "LONG"
                else plan.management.hard_stop - existing.mark_price,
                Decimal("0"),
            )
            replacement_risk = existing.quantity * distance
            added_stop_risk = max(replacement_risk - existing.risk_amount, Decimal("0"))
            used_risk += (replacement_risk - existing.risk_amount) / context.equity
            constraints.append(f"pooled-position-stop-risk={replacement_risk}")
        remaining_portfolio_risk = max(
            effective_portfolio_risk - used_risk,
            Decimal("0"),
        )
        # Widening an existing stop spends the same per-decision budget as the add.
        allowed_fraction = min(
            max(effective_risk - added_stop_risk / context.equity, Decimal("0")),
            remaining_portfolio_risk,
        )
        risk_amount = context.equity * allowed_fraction * context.correlation_risk_multiplier
        if risk_amount <= 0:
            raise ValueError("portfolio risk budget exhausted")
        quantity = risk_amount / stop_distance
        notional = quantity * entry
        margin_cap = context.available_margin * effective_leverage.to_integral_value(
            rounding=ROUND_FLOOR
        )
        if notional > margin_cap:
            notional = margin_cap
            quantity = notional / entry
            risk_amount = quantity * stop_distance
            constraints.append("available-margin-resized")
        if notional > context.liquidity_notional_cap:
            notional = context.liquidity_notional_cap
            quantity = notional / entry
            risk_amount = quantity * stop_distance
            constraints.append("liquidity-cap")
        if quantity < limits.minimum_quantity:
            raise ValueError("risk-sized quantity is below exchange minimum")
        leverage_needed = notional / context.available_margin
        leverage = max(
            Decimal("1"),
            min(
                effective_leverage.to_integral_value(rounding=ROUND_FLOOR),
                leverage_needed.to_integral_value(rounding=ROUND_CEILING),
            ),
        )
        if notional / leverage > context.available_margin:
            raise ValueError("available margin is insufficient within leverage limit")
        constraints.extend(
            [
                f"risk-per-trade<={effective_risk}",
                f"portfolio-risk<={effective_portfolio_risk}",
                f"leverage<={effective_leverage}",
            ]
        )
        return RiskAllocation(
            plan_id=plan.plan_id,
            equity=context.equity,
            risk_amount=risk_amount.quantize(Decimal("0.00000001")),
            existing_stop_risk_increase=added_stop_risk.quantize(Decimal("0.00000001")),
            quantity=quantity.quantize(Decimal("0.00000001"), rounding=ROUND_FLOOR),
            notional=notional.quantize(Decimal("0.00000001")),
            leverage=leverage.quantize(Decimal("0.01")),
            constraints=constraints,
            configuration_fingerprint=configuration_fingerprint(limits),
        )


def resolve_trading_mode(
    *,
    requested: TradingModeV2,
    symbol: str,
    configuration_fingerprint_value: str,
    live_grant: HumanLiveGrant | None,
    demo_credentials_available: bool,
    now: datetime | None = None,
) -> TradingModeV2:
    """Fail closed: an invalid live request routes to demo, then shadow."""

    if (
        requested == TradingModeV2.LIVE_AUTHORIZED
        and live_grant
        and live_grant.permits(
            symbol=symbol,
            configuration_fingerprint=configuration_fingerprint_value,
            now=now,
        )
    ):
        return TradingModeV2.LIVE_AUTHORIZED
    if demo_credentials_available:
        return TradingModeV2.OKX_DEMO
    return TradingModeV2.SHADOW


def quant_gate(
    decision: PortfolioTraderDecision,
    prediction: QuantPrediction | None,
    *,
    require_edge: bool,
    now: datetime | None = None,
) -> PortfolioTraderDecision:
    if decision.action not in {DecisionAction.LONG, DecisionAction.SHORT}:
        return decision
    if prediction and prediction.supports(decision.action, now=now):
        return decision
    if not require_edge:
        return decision
    reason = (
        "Quant edge unavailable or expired"
        if prediction is None or (now or datetime.now(UTC)) > prediction.expires_at
        else "Quant direction conflicts or expected return after cost is non-positive"
    )
    decision_time = now or datetime.now(UTC)
    wake = WakeCondition(
        kind=WakeKind.EXTERNAL_EVENT,
        action=WakeAction.RECALL_LLM,
        path="event",
        value="QUANT_PREDICTION_UPDATED",
        expires_at=decision_time + timedelta(minutes=10),
    )
    thesis_id = decision.thesis.thesis_id if decision.thesis else None
    wake_conditions = [wake]
    if thesis_id is None:
        # A no-thesis wait may never depend solely on a provider event. A market
        # regime transition provides a deterministic, data-driven liveness path.
        wake_conditions.append(
            WakeCondition(
                kind=WakeKind.REGIME_CHANGED,
                action=WakeAction.RECALL_LLM,
                path="regime",
                expires_at=decision_time + timedelta(minutes=10),
            )
        )
    return PortfolioTraderDecision(
        action=DecisionAction.WAIT if thesis_id else DecisionAction.NO_TRADE,
        rationale=[reason, "Deterministic Quant gate blocked entry"],
        thesis=decision.thesis,
        hard_blockers=[NoTradeBlocker.QUANT_LIVE_GATE] if thesis_id is None else [],
        wait_plan=WaitPlan(
            thesis_id=thesis_id,
            reason=reason,
            wake_conditions=wake_conditions,
            expires_at=decision_time + timedelta(minutes=10),
        ),
    )


@dataclass(frozen=True)
class DualLLMResult:
    analysis: MarketAnalysis
    decision: PortfolioTraderDecision
    episode: DecisionEpisode


_MATERIAL_REGIMES = {
    "STRONG_TREND_UP",
    "STRONG_TREND_DOWN",
    "VOLATILITY_EXPANSION",
    "PANIC",
    "REVERSAL",
}


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _ema_alignment(features: dict[str, Any]) -> str | None:
    ema20 = _as_decimal(features.get("ema_20"))
    ema50 = _as_decimal(features.get("ema_50"))
    ema200 = _as_decimal(features.get("ema_200"))
    if None in {ema20, ema50, ema200}:
        return None
    if ema20 > ema50 > ema200:
        return "BULLISH"
    if ema20 < ema50 < ema200:
        return "BEARISH"
    return None


def material_market_state_transitions(
    previous: MarketStateV2 | None,
    current: MarketStateV2,
) -> list[str]:
    """Detect strict edge transitions worthy of a no-thesis LLM recall.

    This intentionally ignores ordinary price ticks. The predicates must move
    from false to true, so a persistent strong trend does not repeatedly wake
    the decision path.
    """

    if previous is None:
        return []
    transitions: list[str] = []
    current_regime = str(current.regime)
    if str(previous.regime) != current_regime and current_regime in _MATERIAL_REGIMES:
        transitions.append(f"regime:{previous.regime}->{current.regime}")
    for key in ("breakout_up", "breakout_down", "structure_invalidated"):
        if bool(current.structure.values.get(key)) and not bool(previous.structure.values.get(key)):
            transitions.append(f"structure:{key}")

    for timeframe in ("5m", "15m"):
        before = previous.timeframe_features.get(timeframe, {})
        after = current.timeframe_features.get(timeframe, {})
        relative_volume = _as_decimal(after.get("relative_volume_20"))
        adx = _as_decimal(after.get("adx_14"))
        if relative_volume is None or adx is None:
            continue
        liquid_trend = relative_volume >= Decimal("1.5") and adx >= Decimal("25")
        if (
            liquid_trend
            and bool(after.get("volatility_expansion"))
            and not bool(before.get("volatility_expansion"))
        ):
            transitions.append(f"{timeframe}:volume-backed-volatility-expansion")
        prior_adx = _as_decimal(before.get("adx_14"))
        alignment = _ema_alignment(after)
        if (
            liquid_trend
            and alignment is not None
            and (prior_adx is None or prior_adx < Decimal("25") <= adx)
        ):
            transitions.append(f"{timeframe}:adx-strength:{alignment}")
        prior_alignment = _ema_alignment(before)
        if liquid_trend and alignment is not None and alignment != prior_alignment:
            transitions.append(f"{timeframe}:ema-alignment:{alignment}")
    return list(dict.fromkeys(transitions))


def _market_wake_payload(state: MarketStateV2 | None) -> dict[str, Any]:
    if state is None:
        return {}
    payload = state.model_dump(mode="json", exclude_none=True)
    payload["price"] = state.market.values.get("last")
    payload["candles"] = state.timeframe_features
    return payload


def _read_payload_path(payload: dict[str, Any], path: str | None) -> Any:
    value: Any = payload
    for part in (path or "").split("."):
        if not part or not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def trade_plan_semantic_errors(
    decision: PortfolioTraderDecision,
    market_state: MarketStateV2,
    *,
    minimum_reward_risk: Decimal,
) -> list[str]:
    """Validate price scale, executable paths, ownership and actual reward/risk."""

    if decision.action not in {DecisionAction.LONG, DecisionAction.SHORT}:
        return []
    plan = decision.trade_plan
    thesis = decision.thesis
    if plan is None or thesis is None:
        return ["TRADE_PLAN_OR_THESIS_MISSING"]
    errors: list[str] = []
    if plan.symbol != market_state.symbol or thesis.symbol != market_state.symbol:
        errors.append("SYMBOL_CONTEXT_MISMATCH")
    if thesis.direction != decision.action:
        errors.append("THESIS_DIRECTION_MISMATCH")
    if plan.expires_at <= market_state.data_as_of:
        errors.append("TRADE_PLAN_ALREADY_EXPIRED")
    if plan.expires_at > market_state.data_as_of + timedelta(hours=4):
        errors.append("TRADE_PLAN_HORIZON_EXCEEDS_4H")

    current_price = _as_decimal(market_state.market.values.get("last"))
    atr_value = _as_decimal(
        market_state.timeframe_features.get("5m", {}).get("atr_14")
    ) or _as_decimal(market_state.volatility.values.get("atr_14"))
    if current_price is None or current_price <= 0:
        errors.append("CURRENT_PRICE_UNAVAILABLE")
    else:
        maximum_distance = max(
            current_price * Decimal("0.20"),
            (atr_value or Decimal("0")) * Decimal("12"),
        )
        price_points = {
            "entry_lower": plan.entry_zone.lower,
            "entry_upper": plan.entry_zone.upper,
            "hard_stop": plan.management.hard_stop,
            **{
                f"target_{index}": target.price
                for index, target in enumerate(plan.management.take_profits, start=1)
            },
        }
        for label, value in price_points.items():
            if abs(value - current_price) > maximum_distance:
                errors.append(f"IMPLAUSIBLE_PRICE_SCALE:{label}")

        payload = _market_wake_payload(market_state)
        price_kinds = {
            WakeKind.PRICE_CROSSES_ABOVE,
            WakeKind.PRICE_CROSSES_BELOW,
            WakeKind.CANDLE_CLOSE_ABOVE,
            WakeKind.CANDLE_CLOSE_BELOW,
        }
        range_kinds = {WakeKind.PRICE_IN_ZONE}
        metric_kinds = {
            WakeKind.METRIC_GT,
            WakeKind.METRIC_LT,
            WakeKind.METRIC_CROSSES_ABOVE,
            WakeKind.METRIC_CROSSES_BELOW,
            WakeKind.METRIC_IN_RANGE,
        }
        for condition in plan.entry_conditions:
            for leaf in iter_wake_condition_leaves(condition):
                if leaf.kind not in {
                    *price_kinds,
                    *range_kinds,
                    *metric_kinds,
                    WakeKind.STRUCTURE_CHANGED,
                    WakeKind.REGIME_CHANGED,
                }:
                    errors.append(f"ENTRY_CONDITION_NOT_MARKET_DERIVED:{leaf.condition_id}")
                    continue
                if leaf.kind in price_kinds:
                    threshold = _as_decimal(leaf.value)
                    if threshold is not None and abs(threshold - current_price) > maximum_distance:
                        errors.append(f"IMPLAUSIBLE_PRICE_SCALE:{leaf.condition_id}")
                elif leaf.kind in range_kinds:
                    if (
                        leaf.lower is not None
                        and abs(leaf.lower - current_price) > maximum_distance
                    ):
                        errors.append(f"IMPLAUSIBLE_PRICE_SCALE:{leaf.condition_id}:lower")
                    if (
                        leaf.upper is not None
                        and abs(leaf.upper - current_price) > maximum_distance
                    ):
                        errors.append(f"IMPLAUSIBLE_PRICE_SCALE:{leaf.condition_id}:upper")

                path: str | None = None
                if leaf.kind in metric_kinds:
                    path = metric_wake_path(leaf)
                elif leaf.kind in {
                    WakeKind.CANDLE_CLOSE_ABOVE,
                    WakeKind.CANDLE_CLOSE_BELOW,
                }:
                    path = f"candles.{leaf.timeframe}.close"
                elif leaf.kind == WakeKind.STRUCTURE_CHANGED:
                    path = leaf.path or "structure.values.structure"
                elif leaf.kind == WakeKind.REGIME_CHANGED:
                    path = leaf.path or "regime"
                if path and _read_payload_path(payload, path) is None:
                    errors.append(f"UNRESOLVABLE_WAKE_PATH:{leaf.condition_id}:{path}")

    entry = plan.execution_price
    stop_distance = abs(entry - plan.management.hard_stop)
    if stop_distance <= 0:
        errors.append("INVALID_STOP_DISTANCE")
    else:
        rewards = [
            (target.price - entry if plan.side == "LONG" else entry - target.price)
            for target in plan.management.take_profits
        ]
        actual_reward_risk = min(rewards) / stop_distance
        if (
            plan.minimum_reward_risk < minimum_reward_risk
            or actual_reward_risk < minimum_reward_risk
        ):
            errors.append(
                "MINIMUM_REWARD_RISK_NOT_MET:"
                f"actual={actual_reward_risk:.4f}:required={minimum_reward_risk}"
            )
    return list(dict.fromkeys(errors))


def decision_policy_errors(
    decision: PortfolioTraderDecision,
    *,
    previous_thesis: dict[str, Any] | None,
) -> list[str]:
    """Legacy policy diagnostics retained for persisted callers.

    The active v2 decision contract intentionally permits a thesis-less WAIT or
    NO_TRADE.  This helper is not part of the hot path, but older integrations
    still use it to flag a directional WAIT that carries a fresh thesis without
    a conditional trade plan.
    """
    if (
        decision.action == DecisionAction.WAIT
        and decision.thesis is not None
        and decision.trade_plan is None
    ):
        return ["DIRECTIONAL_WAIT_REQUIRES_CONDITIONAL_TRADE_PLAN"]
    return []


def new_decision_contract_errors(
    decision: PortfolioTraderDecision,
    *,
    market_state: MarketStateV2 | None = None,
    portfolio: dict[str, Any] | None = None,
) -> list[str]:
    """Enforce generation-only requirements without breaking persisted legacy records."""

    errors: list[str] = []
    plan = decision.trade_plan
    is_trade = decision.action in {DecisionAction.LONG, DecisionAction.SHORT}
    if is_trade and (plan is None or plan.order_type is None):
        errors.append("NEW_TRADE_REQUIRES_IMMEDIATE_ORDER_TYPE")
    if plan and not is_trade:
        errors.append("NON_TRADE_ACTION_CANNOT_CARRY_TRADE_PLAN")
    if market_state and plan:
        if plan.symbol != market_state.symbol or (
            decision.thesis and decision.thesis.symbol != market_state.symbol
        ):
            errors.append("SYMBOL_CONTEXT_MISMATCH")
        if decision.thesis and decision.thesis.direction != decision.action:
            errors.append("THESIS_DIRECTION_MISMATCH")
        price = _as_decimal(market_state.market.values.get("last"))
        if (
            plan.order_type is not None
            and plan.valid_price_range is not None
            and price is not None
            and not plan.valid_price_range.lower <= price <= plan.valid_price_range.upper
        ):
            errors.append("CURRENT_PRICE_OUTSIDE_ENTRY_VALIDITY_RANGE")
        if (
            not market_state.data_as_of
            < plan.expires_at
            <= (market_state.data_as_of + timedelta(hours=4))
        ):
            errors.append("NEW_TRADE_EXPIRY_MUST_BE_FUTURE_WITHIN_4H")
    if (
        decision.action in {DecisionAction.MANAGE_EXISTING, DecisionAction.EXIT}
        and decision.position_id is None
    ):
        errors.append("MANAGEMENT_REQUIRES_POSITION_ID_AND_VERSION")

    target_id = plan.position_id if is_trade and plan else decision.position_id
    target_version = plan.position_version if is_trade and plan else decision.position_version
    if portfolio is not None:
        positions = portfolio.get("symbol_positions", [])
        positions = [
            row for row in positions if str(row.get("state", "OPEN")) not in {"CLOSED", "FLAT"}
        ]
        if target_id is not None:
            matched = next((row for row in positions if row.get("position_id") == target_id), None)
            if matched is None or matched.get("position_version", matched.get("version")) != (
                target_version
            ):
                errors.append("POSITION_TARGET_NOT_CURRENT")
            elif (
                market_state and matched.get("symbol", market_state.symbol) != market_state.symbol
            ) or (is_trade and plan and matched.get("side") != plan.side):
                errors.append("POSITION_TARGET_SYMBOL_OR_DIRECTION_MISMATCH")
        elif is_trade and plan and any(row.get("side") == plan.side for row in positions):
            errors.append("EXISTING_DIRECTION_ADD_REQUIRES_POSITION_TARGET")

    if decision.action in {DecisionAction.WAIT, DecisionAction.NO_TRADE} and decision.wait_plan:
        wait = decision.wait_plan
        if market_state:
            errors.extend(
                wait_plan_liveness_errors(
                    wait, action=decision.action, data_as_of=market_state.data_as_of
                )
            )
        price = _as_decimal(market_state.market.values.get("last")) if market_state else None
        price_kinds = {
            WakeKind.PRICE_CROSSES_ABOVE,
            WakeKind.PRICE_CROSSES_BELOW,
            WakeKind.PRICE_IN_ZONE,
            WakeKind.CANDLE_CLOSE_ABOVE,
            WakeKind.CANDLE_CLOSE_BELOW,
        }

        def future_price(condition: WakeCondition) -> bool:
            if condition.kind in {WakeKind.ALL, WakeKind.ANY}:
                return any(future_price(child) for child in condition.children)
            if condition.kind not in price_kinds:
                return False
            if (
                market_state
                and condition.expires_at
                and (condition.expires_at <= market_state.data_as_of)
            ):
                return False
            if condition.kind == WakeKind.PRICE_IN_ZONE:
                return bool(
                    condition.lower is not None
                    and condition.upper is not None
                    and condition.lower > 0
                    and (price is None or not condition.lower <= price <= condition.upper)
                )
            threshold = _as_decimal(condition.value)
            if threshold is None or not threshold.is_finite() or threshold <= 0:
                return False
            if (
                market_state
                and condition.kind
                in {
                    WakeKind.CANDLE_CLOSE_ABOVE,
                    WakeKind.CANDLE_CLOSE_BELOW,
                }
                and _read_payload_path(
                    _market_wake_payload(market_state), f"candles.{condition.timeframe}.close"
                )
                is None
            ):
                return False
            above = condition.kind in {WakeKind.PRICE_CROSSES_ABOVE, WakeKind.CANDLE_CLOSE_ABOVE}
            return price is None or (threshold > price if above else threshold < price)

        if not any(future_price(condition) for condition in wait.wake_conditions):
            errors.append("NEW_WAIT_REQUIRES_ACTUAL_FUTURE_PRICE_WAKE")
    return errors


def normalize_direct_trade_decision(
    decision: PortfolioTraderDecision,
    market_state: MarketStateV2,
) -> PortfolioTraderDecision:
    """Adapt the sole Trader LLM's exact levels into one executable entry rule.

    Direction, entry zone, stop and targets remain exactly as authored by the
    LLM. Context ownership and lifetime are aligned deterministically so a plan
    cannot remain active after its entry condition has silently expired.
    """

    if decision.action not in {DecisionAction.LONG, DecisionAction.SHORT}:
        return decision
    plan = decision.trade_plan
    thesis = decision.thesis
    if plan is None or thesis is None:
        return decision
    if plan.order_type is not None:
        # Execution owns immediate intents. Never manufacture a durable price wake.
        return decision

    data_as_of = market_state.data_as_of
    maximum_expiry = data_as_of + timedelta(hours=4)
    plan_expiry = min(
        max(plan.expires_at, data_as_of + timedelta(minutes=15)),
        maximum_expiry,
    )
    direct_entry = WakeCondition(
        condition_id=f"direct-entry-{plan.plan_id}",
        kind=WakeKind.PRICE_IN_ZONE,
        action=WakeAction.EXECUTE_ENTRY,
        path="price",
        lower=plan.entry_zone.lower,
        upper=plan.entry_zone.upper,
        expires_at=plan_expiry,
    )
    normalized_thesis = thesis.model_copy(
        update={
            "symbol": market_state.symbol,
            "direction": str(decision.action),
            "expires_at": max(thesis.expires_at, plan_expiry),
        }
    )
    normalized_plan = plan.model_copy(
        update={
            "thesis_id": normalized_thesis.thesis_id,
            "symbol": market_state.symbol,
            "side": str(decision.action),
            "entry_conditions": [direct_entry],
            "expires_at": plan_expiry,
        }
    )
    return decision.model_copy(
        update={
            "thesis": normalized_thesis,
            "trade_plan": normalized_plan,
            "rationale": [
                *decision.rationale,
                "Direct execution: the LLM entry zone is the sole market trigger.",
            ],
        }
    )


def normalize_already_satisfied_wait_conditions(
    wait_plan: WaitPlan,
    *,
    action: DecisionAction | str,
    current: MarketStateV2,
    previous: MarketStateV2 | None,
) -> tuple[WaitPlan, list[str]]:
    """Remove wait rules that describe the present instead of a future edge."""

    current_payload = _market_wake_payload(current)
    previous_payload = _market_wake_payload(previous)
    changes: list[str] = []

    def sanitize(condition: WakeCondition) -> WakeCondition | None:
        if condition.kind == WakeKind.ANY:
            children: list[WakeCondition] = []
            for child in condition.children:
                if evaluate_wake_condition(
                    child,
                    current_payload,
                    previous_payload,
                    now=current.data_as_of,
                ):
                    changes.append(f"REMOVED_ALREADY_TRUE:{child.condition_id}")
                    continue
                children.append(child)
            if not children:
                changes.append(f"REMOVED_EMPTY_COMPOSITE:{condition.condition_id}")
                return None
            if len(children) == 1:
                changes.append(f"COLLAPSED_COMPOSITE:{condition.condition_id}")
                return children[0]
            return condition.model_copy(update={"children": children})
        if evaluate_wake_condition(
            condition,
            current_payload,
            previous_payload,
            now=current.data_as_of,
        ):
            changes.append(f"REMOVED_ALREADY_TRUE:{condition.condition_id}")
            return None
        return condition

    conditions = [
        condition
        for source in wait_plan.wake_conditions
        if (condition := sanitize(source)) is not None
    ]
    interim = wait_plan.model_copy(update={"wake_conditions": conditions})
    normalized, liveness_changes = normalize_wait_plan_liveness(
        interim,
        action=action,
        data_as_of=current.data_as_of,
    )
    return normalized, [*changes, *liveness_changes]


def normalize_wait_plan_semantics(
    wait_plan: WaitPlan,
    *,
    action: DecisionAction | str,
    current: MarketStateV2,
) -> tuple[WaitPlan, list[str]]:
    """Remove hallucinated price scales and paths without inventing a trade signal."""

    payload = _market_wake_payload(current)
    current_price = _as_decimal(current.market.values.get("last"))
    atr_value = _as_decimal(current.timeframe_features.get("5m", {}).get("atr_14")) or _as_decimal(
        current.volatility.values.get("atr_14")
    )
    maximum_distance = (
        max(
            current_price * Decimal("0.20"),
            (atr_value or Decimal("0")) * Decimal("12"),
        )
        if current_price is not None and current_price > 0
        else None
    )
    changes: list[str] = []
    metric_kinds = {
        WakeKind.METRIC_GT,
        WakeKind.METRIC_LT,
        WakeKind.METRIC_CROSSES_ABOVE,
        WakeKind.METRIC_CROSSES_BELOW,
        WakeKind.METRIC_IN_RANGE,
    }

    def invalid_reason(condition: WakeCondition) -> str | None:
        if maximum_distance is not None and current_price is not None:
            if condition.kind in {
                WakeKind.PRICE_CROSSES_ABOVE,
                WakeKind.PRICE_CROSSES_BELOW,
                WakeKind.CANDLE_CLOSE_ABOVE,
                WakeKind.CANDLE_CLOSE_BELOW,
            }:
                threshold = _as_decimal(condition.value)
                if threshold is not None and abs(threshold - current_price) > maximum_distance:
                    return "IMPLAUSIBLE_PRICE_SCALE"
            if condition.kind == WakeKind.PRICE_IN_ZONE and (
                (
                    condition.lower is not None
                    and abs(condition.lower - current_price) > maximum_distance
                )
                or (
                    condition.upper is not None
                    and abs(condition.upper - current_price) > maximum_distance
                )
            ):
                return "IMPLAUSIBLE_PRICE_SCALE"
        path: str | None = None
        if condition.kind in metric_kinds:
            path = metric_wake_path(condition)
        elif condition.kind in {
            WakeKind.CANDLE_CLOSE_ABOVE,
            WakeKind.CANDLE_CLOSE_BELOW,
        }:
            path = f"candles.{condition.timeframe}.close"
        elif condition.kind == WakeKind.STRUCTURE_CHANGED:
            path = condition.path or "structure.values.structure"
        elif condition.kind == WakeKind.REGIME_CHANGED:
            path = condition.path or "regime"
        if path and _read_payload_path(payload, path) is None:
            return f"UNRESOLVABLE_WAKE_PATH:{path}"
        return None

    def sanitize(condition: WakeCondition) -> WakeCondition | None:
        if condition.kind not in {WakeKind.ALL, WakeKind.ANY}:
            reason = invalid_reason(condition)
            if reason:
                changes.append(f"REMOVED_{reason}:{condition.condition_id}")
                return None
            return condition
        children = [
            child for source in condition.children if (child := sanitize(source)) is not None
        ]
        if not children:
            changes.append(f"REMOVED_EMPTY_COMPOSITE:{condition.condition_id}")
            return None
        if len(children) == 1:
            changes.append(f"COLLAPSED_COMPOSITE:{condition.condition_id}")
            return children[0]
        return condition.model_copy(update={"children": children})

    conditions = [
        condition
        for source in wait_plan.wake_conditions
        if (condition := sanitize(source)) is not None
    ]
    interim = wait_plan.model_copy(update={"wake_conditions": conditions})
    normalized, liveness_changes = normalize_wait_plan_liveness(
        interim,
        action=action,
        data_as_of=current.data_as_of,
    )
    price_wake_kinds = {
        WakeKind.PRICE_CROSSES_ABOVE,
        WakeKind.PRICE_CROSSES_BELOW,
        WakeKind.PRICE_IN_ZONE,
        WakeKind.CANDLE_CLOSE_ABOVE,
        WakeKind.CANDLE_CLOSE_BELOW,
    }
    if current_price and not any(
        leaf.kind in price_wake_kinds
        for condition in normalized.wake_conditions
        for leaf in iter_wake_condition_leaves(condition)
    ):
        step = max(
            atr_value or Decimal("0"),
            current_price * Decimal("0.005"),
            Decimal("0.00000001"),
        )
        deadline = normalized.expires_at
        wake_id = current.state_hash[:12]
        normalized = normalized.model_copy(
            update={
                "wake_conditions": [
                    *normalized.wake_conditions,
                    WakeCondition(
                        condition_id=f"future-price-wake-{wake_id}",
                        kind=WakeKind.ANY,
                        action=WakeAction.RECALL_LLM,
                        children=[
                            WakeCondition(
                                condition_id=f"future-price-up-{wake_id}",
                                kind=WakeKind.PRICE_CROSSES_ABOVE,
                                action=WakeAction.RECALL_LLM,
                                path="price",
                                value=current_price + step,
                                expires_at=deadline,
                            ),
                            WakeCondition(
                                condition_id=f"future-price-down-{wake_id}",
                                kind=WakeKind.PRICE_CROSSES_BELOW,
                                action=WakeAction.RECALL_LLM,
                                path="price",
                                value=max(current_price - step, Decimal("0.00000001")),
                                expires_at=deadline,
                            ),
                        ],
                        expires_at=deadline,
                    ),
                ]
            }
        )
        liveness_changes = [*liveness_changes, "ADDED_FUTURE_PRICE_WAKE"]
    return normalized, [*changes, *liveness_changes]


class DualLLMDecisionService:
    """Exactly two decision roles: MarketAnalyst followed by PortfolioTrader."""

    def __init__(self, gateway: AIGateway):
        self.gateway = gateway

    async def decide(
        self,
        *,
        market_state: MarketStateV2,
        delta: DeltaContext,
        quant: QuantPrediction | None,
        portfolio: dict[str, Any],
        previous_thesis: dict[str, Any] | None,
        previous_plan: dict[str, Any] | None,
        require_quant_edge: bool,
        minimum_reward_risk: Decimal = Decimal("1.8"),
        previous_wait_plan: dict[str, Any] | None = None,
        previous_market_state: MarketStateV2 | None = None,
        learning: dict[str, Any] | None = None,
    ) -> DualLLMResult:
        analyst_prompt = json.dumps(
            {
                "market_state": market_state.model_dump(mode="json", exclude_none=True),
                "delta": delta.model_dump(mode="json", exclude_none=True),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        analyst_result, _ = await self.gateway.generate_json(
            system=(
                "You are COSMOS MarketAnalyst. Explain only the normalized market state. "
                "Do not recommend trades, position size, leverage, or exchange actions."
            ),
            prompt=analyst_prompt,
            schema=MarketAnalysis.model_json_schema(),
            schema_name="market_analysis_v2",
            operation="trading_intelligence_analyst",
            prompt_version="market_analyst_v2",
            schema_version="market_analysis_v2",
            max_tokens=3000,
            thinking_enabled=False,
        )
        analysis, _ = await self._validate_with_one_repair(
            MarketAnalysis,
            analyst_result.data,
            role="MarketAnalyst",
            source_prompt=analyst_prompt,
        )
        trader_prompt = json.dumps(
            {
                "market_state_hash": market_state.state_hash,
                "data_as_of": market_state.data_as_of.isoformat(),
                "market_state": market_state.model_dump(mode="json", exclude_none=True),
                "analyst": analysis.model_dump(mode="json", exclude_none=True),
                "quant": quant.model_dump(mode="json") if quant else None,
                "quant_policy": "HARD_GATE" if require_quant_edge else "ADVISORY",
                "minimum_reward_risk": str(minimum_reward_risk),
                "portfolio": portfolio,
                "learning": learning or {"policy": "ADVISORY_ONLY", "groups": []},
                "previous_thesis": previous_thesis,
                "previous_plan": previous_plan,
                "previous_wait_plan": previous_wait_plan,
                "delta": delta.model_dump(mode="json", exclude_none=True),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        trader_result, _ = await self.gateway.generate_json(
            system=(
                "You are the sole COSMOS trading decision authority. Return only the decision "
                "contract; there is no downstream strategy reviewer or critic. "
                "Never output quantity, position fraction, leverage, or exchange requests. "
                "Evaluate only portfolio.decision_symbol. Positions in OTHER symbols do not "
                "make this symbol occupied and never imply MANAGE_EXISTING or EXIT here. "
                "Use symbol_positions for position ownership. LONG and SHORT are independent "
                "hedged directions on the same symbol. For an add to an existing direction, "
                "TradePlan must target its exact position_id and position_version; management "
                "rules will govern the entire pooled position after the add fills. For "
                "MANAGE_EXISTING or EXIT set decision.position_id and position_version to the "
                "chosen position's id and current version. An opposite position does not block "
                "Copy these fields exactly from portfolio.symbol_positions; never invent them "
                "or copy a plan version. If ownership is unavailable, return WAIT. "
                "a new direction. Do not invalidate another position's thesis. "
                "Learning contains versioned, cost-aware historical outcomes. Use its "
                "sample counts and shrunken estimates to improve entry location, stop placement "
                "and profit-taking. Learning is ADVISORY_ONLY: low win rate, negative expectancy "
                "or missing samples never create a new blocker or veto. Explain any adaptation "
                "in rationale. Current market structure takes precedence over past outcomes. "
                "Tag TradePlan.setup consistently as trend_following, pullback, breakout, "
                "or mean_reversion where applicable so outcomes can improve the same setup. "
                "Portfolio cash, margin and order sizing are deliberately absent: deterministic "
                "Risk Engine owns affordability, size and leverage, so never decline a setup for "
                "cash, available margin, or position-size concerns. quant_policy=ADVISORY means "
                "Quant may lower confidence but may not veto a Demo/Shadow TradePlan; only "
                "quant_policy=HARD_GATE can block an otherwise valid direction. "
                "LONG/SHORT authorize immediate order submission after deterministic risk checks. "
                "Return a complete thesis and TradePlan with order_type MARKET or LIMIT and "
                "entry_conditions empty. MARKET requires reference_price at the current market; "
                "LIMIT requires a positive limit_price and valid_price_range lower/upper "
                "containing both the limit price and current market_state.market.values.last. "
                "This is the setup's validity range, not a narrow fill-price target: current "
                "price outside it rejects submission and cancels resting entries. LIMIT is "
                "submitted now, even when the limit price is away from market. "
                "entry_zone is descriptive; execution and risk use the explicit limit/reference "
                "price. Choose WAIT when a setup requires future confirmation before an order "
                "may be submitted. WAIT may have no prior or returned thesis. NO_TRADE may mean "
                "no current opportunity without any hard_blocker; explain the reason honestly. "
                "Healthy market data never obliges a trade. Both WAIT and NO_TRADE require a "
                "real future price wake with action RECALL_LLM and a bounded expiry. "
                "Every WakeCondition must obey: "
                "kind is exactly one of PRICE_CROSSES_ABOVE, PRICE_CROSSES_BELOW, "
                "PRICE_IN_ZONE, CANDLE_CLOSE_ABOVE, CANDLE_CLOSE_BELOW, METRIC_GT, "
                "METRIC_LT, METRIC_CROSSES_ABOVE, METRIC_CROSSES_BELOW, METRIC_IN_RANGE, "
                "STRUCTURE_CHANGED, REGIME_CHANGED, EXPIRES_AT, ORDER_EVENT, "
                "POSITION_EVENT, EXTERNAL_EVENT, ALL, ANY; never invent another kind. "
                "ALL/ANY has at least two children with the same action; PRICE_IN_ZONE and "
                "METRIC_IN_RANGE have ordered lower/upper; metric comparisons have path and "
                "value; candle-close comparisons have timeframe and value. WAIT/NO_TRADE "
                "wake actions are RECALL_LLM. Prefer paths price, mark_price, adx, rsi, "
                "macd_histogram, relative_volume, trade_flow_imbalance, or full MarketState "
                "paths. Wake prices must describe a future observation that recalls the trader; "
                "they never authorize entry. All expirations must be later than data_as_of."
                " A WAIT condition must describe a future state: never return a condition "
                "that is already satisfied by market_state, and never repeat the exact "
                "condition that caused this invocation unless its threshold materially changes. "
                "EXTERNAL_EVENT value may only be QUANT_PREDICTION_UPDATED. Official risk "
                "events use a separate trusted dispatcher. WAIT/NO_TRADE must include at least "
                "one future price or candle-price wake; a timer, metric or external event alone "
                "is invalid. Optional hard_blockers for NO_TRADE are chosen only from "
                "DATA_UNHEALTHY, OFFICIAL_RISK_WINDOW, LIQUIDITY_UNSAFE, NO_VALID_STOP, "
                "ORDER_POSITION_UNCERTAIN, or QUANT_LIVE_GATE. QUANT_LIVE_GATE applies only when "
                "quant_policy=HARD_GATE. NO_TRADE expires "
                "within 60 minutes and WAIT within 120 minutes of data_as_of. "
                "Thesis expiry must be no more than four hours after data_as_of."
            ),
            prompt=trader_prompt,
            schema=PortfolioTraderDecision.model_json_schema(),
            schema_name="portfolio_trader_decision_v2",
            operation="trading_intelligence_trader",
            prompt_version="portfolio_trader_v2_immediate_v1",
            schema_version="portfolio_trader_decision_v2",
            max_tokens=5000,
            thinking_enabled=False,
        )
        decision, _ = await self._validate_with_one_repair(
            PortfolioTraderDecision,
            trader_result.data,
            role="PortfolioTrader",
            source_prompt=trader_prompt,
            market_state=market_state,
            portfolio=portfolio,
        )
        decision = quant_gate(
            decision,
            quant,
            require_edge=require_quant_edge,
            now=market_state.data_as_of,
        )
        # The Trader is the sole strategy authority.  The former semantic
        # reviewer/logic-repair pass is intentionally absent from the hot path.
        # Immediate intents retain the authored order price and never gain entry wakes.
        decision = normalize_direct_trade_decision(decision, market_state)
        if decision.wait_plan:
            normalized_wait, normalization_changes = normalize_wait_plan_liveness(
                decision.wait_plan,
                action=decision.action,
                data_as_of=market_state.data_as_of,
            )
            if normalization_changes:
                decision = decision.model_copy(
                    update={
                        "wait_plan": normalized_wait,
                        "rationale": [
                            *decision.rationale,
                            "Deterministic wait-liveness normalization: "
                            + ", ".join(normalization_changes),
                        ],
                    }
                )
            future_wait, future_changes = normalize_already_satisfied_wait_conditions(
                decision.wait_plan,
                action=decision.action,
                current=market_state,
                previous=previous_market_state,
            )
            if future_changes:
                decision = decision.model_copy(
                    update={
                        "wait_plan": future_wait,
                        "rationale": [
                            *decision.rationale,
                            "Deterministic future-wake normalization: " + ", ".join(future_changes),
                        ],
                    }
                )
            semantic_wait, semantic_wait_changes = normalize_wait_plan_semantics(
                decision.wait_plan,
                action=decision.action,
                current=market_state,
            )
            if semantic_wait_changes:
                decision = decision.model_copy(
                    update={
                        "wait_plan": semantic_wait,
                        "rationale": [
                            *decision.rationale,
                            "Deterministic wait-semantic normalization: "
                            + ", ".join(semantic_wait_changes),
                        ],
                    }
                )
            normalized_wait = decision.wait_plan
            liveness_errors = wait_plan_liveness_errors(
                normalized_wait,
                action=decision.action,
                data_as_of=market_state.data_as_of,
            )
            if liveness_errors:
                decision = decision.model_copy(
                    update={
                        "rationale": [
                            *decision.rationale,
                            "Wait diagnostics: " + ", ".join(liveness_errors),
                        ]
                    }
                )
        episode_status = {
            DecisionAction.WAIT: "WAITING",
            DecisionAction.NO_TRADE: "NO_TRADE",
        }.get(decision.action, "DECIDED")
        episode = DecisionEpisode(
            symbol=market_state.symbol,
            status=episode_status,
            market_state_hash=market_state.state_hash,
            delta=delta,
            analysis_id=analysis.analysis_id,
            quant_prediction_id=quant.prediction_id if quant else None,
            decision_id=decision.decision_id,
            thesis_id=decision.thesis.thesis_id if decision.thesis else None,
            plan_id=decision.trade_plan.plan_id if decision.trade_plan else None,
            prompt_versions={
                "analyst": "market_analyst_v2",
                "trader": "portfolio_trader_v2_immediate_v1",
            },
            model_versions={"analyst": analyst_result.model, "trader": trader_result.model},
            token_usage={
                "analyst": analyst_result.usage.total_tokens,
                "trader": trader_result.usage.total_tokens,
            },
            latency_ms={
                "analyst": analyst_result.latency_ms,
                "trader": trader_result.latency_ms,
            },
            input_hashes={
                "market_state": market_state.state_hash,
                "analyst_prompt": sha256(analyst_prompt.encode()).hexdigest(),
                "trader_prompt": sha256(trader_prompt.encode()).hexdigest(),
                "learning_version": str((learning or {}).get("version", "none")),
            },
            output_hashes={
                "analysis": content_hash(analysis),
                "decision": content_hash(decision),
            },
        )
        return DualLLMResult(analysis=analysis, decision=decision, episode=episode)

    async def _validate_with_one_repair(
        self,
        model: type[BaseModel],
        payload: dict[str, Any],
        *,
        role: str,
        source_prompt: str,
        market_state: MarketStateV2 | None = None,
        portfolio: dict[str, Any] | None = None,
    ) -> tuple[Any, bool]:
        def validate(data: dict[str, Any]) -> BaseModel:
            result = model.model_validate(data)
            if isinstance(result, PortfolioTraderDecision):
                errors = new_decision_contract_errors(
                    result, market_state=market_state, portfolio=portfolio
                )
                # WAIT/NO_TRADE liveness is a deterministic post-processing
                # concern.  Do not spend the single LLM repair pass on stale
                # wake levels; immediate trade shape and ownership errors still
                # require a bounded schema repair.
                if result.action in {DecisionAction.WAIT, DecisionAction.NO_TRADE}:
                    liveness_prefixes = (
                        "UNREACHABLE_EXTERNAL_EVENT",
                        "WAIT_PLAN_MAX_SILENCE_EXCEEDED",
                        "NEW_WAIT_REQUIRES_ACTUAL_FUTURE_PRICE_WAKE",
                        "NO_TRADE_REQUIRES_MARKET_DERIVED_WAKE",
                        "WAIT_PLAN_ALREADY_EXPIRED",
                    )
                    errors = [error for error in errors if not error.startswith(liveness_prefixes)]
                if errors:
                    raise ValueError("; ".join(errors))
            return result

        def errors_for(error: ValueError) -> list[dict[str, Any]]:
            if isinstance(error, ValidationError):
                return error.errors(include_url=False)
            return [{"loc": ["decision"], "type": "value_error", "msg": str(error)}]

        try:
            return validate(payload), False
        except ValueError as first_error:
            repair_prompt = json.dumps(
                {
                    "original_context_hash": sha256(source_prompt.encode()).hexdigest(),
                    "original_context": json.loads(source_prompt),
                    "invalid_output": payload,
                    "validation_errors": errors_for(first_error),
                    "instruction": (
                        "Repair schema only; do not add unsupported facts. Remove any invalid "
                        "optional WakeCondition if necessary, while retaining at least one "
                        "actual future price RECALL_LLM condition for WAIT/NO_TRADE. WAIT may "
                        "omit thesis_id; NO_TRADE may omit hard_blockers when no opportunity "
                        "exists. "
                        "New LONG/SHORT plans require order_type MARKET with reference_price, or "
                        "LIMIT with limit_price and valid_price_range containing both that "
                        "price and the current market price. Preserve the setup's genuine "
                        "invalidation levels; if it is not valid now, return WAIT instead. "
                        "entry_conditions must be empty: orders submit immediately. A setup "
                        "requiring confirmation is WAIT. Target existing positions using matching "
                        "position_id and position_version; management governs the entire pool. "
                        "For ALL/ANY use at least two children with matching actions. For "
                        "range rules provide "
                        "ordered lower and upper. For metric comparisons provide path and "
                        "value. For candle close rules provide timeframe and value. Kind "
                        "must be exactly one of PRICE_CROSSES_ABOVE, PRICE_CROSSES_BELOW, "
                        "PRICE_IN_ZONE, CANDLE_CLOSE_ABOVE, CANDLE_CLOSE_BELOW, METRIC_GT, "
                        "METRIC_LT, METRIC_CROSSES_ABOVE, METRIC_CROSSES_BELOW, "
                        "METRIC_IN_RANGE, STRUCTURE_CHANGED, REGIME_CHANGED, EXPIRES_AT, "
                        "ORDER_EVENT, POSITION_EVENT, EXTERNAL_EVENT, ALL, ANY. "
                        "EXTERNAL_EVENT value is only QUANT_PREDICTION_UPDATED. NO_TRADE "
                        "requires an actual future price wake and "
                        "must expire within 60 minutes; WAIT must expire within 120 minutes. "
                        "For LONG, hard stop is below entry and every target is above entry; "
                        "for SHORT the geometry is reversed. Preserve the supplied symbol's "
                        "price scale and meet original_context.minimum_reward_risk using the "
                        "explicit limit/reference price, stop distance and nearest target."
                    ),
                },
                ensure_ascii=False,
                default=str,
            )
            repaired, _ = await self.gateway.generate_json(
                system=f"You are the {role} schema repair pass. Return one corrected object.",
                prompt=repair_prompt,
                schema=model.model_json_schema(),
                schema_name=f"{role.lower()}_repair_v2",
                operation="trading_intelligence_schema_repair",
                prompt_version="schema_repair_once_v2",
                schema_version="v2",
                max_tokens=5000,
                thinking_enabled=False,
            )
            try:
                return validate(repaired.data), True
            except ValueError as second_error:
                # Wait liveness is normalized deterministically immediately after
                # this boundary (unreachable events are removed, already-satisfied
                # prices are moved to a future wake, and the expiry is bounded).
                # Keep the bounded repair contract compatible with persisted/older
                # providers that return a semantically stale wait on the repair
                # pass; trade-shape errors must still fail closed here.
                repaired_model = model.model_validate(repaired.data)
                if isinstance(repaired_model, PortfolioTraderDecision):
                    liveness_only = {
                        "UNREACHABLE_EXTERNAL_EVENT",
                        "WAIT_PLAN_MAX_SILENCE_EXCEEDED",
                        "NEW_WAIT_REQUIRES_ACTUAL_FUTURE_PRICE_WAKE",
                        "NO_TRADE_REQUIRES_MARKET_DERIVED_WAKE",
                        "WAIT_PLAN_ALREADY_EXPIRED",
                    }
                    semantic_errors = new_decision_contract_errors(
                        repaired_model,
                        market_state=market_state,
                        portfolio=portfolio,
                    )
                    if (
                        repaired_model.action
                        in {
                            DecisionAction.WAIT,
                            DecisionAction.NO_TRADE,
                        }
                        and semantic_errors
                        and all(
                            any(error.startswith(prefix) for prefix in liveness_only)
                            for error in semantic_errors
                        )
                    ):
                        return repaired_model, True
                error_summary = [
                    {
                        "path": ".".join(str(part) for part in item["loc"]),
                        "type": item["type"],
                        "message": item["msg"],
                    }
                    for item in errors_for(second_error)[:12]
                ]
                raise ValueError(
                    "CONTRACT_ERROR: structured output failed one schema repair; "
                    f"errors={json.dumps(error_summary, separators=(',', ':'))}"
                ) from second_error


class EventDrivenTradingEngine:
    """Market updates persist state; only durable wake transitions may invoke callbacks."""

    def __init__(
        self,
        store: TradingIntelligenceStore,
        *,
        on_wake: Callable[[str, WakeCondition, MarketStateV2, str], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.on_wake = on_wake

    async def ingest_market_state(self, state: MarketStateV2) -> list[str]:
        previous = self.store.latest_market_state(state.symbol)
        self.store.save_market_state(state)
        if not state.healthy_for_new_risk:
            return []
        current_payload = self._wake_payload(state)
        previous_payload = self._wake_payload(previous) if previous else None
        fired: list[str] = []
        active_waits = self.store.list_wait_runtimes(symbol=state.symbol, active_only=True)
        for runtime in active_waits:
            if state.data_as_of >= runtime.wait_plan.expires_at:
                continue
            if runtime.wait_plan.reason.startswith(
                ("[BOUNDED_RETRY]", "[FAIL_CLOSED_COOLDOWN]", "[POSITION_REVIEW]")
            ):
                continue
            action = DecisionAction.WAIT if runtime.wait_plan.thesis_id else DecisionAction.NO_TRADE
            liveness_errors = wait_plan_liveness_errors(
                runtime.wait_plan,
                action=action,
                data_as_of=state.data_as_of,
            )
            if not liveness_errors:
                continue
            recovery_condition = WakeCondition(
                condition_id=f"liveness-recovery-{runtime.wait_runtime_id}",
                kind=WakeKind.REGIME_CHANGED,
                action=WakeAction.RECALL_LLM,
                path="runtime_liveness",
                value="REVALIDATE_WAIT_PLAN",
            )
            event_id, created = self.store.fire_wake_once(
                condition_id=recovery_condition.condition_id,
                event_key=f"liveness:{runtime.version}",
                aggregate_id=runtime.wait_runtime_id,
                aggregate_version=runtime.version,
                payload={
                    "condition": recovery_condition.model_dump(mode="json", exclude_none=True),
                    "market_state_hash": state.state_hash,
                    "data_as_of": state.data_as_of.isoformat(),
                    "episode_id": runtime.episode_id,
                    "reason": "WAIT_PLAN_LIVENESS_INVALID",
                    "errors": liveness_errors,
                },
            )
            if not created:
                continue
            self.store.update_wait_runtime(
                runtime.wait_runtime_id,
                expected_version=runtime.version,
                status=WaitRuntimeStatus.FIRED,
            )
            fired.append(event_id)
            if self.on_wake:
                await self.on_wake(
                    runtime.wait_runtime_id,
                    recovery_condition,
                    state,
                    event_id,
                )
            # The callback may have installed a replacement wait. Do not
            # evaluate the stale snapshot or create a second decision claim.
            return fired
        for plan in self.store.list_open_plans(state.symbol):
            if plan.order_type is not None:
                continue
            if plan.state not in {PlanState.ACTIVE, PlanState.WAITING_TRIGGER, PlanState.ARMED}:
                continue
            if state.data_as_of > plan.expires_at:
                continue
            for condition in plan.entry_conditions:
                if not evaluate_wake_condition(
                    condition,
                    current_payload,
                    previous_payload,
                    now=state.data_as_of,
                ):
                    continue
                event_id, created = self.store.fire_wake_once(
                    condition_id=condition.condition_id,
                    event_key=state.state_hash,
                    aggregate_id=plan.plan_id,
                    aggregate_version=plan.version,
                    payload={
                        "condition": condition.model_dump(mode="json", exclude_none=True),
                        "market_state_hash": state.state_hash,
                        "data_as_of": state.data_as_of.isoformat(),
                    },
                )
                if not created:
                    continue
                fired.append(event_id)
                if self.on_wake:
                    await self.on_wake(plan.plan_id, condition, state, event_id)
        for runtime in self.store.list_wait_runtimes(symbol=state.symbol, active_only=True):
            if state.data_as_of > runtime.wait_plan.expires_at:
                expired = self.store.update_wait_runtime(
                    runtime.wait_runtime_id,
                    expected_version=runtime.version,
                    status=WaitRuntimeStatus.EXPIRED,
                )
                event_id, created = self.store.append_event(
                    dedupe_key=f"wait-expired:{expired.wait_runtime_id}:{expired.version}",
                    event_type="WAIT_PLAN_EXPIRED",
                    aggregate_type="wait_runtime",
                    aggregate_id=expired.wait_runtime_id,
                    aggregate_version=expired.version,
                    payload={"market_state_hash": state.state_hash},
                )
                if created and self.on_wake:
                    await self.on_wake(
                        expired.wait_runtime_id,
                        WakeCondition(
                            kind=WakeKind.EXPIRES_AT,
                            action=WakeAction.RECALL_LLM,
                            value=expired.wait_plan.expires_at,
                        ),
                        state,
                        event_id,
                    )
                continue
            for condition in runtime.wait_plan.wake_conditions:
                if not evaluate_wake_condition(
                    condition,
                    current_payload,
                    previous_payload,
                    now=state.data_as_of,
                ):
                    continue
                event_id, created = self.store.fire_wake_once(
                    condition_id=condition.condition_id,
                    event_key=state.state_hash,
                    aggregate_id=runtime.wait_runtime_id,
                    aggregate_version=runtime.version,
                    payload={
                        "condition": condition.model_dump(mode="json", exclude_none=True),
                        "market_state_hash": state.state_hash,
                        "data_as_of": state.data_as_of.isoformat(),
                        "episode_id": runtime.episode_id,
                    },
                )
                if not created:
                    continue
                self.store.update_wait_runtime(
                    runtime.wait_runtime_id,
                    expected_version=runtime.version,
                    status=WaitRuntimeStatus.FIRED,
                )
                fired.append(event_id)
                if self.on_wake:
                    await self.on_wake(
                        runtime.wait_runtime_id,
                        condition,
                        state,
                        event_id,
                    )
        if fired or previous is None:
            return fired
        if self.store.list_open_plans(state.symbol) or self.store.list_positions(
            symbol=state.symbol
        ):
            return fired
        no_thesis_wait = next(
            (
                item
                for item in self.store.list_wait_runtimes(symbol=state.symbol, active_only=True)
                if item.wait_plan.thesis_id is None
                and not item.wait_plan.reason.startswith(
                    ("[BOUNDED_RETRY]", "[FAIL_CLOSED_COOLDOWN]")
                )
            ),
            None,
        )
        transitions = material_market_state_transitions(previous, state)
        if no_thesis_wait is None or not transitions:
            return fired
        material_condition = WakeCondition(
            condition_id=f"material-transition-{no_thesis_wait.wait_runtime_id}",
            kind=WakeKind.REGIME_CHANGED,
            action=WakeAction.RECALL_LLM,
            path="material_transition",
            value=transitions[0],
        )
        event_id, created = self.store.fire_wake_once(
            condition_id=material_condition.condition_id,
            event_key=f"{previous.state_hash}:{state.state_hash}",
            aggregate_id=no_thesis_wait.wait_runtime_id,
            aggregate_version=no_thesis_wait.version,
            payload={
                "condition": material_condition.model_dump(mode="json", exclude_none=True),
                "market_state_hash": state.state_hash,
                "prior_market_state_hash": previous.state_hash,
                "data_as_of": state.data_as_of.isoformat(),
                "episode_id": no_thesis_wait.episode_id,
                "reason": "MATERIAL_MARKET_STATE_TRANSITION",
                "material_changes": transitions,
            },
        )
        if not created:
            return fired
        self.store.update_wait_runtime(
            no_thesis_wait.wait_runtime_id,
            expected_version=no_thesis_wait.version,
            status=WaitRuntimeStatus.FIRED,
        )
        fired.append(event_id)
        if self.on_wake:
            await self.on_wake(
                no_thesis_wait.wait_runtime_id,
                material_condition,
                state,
                event_id,
            )
        return fired

    async def ingest_external_event(
        self,
        *,
        symbol: str,
        event_type: str,
        event_key: str,
        payload: dict[str, Any] | None = None,
    ) -> list[str]:
        """Evaluate bounded EXTERNAL_EVENT rules against one durable provider event."""

        state = self.store.latest_market_state(symbol)
        if state is None:
            return []
        wake_payload = {"event": event_type, **(payload or {})}
        fired: list[str] = []
        for runtime in self.store.list_wait_runtimes(symbol=symbol, active_only=True):
            for condition in runtime.wait_plan.wake_conditions:
                if not evaluate_wake_condition(condition, wake_payload, now=datetime.now(UTC)):
                    continue
                event_id, created = self.store.fire_wake_once(
                    condition_id=condition.condition_id,
                    event_key=event_key,
                    aggregate_id=runtime.wait_runtime_id,
                    aggregate_version=runtime.version,
                    payload={
                        "condition": condition.model_dump(mode="json", exclude_none=True),
                        "provider_event": event_type,
                        "provider_event_key": event_key,
                    },
                )
                if not created:
                    continue
                self.store.update_wait_runtime(
                    runtime.wait_runtime_id,
                    expected_version=runtime.version,
                    status=WaitRuntimeStatus.FIRED,
                )
                fired.append(event_id)
                if self.on_wake:
                    await self.on_wake(
                        runtime.wait_runtime_id,
                        condition,
                        state,
                        event_id,
                    )
        return fired

    @staticmethod
    def _wake_payload(state: MarketStateV2 | None) -> dict[str, Any]:
        return _market_wake_payload(state)
