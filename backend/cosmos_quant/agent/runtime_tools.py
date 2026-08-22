from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ..configuration import StrategyKind
from ..indicators import recommend_indicator_stack
from ..market_evidence import MarketEvidenceSnapshot, build_market_evidence
from ..risk_state import RiskStateService
from ..runtime import RuntimeManager
from ..signals import (
    MarketStateAssessment,
    evaluate_signal,
    score_market_state,
    strategy_family_fit,
)
from ..strategy_adaptation import build_adaptive_strategy_candidates
from ..timeframes import closed_candles, confirmation_timeframe, timeframe_seconds
from .schemas import ToolRiskClass
from .store import AgentStore
from .tools import AgentTool, AgentToolRegistry


def _strategy_family_fit(
    assessment: MarketStateAssessment,
    kind: StrategyKind,
) -> tuple[bool, Decimal, Decimal, str]:
    """Compatibility alias for callers and tests using the former local helper."""

    return strategy_family_fit(assessment, kind)


def classify_execution_context(
    signal_direction: str,
    evidence: MarketEvidenceSnapshot,
) -> dict:
    """Classify transient execution friction separately from a hard conflict.

    A single order-book/trade-flow snapshot is too short-lived to veto a
    closed-candle 1h/4h strategy. It may reduce entry risk. A hard block
    requires both strong microstructure and independent derivatives evidence
    to oppose the strategy direction.
    """

    direction = str(signal_direction)
    directional = direction in {"long", "short"}
    microstructure_headwind = bool(
        directional
        and evidence.directional_bias in {"long", "short"}
        and evidence.directional_bias != direction
        and evidence.directional_strength >= Decimal("0.35")
    )
    derivatives_opposition = bool(
        directional
        and evidence.derivatives_confirmation in {"long", "short"}
        and evidence.derivatives_confirmation != direction
        and evidence.derivatives_strength >= Decimal("0.25")
    )
    hard_conflict = bool(
        microstructure_headwind
        and evidence.directional_strength >= Decimal("0.65")
        and derivatives_opposition
    )
    if hard_conflict:
        context_class = "multi_source_hard_conflict"
        risk_multiplier = Decimal("0")
        reason = "微观结构与独立衍生品证据均强烈反向，当前时点禁止新增风险"
    elif microstructure_headwind:
        context_class = "soft_microstructure_headwind"
        risk_multiplier = Decimal("0.60")
        reason = "短时订单流与已收盘策略方向相反；不否决策略，但将单笔风险降至 60%"
    else:
        context_class = "aligned_or_neutral"
        risk_multiplier = Decimal("1")
        reason = "执行上下文与策略方向一致或中性，不调整风险"
    return {
        "class": context_class,
        "microstructure_headwind": microstructure_headwind,
        "derivatives_opposition": derivatives_opposition,
        "hard_conflict": hard_conflict,
        "risk_multiplier": risk_multiplier,
        "reason": reason,
    }


def build_core_tool_registry(
    runtime: RuntimeManager,
    risk_state: RiskStateService,
    agent_store: AgentStore,
) -> AgentToolRegistry:
    """Expose bounded domain tools, never raw exchange or database primitives."""

    registry = AgentToolRegistry()

    async def get_market_context(symbol: str, timeframe: str, limit: int = 120) -> dict:
        bounded = min(max(limit, 60), 300)
        market, history = await runtime.market_context(
            limit=bounded, symbol=symbol, timeframe=timeframe
        )
        candles = closed_candles(history, timeframe, market.timestamp)
        evidence_method = getattr(runtime, "market_evidence", None)
        market_evidence = (
            await evidence_method(
                symbol,
                timeframe,
                market=market,
                candles=candles,
            )
            if evidence_method
            else build_market_evidence(candles, market, timeframe)
        )
        return {
            "market": market.model_dump(mode="json"),
            "market_evidence": market_evidence.model_dump(mode="json"),
            "closed_candle_count": len(candles),
            "data_start": candles[0].timestamp.isoformat() if candles else None,
            "data_end": candles[-1].timestamp.isoformat() if candles else None,
            "recent_candles": [candle.model_dump(mode="json") for candle in candles[-60:]],
        }

    async def analyze_market_regime(symbol: str, timeframe: str) -> dict:
        market, history = await runtime.market_context(
            limit=300, symbol=symbol, timeframe=timeframe
        )
        candles = closed_candles(history, timeframe, market.timestamp)
        evidence_method = getattr(runtime, "market_evidence", None)
        market_evidence = (
            await evidence_method(
                symbol,
                timeframe,
                market=market,
                candles=candles,
            )
            if evidence_method
            else build_market_evidence(candles, market, timeframe)
        )
        assessment = score_market_state(candles)
        higher_timeframe = confirmation_timeframe(timeframe)
        if higher_timeframe == timeframe:
            higher_market = market
            higher_candles = candles
        else:
            higher_market, higher_history = await runtime.market_context(
                limit=300,
                symbol=symbol,
                timeframe=higher_timeframe,
            )
            higher_candles = closed_candles(
                higher_history,
                higher_timeframe,
                higher_market.timestamp,
            )
        higher_assessment = score_market_state(higher_candles)
        validation = runtime.configuration_service.current_configuration().strategy.validation
        require_confirmation = validation.require_higher_timeframe_confirmation
        signals = {
            kind.value: evaluate_signal(
                kind,
                candles=candles,
                snapshot=market,
                timeframe=timeframe,
                allow_short=True,
                confirmation_candles=higher_candles,
                require_confirmation=require_confirmation,
            )
            for kind in StrategyKind
        }
        adaptive_signals = []
        for kind in StrategyKind:
            for candidate in build_adaptive_strategy_candidates(
                kind,
                assessment,
                market_evidence,
            ):
                signal = evaluate_signal(
                    kind,
                    candles=candles,
                    snapshot=market,
                    timeframe=timeframe,
                    allow_short=True,
                    confirmation_candles=higher_candles,
                    require_confirmation=require_confirmation,
                    strategy_spec=candidate.spec,
                )
                current_fit = _strategy_family_fit(assessment, kind)
                confirmation_fit = _strategy_family_fit(higher_assessment, kind)
                # Higher-timeframe EMA direction is already enforced inside
                # evaluate_signal. Requiring the higher timeframe to use the
                # identical strategy family as well would duplicate that gate
                # and suppress aligned lower-timeframe mean reversion setups.
                family_fit_passed = current_fit[0]
                adaptive_signals.append(
                    {
                        "signal": signal,
                        "adaptation": candidate,
                        "family_fit_passed": family_fit_passed,
                        "current_family_score": current_fit[1],
                        "current_family_relative_score": current_fit[2],
                        "confirmation_family_score": confirmation_fit[1],
                        "confirmation_family_relative_score": (confirmation_fit[2]),
                        "family_fit_reasons": [
                            current_fit[3],
                            (
                                f"{higher_timeframe} 家族仅作上下文："
                                f"{confirmation_fit[3]}；方向仍须通过高周期 EMA 确认"
                            ),
                        ],
                        "selection_score": (
                            signal.confidence * Decimal("0.7")
                            + assessment.scores[kind.value] * Decimal("0.3")
                        ),
                    }
                )
        raw_actionable = sorted(
            (item for item in adaptive_signals if item["signal"].actionable),
            key=lambda item: item["selection_score"],
            reverse=True,
        )
        actionable = [item for item in raw_actionable if item["family_fit_passed"]]
        recommended = next(
            (
                item
                for item in actionable
                if item["signal"].algorithm == assessment.selected_kind.value
            ),
            None,
        ) or (actionable[0] if actionable else None)
        regimes_agree = (
            assessment.selected_kind == higher_assessment.selected_kind
            and not assessment.ambiguous
            and not higher_assessment.ambiguous
        )
        quality = runtime.market_quality
        data_quality_acceptable = bool(
            quality and quality.status == "healthy" and not quality.is_cached
        )
        execution_context = classify_execution_context(
            (str(recommended["signal"].direction) if recommended is not None else "flat"),
            market_evidence,
        )
        microstructure_conflict = bool(execution_context["hard_conflict"])
        can_consider_plan = bool(
            recommended is not None
            and data_quality_acceptable
            and not microstructure_conflict
            and (not assessment.ambiguous or recommended["signal"].confidence >= Decimal("0.65"))
        )
        recommended_signal = recommended["signal"] if recommended else None
        recommended_adaptation = recommended["adaptation"] if recommended else None
        recommendation = {
            "action": "consider_plan" if can_consider_plan else "wait",
            "strategy_kind": (recommended_signal.algorithm if recommended_signal else None),
            "direction": (recommended_signal.direction if recommended_signal else "flat"),
            "confidence": (recommended_signal.confidence if recommended_signal else 0),
            "adaptive_label": (recommended_adaptation.label if recommended_adaptation else None),
            "adaptive_spec_fingerprint": (
                recommended_adaptation.spec_fingerprint if recommended_adaptation else None
            ),
            "regimes_agree": regimes_agree,
            "data_quality_acceptable": data_quality_acceptable,
            "microstructure_bias": market_evidence.directional_bias,
            "microstructure_strength": market_evidence.directional_strength,
            "microstructure_headwind": execution_context["microstructure_headwind"],
            "microstructure_conflict": microstructure_conflict,
            "derivatives_regime": market_evidence.derivatives_regime,
            "derivatives_confirmation": (market_evidence.derivatives_confirmation),
            "derivatives_strength": market_evidence.derivatives_strength,
            "derivatives_opposition": execution_context["derivatives_opposition"],
            "execution_context_class": execution_context["class"],
            "execution_risk_multiplier": execution_context["risk_multiplier"],
            "execution_context_reason": execution_context["reason"],
            "family_fit_passed": (recommended["family_fit_passed"] if recommended else False),
            "current_family_score": (recommended["current_family_score"] if recommended else 0),
            "confirmation_family_score": (
                recommended["confirmation_family_score"] if recommended else 0
            ),
            "family_fit_reasons": (recommended["family_fit_reasons"] if recommended else []),
            "reason": (
                (
                    "存在经高周期确认的候选信号；短时订单流逆风仅降低风险，"
                    "不再单独否决 1h/4h 策略；仍须通过回测、授权与硬风险门禁"
                    if execution_context["microstructure_headwind"]
                    else "当前周期存在经高周期确认的候选信号；仍须通过计划回测、授权与风险门禁"
                )
                if can_consider_plan
                else (
                    "行情质量不是健康的实时数据，保持等待"
                    if not data_quality_acceptable
                    else (
                        "微观结构与独立衍生品证据均强烈反向，当前时点禁止新增风险"
                        if microstructure_conflict
                        else (
                            "可执行参数候选与当前或高周期市场结构不一致，禁止跨家族强行回退"
                            if raw_actionable and not actionable
                            else (
                                "没有经高周期确认的受约束参数候选；"
                                "市场状态歧义时仅允许高确信度候选进入回测"
                            )
                        )
                    )
                )
            ),
        }
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "market_timestamp": market.timestamp.isoformat(),
            "assessment": assessment.model_dump(mode="json"),
            "recommended_indicator_stack": recommend_indicator_stack(assessment.primary_regime),
            "confirmation": {
                "timeframe": higher_timeframe,
                "market_timestamp": higher_market.timestamp.isoformat(),
                "closed_candle_count": len(higher_candles),
                "assessment": higher_assessment.model_dump(mode="json"),
            },
            "closed_candle_count": len(candles),
            "market": {
                "last": market.last,
                "spread_bps": market.spread_bps,
                "funding_rate": market.funding_rate,
                "source": market.source,
            },
            "signals": {kind: signal.model_dump(mode="json") for kind, signal in signals.items()},
            "actionable_candidates": [
                {
                    "strategy_kind": item["signal"].algorithm,
                    "direction": item["signal"].direction,
                    "confidence": item["signal"].confidence,
                    "adaptive_label": item["adaptation"].label,
                    "spec_fingerprint": (item["adaptation"].spec_fingerprint),
                    "selection_basis": (item["adaptation"].selection_basis),
                    "backtest_tuned": item["adaptation"].backtest_tuned,
                    "rationale": item["adaptation"].rationale,
                    "strategy_spec": item["adaptation"].spec,
                    "current_family_score": item["current_family_score"],
                    "confirmation_family_score": (item["confirmation_family_score"]),
                    "family_fit_reasons": item["family_fit_reasons"],
                }
                for item in actionable
            ],
            "rejected_actionable_candidates": [
                {
                    "strategy_kind": item["signal"].algorithm,
                    "direction": item["signal"].direction,
                    "confidence": item["signal"].confidence,
                    "adaptive_label": item["adaptation"].label,
                    "spec_fingerprint": (item["adaptation"].spec_fingerprint),
                    "current_family_score": item["current_family_score"],
                    "confirmation_family_score": (item["confirmation_family_score"]),
                    "family_fit_reasons": item["family_fit_reasons"],
                }
                for item in raw_actionable
                if not item["family_fit_passed"]
            ],
            "recommendation": recommendation,
            "market_quality": quality.model_dump(mode="json") if quality else None,
            "market_evidence": market_evidence.model_dump(mode="json"),
        }

    async def get_portfolio_state() -> dict:
        portfolio = await runtime.portfolio()
        return portfolio.model_dump(mode="json")

    async def get_open_orders() -> dict:
        await runtime.initialize()
        if runtime.execution is None:
            raise RuntimeError("execution gateway is unavailable")
        orders = await runtime.execution.open_orders()
        return {
            "orders": [order.model_dump(mode="json") for order in orders],
            "count": len(orders),
        }

    def search_memory(query: str = "", limit: int = 10) -> dict:
        memories = agent_store.recent_memories(min(max(limit, 1), 50))
        normalized = query.strip().lower()
        if normalized:
            memories = [
                memory
                for memory in memories
                if normalized in memory.content.lower()
                or any(normalized in tag.lower() for tag in memory.tags)
            ]
        return {
            "memories": [memory.model_dump(mode="json") for memory in memories],
            "count": len(memories),
        }

    def halt_trading(reason: str) -> dict:
        state = risk_state.halt(f"Agent 自动停机：{reason[:240]}", source="agent")
        return state.model_dump(mode="json")

    for tool in (
        AgentTool(
            name="get_market_context",
            description=(
                "Read a bounded window of verified closed candles and the latest market snapshot"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=get_market_context,
        ),
        AgentTool(
            name="analyze_market_regime",
            description=(
                "Build deterministic multi-timeframe evidence: score market regimes, "
                "evaluate trend/range/breakout signals, confirm direction on the higher "
                "timeframe, and return a bounded plan-or-wait recommendation"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=analyze_market_regime,
        ),
        AgentTool(
            name="get_portfolio_state",
            description="Read current equity, margin, exposure, and positions",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=get_portfolio_state,
        ),
        AgentTool(
            name="get_open_orders",
            description="Read current venue open orders through the reconciled execution gateway",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=get_open_orders,
        ),
        AgentTool(
            name="search_memory",
            description="Retrieve durable Agent lessons and prior episode summaries",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=search_memory,
        ),
        AgentTool(
            name="halt_trading",
            description="Persistently block all new entries when safety is uncertain",
            risk_class=ToolRiskClass.CONTROL,
            handler=halt_trading,
        ),
    ):
        registry.register(tool)
    return registry


def scheduled_event_key(
    goal_id: str,
    poll_seconds: int,
    now: datetime | None = None,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> str:
    """Deduplicate scheduled wakeups until a market can have a new closed bar.

    ``poll_seconds`` remains a lower-frequency operator throttle. Re-running a
    closed-bar strategy inside the same candle adds AI cost without new evidence.
    """

    current = now or datetime.now(UTC)
    interval = poll_seconds
    if timeframe:
        interval = max(interval, timeframe_seconds(timeframe))
    bucket = int(current.timestamp()) // interval
    scope = f":{symbol}:{timeframe}" if symbol and timeframe else ""
    return f"schedule:{goal_id}{scope}:{interval}:{bucket}"
