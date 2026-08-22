from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from functools import partial
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from anyio import CapacityLimiter, to_process
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator

from .acceptance import ProductAcceptanceReport
from .agent.coordinator import AgentCoordinator
from .agent.parameter_validation import (
    ParameterFoldEvidence,
    build_rolling_parameter_report,
)
from .agent.performance import AgentForwardPerformance, build_forward_performance
from .agent.policy import AgentPolicy
from .agent.reasoner import GatewayAgentReasoner
from .agent.runtime_tools import (
    build_core_tool_registry,
    scheduled_event_key,
    timeframe_seconds,
)
from .agent.schemas import (
    AgentAutonomyMode,
    AgentEpisodeRuntime,
    AgentEvent,
    AgentEventStatus,
    AgentEventType,
    AgentGoal,
    AgentGoalStatus,
    AgentMemory,
    AgentReadiness,
    AgentReadinessCheck,
    AgentRun,
    AgentRunStatus,
    AgentState,
    MemoryType,
    ToolRiskClass,
    TradeEpisode,
    TradeEpisodeStatus,
)
from .agent.screening import (
    candidate_selection_score,
    evidence_rank_key,
    evidence_summary,
    stratified_actionable_candidates,
)
from .agent.shadow_validation import (
    ShadowOutcomeStatus,
    ShadowPlanOutcome,
    ShadowValidationTrial,
    evaluate_shadow_plan,
    summarize_shadow_trials,
)
from .agent.store import AgentStore
from .agent.tools import AgentTool, AgentToolExecutor
from .ai_config_service import AIConfigurationService
from .ai_configuration import (
    AIConfigurationUpdate,
    AIConfigurationView,
    AIProvider,
)
from .ai_gateway import (
    AIGateway,
    AIProviderError,
    AIProviderTestResult,
    AIUsage,
)
from .ai_usage import AIUsageReport
from .autonomous_research import (
    AutonomousResearchRunRequest,
    FastGateEngine,
    ResearchRunResult,
)
from .backtest import (
    CAUSAL_LIBRARY_POLICY_VERSION,
    BacktestResult,
    CausalStrategyPrior,
    causal_library_policy_fingerprint,
    rank_causal_strategy_priors,
    run_walk_forward_backtest,
)
from .backup import BackupResult, BackupService
from .build_identity import capture_runtime_build_identity
from .carry import (
    CARRY_BACKTEST_ENGINE_VERSION,
    CARRY_STRATEGY_LIBRARY,
    CarryBacktestReport,
    CarryStrategySpec,
    run_carry_backtest,
)
from .carry_campaign import (
    CARRY_CAMPAIGN_ENGINE_VERSION,
    CarryMarketData,
    CarryResearchCampaignReport,
    carry_research_lineage_id,
    carry_strategy_id,
    run_carry_research_campaign,
)
from .catalog_journal import (
    MarketCatalogChangeEvent,
    MarketCatalogJournalCoverage,
    MarketCatalogJournalSnapshot,
    build_catalog_journal_observation,
    evaluate_catalog_journal_coverage,
)
from .config_service import ConfigurationService
from .configuration import (
    CommissioningResult,
    ConfigurationUpdate,
    ConfigurationView,
    ConnectionTestResult,
    ResearchValidationPreferences,
    StrategyKind,
    UserConfiguration,
)
from .domain import (
    AuditEvent,
    BasisPoint,
    Candle,
    EntryRule,
    ExitPlan,
    Fill,
    FundingRatePoint,
    LiquidationEvent,
    LiquidationFlowPoint,
    OpenInterestPoint,
    Order,
    OrderFlowBar,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PlanStatus,
    PortfolioState,
    PositioningPoint,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
    TradingMode,
)
from .exchanges.base import PositionProtectionError
from .exchanges.ccxt_adapter import CcxtExchangeAdapter
from .exchanges.paper import PaperExchange
from .execution import ExecutionEngine, ProtectionVerificationError
from .historical_anchor_scan import (
    HistoricalAnchorScan,
    build_binance_historical_anchor_scan,
)
from .indicators import INDICATOR_CATALOG, atr
from .liquidation_stream import (
    BINANCE_LIQUIDATION_STREAM,
    BinanceLiquidationCollector,
)
from .market_data import TIMEFRAME_SECONDS, validate_candles
from .market_data_capture import MarketDataCaptureSession, calculate_capture_coverage
from .market_evidence import MarketEvidenceSnapshot
from .ml_challenger import MLChallengerReport, detect_return_drift, train_ml_challenger
from .multi_leg_execution import (
    MultiLegCommissioningResult,
    MultiLegExecutionCoordinator,
    MultiLegExecutionRequest,
    MultiLegExecutionRun,
    MultiLegExecutionStatus,
    MultiLegExitMonitorState,
    MultiLegOrderSpec,
    build_multi_leg_commissioning_result,
    build_multi_leg_execution_certificate,
    evaluate_carry_exit,
    evaluate_pair_exit,
    validate_multi_leg_commissioning_result,
    validate_multi_leg_execution_certificate,
)
from .observability import Metrics
from .official_archive_index import (
    OfficialArchiveIndex,
    download_official_archive_index,
)
from .operator_workspace import (
    AgentGoalOverview,
    AgentLaunchPreview,
    AgentLaunchProfile,
    AgentLaunchRequest,
    AgentLaunchResult,
    AgentMarketSense,
    AgentRuntimeOverview,
    AgentStrategySummary,
    AuditEventPage,
    PersonalStrategyCreate,
    PersonalStrategyRecord,
    PersonalStrategyUpdate,
    StrategyLibraryItem,
    StrategyLibraryPage,
    StrategyLibraryPreference,
    stable_fingerprint,
)
from .order_ledger import OrderLedger
from .plan_lifecycle import EntrySignalState, assess_entry_signal
from .relative_value import (
    PAIR_BACKTEST_ENGINE_VERSION,
    PAIR_STRATEGY_LIBRARY,
    PairBacktestReport,
    PairStrategySpec,
    latest_causal_pair_signal,
    run_pair_backtest,
)
from .relative_value_campaign import (
    PAIR_CAMPAIGN_ENGINE_VERSION,
    PairMarketData,
    PairResearchCampaignReport,
    pair_research_lineage_id,
    pair_strategy_id,
    run_pair_research_campaign,
)
from .research import (
    BacktestJob,
    ResearchDataset,
    ResearchJobStatus,
    build_research_dataset,
    verify_research_dataset,
)
from .research_campaign import (
    CAMPAIGN_CELL_CONCURRENCY,
    CAMPAIGN_ENGINE_VERSION,
    ResearchCampaignCell,
    ResearchCampaignJob,
    ResearchCampaignJobStatus,
    ResearchCampaignReport,
    aggregate_campaign,
    build_campaign_execution_certificate,
    campaign_candidate_fingerprint,
    campaign_cell,
    campaign_cell_cache_key,
    campaign_request_fingerprint,
    causal_policy_definitions_fingerprint,
    finalize_campaign,
    historical_feature_eligibility,
    ineligible_campaign_cell,
    redact_campaign_for_research_agent,
    research_experiment_fingerprint,
    research_lineage_id,
    research_trial_family_id,
    validate_campaign_execution_certificate,
    validate_causal_policy_definition,
)
from .research_certificates import GeneratorEnvelopeCertificate
from .research_evolution import EvolutionOperation
from .research_holdout import (
    SealedHoldoutReport,
    SealedHoldoutRequest,
    seal_research_holdout,
)
from .research_hypothesis import (
    FailureAttribution,
    HypothesisRevision,
    generate_campaign_failure_revisions,
    generate_failure_driven_revisions,
)
from .research_integrity import (
    OnlineFDRController,
    PermanentTrialRecord,
    TrialResult,
    TrialType,
)
from .research_lifecycle import (
    CandidateStatus,
    PromotionDecision,
    StrategyCandidate,
    ValidatedStrategyArtifact,
    attach_backtest,
    backtest_artifact_fingerprint,
    compare_for_promotion,
    compile_champion,
    create_candidate,
    validate_compiled_champion,
)
from .research_universe import (
    HistoricalCatalogArchiveEvidence,
    MarketCatalogEntry,
    PointInTimeUniversePolicy,
    ResearchUniverseSnapshot,
    build_point_in_time_universe_snapshot,
    validate_universe_snapshot,
)
from .risk import RiskCheck, RiskDecision, RiskEngine, RiskLimits
from .risk_state import RiskControlState, RiskStateService
from .runtime import RuntimeManager
from .secret_store import SecretStore
from .settings import Settings
from .signals import (
    MarketStateAssessment,
    NoTradeSignal,
    score_market_state,
    select_strategy_kind,
)
from .storage import SqliteStore
from .strategy_adaptation import (
    AdaptiveStrategyCandidate,
    build_adaptive_strategy_candidates,
)
from .strategy_dsl import ResearchHypothesisV2, StrategyDSLV2, compile_strategy_dsl
from .strategy_factory import StrategyFactory
from .strategy_library import (
    StrategyLibraryEntry,
    infer_required_data,
    library_entry,
    list_strategy_library,
    rank_strategy_library,
    seed_strategy_library_candidates,
)
from .strategy_portfolio import (
    StrategyHealthReport,
    admit_strategy_risk,
    assess_strategy_health,
)
from .strategy_spec import (
    default_strategy_spec,
    strategy_spec_json_schema,
    validate_strategy_spec,
)
from .timeframes import (
    candle_close_time,
    closed_candles,
    confirmation_timeframe,
    periods_per_year,
    resample_closed_candles,
    timeframe_delta,
)

env_settings = Settings()
store = SqliteStore(env_settings.cosmos_data_dir / "cosmos.db")
runtime_build_identity = capture_runtime_build_identity()
secret_store = SecretStore(env_settings.cosmos_data_dir / "cosmos.db")
configuration_service = ConfigurationService(store, secret_store)
ai_configuration_service = AIConfigurationService(store, secret_store)
ai_gateway = AIGateway(ai_configuration_service)
risk_state_service = RiskStateService(store)
runtime = RuntimeManager(configuration_service, risk_state_service)
agent_store = AgentStore(store)
backup_service = BackupService(store, env_settings.cosmos_data_dir / "backups")
metrics = Metrics()
dashboard_backtest_cache: dict[tuple, BacktestResult] = {}
backtest_job_semaphore = asyncio.Semaphore(2)
RESEARCH_PROCESS_CONCURRENCY = CAMPAIGN_CELL_CONCURRENCY
research_process_limiter = CapacityLimiter(RESEARCH_PROCESS_CONCURRENCY)
backtest_job_tasks: set[asyncio.Task] = set()
campaign_job_tasks: set[asyncio.Task] = set()
universe_job_tasks: set[asyncio.Task] = set()
agent_commissioning_lock = asyncio.Lock()
multi_leg_commissioning_lock = asyncio.Lock()
stale_candidate_audit_ids: set[str] = set()
liquidation_collector: BinanceLiquidationCollector | None = None
catalog_journal_lock = asyncio.Lock()

CHART_CANDLE_LIMIT = 300
SUPPORTED_RESEARCH_EXCHANGES = {"okx", "binance", "bybit"}
CATALOG_JOURNAL_SOURCE_URLS = {
    ("binance", "spot"): "https://api.binance.com/api/v3/exchangeInfo",
    ("binance", "swap"): "https://fapi.binance.com/fapi/v1/exchangeInfo",
    ("okx", "spot"): "https://www.okx.com/api/v5/public/instruments?instType=SPOT",
    ("okx", "swap"): "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
    ("bybit", "spot"): "https://api.bybit.com/v5/market/instruments-info?category=spot",
    ("bybit", "swap"): "https://api.bybit.com/v5/market/instruments-info?category=linear",
}
PRODUCT_ACCEPTANCE_REPORT_PATH = (
    Path(__file__).resolve().parents[2] / "output" / "acceptance" / "latest.json"
)


def parse_strategy_candidate_payload(
    payload: dict,
    *,
    audit_context: str,
    emit_audit: bool = True,
) -> StrategyCandidate | None:
    """Fail closed on stale immutable candidates without breaking the pool.

    StrategySpec defaults can evolve between application versions. Persisted
    candidates keep their original fingerprint and must never be silently
    reinterpreted under a newer contract. They are isolated from execution and
    research selection while the remaining valid candidates continue to work.
    """

    try:
        return StrategyCandidate.model_validate(payload)
    except ValidationError as exc:
        candidate_id = str(payload.get("id") or "unknown")
        if emit_audit and candidate_id not in stale_candidate_audit_ids:
            stale_candidate_audit_ids.add(candidate_id)
            add_audit(
                "research",
                "旧版策略候选与当前 StrategySpec 合约不兼容，已失效隔离",
                severity="warning",
                candidate_id=candidate_id,
                context=audit_context,
                error=str(exc)[:500],
            )
        return None


def agent_trailing_state(
    plan: StrategyPlan,
    history: list[Candle],
    market_timestamp: datetime,
    market_price: Decimal,
    entry_time: datetime,
    entry_price: Decimal,
) -> dict:
    """Evaluate the executable, closed-bar Agent-managed trailing stop.

    The exchange-native hard stop remains active throughout. This soft trailing
    level may only request a safe full exit after its declared activation rule.
    """

    state = {
        "configured": bool(
            plan.exit.trailing_stop_atr is not None or plan.exit.break_even_after_first_target
        ),
        "activation": str(plan.exit.trailing_activation),
        "active": False,
        "break_even_active": False,
        "history_complete": False,
        "targets_reached": 0,
        "favorable_price": None,
        "stop_price": None,
        "breached": False,
    }
    if not state["configured"]:
        return state
    closed = closed_candles(history, plan.timeframe, market_timestamp)
    eligible = [candle for candle in closed if candle.timestamp >= entry_time]
    if not eligible:
        return state
    state["history_complete"] = entry_time >= history[0].timestamp
    if not state["history_complete"]:
        return state
    favorable_price = (
        max(candle.high for candle in eligible)
        if plan.side == Side.LONG
        else min(candle.low for candle in eligible)
    )
    targets_reached = sum(
        1
        for target in plan.exit.take_profits
        if (
            favorable_price >= target.price
            if plan.side == Side.LONG
            else favorable_price <= target.price
        )
    )
    favorable_move = (
        favorable_price - entry_price if plan.side == Side.LONG else entry_price - favorable_price
    )
    initial_risk = abs(entry_price - plan.exit.stop_loss)
    trailing_active = bool(
        plan.exit.trailing_stop_atr is not None
        and len(eligible) >= 15
        and plan.exit.trailing_is_active(
            targets_reached=targets_reached,
            favorable_move=max(favorable_move, Decimal("0")),
            initial_risk=initial_risk,
        )
    )
    break_even_active = bool(plan.exit.break_even_after_first_target and targets_reached >= 1)
    active = trailing_active or break_even_active
    state.update(
        {
            "active": active,
            "break_even_active": break_even_active,
            "targets_reached": targets_reached,
            "favorable_price": str(favorable_price),
        }
    )
    if not active:
        return state
    stop_price = plan.exit.stop_loss
    if break_even_active:
        stop_price = (
            max(stop_price, entry_price) if plan.side == Side.LONG else min(stop_price, entry_price)
        )
    if trailing_active:
        assert plan.exit.trailing_stop_atr is not None
        trail_distance = atr(eligible[-15:], 14) * plan.exit.trailing_stop_atr
        stop_price = (
            max(stop_price, favorable_price - trail_distance)
            if plan.side == Side.LONG
            else min(stop_price, favorable_price + trail_distance)
        )
    breached = market_price <= stop_price if plan.side == Side.LONG else market_price >= stop_price
    state.update(
        {
            "stop_price": str(stop_price),
            "breached": breached,
        }
    )
    return state


def agent_time_exit_state(
    plan: StrategyPlan,
    history: list[Candle],
    market_timestamp: datetime,
    entry_time: datetime,
) -> dict:
    """Evaluate a declared holding limit from causally closed candles only."""

    maximum_holding_bars = plan.exit.maximum_holding_bars
    state = {
        "configured": maximum_holding_bars is not None,
        "maximum_holding_bars": maximum_holding_bars,
        "history_complete": False,
        "held_bars": 0,
        "evaluated_through": None,
        "due": False,
    }
    if maximum_holding_bars is None or not history:
        return state
    closed = closed_candles(history, plan.timeframe, market_timestamp)
    state["history_complete"] = entry_time >= history[0].timestamp
    if not state["history_complete"]:
        return state
    eligible = [
        candle for candle in closed if candle_close_time(candle, plan.timeframe) > entry_time
    ]
    held_bars = len(eligible)
    state.update(
        {
            "held_bars": held_bars,
            "evaluated_through": (
                candle_close_time(eligible[-1], plan.timeframe).isoformat() if eligible else None
            ),
            "due": plan.exit.time_exit_is_due(held_bars=held_bars),
        }
    )
    return state


def sandbox_probe_evidence(
    result: BacktestResult,
    validation: ResearchValidationPreferences,
) -> dict:
    """Return an explicit low-risk sandbox evidence tier.

    This tier exists only to gather forward evidence. It is never sufficient
    for live trading and never bypasses stop, protection, drawdown, or
    position-limit checks.
    """

    recent = result.recent_window
    temporal = result.temporal_stability
    minimum_sample_bars = max(
        1000,
        int(Decimal(validation.minimum_sample_bars) * Decimal("0.95")),
    )
    checks = {
        "sample_bars": result.sample_bars >= minimum_sample_bars,
        "trade_sample": result.total_trades >= 30,
        "positive_return": result.return_percent > 0,
        "profit_factor": result.profit_factor >= Decimal("1"),
        "stress_positive": (
            result.stress_return_percent is not None and result.stress_return_percent > 0
        ),
        "drawdown_bounded": result.max_drawdown_percent
        <= min(
            validation.maximum_drawdown_percent,
            Decimal("10"),
        ),
        "recent_evidence": bool(
            recent
            and recent.total_trades
            >= min(
                validation.minimum_recent_trades,
                10,
            )
            and recent.return_percent > 0
            and recent.profit_factor >= validation.minimum_profit_factor
        ),
        "temporal_evidence": bool(
            temporal
            and temporal.passed
            and temporal.evaluable_folds >= 3
            and temporal.positive_fold_ratio >= Decimal("0.75")
            and temporal.worst_fold_return_percent is not None
            and temporal.worst_fold_return_percent >= Decimal("-2")
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "eligible": not failed,
        "tier": "sandbox_probe" if not failed else "insufficient",
        "checks": checks,
        "failed_checks": failed,
        "risk_multiplier": "0.20",
        "live_eligible": False,
        "purpose": "forward_evidence_collection",
    }


def sandbox_probe_risk_cap(policy: AgentPolicy) -> Decimal:
    return (policy.max_risk_per_trade * Decimal("0.20")).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_DOWN,
    )


def rescale_plan_risk(plan: StrategyPlan, risk_fraction: Decimal) -> StrategyPlan:
    if risk_fraction <= 0 or risk_fraction >= plan.sizing.risk_fraction:
        return plan
    scale = risk_fraction / plan.sizing.risk_fraction
    quantity = (plan.sizing.suggested_quantity * scale).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_DOWN,
    )
    if quantity <= 0:
        raise ValueError("sandbox probe risk scales the order quantity to zero")
    entry_mid = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    sizing = PositionSizing(
        risk_fraction=risk_fraction,
        risk_amount=(plan.sizing.risk_amount * scale).quantize(Decimal("0.01")),
        suggested_quantity=quantity,
        notional=(quantity * entry_mid).quantize(Decimal("0.01")),
    )
    note = (
        "测试网探测层：历史证据尚未达到实盘标准，"
        f"风险降至权益 {risk_fraction * Decimal('100'):.3f}% 收集前向证据"
    )
    return plan.model_copy(
        update={
            "sizing": sizing,
            "reasons": [
                *[reason for reason in plan.reasons if not reason.startswith("测试网探测层：")],
                note,
            ],
            "approved_at": None,
            "approved_fingerprint": None,
        }
    )


def effective_research_bars(configured_bars: int, timeframe: str) -> int:
    """Give 1h/4h research at least one year and permit multi-year generations."""

    if timeframe not in {"1h", "4h"}:
        return configured_bars
    return min(
        50000,
        max(configured_bars, int(periods_per_year(timeframe)) + 1),
    )


def confirmation_history(history, timeframe: str):
    target = confirmation_timeframe(timeframe)
    if target == timeframe:
        return None
    return resample_closed_candles(history, timeframe, target)


def configured_strategy_kind(configuration: UserConfiguration, history):
    if configuration.strategy.automatic_regime_selection:
        return select_strategy_kind(history)
    return configuration.strategy.default_kind


def requires_higher_confirmation(
    configuration: UserConfiguration,
    trading_mode: TradingMode | str | None = None,
) -> bool:
    mode = TradingMode(trading_mode or configuration.trading_mode)
    return (
        configuration.strategy.validation.require_higher_timeframe_confirmation
        and mode != TradingMode.PAPER
    )


async def materialize_research_dataset(
    candles: list[Candle],
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    adapter: CcxtExchangeAdapter | None = None,
    quality=None,
    fetch_derivative_features: bool = True,
) -> tuple[
    ResearchDataset,
    list[FundingRatePoint],
    list[OpenInterestPoint],
    list[PositioningPoint],
    list[BasisPoint],
]:
    funding_rates: list[FundingRatePoint] = []
    open_interest: list[OpenInterestPoint] = []
    positioning: list[PositioningPoint] = []
    basis: list[BasisPoint] = []
    if market_type == "swap" and fetch_derivative_features:
        if adapter is not None:
            feature_requests = (
                adapter.fetch_funding_history(
                    symbol,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                adapter.fetch_open_interest_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                adapter.fetch_positioning_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                adapter.fetch_basis_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
            )
        else:
            feature_requests = (
                runtime.funding_history(
                    symbol,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                runtime.open_interest_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                runtime.positioning_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
                runtime.basis_history(
                    symbol,
                    timeframe,
                    candles[0].timestamp,
                    candles[-1].timestamp,
                ),
            )
        feature_results = await asyncio.gather(
            *feature_requests,
            return_exceptions=True,
        )
        funding_rates, open_interest, positioning, basis = [
            [] if isinstance(result, BaseException) else result for result in feature_results
        ]
    dataset = build_research_dataset(
        candles,
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        quality=quality or runtime.market_quality,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
    )
    await asyncio.to_thread(
        store.save_research_dataset,
        dataset,
        candles,
        funding_rates,
        open_interest,
        positioning,
        basis,
    )
    return dataset, funding_rates, open_interest, positioning, basis


async def verify_stored_research_dataset(dataset: ResearchDataset) -> bool:
    (
        candle_payloads,
        funding_payloads,
        open_interest_payloads,
        positioning_payloads,
        basis_payloads,
        order_flow_payloads,
        liquidation_flow_payloads,
    ) = await asyncio.gather(
        asyncio.to_thread(store.load_research_dataset_candles, dataset.id),
        asyncio.to_thread(store.load_research_dataset_funding, dataset.id),
        asyncio.to_thread(store.load_research_dataset_open_interest, dataset.id),
        asyncio.to_thread(store.load_research_dataset_positioning, dataset.id),
        asyncio.to_thread(store.load_research_dataset_basis, dataset.id),
        asyncio.to_thread(store.load_research_dataset_order_flow, dataset.id),
        asyncio.to_thread(store.load_research_dataset_liquidation_flow, dataset.id),
    )
    verification = verify_research_dataset(
        dataset,
        [Candle.model_validate(item) for item in candle_payloads],
        [FundingRatePoint.model_validate(item) for item in funding_payloads],
        [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
        [PositioningPoint.model_validate(item) for item in positioning_payloads],
        [BasisPoint.model_validate(item) for item in basis_payloads],
        [OrderFlowBar.model_validate(item) for item in order_flow_payloads],
        [LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads],
    )
    return bool(verification["verified"])


async def prepare_research_universe(
    request: ResearchUniverseRequest,
    *,
    progress_callback: Callable[[int, int, ResearchUniverseCell], Awaitable[None]] | None = None,
) -> ResearchUniverseReport:
    configuration = configuration_service.current_configuration()
    research_market_type = request.market_type or str(configuration.market_type)
    symbols = list(
        dict.fromkeys(symbol.strip().upper().replace("-", "/") for symbol in request.symbols)
    )
    if request.selection_mode == "explicit" and not symbols:
        raise ValueError("explicit research universe requires at least one symbol")
    timeframes = list(dict.fromkeys(timeframe.strip().lower() for timeframe in request.timeframes))
    unknown_timeframes = [
        timeframe for timeframe in timeframes if timeframe not in TIMEFRAME_SECONDS
    ]
    if unknown_timeframes:
        raise ValueError("unsupported research timeframes: " + ",".join(unknown_timeframes))
    exchange_ids = list(
        dict.fromkeys(
            exchange_id.strip().lower()
            for exchange_id in (request.exchange_ids or [configuration.exchange_id])
        )
    )
    unsupported_exchanges = [
        exchange_id
        for exchange_id in exchange_ids
        if exchange_id not in SUPPORTED_RESEARCH_EXCHANGES
    ]
    if unsupported_exchanges:
        raise ValueError("unsupported research exchanges: " + ",".join(unsupported_exchanges))
    cells: list[ResearchUniverseCell] = []
    existing_datasets = [
        ResearchDataset.model_validate(payload)
        for payload in await asyncio.to_thread(store.list_research_datasets, 1000)
        if payload.get("research_role", "general") == "general"
    ]
    adapters = {
        exchange_id: CcxtExchangeAdapter(
            exchange_id,
            {},
            sandbox=False,
            live_enabled=False,
            public_only=True,
            market_type=research_market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            proxy_url=configuration.network_proxy_url,
        )
        for exchange_id in exchange_ids
    }
    universe_snapshot: ResearchUniverseSnapshot | None = None
    universe_evidence = {}
    if request.selection_mode == "frozen_snapshot":
        try:
            if not request.universe_snapshot_id:
                raise ValueError("frozen_snapshot selection requires universe_snapshot_id")
            payload = await asyncio.to_thread(
                store.load_research_universe_snapshot,
                request.universe_snapshot_id,
            )
            if payload is None:
                raise ValueError("frozen research universe snapshot was not found")
            universe_snapshot = ResearchUniverseSnapshot.model_validate(payload)
            validate_universe_snapshot(universe_snapshot)
            selected = [item for item in universe_snapshot.instruments if item.selected]
            expected_exchanges = {item.entry.exchange for item in selected}
            expected_symbols = {item.entry.symbol for item in selected}
            expected_timeframes = set(universe_snapshot.policy.timeframes)
            if set(exchange_ids) != expected_exchanges:
                raise ValueError("exchange_ids must exactly match the frozen universe")
            if set(symbols) != expected_symbols:
                raise ValueError("symbols must exactly match the frozen universe")
            if set(timeframes) != expected_timeframes:
                raise ValueError("timeframes must exactly match the frozen universe")
            if research_market_type != universe_snapshot.policy.market_type:
                raise ValueError("market_type must exactly match the frozen universe")
            universe_evidence = {
                (item.entry.exchange, item.entry.symbol): item for item in selected
            }
            markets = [
                (item.entry.exchange, item.entry.symbol, timeframe)
                for item in sorted(
                    selected,
                    key=lambda value: (
                        value.entry.exchange,
                        value.rank or 0,
                        value.entry.symbol,
                    ),
                )
                for timeframe in timeframes
            ]
            if len(markets) > request.max_markets:
                raise ValueError(
                    "frozen universe exceeds max_markets; increase the bound instead "
                    "of silently truncating it"
                )
        except BaseException:
            await asyncio.gather(
                *(adapter.close() for adapter in adapters.values()),
                return_exceptions=True,
            )
            raise
    elif request.selection_mode == "point_in_time":

        async def discover_point_in_time_universe():
            if research_market_type not in {"spot", "swap", "future"}:
                raise ValueError(
                    "point-in-time universe requires spot, swap, or future market type"
                )
            catalog_results = await asyncio.gather(
                *(adapters[exchange_id].research_market_catalog() for exchange_id in exchange_ids),
                return_exceptions=True,
            )
            catalogs = {}
            catalog_errors = {}
            for exchange_id, result in zip(exchange_ids, catalog_results, strict=True):
                if isinstance(result, BaseException):
                    catalogs[exchange_id] = []
                    catalog_errors[exchange_id] = str(result)[:500]
                else:
                    catalogs[exchange_id] = result
            policy = PointInTimeUniversePolicy(
                quote_currency=request.quote_currency.upper(),
                market_type=research_market_type,
                timeframes=timeframes,
                allowed_symbols=symbols,
                minimum_listing_age_days=request.minimum_listing_age_days,
                minimum_quote_volume_24h=request.minimum_quote_volume_24h,
                required_bars_by_timeframe={
                    timeframe: effective_research_bars(request.bars, timeframe)
                    for timeframe in timeframes
                },
                minimum_history_coverage_ratio=(request.minimum_history_coverage_ratio),
                minimum_symbols_per_exchange=request.minimum_symbols_per_exchange,
                maximum_symbols_per_exchange=request.maximum_symbols_per_exchange,
            )
            snapshot = build_point_in_time_universe_snapshot(
                catalogs,
                policy=policy,
                catalog_errors=catalog_errors,
            )
            validate_universe_snapshot(snapshot)
            await asyncio.to_thread(store.save_research_universe_snapshot, snapshot)
            selected = [item for item in snapshot.instruments if item.selected]
            if not selected:
                raise ValueError("point-in-time universe selected no eligible markets")
            evidence = {(item.entry.exchange, item.entry.symbol): item for item in selected}
            selected_markets = [
                (item.entry.exchange, item.entry.symbol, timeframe)
                for item in sorted(
                    selected,
                    key=lambda value: (
                        value.entry.exchange,
                        value.rank or 0,
                        value.entry.symbol,
                    ),
                )
                for timeframe in timeframes
            ]
            if len(selected_markets) > request.max_markets:
                raise ValueError(
                    "point-in-time universe exceeds max_markets; increase the bound instead "
                    "of silently truncating the frozen universe"
                )
            return snapshot, evidence, selected_markets

        try:
            universe_snapshot, universe_evidence, markets = await discover_point_in_time_universe()
        except BaseException:
            await asyncio.gather(
                *(adapter.close() for adapter in adapters.values()),
                return_exceptions=True,
            )
            raise
    else:
        markets = [
            (exchange_id, symbol, timeframe)
            for exchange_id in exchange_ids
            for symbol in symbols
            for timeframe in timeframes
        ][: request.max_markets]
    # Research adapters are public-only and independent from the configured
    # trading venue. Sequential pagination respects each exchange rate limit,
    # while every dataset retains its own venue identity and quality evidence.
    try:
        for exchange_id, symbol, timeframe in markets:
            requested_bars = effective_research_bars(request.bars, timeframe)
            try:
                freshness_cutoff = datetime.now(UTC) - timedelta(
                    seconds=TIMEFRAME_SECONDS[timeframe] * 2
                )
                reusable_candidates = sorted(
                    (
                        dataset
                        for dataset in existing_datasets
                        if dataset.exchange == exchange_id
                        and dataset.symbol == symbol
                        and dataset.timeframe == timeframe
                        and dataset.market_type == research_market_type
                        and dataset.candle_count >= requested_bars
                        and dataset.data_end >= freshness_cutoff
                        and dataset.gap_count == 0
                        and dataset.invalid_count == 0
                    ),
                    key=lambda dataset: (dataset.candle_count, dataset.created_at),
                    reverse=True,
                )
                reusable = None
                for candidate in reusable_candidates:
                    if await verify_stored_research_dataset(candidate):
                        reusable = candidate
                        break
                if reusable is not None:
                    snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                    cell = ResearchUniverseCell(
                        exchange=exchange_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        requested_bars=requested_bars,
                        candle_count=reusable.candle_count,
                        coverage_ratio=min(
                            Decimal(reusable.candle_count) / Decimal(requested_bars),
                            Decimal("1"),
                        ).quantize(Decimal("0.001")),
                        dataset_id=reusable.id,
                        data_start=reusable.data_start,
                        data_end=reusable.data_end,
                        quality_status=reusable.quality_status,
                        gap_count=reusable.gap_count,
                        funding_count=reusable.funding_count,
                        open_interest_count=reusable.open_interest_count,
                        open_interest_coverage_ratio=(reusable.open_interest_coverage_ratio),
                        positioning_count=reusable.positioning_count,
                        positioning_coverage_ratio=(reusable.positioning_coverage_ratio),
                        basis_count=reusable.basis_count,
                        basis_coverage_ratio=reusable.basis_coverage_ratio,
                        universe_snapshot_id=(
                            universe_snapshot.id if universe_snapshot is not None else None
                        ),
                        universe_rank=(
                            snapshot_evidence.rank if snapshot_evidence is not None else None
                        ),
                        listed_at=(
                            snapshot_evidence.entry.listed_at
                            if snapshot_evidence is not None
                            else None
                        ),
                        quote_volume_24h=(
                            snapshot_evidence.entry.quote_volume_24h
                            if snapshot_evidence is not None
                            else None
                        ),
                        minimum_required_bars=requested_bars,
                        history_coverage_passed=True,
                        ok=True,
                    )
                    cells.append(cell)
                    if progress_callback is not None:
                        await progress_callback(len(cells), len(markets), cell)
                    continue
                adapter = adapters[exchange_id]
                raw_history = await adapter.fetch_candles(
                    symbol,
                    timeframe,
                    requested_bars + 2,
                )
                history = closed_candles(
                    raw_history,
                    timeframe,
                    datetime.now(UTC),
                )[-requested_bars:]
                history, quality = validate_candles(history, timeframe)
                materialize_kwargs = {
                    "exchange": exchange_id,
                    "market_type": research_market_type,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "adapter": adapter,
                    "quality": quality,
                }
                if request.feature_mode == "ohlcv_only":
                    materialize_kwargs["fetch_derivative_features"] = False
                (
                    dataset,
                    funding,
                    open_interest,
                    positioning,
                    basis,
                ) = await materialize_research_dataset(history, **materialize_kwargs)
                coverage = min(
                    Decimal(dataset.candle_count) / Decimal(requested_bars),
                    Decimal("1"),
                )
                snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                cell = ResearchUniverseCell(
                    exchange=exchange_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    requested_bars=requested_bars,
                    candle_count=dataset.candle_count,
                    coverage_ratio=coverage.quantize(Decimal("0.001")),
                    dataset_id=dataset.id,
                    data_start=dataset.data_start,
                    data_end=dataset.data_end,
                    quality_status=dataset.quality_status,
                    gap_count=dataset.gap_count,
                    funding_count=len(funding),
                    open_interest_count=len(open_interest),
                    open_interest_coverage_ratio=(dataset.open_interest_coverage_ratio),
                    positioning_count=len(positioning),
                    positioning_coverage_ratio=(dataset.positioning_coverage_ratio),
                    basis_count=len(basis),
                    basis_coverage_ratio=dataset.basis_coverage_ratio,
                    universe_snapshot_id=(
                        universe_snapshot.id if universe_snapshot is not None else None
                    ),
                    universe_rank=(
                        snapshot_evidence.rank if snapshot_evidence is not None else None
                    ),
                    listed_at=(
                        snapshot_evidence.entry.listed_at if snapshot_evidence is not None else None
                    ),
                    quote_volume_24h=(
                        snapshot_evidence.entry.quote_volume_24h
                        if snapshot_evidence is not None
                        else None
                    ),
                    minimum_required_bars=requested_bars,
                    history_coverage_passed=bool(
                        coverage >= request.minimum_history_coverage_ratio
                    ),
                    ok=bool(
                        dataset.gap_count == 0
                        and dataset.invalid_count == 0
                        and coverage >= request.minimum_history_coverage_ratio
                    ),
                )
                cells.append(cell)
            except Exception as exc:
                snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                cell = ResearchUniverseCell(
                    exchange=exchange_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    requested_bars=requested_bars,
                    universe_snapshot_id=(
                        universe_snapshot.id if universe_snapshot is not None else None
                    ),
                    universe_rank=(
                        snapshot_evidence.rank if snapshot_evidence is not None else None
                    ),
                    listed_at=(
                        snapshot_evidence.entry.listed_at if snapshot_evidence is not None else None
                    ),
                    quote_volume_24h=(
                        snapshot_evidence.entry.quote_volume_24h
                        if snapshot_evidence is not None
                        else None
                    ),
                    minimum_required_bars=requested_bars,
                    error=str(exc)[:500],
                )
                cells.append(cell)
            if progress_callback is not None:
                await progress_callback(len(cells), len(markets), cell)
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )
    materialized_scope_complete = bool(
        len(cells) == len(markets)
        and all(cell.dataset_id is not None and cell.ok for cell in cells)
    )
    selection_limitations = (
        list(universe_snapshot.limitations) if universe_snapshot is not None else []
    )
    if universe_snapshot is not None and not materialized_scope_complete:
        selection_limitations.append(
            "The frozen market scope did not fully materialize at the required "
            "history coverage; Campaign execution is prohibited."
        )
    return ResearchUniverseReport(
        requested_markets=len(markets),
        completed_markets=sum(cell.dataset_id is not None for cell in cells),
        healthy_markets=sum(cell.ok for cell in cells),
        total_candles=sum(cell.candle_count for cell in cells),
        cells=cells,
        universe_snapshot_id=(universe_snapshot.id if universe_snapshot is not None else None),
        universe_snapshot_fingerprint=(
            universe_snapshot.fingerprint if universe_snapshot is not None else None
        ),
        point_in_time_selection_passed=bool(
            universe_snapshot and universe_snapshot.point_in_time_selection_passed
        ),
        historical_survivorship_complete=bool(
            universe_snapshot and universe_snapshot.historical_survivorship_complete
        ),
        materialized_scope_complete=materialized_scope_complete,
        selection_limitations=selection_limitations,
    )


async def execute_research_backtest(
    dataset: ResearchDataset,
    candles: list[Candle],
    *,
    cache_key: tuple | None = None,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
    **kwargs,
) -> BacktestResult:
    if cache_key is not None:
        cached = dashboard_backtest_cache.get(cache_key)
        if cached is not None:
            return cached
    if positioning is None and dataset.positioning_count:
        positioning_payloads = await asyncio.to_thread(
            store.load_research_dataset_positioning,
            dataset.id,
        )
        positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
    if basis is None and dataset.basis_count:
        basis_payloads = await asyncio.to_thread(
            store.load_research_dataset_basis,
            dataset.id,
        )
        basis = [BasisPoint.model_validate(item) for item in basis_payloads]
    if order_flow is None and dataset.order_flow_count:
        order_flow_payloads = await asyncio.to_thread(
            store.load_research_dataset_order_flow,
            dataset.id,
        )
        order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
    if liquidation_flow is None and dataset.liquidation_flow_count:
        liquidation_flow_payloads = await asyncio.to_thread(
            store.load_research_dataset_liquidation_flow,
            dataset.id,
        )
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
        ]
    result = await to_process.run_sync(
        partial(
            run_walk_forward_backtest,
            candles,
            positioning=positioning,
            basis=basis,
            order_flow=order_flow,
            liquidation_flow=liquidation_flow,
            **kwargs,
        ),
        cancellable=True,
        limiter=research_process_limiter,
    )
    result.dataset_id = dataset.id
    result.data_fingerprint = dataset.data_fingerprint
    result.data_start = dataset.data_start
    result.data_end = dataset.data_end
    dataset_verification = verify_research_dataset(
        dataset,
        candles,
        kwargs.get("funding_rates"),
        kwargs.get("open_interest"),
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    )
    immutable_binding_ok = bool(
        result.sample_bars == dataset.candle_count
        and dataset.data_fingerprint
        and dataset.id == f"ds_{dataset.data_fingerprint}"
        and dataset_verification["verified"]
    )
    data_quality_ok = dataset.gap_count == 0 and dataset.invalid_count == 0
    result.integrity_checks.update(
        {
            "research_execution_isolation": "worker_process",
            "research_process_concurrency_limit": RESEARCH_PROCESS_CONCURRENCY,
            "immutable_dataset_binding": immutable_binding_ok,
            "dataset_candle_count": dataset.candle_count,
            "dataset_gap_count": dataset.gap_count,
            "dataset_invalid_count": dataset.invalid_count,
            "dataset_quality_acceptable": data_quality_ok,
            "strategy_fingerprint_bound": bool(result.strategy_fingerprint)
            if result.strategy_fingerprint is not None
            else "bound_after_plan_backtest",
            "dataset_fingerprint_recomputed": bool(dataset_verification["fingerprint_matches"]),
            "dataset_time_bounds_match": bool(dataset_verification["bounds_match"]),
            "dataset_strictly_ordered_unique": bool(
                dataset_verification["strictly_ordered_unique"]
            ),
            "dataset_open_interest_count": dataset.open_interest_count,
            "dataset_open_interest_verified": bool(
                dataset_verification["open_interest_count_matches"]
                and dataset_verification["open_interest_strictly_ordered_unique"]
            ),
            "dataset_positioning_count": dataset.positioning_count,
            "dataset_positioning_verified": bool(
                dataset_verification["positioning_count_matches"]
                and dataset_verification["positioning_strictly_ordered_unique"]
                and dataset_verification["positioning_causal"]
            ),
            "dataset_basis_count": dataset.basis_count,
            "dataset_basis_verified": bool(
                dataset_verification["basis_count_matches"]
                and dataset_verification["basis_strictly_ordered_unique"]
                and dataset_verification["basis_causal"]
            ),
            "dataset_order_flow_count": dataset.order_flow_count,
            "dataset_order_flow_verified": bool(
                dataset_verification["order_flow_count_matches"]
                and dataset_verification["order_flow_strictly_ordered_unique"]
                and dataset_verification["order_flow_causal"]
            ),
            "dataset_liquidation_flow_count": dataset.liquidation_flow_count,
            "dataset_liquidation_flow_verified": bool(
                dataset_verification["liquidation_flow_count_matches"]
                and dataset_verification["liquidation_flow_strictly_ordered_unique"]
                and dataset_verification["liquidation_flow_causal"]
            ),
        }
    )
    integrity_reasons: list[str] = []
    if not immutable_binding_ok:
        integrity_reasons.append("不可变数据集指纹或 K 线数量绑定不一致")
    if not data_quality_ok:
        integrity_reasons.append(
            f"研究数据存在 {dataset.gap_count} 个缺口和 {dataset.invalid_count} 根非法 K 线"
        )
    if integrity_reasons:
        result.robustness_passed = False
        result.validation_reasons = list(
            dict.fromkeys([*result.validation_reasons, *integrity_reasons])
        )
    if cache_key is not None:
        if len(dashboard_backtest_cache) >= 32:
            dashboard_backtest_cache.pop(next(iter(dashboard_backtest_cache)))
        dashboard_backtest_cache[cache_key] = result
    return result


async def backtest_candidate_on_dataset(
    candidate: StrategyCandidate,
    dataset: ResearchDataset,
    candles: list[Candle],
    funding_rates: list[FundingRatePoint],
    open_interest: list[OpenInterestPoint] | None = None,
    *,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
    research_trial_count: int = 1,
) -> BacktestResult:
    """Evaluate one immutable StrategySpec against one immutable dataset."""

    configuration = configuration_service.current_configuration()
    result = await execute_research_backtest(
        dataset,
        candles,
        initial_equity=configuration.paper_equity,
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        strategy_kind=StrategyKind(candidate.spec["family"]),
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        exchange=dataset.exchange,
        risk_fraction=configuration.risk.risk_per_trade,
        minimum_reward_risk=configuration.risk.minimum_reward_risk,
        allow_short=dataset.market_type == "swap",
        require_confirmation=False,
        validation=configuration.strategy.validation,
        strategy_spec=candidate.spec,
        funding_rates=funding_rates,
        open_interest=open_interest or [],
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        research_trial_count=research_trial_count,
        # Campaigns validate the strategy itself across every supplied market.
        # Market-regime ranking happens before live selection; an additional
        # family gate here would both hide losing regimes and over-protect entry.
        require_regime_family_fit=False,
    )
    result.strategy_fingerprint = candidate.spec_fingerprint
    return result


async def run_backtest_job(job: BacktestJob) -> None:
    async with backtest_job_semaphore:
        job.status = ResearchJobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_research_job, job)
        try:
            job.result = await backtest_strategy(job.plan_id)
            job.status = ResearchJobStatus.COMPLETED
        except asyncio.CancelledError:
            job.status = ResearchJobStatus.FAILED
            job.error = "backtest worker interrupted before completion"
            raise
        except Exception as exc:
            job.status = ResearchJobStatus.FAILED
            job.error = str(getattr(exc, "detail", exc))[:500]
        finally:
            job.completed_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_research_job, job)


def evaluate_current_plan_signal(plan: StrategyPlan, history, market):
    configuration = configuration_service.current_configuration()
    completed = closed_candles(history, plan.timeframe, market.timestamp)
    return strategy_factory.evaluate(
        StrategyKind(plan.algorithm),
        candles=completed,
        snapshot=market,
        timeframe=plan.timeframe,
        allow_short=plan.market_type == "swap",
        confirmation_candles=confirmation_history(completed, plan.timeframe),
        require_confirmation=requires_higher_confirmation(configuration, plan.trading_mode),
        strategy_spec=plan.strategy_spec or None,
    )


def add_signal_gate(decision: RiskDecision, plan: StrategyPlan, signal) -> RiskDecision:
    assessment = assess_entry_signal(plan, signal)
    aligned = assessment.execution_allowed
    check = RiskCheck(
        code="signal_lifecycle",
        passed=aligned,
        blocking=assessment.blocking,
        message=assessment.reason,
    )
    return RiskDecision(
        approved=decision.approved and assessment.execution_allowed,
        checks=[*decision.checks, check],
    )


def build_plan_forward_outcome_evidence(
    plan: StrategyPlan,
    episodes: list[TradeEpisode],
    plans_by_id: dict[str, StrategyPlan],
) -> dict:
    """Summarize mature, fully attributed outcomes for candidate ranking only."""

    matched_returns: list[Decimal] = []
    matched_episode_ids: list[str] = []
    for episode in episodes:
        historical_plan = plans_by_id.get(episode.plan_id)
        if (
            episode.status != TradeEpisodeStatus.CLOSED
            or episode.return_on_risk is None
            or not historical_plan
            or episode.symbol != plan.symbol
            or episode.side != str(plan.side)
            or historical_plan.timeframe != plan.timeframe
            or historical_plan.algorithm != plan.algorithm
        ):
            continue
        matched_returns.append(episode.return_on_risk)
        matched_episode_ids.append(episode.id)
    sample = len(matched_returns)
    total_r = sum(matched_returns, Decimal("0"))
    expectancy_r = total_r / Decimal(sample) if sample else None
    return {
        "sample": sample,
        "total_r": total_r,
        "expectancy_r": expectancy_r,
        "mature_for_ranking": sample >= 5,
        "episode_ids": matched_episode_ids[-10:],
        "role": "ranking_only",
    }


def settle_agent_entry_exception(
    plan: StrategyPlan,
    episode: TradeEpisode,
    exc: Exception,
) -> TradeEpisodeStatus:
    """Separate deterministic pre-submit rejection from exchange uncertainty."""

    risk_state = risk_state_service.get()
    exchange_effect_uncertain = (
        not isinstance(exc, HTTPException)
        or exc.status_code == 502
        or risk_state.source == "execution_uncertain"
    )
    if exchange_effect_uncertain:
        episode.status = TradeEpisodeStatus.UNCERTAIN
        episode.summary = (
            "entry execution may have reached the exchange; reconciliation required: "
            f"{str(getattr(exc, 'detail', exc))[:240]}"
        )
    else:
        episode.status = TradeEpisodeStatus.CANCELED
        episode.summary = (
            f"entry canceled before exchange submission: {str(getattr(exc, 'detail', exc))[:240]}"
        )
        plan.status = PlanStatus.ARCHIVED
        store.save_strategy(plan)
    agent_store.save_episode(episode)
    return episode.status


def archive_unselected_agent_candidate_plans(
    candidate_plans: list[StrategyPlan],
    selected_plan_id: str | None,
) -> list[str]:
    """Close research-only plan lifecycles once screening has selected a winner."""

    archived: list[str] = []
    for plan in candidate_plans:
        if (
            plan.id == selected_plan_id
            or plan.decision_origin != "agent"
            or plan.status != PlanStatus.DRAFT
        ):
            continue
        plan.status = PlanStatus.ARCHIVED
        store.save_strategy(plan)
        archived.append(plan.id)
    return archived


def archive_stale_agent_screening_plans() -> list[str]:
    """Repair non-executable plans left by terminal historical Agent runs."""

    protected_plan_ids = {
        episode.plan_id
        for episode in agent_store.list_episodes(10000)
        if episode.status
        in {
            TradeEpisodeStatus.RESERVED,
            TradeEpisodeStatus.PENDING_ENTRY,
            TradeEpisodeStatus.OPEN,
            TradeEpisodeStatus.UNCERTAIN,
            TradeEpisodeStatus.AWAITING_REVIEW,
        }
    }
    stale: list[StrategyPlan] = []
    for plan in plans.values():
        if (
            plan.decision_origin != "agent"
            or plan.status
            not in {
                PlanStatus.DRAFT,
                PlanStatus.APPROVED,
            }
            or plan.id in protected_plan_ids
        ):
            continue
        run = agent_store.load_run(plan.agent_run_id) if plan.agent_run_id else None
        if run and run.status == AgentRunStatus.RUNNING:
            continue
        stale.append(plan)
    archived: list[str] = []
    for plan in stale:
        plan.status = PlanStatus.ARCHIVED
        store.save_strategy(plan)
        archived.append(plan.id)
    return archived


async def capture_catalog_journal_scope(
    exchange: str,
    market_type: Literal["spot", "swap"],
) -> CatalogJournalCaptureResult:
    source_url = CATALOG_JOURNAL_SOURCE_URLS.get((exchange, market_type))
    if source_url is None:
        raise ValueError("catalog journal source is not configured for this scope")
    configuration = configuration_service.current_configuration()
    adapter = CcxtExchangeAdapter(
        exchange,
        {},
        sandbox=False,
        live_enabled=False,
        public_only=True,
        market_type=market_type,
        margin_mode=str(configuration.margin_mode),
        contract_leverage=configuration.contract_leverage,
        proxy_url=configuration.network_proxy_url,
    )
    try:
        entries = await adapter.research_market_catalog(include_tickers=False)
    finally:
        await adapter.close()
    scoped_entries = [item for item in entries if item.market_type == market_type]
    if not scoped_entries:
        raise RuntimeError("exchange returned an empty structural market catalog")
    async with catalog_journal_lock:
        previous_payload = await asyncio.to_thread(
            partial(
                store.load_latest_market_catalog_journal_snapshot,
                exchange=exchange,
                market_type=market_type,
            )
        )
        previous = (
            MarketCatalogJournalSnapshot.model_validate(previous_payload)
            if previous_payload
            else None
        )
        snapshot, events = build_catalog_journal_observation(
            scoped_entries,
            exchange=exchange,
            market_type=market_type,
            source_url=source_url,
            previous=previous,
        )
        await asyncio.to_thread(
            store.append_market_catalog_journal_observation,
            snapshot,
            events,
        )
    return CatalogJournalCaptureResult(snapshot=snapshot, events=events)


async def catalog_journal_collection_loop() -> None:
    """Continuously append structural catalogs; failures create gaps, never fake data."""

    while True:
        for exchange in ("binance", "okx", "bybit"):
            for market_type in ("spot", "swap"):
                try:
                    result = await capture_catalog_journal_scope(exchange, market_type)
                    add_audit(
                        "market_data",
                        "Prospective exchange catalog observation persisted",
                        exchange=exchange,
                        market_type=market_type,
                        snapshot_id=result.snapshot.id,
                        records=result.snapshot.record_count,
                        changes=len(result.events),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    metrics.increment("catalog_journal_collection_failures_total")
                    add_audit(
                        "market_data",
                        "Prospective exchange catalog observation failed; "
                        "continuity remains incomplete",
                        severity="warning",
                        exchange=exchange,
                        market_type=market_type,
                        error=str(exc)[:300],
                    )
        await asyncio.sleep(3600)


async def maintenance_loop() -> None:
    consecutive_failures = 0
    while True:
        try:
            await asyncio.to_thread(backup_service.create_if_due)
            consecutive_failures = 0
            risk_state_service.recover_maintenance()
        except Exception as exc:
            consecutive_failures += 1
            metrics.increment("backup_failures_total")
            if consecutive_failures >= 3:
                risk_state_service.halt(
                    f"自动备份连续失败 {consecutive_failures} 次：{str(exc)[:180]}",
                    source="maintenance",
                )
        await asyncio.sleep(3600)


async def strategy_execution_loop() -> None:
    while True:
        configuration = configuration_service.current_configuration()
        try:
            await run_strategy_scan_once(configuration.automatic_execution_enabled)
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment("strategy_worker_errors_total")
        await asyncio.sleep(configuration.strategy_poll_seconds)


async def collect_agent_episode_evidence(
    plan: StrategyPlan, episode: TradeEpisode
) -> tuple[list[Order], list[Fill]]:
    """Resolve entry orders and fills without trusting model-provided attribution."""

    await runtime.initialize()
    assert runtime.execution is not None
    entry_orders: list[Order] = []
    tracked_orders: list[Order] = []
    tracked_order_ids = list(dict.fromkeys([*episode.entry_order_ids, *episode.exit_order_ids]))
    for order_id in tracked_order_ids:
        order: Order | None = None
        try:
            order = await runtime.execution.fetch_order(order_id, plan.symbol)
        except Exception:
            if runtime.order_ledger and order_id in runtime.order_ledger.records:
                order = runtime.order_ledger.records[order_id].order
        if order:
            tracked_orders.append(order)
            if order_id in episode.entry_order_ids:
                entry_orders.append(order)
    if len(entry_orders) != len(episode.entry_order_ids):
        raise RuntimeError("not all Agent entry orders can be reconciled")
    protection_exit_resolver = getattr(runtime.execution, "protection_exit_order_ids", None)
    protection_exit_order_ids = (
        await protection_exit_resolver(entry_orders, plan.symbol)
        if protection_exit_resolver
        else set()
    )
    tracked_order_ids = list(dict.fromkeys([*tracked_order_ids, *protection_exit_order_ids]))
    client_prefixes = {
        order.client_order_id[:-1] if order.client_order_id.endswith("e") else order.client_order_id
        for order in tracked_orders
        if order.client_order_id
    }
    tracked_client_order_ids = {
        *episode.exit_client_order_ids,
        *(order.client_order_id for order in tracked_orders if order.client_order_id),
    }
    fills: list[Fill] | None = None
    fill_error: Exception | None = None
    for attempt in range(3):
        try:
            fills = await runtime.execution.fetch_fills(
                plan.symbol,
                episode.opened_at - timedelta(minutes=5),
                limit=500,
            )
            break
        except Exception as exc:
            fill_error = exc
            metrics.increment(
                "agent_fill_history_retry_total",
                attempt=str(attempt + 1),
            )
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    if fills is None:
        assert fill_error is not None
        raise fill_error
    plan_fills = [
        fill
        for fill in fills
        if fill.plan_id == plan.id
        or fill.order_id in tracked_order_ids
        or fill.client_order_id in tracked_client_order_ids
        or (
            fill.client_order_id
            and any(fill.client_order_id.startswith(prefix) for prefix in client_prefixes)
        )
    ]
    return entry_orders, plan_fills


async def agent_execution_loop() -> None:
    """Wake active durable goals and process one bounded run per schedule bucket."""

    while True:
        try:
            await poll_agent_trade_events()
            goals = await asyncio.to_thread(agent_store.list_goals, 100)
            for goal in goals:
                if goal.status != AgentGoalStatus.ACTIVE:
                    continue
                if goal.valid_until and goal.valid_until <= datetime.now(UTC):
                    continue
                policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
                if not policy:
                    continue
                schedule_now = datetime.now(UTC)
                active_schedule_keys: list[str] = []
                for symbol in goal.symbols:
                    for timeframe in goal.timeframes:
                        dedupe_key = scheduled_event_key(
                            goal.id,
                            goal.poll_seconds,
                            now=schedule_now,
                            symbol=symbol,
                            timeframe=timeframe,
                        )
                        active_schedule_keys.append(dedupe_key)
                        event = AgentEvent(
                            event_type=AgentEventType.SCHEDULE_TRIGGERED,
                            source="agent-scheduler",
                            goal_id=goal.id,
                            dedupe_key=dedupe_key,
                            payload={
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "scheduled_at": schedule_now.isoformat(),
                            },
                        )
                        canonical_event = await asyncio.to_thread(
                            agent_store.append_event,
                            event,
                        )
                        requeued = await asyncio.to_thread(
                            agent_store.requeue_safe_failed_schedule_event,
                            canonical_event.dedupe_key,
                        )
                        if requeued:
                            metrics.increment(
                                "agent_schedule_events_retried_total",
                                symbol=symbol,
                                timeframe=timeframe,
                            )
                superseded = await asyncio.to_thread(
                    agent_store.supersede_stale_schedule_events,
                    goal.id,
                    active_schedule_keys,
                )
                if superseded:
                    metrics.increment(
                        "agent_schedule_events_superseded_total",
                        value=superseded,
                    )
                coordinator = build_agent_coordinator(goal, policy)
                run = await coordinator.process_next(
                    worker_id=f"scheduler-{goal.id}", goal=goal, policy=policy
                )
                if run:
                    metrics.increment(
                        "agent_runs_total", status=str(run.status), mode=str(goal.autonomy_mode)
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment("agent_worker_errors_total")
        await asyncio.sleep(5)


async def poll_agent_trade_events() -> None:
    """Translate exchange/order truth into durable Agent lifecycle events."""

    episodes = await asyncio.to_thread(agent_store.list_episodes, 200)
    tracked = [
        episode
        for episode in episodes
        if episode.status
        in {
            TradeEpisodeStatus.RESERVED,
            TradeEpisodeStatus.PENDING_ENTRY,
            TradeEpisodeStatus.OPEN,
            TradeEpisodeStatus.UNCERTAIN,
        }
    ]
    if not tracked:
        return
    await runtime.initialize()
    assert runtime.execution is not None
    symbols = list(dict.fromkeys(episode.symbol for episode in tracked))
    for symbol in symbols:
        with suppress(Exception):
            await runtime.market_context(symbol=symbol)
    portfolio = await runtime.portfolio()
    positions = {position.symbol: position for position in portfolio.open_positions}
    for episode in tracked:
        goal = await asyncio.to_thread(agent_store.load_goal, episode.goal_id)
        if not goal:
            continue
        policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
        if (
            policy
            and policy.max_daily_drawdown > 0
            and portfolio.daily_drawdown_fraction >= policy.max_daily_drawdown
        ):
            risk_state_service.halt(
                "Agent policy daily drawdown budget reached while monitoring positions",
                source="agent_daily_drawdown",
            )
            await asyncio.to_thread(
                agent_store.append_event,
                AgentEvent(
                    event_type=AgentEventType.RISK_HALTED,
                    source="agent-monitor",
                    goal_id=goal.id,
                    dedupe_key=(
                        f"goal:{goal.id}:risk_halted:{datetime.now(UTC).date().isoformat()}"
                    ),
                    payload={
                        "daily_drawdown_fraction": str(portfolio.daily_drawdown_fraction),
                        "policy_limit": str(policy.max_daily_drawdown),
                        "episode_id": episode.id,
                    },
                ),
            )
        if episode.status == TradeEpisodeStatus.RESERVED:
            # A reservation should immediately become an order. Surviving without an
            # order means the process may have died in the exchange uncertainty window.
            if (datetime.now(UTC) - episode.opened_at) > timedelta(minutes=2):
                episode.status = TradeEpisodeStatus.UNCERTAIN
                episode.summary = "entry reservation survived without a confirmed order"
                await asyncio.to_thread(agent_store.save_episode, episode)
                risk_state_service.halt(episode.summary, source="agent_reconciliation")
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.ORDER_UNCERTAIN,
                        source="agent-monitor",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:order_uncertain",
                        payload={"episode_id": episode.id, "plan_id": episode.plan_id},
                    ),
                )
            continue
        if episode.status == TradeEpisodeStatus.UNCERTAIN:
            plan = plans.get(episode.plan_id)
            try:
                if not plan:
                    raise RuntimeError("Agent plan is unavailable for reconciliation")
                entry_orders, evidence = await collect_agent_episode_evidence(plan, episode)
                entry_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
                entered = sum(
                    (fill.quantity for fill in evidence if fill.side == entry_side),
                    Decimal("0"),
                )
                exited = sum(
                    (fill.quantity for fill in evidence if fill.side != entry_side),
                    Decimal("0"),
                )
                attributable = max(entered - exited, Decimal("0"))
                position = positions.get(episode.symbol)
                aligned = bool(
                    position and str(position.side) == episode.side and position.quantity > 0
                )
                expected = (
                    min(attributable, position.quantity) if aligned and position else Decimal("0")
                )
                protected = await runtime.execution.verify_position_protection(
                    plan, entry_orders, expected
                )
                if not protected:
                    raise RuntimeError(
                        "exchange protection does not exactly cover the attributed position"
                    )
                recovered_risk = risk_state_service.recover_protection_reconciliation()
                global_resume_required = recovered_risk.halted
                if expected > 0:
                    episode.status = TradeEpisodeStatus.OPEN
                    event_type = AgentEventType.POSITION_UPDATED
                    event_suffix = "protection_reconciled"
                    episode.summary = (
                        "exchange position and native protection were reconciled; "
                        + (
                            "another global halt remains and requires operator review"
                            if global_resume_required
                            else "transient protection reconciliation halt recovered"
                        )
                    )
                else:
                    episode.status = TradeEpisodeStatus.AWAITING_REVIEW
                    event_type = AgentEventType.POSITION_CLOSED
                    event_suffix = "position_closed_after_reconciliation"
                    episode.summary = (
                        "no attributed position or active native protection remains; "
                        "trade is ready for deterministic review"
                    )
                await asyncio.to_thread(agent_store.save_episode, episode)
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=event_type,
                        source="agent-reconciliation",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:{event_suffix}",
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                            "attributed_quantity": str(attributable),
                            "verified_quantity": str(expected),
                            "global_resume_required": global_resume_required,
                        },
                    ),
                )
            except Exception as exc:
                episode.summary = f"protection reconciliation pending: {str(exc)[:180]}"
                await asyncio.to_thread(agent_store.save_episode, episode)
                risk_state_service.halt(episode.summary, source="protection_rebalance")
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.ORDER_UNCERTAIN,
                        source="agent-reconciliation",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:protection_uncertain",
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                            "reason": str(exc)[:240],
                        },
                    ),
                )
            continue
        orders: list[Order] = []
        for order_id in episode.entry_order_ids:
            try:
                order = await runtime.execution.fetch_order(order_id, episode.symbol)
                orders.append(order)
                if runtime.order_ledger:
                    runtime.order_ledger.record(order, protection_verified=True)
            except Exception:
                continue
        if episode.status == TradeEpisodeStatus.PENDING_ENTRY:
            filled = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
            if any(order.status in filled for order in orders):
                episode.status = TradeEpisodeStatus.OPEN
                await asyncio.to_thread(agent_store.save_episode, episode)
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.POSITION_OPENED,
                        source="agent-monitor",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:position_opened",
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                        },
                    ),
                )
            elif orders and all(
                order.status in {OrderStatus.CANCELED, OrderStatus.REJECTED} for order in orders
            ):
                episode.status = TradeEpisodeStatus.CANCELED
                episode.summary = "all Agent entry orders reached a terminal non-fill state"
                await asyncio.to_thread(agent_store.save_episode, episode)
                await asyncio.to_thread(archive_agent_plan_for_canceled_episode, episode)
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.ORDER_CANCELED,
                        source="agent-monitor",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:entry_canceled",
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                        },
                    ),
                )
            continue
        plan = plans.get(episode.plan_id)
        try:
            if not plan:
                raise RuntimeError("Agent plan is unavailable for open-position monitoring")
            entry_orders, evidence = await collect_agent_episode_evidence(plan, episode)
            entry_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
            entered = sum(
                (fill.quantity for fill in evidence if fill.side == entry_side),
                Decimal("0"),
            )
            exited_fills = [fill for fill in evidence if fill.side != entry_side]
            exited = sum((fill.quantity for fill in exited_fills), Decimal("0"))
            if entered <= 0:
                raise RuntimeError("verified Agent entry fills are temporarily unavailable")
            attributable = max(entered - exited, Decimal("0"))
            position = positions.get(episode.symbol)
            aligned = bool(
                position and str(position.side) == episode.side and position.quantity > 0
            )
            if not aligned and attributable > 0:
                raise RuntimeError(
                    "account position is flat but attributed exit fills are incomplete"
                )
            expected = (
                min(attributable, position.quantity) if aligned and position else Decimal("0")
            )
            protected = await runtime.execution.verify_position_protection(
                plan, entry_orders, expected
            )
            if not protected:
                raise RuntimeError(
                    "native protection does not exactly cover the monitored position"
                )
            previous_exit_ids = set(episode.exit_order_ids)
            episode.exit_order_ids = list(
                dict.fromkeys(
                    [
                        *episode.exit_order_ids,
                        *(fill.order_id for fill in exited_fills if fill.order_id),
                    ]
                )
            )
            discovered_exit_ids = set(episode.exit_order_ids) - previous_exit_ids
            if expected <= 0:
                episode.status = TradeEpisodeStatus.AWAITING_REVIEW
                episode.summary = "position closure and native protection cleanup verified"
                await asyncio.to_thread(agent_store.save_episode, episode)
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.POSITION_CLOSED,
                        source="agent-monitor",
                        goal_id=goal.id,
                        dedupe_key=f"episode:{episode.id}:position_closed",
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                            "exit_order_ids": episode.exit_order_ids,
                        },
                    ),
                )
            else:
                aligned_summary = (
                    "position quantity and native protection remain aligned after exits"
                    if discovered_exit_ids
                    else "position quantity and native protection are aligned"
                )
                summary_changed = episode.summary != aligned_summary
                episode.summary = aligned_summary
                if summary_changed:
                    await asyncio.to_thread(agent_store.save_episode, episode)
            if expected > 0 and discovered_exit_ids:
                await asyncio.to_thread(agent_store.save_episode, episode)
                await asyncio.to_thread(
                    agent_store.append_event,
                    AgentEvent(
                        event_type=AgentEventType.POSITION_UPDATED,
                        source="agent-monitor",
                        goal_id=goal.id,
                        dedupe_key=(
                            f"episode:{episode.id}:exit_fills:"
                            + ":".join(sorted(discovered_exit_ids))
                        )[:300],
                        payload={
                            "episode_id": episode.id,
                            "plan_id": episode.plan_id,
                            "symbol": episode.symbol,
                            "remaining_quantity": str(expected),
                            "new_exit_order_ids": sorted(discovered_exit_ids),
                        },
                    ),
                )
        except Exception as exc:
            episode.status = TradeEpisodeStatus.UNCERTAIN
            episode.summary = f"open-position protection is uncertain: {str(exc)[:170]}"
            await asyncio.to_thread(agent_store.save_episode, episode)
            risk_state_service.halt(episode.summary, source="protection_reconciliation")
            await asyncio.to_thread(
                agent_store.append_event,
                AgentEvent(
                    event_type=AgentEventType.ORDER_UNCERTAIN,
                    source="agent-monitor",
                    goal_id=goal.id,
                    dedupe_key=f"episode:{episode.id}:open_protection_uncertain",
                    payload={
                        "episode_id": episode.id,
                        "plan_id": episode.plan_id,
                        "symbol": episode.symbol,
                        "reason": str(exc)[:240],
                    },
                ),
            )


async def run_strategy_scan_once(automatic_execution_enabled: bool) -> None:
    await expire_stale_strategy_orders()
    if not automatic_execution_enabled:
        return
    for plan in list(plans.values()):
        if plan.status != PlanStatus.ACTIVE:
            continue
        # Agent-owned plans have their own authorize -> activate -> submit
        # lifecycle with a run-scoped idempotency key. Letting the generic
        # strategy scanner submit them as well creates a second execution
        # owner and a duplicate-order race.
        if is_agent_owned_plan(plan):
            continue
        market, _ = await runtime.market_context(symbol=plan.symbol, timeframe=plan.timeframe)
        executable_price = market.ask if plan.side == "long" else market.bid
        if not (plan.entry.zone.lower <= executable_price <= plan.entry.zone.upper):
            continue
        response = await execute_strategy(plan.id)
        if response.order:
            plan.status = PlanStatus.PAUSED
            store.save_strategy(plan)
            add_audit(
                "execution",
                "入场条件满足，自动提交保护订单并暂停重复信号",
                plan_id=plan.id,
                order_count=len(response.orders),
            )
            metrics.increment("automatic_executions_total")


async def expire_stale_strategy_orders() -> None:
    now = datetime.now(UTC)
    for plan in list(plans.values()):
        expires_at = plan.entry.expires_at
        if plan.status not in {PlanStatus.ACTIVE, PlanStatus.PAUSED} or not expires_at:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at > now:
            continue
        if plan.status == PlanStatus.PAUSED and not (runtime.order_ledger and runtime.execution):
            risk_state_service.halt(
                "策略已到期但执行网关不可用，无法确认撤单",
                source="reconciliation",
            )
            continue
        if runtime.order_ledger and runtime.execution:
            for record in list(runtime.order_ledger.records.values()):
                if record.order.plan_id == plan.id and record.order.status in {
                    "pending",
                    "open",
                    "partially_filled",
                }:
                    canceled = await runtime.execution.cancel_order(
                        record.order.id, record.order.symbol
                    )
                    runtime.order_ledger.record(
                        canceled,
                        protection_verified=record.protection_verified,
                    )
        plan.status = PlanStatus.ARCHIVED
        store.save_strategy(plan)
        add_audit("strategy", "策略与未成交入场单已到期撤销", plan_id=plan.id)


def archive_agent_plan_for_canceled_episode(episode: TradeEpisode) -> bool:
    """Make a terminal non-fill close the corresponding Agent plan lifecycle."""

    if episode.status != TradeEpisodeStatus.CANCELED:
        return False
    plan = plans.get(episode.plan_id)
    if (
        not plan
        or plan.decision_origin != "agent"
        or plan.status
        not in {
            PlanStatus.APPROVED,
            PlanStatus.ACTIVE,
            PlanStatus.PAUSED,
        }
    ):
        return False
    plan.status = PlanStatus.ARCHIVED
    store.save_strategy(plan)
    return True


def reconcile_terminal_agent_plan_states() -> int:
    """Repair stale Agent plan states left by older process versions."""

    return sum(
        archive_agent_plan_for_canceled_episode(episode)
        for episode in agent_store.list_episodes(10000)
    )


async def initialize_runtime_on_startup() -> bool:
    try:
        await runtime.initialize()
        return True
    except Exception as exc:
        metrics.increment("runtime_startup_initialization_failures_total")
        add_audit(
            "runtime",
            "application startup could not initialize the trading runtime; "
            "entry remains fail-closed and background workers will retry",
            severity="warning",
            error=str(exc)[:300],
        )
        return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global liquidation_collector
    seeded_library_entries = await asyncio.to_thread(
        seed_strategy_library_candidates,
        store,
    )
    if seeded_library_entries:
        add_audit(
            "research",
            "application startup seeded the non-empty public strategy library",
            seeded_entries=seeded_library_entries,
            total_entries=len(list_strategy_library()),
        )
    archived_terminal_plans = await asyncio.to_thread(reconcile_terminal_agent_plan_states)
    if archived_terminal_plans:
        add_audit(
            "agent",
            "application startup archived Agent plans whose entry episodes were canceled",
            severity="warning",
            archived_plans=archived_terminal_plans,
        )
    interrupted_agent_work = await asyncio.to_thread(agent_store.recover_interrupted_work)
    archived_stale_screening_plans = await asyncio.to_thread(archive_stale_agent_screening_plans)
    if archived_stale_screening_plans:
        add_audit(
            "agent",
            "application startup archived stale Agent screening plans",
            severity="warning",
            archived_plans=len(archived_stale_screening_plans),
        )
    uncertain_agent_work = [
        item for item in interrupted_agent_work if item["external_effect_uncertain"]
    ]
    if interrupted_agent_work:
        add_audit(
            "agent",
            "应用启动时已收口进程中断的 Agent 工具调用",
            severity="critical" if uncertain_agent_work else "warning",
            recovered_tool_calls=len(interrupted_agent_work),
            uncertain_execution_calls=len(uncertain_agent_work),
            tool_names=",".join(sorted({item["tool_name"] for item in interrupted_agent_work})),
        )
    if uncertain_agent_work:
        risk_state_service.halt(
            "检测到进程中断时可能影响交易状态的 Agent 调用；完成订单、仓位与保护单对账前禁止新开仓",
            source="agent_restart_recovery",
        )
    interrupted_jobs = await asyncio.to_thread(store.list_research_jobs, 200)
    for payload in interrupted_jobs:
        job = BacktestJob.model_validate(payload)
        if job.status in {ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING}:
            job.status = ResearchJobStatus.FAILED
            job.error = "application restarted before backtest completed"
            job.completed_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_research_job, job)
    interrupted_campaigns = await asyncio.to_thread(
        store.list_research_campaign_jobs,
        200,
    )
    for payload in interrupted_campaigns:
        campaign_job = ResearchCampaignJob.model_validate(payload)
        if campaign_job.status in {
            ResearchCampaignJobStatus.QUEUED,
            ResearchCampaignJobStatus.RUNNING,
        }:
            campaign_job.status = ResearchCampaignJobStatus.QUEUED
            campaign_job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(
                store.save_research_campaign_job,
                campaign_job,
            )
            schedule_research_campaign_job(campaign_job)
    interrupted_universe_jobs = await asyncio.to_thread(
        store.list_research_universe_jobs,
        200,
    )
    for payload in interrupted_universe_jobs:
        universe_job = ResearchUniverseJob.model_validate(payload)
        if universe_job.status in {
            ResearchJobStatus.QUEUED,
            ResearchJobStatus.RUNNING,
        }:
            universe_job.status = ResearchJobStatus.QUEUED
            universe_job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(
                store.save_research_universe_job,
                universe_job,
            )
            schedule_research_universe_job(universe_job)
    await initialize_runtime_on_startup()
    configuration = configuration_service.current_configuration()
    liquidation_collector = BinanceLiquidationCollector(
        store,
        symbols=configuration.strategy.watchlist,
        proxy_url=configuration.network_proxy_url,
    )
    liquidation_collector.start()
    maintenance = asyncio.create_task(maintenance_loop(), name="database-backup-maintenance")
    catalog_worker = asyncio.create_task(
        catalog_journal_collection_loop(),
        name="market-catalog-journal-collection",
    )
    strategy_worker = asyncio.create_task(
        strategy_execution_loop(), name="automatic-strategy-execution"
    )
    agent_worker = asyncio.create_task(agent_execution_loop(), name="durable-agent-execution")
    multi_leg_monitor = asyncio.create_task(
        multi_leg_exit_monitor_loop(),
        name="multi-leg-exit-monitor",
    )
    try:
        yield
    finally:
        maintenance.cancel()
        catalog_worker.cancel()
        strategy_worker.cancel()
        agent_worker.cancel()
        multi_leg_monitor.cancel()
        for task in list(backtest_job_tasks):
            task.cancel()
        for task in list(campaign_job_tasks):
            task.cancel()
        for task in list(universe_job_tasks):
            task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        with suppress(asyncio.CancelledError):
            await catalog_worker
        with suppress(asyncio.CancelledError):
            await strategy_worker
        with suppress(asyncio.CancelledError):
            await agent_worker
        with suppress(asyncio.CancelledError):
            await multi_leg_monitor
        if backtest_job_tasks:
            await asyncio.gather(*backtest_job_tasks, return_exceptions=True)
        if campaign_job_tasks:
            await asyncio.gather(*campaign_job_tasks, return_exceptions=True)
        if universe_job_tasks:
            await asyncio.gather(*universe_job_tasks, return_exceptions=True)
        if liquidation_collector is not None:
            await liquidation_collector.close()
            liquidation_collector = None
        await runtime.close()


app = FastAPI(
    title="COSMOS Quant API",
    version="1.0.0",
    description="Explainable strategy, risk, backtest, configuration, and execution API",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver", "test"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=env_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

strategy_factory = StrategyFactory()
plans: dict[str, StrategyPlan] = {
    item["id"]: StrategyPlan.model_validate(item) for item in store.load_strategies()
}
audit: list[AuditEvent] = [AuditEvent.model_validate(item) for item in store.load_audit()]


def reconciliation_protected_plan_ids() -> set[str]:
    plan_ids = {
        plan.id
        for plan in plans.values()
        if plan.status
        in {
            PlanStatus.APPROVED,
            PlanStatus.ACTIVE,
            PlanStatus.PAUSED,
        }
    }
    plan_ids.update(
        episode.plan_id
        for episode in agent_store.list_episodes(500)
        if episode.status
        in {
            TradeEpisodeStatus.RESERVED,
            TradeEpisodeStatus.PENDING_ENTRY,
            TradeEpisodeStatus.OPEN,
            TradeEpisodeStatus.UNCERTAIN,
            TradeEpisodeStatus.AWAITING_REVIEW,
        }
    )
    return plan_ids


runtime.reconciliation_plan_ids_provider = reconciliation_protected_plan_ids


def audit_value(value) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return json.dumps(
        value,
        default=str,
        ensure_ascii=False,
        sort_keys=True,
    )


def add_audit(category: str, message: str, severity: str = "info", **data) -> None:
    event = AuditEvent(
        id=f"evt_{uuid4().hex[:12]}",
        category=category,
        severity=severity,
        message=message,
        data={key: audit_value(value) for key, value in data.items()},
    )
    audit.insert(0, event)
    del audit[100:]
    store.append_audit(event)


def risk_engine() -> RiskEngine:
    configuration = configuration_service.current_configuration()
    preferences = configuration.risk
    state = risk_state_service.get()
    return RiskEngine(
        RiskLimits(
            max_risk_per_trade=preferences.risk_per_trade,
            max_daily_drawdown=preferences.max_daily_drawdown,
            max_gross_exposure=preferences.max_gross_exposure,
            max_portfolio_risk=preferences.max_portfolio_risk,
            existing_position_stress=preferences.existing_position_stress,
            max_directional_exposure=preferences.max_directional_exposure,
            max_leverage=preferences.max_leverage,
            max_symbol_exposure=preferences.max_symbol_exposure,
            max_open_positions=preferences.max_open_positions,
            min_reward_risk=preferences.minimum_reward_risk,
            max_spread_bps=preferences.max_spread_bps,
            max_abs_funding_rate=preferences.max_abs_funding_rate,
            execution_leverage=(
                configuration.contract_leverage
                if configuration.market_type == "swap"
                else Decimal("1")
            ),
            allow_short=(configuration.market_type == "swap"),
            operational_halt=state.halted,
            halt_reason=state.reason,
        )
    )


def constrain_plan_to_portfolio(
    plan: StrategyPlan,
    portfolio: PortfolioState,
    configuration: UserConfiguration,
) -> StrategyPlan:
    """Shrink a new entry to current exposure and margin capacity before review."""
    entry_reference = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    if entry_reference <= 0 or portfolio.equity <= 0:
        return plan
    existing_symbol_exposure = sum(
        (
            abs(position.quantity * position.mark_price)
            for position in portfolio.open_positions
            if position.symbol == plan.symbol
        ),
        Decimal("0"),
    )
    gross_capacity = max(
        portfolio.equity * configuration.risk.max_gross_exposure - portfolio.gross_exposure,
        Decimal("0"),
    )
    symbol_capacity = max(
        portfolio.equity * configuration.risk.max_symbol_exposure - existing_symbol_exposure,
        Decimal("0"),
    )
    estimated_existing_risk = portfolio.gross_exposure * configuration.risk.existing_position_stress
    portfolio_risk_capacity = max(
        portfolio.equity * configuration.risk.max_portfolio_risk - estimated_existing_risk,
        Decimal("0"),
    )
    risk_scale = (
        min(portfolio_risk_capacity / plan.sizing.risk_amount, Decimal("1"))
        if plan.sizing.risk_amount > 0
        else Decimal("0")
    )
    same_direction_exposure = sum(
        (
            abs(position.quantity * position.mark_price)
            for position in portfolio.open_positions
            if position.side == plan.side
        ),
        Decimal("0"),
    )
    directional_capacity = max(
        portfolio.equity * configuration.risk.max_directional_exposure - same_direction_exposure,
        Decimal("0"),
    )
    available = (
        portfolio.available_margin if portfolio.available_margin is not None else portfolio.cash
    )
    leverage = plan.leverage if plan.market_type == "swap" else Decimal("1")
    capital_capacity = max(available * leverage, Decimal("0"))
    capacity = min(
        gross_capacity,
        symbol_capacity,
        capital_capacity,
        directional_capacity,
        plan.sizing.notional * risk_scale,
    )
    if capacity <= 0 or plan.sizing.notional <= capacity:
        return plan
    buffered_capacity = capacity * Decimal("0.98")
    quantity = (buffered_capacity / entry_reference).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    if quantity <= 0:
        return plan
    notional = (quantity * entry_reference).quantize(Decimal("0.01"))
    stop_distance = abs(entry_reference - plan.exit.stop_loss)
    risk_amount = (quantity * stop_distance).quantize(Decimal("0.01"))
    actual_risk_fraction = risk_amount / portfolio.equity
    plan.sizing = PositionSizing(
        risk_fraction=actual_risk_fraction,
        risk_amount=risk_amount,
        suggested_quantity=quantity,
        notional=notional,
    )
    plan.reasons.append(
        f"仓位受当前敞口与可用保证金约束，名义价值收缩至 {notional:f} USDT，"
        f"实际风险为权益的 {actual_risk_fraction * 100:.3f}%"
    )
    return plan


def is_agent_owned_plan(plan: StrategyPlan) -> bool:
    """Return whether execution ownership belongs to an Agent run."""

    return plan.decision_origin == "agent" or plan.agent_goal_id is not None


def entry_window_expired(
    plan: StrategyPlan,
    *,
    now: datetime | None = None,
) -> bool:
    expires_at = plan.entry.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC))


def ensure_plan(history, market, portfolio: PortfolioState) -> StrategyPlan | None:
    configuration = configuration_service.current_configuration()
    history = closed_candles(
        history,
        configuration.default_timeframe,
        market.timestamp,
    )
    if len(history) < 55:
        raise RuntimeError("已收盘 K 线不足 55 根，不能生成策略")
    selected_kind = configured_strategy_kind(configuration, history)
    for candidate in plans.values():
        if (
            not is_agent_owned_plan(candidate)
            and candidate.status in {PlanStatus.DRAFT, PlanStatus.APPROVED}
            and entry_window_expired(candidate)
        ):
            previous_status = str(candidate.status)
            candidate.status = PlanStatus.ARCHIVED
            store.save_strategy(candidate)
            add_audit(
                "strategy",
                "未执行计划的入场窗口已过期，已归档并允许按最新行情重新生成",
                plan_id=candidate.id,
                previous_status=previous_status,
            )
            continue
        if (
            candidate.exchange == configuration.exchange_id
            and candidate.trading_mode == configuration.trading_mode
            and candidate.market_type == configuration.market_type
            and not is_agent_owned_plan(candidate)
            and configuration.market_type == "spot"
            and candidate.side == Side.SHORT
            and candidate.status != PlanStatus.ARCHIVED
        ):
            candidate.status = PlanStatus.ARCHIVED
            store.save_strategy(candidate)
            add_audit(
                "strategy",
                "现货环境不支持建立空头，旧做空策略已自动归档",
                severity="warning",
                plan_id=candidate.id,
            )
    matching = [
        plan
        for plan in plans.values()
        if plan.symbol == configuration.default_symbol
        and plan.algorithm == selected_kind.value
        and plan.timeframe == configuration.default_timeframe
        and plan.exchange == configuration.exchange_id
        and plan.trading_mode == configuration.trading_mode
        and plan.market_type == configuration.market_type
        and plan.leverage
        == (
            configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
        )
        and plan.margin_mode
        == (str(configuration.margin_mode) if configuration.market_type == "swap" else "cash")
        and not is_agent_owned_plan(plan)
        and plan.status != PlanStatus.ARCHIVED
        and not (configuration.market_type == "spot" and plan.side == Side.SHORT)
    ]
    if matching:
        status_priority = {
            PlanStatus.ACTIVE: 5,
            PlanStatus.PAUSED: 4,
            PlanStatus.APPROVED: 3,
            PlanStatus.DRAFT: 2,
            PlanStatus.ARCHIVED: 1,
        }
        selected = max(
            matching,
            key=lambda plan: (
                status_priority.get(PlanStatus(plan.status), 0),
                plan.version,
                plan.created_at,
            ),
        )
        # A draft has no execution authorization or immutable approval
        # fingerprint yet, so it may be safely reduced when free margin or
        # portfolio capacity has fallen since it was generated. Approved and
        # active plans are never silently mutated; they must be revised.
        if selected.status == PlanStatus.DRAFT:
            previous_notional = selected.sizing.notional
            selected = constrain_plan_to_portfolio(
                selected,
                portfolio,
                configuration,
            )
            if selected.sizing.notional != previous_notional:
                plans[selected.id] = selected
                store.save_strategy(selected)
                add_audit(
                    "risk",
                    "未授权草稿已按最新账户容量自动缩仓",
                    plan_id=selected.id,
                    previous_notional=str(previous_notional),
                    adjusted_notional=str(selected.sizing.notional),
                )
        return selected
    try:
        generation_kwargs = dict(
            candles=history,
            snapshot=market,
            equity=portfolio.equity,
            risk_fraction=configuration.risk.risk_per_trade,
            exchange=configuration.exchange_id,
            timeframe=configuration.default_timeframe,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
            allow_short=configuration.market_type == "swap",
            confirmation_candles=confirmation_history(history, configuration.default_timeframe),
            require_confirmation=requires_higher_confirmation(configuration),
        )
        if configuration.strategy.automatic_regime_selection:
            plan = strategy_factory.generate_automatic(**generation_kwargs)
        else:
            plan = strategy_factory.generate(selected_kind, **generation_kwargs)
    except NoTradeSignal:
        return None
    plan.lineage_id = plan.id
    plan.trading_mode = configuration.trading_mode
    plan.market_type = configuration.market_type
    plan.margin_mode = (
        str(configuration.margin_mode) if configuration.market_type == "swap" else "cash"
    )
    plan.leverage = (
        configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
    )
    if configuration.market_type == "swap":
        plan.reasons.append(
            f"USDT 永续采用 {plan.leverage:f}x "
            f"{'全仓' if plan.margin_mode == 'cross' else '逐仓'}保证金；"
            "仓位按账户权益和止损距离计算"
        )
    plan = constrain_plan_to_portfolio(plan, portfolio, configuration)
    plan.source_prompt = configuration.strategy.default_prompt
    plans[plan.id] = plan
    store.save_strategy(plan)
    add_audit("strategy", f"已生成初始 {plan.symbol} 可解释策略", plan_id=plan.id)
    return plan


class GenerateRequest(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None
    equity: Decimal | None = Field(default=None, gt=0)
    risk_fraction: Decimal | None = Field(default=None, gt=0, le=Decimal("0.05"))
    prompt: str = "顺应主要趋势，在回踩确认后入场，避免追涨杀跌。"
    strategy_kind: StrategyKind = StrategyKind.TREND
    strategy_spec: dict | None = None
    research_candidate_id: str | None = None
    revision_of: str | None = None


class CandidateRequest(BaseModel):
    strategy_spec: dict
    parent_candidate_id: str | None = None


class CandidateRevisionRequest(BaseModel):
    max_revisions: int = Field(default=3, ge=1, le=5)


class CampaignRevisionRequest(BaseModel):
    library_entry_id: str | None = None
    max_revisions: int = Field(default=3, ge=1, le=3)


class CausalPolicyRevisionRequest(BaseModel):
    campaign_entry_id: str | None = None
    max_revisions: int = Field(default=3, ge=1, le=3)


class CandidateRevisionResponse(BaseModel):
    parent_candidate_id: str
    attribution: FailureAttribution
    revisions: list[HypothesisRevision]


class StrategyGovernanceReport(BaseModel):
    reviewed: int
    retired_candidate_ids: list[str] = Field(default_factory=list)
    health: list[StrategyHealthReport] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AIProposalRequest(BaseModel):
    hypothesis: str = Field(min_length=12, max_length=2000)
    provider: AIProvider | None = None
    family: StrategyKind | None = None
    symbol: str | None = None
    timeframe: str | None = None


class AIProposalItem(BaseModel):
    provider: AIProvider
    model: str
    candidate: StrategyCandidate
    usage: AIUsage
    latency_ms: int
    route: list[AIProvider]


class AIProposalResponse(BaseModel):
    mode: str
    proposals: list[AIProposalItem]


class ResearchUniverseRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=100)
    timeframes: list[str] = Field(min_length=1, max_length=12)
    exchange_ids: list[str] = Field(default_factory=list, max_length=3)
    bars: int = Field(default=5000, ge=500, le=50000)
    max_markets: int = Field(default=12, ge=1, le=300)
    selection_mode: str = Field(
        default="explicit",
        pattern="^(explicit|point_in_time|frozen_snapshot)$",
    )
    universe_snapshot_id: str | None = None
    market_type: str | None = Field(default=None, pattern="^(spot|swap|future)$")
    feature_mode: Literal["full", "ohlcv_only"] = "full"
    quote_currency: str = Field(default="USDT", min_length=2, max_length=12)
    minimum_listing_age_days: int = Field(default=90, ge=0, le=3650)
    minimum_quote_volume_24h: Decimal = Field(default=Decimal("0"), ge=0)
    minimum_history_coverage_ratio: Decimal = Field(
        default=Decimal("0.90"),
        gt=0,
        le=1,
    )
    minimum_symbols_per_exchange: int = Field(default=3, ge=1, le=50)
    maximum_symbols_per_exchange: int = Field(default=10, ge=1, le=100)


class HistoricalUniverseArchiveImportRequest(BaseModel):
    as_of: datetime
    policy: PointInTimeUniversePolicy
    catalogs: dict[str, list[MarketCatalogEntry]] = Field(min_length=1, max_length=3)
    archive_evidence: list[HistoricalCatalogArchiveEvidence] = Field(
        min_length=1,
        max_length=3,
    )


class CatalogJournalCaptureRequest(BaseModel):
    exchange_ids: list[str] = Field(
        default_factory=lambda: ["binance", "okx", "bybit"],
        min_length=1,
        max_length=3,
    )
    market_types: list[Literal["spot", "swap"]] = Field(
        default_factory=lambda: ["spot", "swap"],
        min_length=1,
        max_length=2,
    )


class CatalogJournalCaptureResult(BaseModel):
    snapshot: MarketCatalogJournalSnapshot
    events: list[MarketCatalogChangeEvent]


class OfficialArchiveCaptureRequest(BaseModel):
    exchange_ids: list[Literal["binance", "bybit"]] = Field(
        default_factory=lambda: ["binance", "bybit"],
        min_length=1,
        max_length=2,
    )


class HistoricalAnchorScanRequest(BaseModel):
    official_archive_index_id: str = Field(
        min_length=25,
        max_length=64,
        pattern=r"^official_archive_[0-9a-f]{24}$",
    )
    anchor_month: date
    maximum_workers: int = Field(default=24, ge=1, le=64)


class ResearchUniverseCell(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    requested_bars: int
    candle_count: int = 0
    coverage_ratio: Decimal = Decimal("0")
    dataset_id: str | None = None
    data_start: datetime | None = None
    data_end: datetime | None = None
    quality_status: str = "unavailable"
    gap_count: int = 0
    funding_count: int = 0
    open_interest_count: int = 0
    open_interest_coverage_ratio: Decimal = Decimal("0")
    positioning_count: int = 0
    positioning_coverage_ratio: Decimal = Decimal("0")
    basis_count: int = 0
    basis_coverage_ratio: Decimal = Decimal("0")
    universe_snapshot_id: str | None = None
    universe_rank: int | None = None
    listed_at: datetime | None = None
    quote_volume_24h: Decimal | None = None
    minimum_required_bars: int = 0
    history_coverage_passed: bool = False
    ok: bool = False
    error: str | None = None


class ResearchUniverseReport(BaseModel):
    requested_markets: int
    completed_markets: int
    healthy_markets: int
    total_candles: int
    cells: list[ResearchUniverseCell]
    universe_snapshot_id: str | None = None
    universe_snapshot_fingerprint: str | None = None
    point_in_time_selection_passed: bool = False
    historical_survivorship_complete: bool = False
    materialized_scope_complete: bool = False
    selection_limitations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchUniverseJob(BaseModel):
    id: str = Field(default_factory=lambda: f"ruj_{uuid4().hex[:16]}")
    request: dict
    request_fingerprint: str
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    total_markets: int = 0
    completed_markets: int = 0
    healthy_markets: int = 0
    total_candles: int = 0
    last_cell: ResearchUniverseCell | None = None
    report: ResearchUniverseReport | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def research_universe_request_fingerprint(request_payload: dict) -> str:
    encoded = json.dumps(
        request_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


class CausalPolicyCampaignSpec(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,79}$")
    library_entry_ids: list[str] = Field(min_length=2, max_length=200)
    maximum_candidates_per_bar: int = Field(default=3, ge=1, le=20)
    abstain_on_ambiguous_regime: bool = True
    minimum_regime_confidence: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
    )
    minimum_signal_confidence: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
    )
    minimum_directional_consensus: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def validate_unique_policy_entries(self):
        if len(self.library_entry_ids) != len(set(self.library_entry_ids)):
            raise ValueError("causal policy library entry identifiers must be unique")
        if self.minimum_directional_consensus > self.maximum_candidates_per_bar:
            raise ValueError("causal policy directional consensus cannot exceed max candidates")
        return self


class CausalPolicyRevisionResponse(BaseModel):
    parent_campaign_entry_id: str
    parent_policy_fingerprint: str
    failure_reasons: list[str]
    revisions: list[CausalPolicyCampaignSpec]


class ResearchCampaignRequest(BaseModel):
    dataset_ids: list[str] = Field(min_length=1, max_length=60)
    blind_dataset_ids: list[str] = Field(default_factory=list, max_length=30)
    universe_snapshot_id: str | None = None
    library_entry_ids: list[str] = Field(
        default_factory=list,
        max_length=200,
    )
    candidate_ids: list[str] = Field(default_factory=list, max_length=40)
    causal_policy_specs: list[CausalPolicyCampaignSpec] = Field(
        default_factory=list,
        max_length=10,
    )
    max_backtests: int = Field(default=240, ge=1, le=6000)
    parent_campaign_id: str | None = None

    @model_validator(mode="after")
    def validate_campaign_hypothesis_mode(self):
        if self.causal_policy_specs and (self.library_entry_ids or self.candidate_ids):
            raise ValueError(
                "causal policy hypotheses cannot be mixed with static strategy hypotheses"
            )
        if self.causal_policy_specs and len(self.causal_policy_specs) < 2:
            raise ValueError("causal policy Campaign requires at least two hypotheses for PBO")
        policy_ids = [item.id for item in self.causal_policy_specs]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("causal policy Campaign identifiers must be unique")
        policy_shapes = {
            (
                tuple(sorted(item.library_entry_ids)),
                item.maximum_candidates_per_bar,
                item.abstain_on_ambiguous_regime,
                item.minimum_regime_confidence,
                item.minimum_signal_confidence,
                item.minimum_directional_consensus,
            )
            for item in self.causal_policy_specs
        }
        if len(policy_shapes) != len(self.causal_policy_specs):
            raise ValueError("causal policy Campaign hypotheses must be structurally unique")
        return self


class CausalPolicyBacktestRequest(BaseModel):
    dataset_id: str
    library_entry_ids: list[str] = Field(min_length=2, max_length=200)
    maximum_candidates_per_bar: int = Field(default=3, ge=1, le=20)
    abstain_on_ambiguous_regime: bool = True
    minimum_regime_confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    minimum_signal_confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    minimum_directional_consensus: int = Field(default=1, ge=1, le=20)
    research_trial_count: int = Field(default=1, ge=1, le=100000)

    @model_validator(mode="after")
    def validate_unique_policy_entries(self):
        if len(self.library_entry_ids) != len(set(self.library_entry_ids)):
            raise ValueError("causal policy library entry identifiers must be unique")
        if self.minimum_directional_consensus > self.maximum_candidates_per_bar:
            raise ValueError("causal policy directional consensus cannot exceed max candidates")
        return self


class RelativeValueResearchRequest(BaseModel):
    dataset_a_id: str
    dataset_b_id: str
    strategy_specs: list[dict] = Field(default_factory=list, max_length=10)


class RelativeValueCampaignRequest(BaseModel):
    dataset_ids: list[str] = Field(min_length=6, max_length=12)
    blind_dataset_ids: list[str] = Field(default_factory=list, max_length=12)
    strategy_specs: list[dict] = Field(default_factory=list, max_length=10)
    fold_count: int = Field(default=6, ge=4, le=12, multiple_of=2)
    formation_fraction: Decimal = Field(
        default=Decimal("0.25"),
        ge=Decimal("0.10"),
        le=Decimal("0.75"),
    )
    parent_campaign_id: str | None = None
    generate_failure_revisions: bool = True


class CarryResearchRequest(BaseModel):
    spot_dataset_id: str
    perpetual_dataset_id: str
    strategy_specs: list[dict] = Field(default_factory=list, max_length=10)
    formation_fraction: Decimal = Field(
        default=Decimal("0.25"),
        ge=Decimal("0.10"),
        le=Decimal("0.75"),
    )


class CarryCampaignRequest(BaseModel):
    dataset_ids: list[str] = Field(min_length=12, max_length=30)
    blind_dataset_ids: list[str] = Field(default_factory=list, max_length=30)
    strategy_specs: list[dict] = Field(default_factory=list, max_length=10)
    fold_count: int = Field(default=6, ge=4, le=12, multiple_of=2)
    formation_fraction: Decimal = Field(
        default=Decimal("0.25"),
        ge=Decimal("0.10"),
        le=Decimal("0.75"),
    )
    parent_campaign_id: str | None = None
    generate_failure_revisions: bool = True


def _validate_sealed_blind_scope(
    development: list[ResearchDataset],
    blind: list[ResearchDataset],
) -> str | None:
    """Validate one chronological, source-matched, one-time holdout contract."""

    if not blind:
        if any(item.research_role == "blind" or item.blind_sealed for item in development):
            raise ValueError("sealed blind datasets cannot be used for strategy selection")
        return None
    if any(
        item.research_role != "blind"
        or not item.blind_sealed
        or not item.blind_set_id
        or not item.source_dataset_id
        for item in blind
    ):
        raise ValueError("blind datasets must be source-bound, fingerprinted and sealed")
    blind_set_ids = {item.blind_set_id for item in blind}
    if len(blind_set_ids) != 1:
        raise ValueError("blind datasets must belong to one sealed blind set")
    blind_set_id = next(iter(blind_set_ids))
    if any(
        item.research_role != "development"
        or item.blind_sealed
        or item.blind_set_id != blind_set_id
        or not item.source_dataset_id
        for item in development
    ):
        raise ValueError("blind evaluation requires development splits from the same sealed set")
    development_by_source = {item.source_dataset_id: item for item in development}
    blind_by_source = {item.source_dataset_id: item for item in blind}
    if (
        len(development_by_source) != len(development)
        or len(blind_by_source) != len(blind)
        or set(development_by_source) != set(blind_by_source)
    ):
        raise ValueError(
            "development and blind datasets must contain one matching split per source market"
        )
    for source_id, development_item in development_by_source.items():
        blind_item = blind_by_source[source_id]
        development_identity = (
            development_item.exchange,
            development_item.symbol,
            development_item.timeframe,
            development_item.market_type,
        )
        blind_identity = (
            blind_item.exchange,
            blind_item.symbol,
            blind_item.timeframe,
            blind_item.market_type,
        )
        if development_identity != blind_identity:
            raise ValueError("development and blind market identities do not match")
        if (
            development_item.split_boundary is None
            or development_item.split_boundary != blind_item.split_boundary
            or development_item.embargo_bars != blind_item.embargo_bars
            or development_item.data_end >= blind_item.data_start
            or blind_item.data_start < blind_item.split_boundary
        ):
            raise ValueError("development/blind chronological split contract is invalid")
    return str(blind_set_id)


def _sealed_blind_evidence_fingerprint(
    *,
    strategy_fingerprint: str,
    candidate,
    datasets: list[ResearchDataset],
) -> str:
    payload = {
        "disclosure": "pass-fail-only-v1",
        "strategy_fingerprint": strategy_fingerprint,
        "candidate_aggregate_fingerprint": campaign_candidate_fingerprint(candidate),
        "blind_dataset_fingerprints": sorted(item.data_fingerprint for item in datasets),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class CandidatePromotionRequest(BaseModel):
    confirmation: str


def strategy_sizing_equity(
    configuration: UserConfiguration,
    portfolio: PortfolioState,
    requested_equity: Decimal | None,
) -> Decimal:
    """Only paper trading may use an operator-supplied hypothetical equity."""
    if configuration.trading_mode == TradingMode.PAPER:
        return requested_equity or configuration.paper_equity
    return portfolio.equity


class ActivateRequest(BaseModel):
    confirmation: str


class ApproveRequest(BaseModel):
    confirmation: str


class ExecuteResponse(BaseModel):
    decision: RiskDecision
    order: Order | None = None
    orders: list[Order] = Field(default_factory=list)


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ResumeRequest(BaseModel):
    confirmation: str


@app.middleware("http")
async def operational_headers_and_metrics(request: Request, call_next):
    started = perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        metrics.increment(
            "http_requests_total",
            method=request.method,
            path=path,
            status=str(status),
        )
        metrics.increment(
            "http_request_duration_seconds_total",
            perf_counter() - started,
            method=request.method,
            path=path,
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("application/json") and "charset=" not in content_type:
            response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def prometheus_metrics():
    state = risk_state_service.get()
    metrics.gauge("trading_halted", 1 if state.halted else 0)
    if runtime.reconciliation_report:
        report = runtime.reconciliation_report
        metrics.gauge("orders_uncertain", len(report.uncertain_order_ids))
        metrics.gauge("orders_unprotected", len(report.unprotected_order_ids))
        metrics.gauge(
            "positions_unknown",
            len(report.unknown_position_symbols),
        )
    return metrics.render_prometheus()


@app.get("/api/ready")
async def readiness():
    checks: dict[str, str | bool] = {
        "database": store.integrity_check(),
        "configuration": True,
    }
    try:
        await runtime.initialize()
        checks["runtime"] = True
    except Exception as exc:
        checks["runtime"] = str(exc)
    state = risk_state_service.get()
    checks["trading_halted"] = state.halted
    ready = checks["database"] == "ok" and checks.get("runtime") is True
    if not ready:
        raise HTTPException(503, {"ready": False, "checks": checks})
    return {"ready": True, "checks": checks}


@app.post("/api/maintenance/backup", response_model=BackupResult)
async def create_backup():
    try:
        result = await asyncio.to_thread(backup_service.create)
    except Exception as exc:
        metrics.increment("backup_failures_total")
        raise HTTPException(500, f"备份失败：{exc}") from exc
    metrics.increment("backups_total")
    add_audit("maintenance", "数据库在线备份已完成", path=result.path)
    return result


@app.get("/api/maintenance/backup/latest", response_model=BackupResult | None)
async def latest_backup():
    return await asyncio.to_thread(backup_service.latest)


@app.get("/api/health")
async def health():
    view = configuration_service.get()
    return {
        "status": "ok",
        "version": app.version,
        "server_time": datetime.now(UTC),
        "trading_mode": view.configuration.trading_mode,
        "live_trading_enabled": view.configuration.live_trading_enabled,
        "exchange": view.configuration.exchange_id,
        "credentials": view.credentials,
        "market_client_ready": runtime.market is not None,
        "risk_state": risk_state_service.get(),
    }


@app.get("/api/acceptance/product", response_model=ProductAcceptanceReport)
async def product_acceptance():
    if not PRODUCT_ACCEPTANCE_REPORT_PATH.exists():
        raise HTTPException(
            404,
            "product acceptance report is unavailable; run scripts/acceptance_audit.py",
        )
    try:
        payload = await asyncio.to_thread(
            PRODUCT_ACCEPTANCE_REPORT_PATH.read_text,
            encoding="utf-8",
        )
        return ProductAcceptanceReport.model_validate_json(payload)
    except (OSError, ValidationError, ValueError) as exc:
        raise HTTPException(500, f"product acceptance report is invalid: {exc}") from exc


@app.get("/api/settings", response_model=ConfigurationView)
async def get_settings():
    return configuration_service.get()


@app.get("/api/ai/settings", response_model=AIConfigurationView)
async def get_ai_settings():
    return ai_configuration_service.get()


@app.get("/api/ai/usage", response_model=AIUsageReport)
async def get_ai_usage(
    days: int = Query(default=30, ge=1, le=365),
    provider: AIProvider | None = None,
):
    return await asyncio.to_thread(
        ai_configuration_service.usage_report,
        days=days,
        provider=provider,
    )


@app.put("/api/ai/settings", response_model=AIConfigurationView)
async def update_ai_settings(update: AIConfigurationUpdate):
    try:
        view = await asyncio.to_thread(ai_configuration_service.save, update)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    enabled = ",".join(str(item.provider) for item in view.configuration.providers if item.enabled)
    add_audit(
        "settings",
        "AI 服务配置已更新；凭证仅保存在本机系统凭证库",
        enabled_providers=enabled,
        routing_mode=view.configuration.routing_mode,
    )
    return view


@app.post(
    "/api/ai/providers/{provider}/test",
    response_model=AIProviderTestResult,
)
async def test_ai_provider(provider: AIProvider):
    ai_gateway.service = ai_configuration_service
    ai_gateway.proxy_url = configuration_service.current_configuration().network_proxy_url
    result = await ai_gateway.test_provider(provider)
    add_audit(
        "ai",
        "AI 厂商连接验收已完成",
        provider=provider,
        ok=result.ok,
        failure_code=result.failure_code,
        latency_ms=result.latency_ms,
    )
    return result


@app.post(
    "/api/ai/providers/{provider}/discover-models",
    response_model=list[str],
)
async def discover_ai_models(provider: AIProvider):
    ai_gateway.service = ai_configuration_service
    ai_gateway.proxy_url = configuration_service.current_configuration().network_proxy_url
    try:
        return await ai_gateway.discover_models(provider)
    except AIProviderError as exc:
        raise HTTPException(502, {"code": exc.code, "message": exc.public_message}) from exc


class AgentPolicyUpdate(BaseModel):
    policy: AgentPolicy
    confirmation: str


class AgentGoalScopeUpdate(BaseModel):
    objective: str = Field(min_length=8, max_length=1000)
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    poll_seconds: int = Field(default=60, ge=15, le=3600)
    confirmation: str


class AgentTriggerRequest(BaseModel):
    event_type: AgentEventType = AgentEventType.MANUAL_TRIGGERED
    payload: dict = Field(default_factory=dict)
    dedupe_key: str | None = None


class AgentGoalStatusUpdate(BaseModel):
    status: AgentGoalStatus
    confirmation: str


def platform_agent_mode(configuration: UserConfiguration) -> AgentAutonomyMode:
    return {
        TradingMode.PAPER: AgentAutonomyMode.PAPER,
        TradingMode.SANDBOX: AgentAutonomyMode.SANDBOX,
        TradingMode.LIVE: AgentAutonomyMode.LIVE_CAPPED,
    }[configuration.trading_mode]


def launch_policy(request: AgentLaunchRequest) -> AgentPolicy:
    configuration = configuration_service.current_configuration()
    execution_mode = platform_agent_mode(configuration)
    can_trade = request.autonomy_mode == execution_mode
    risk = configuration.risk
    return AgentPolicy(
        id=f"launch-{request.profile_id}-{request.autonomy_mode}",
        autonomy_mode=request.autonomy_mode,
        allowed_symbols=request.symbols,
        allowed_timeframes=request.timeframes,
        max_risk_per_trade=risk.risk_per_trade if can_trade else Decimal("0"),
        max_daily_drawdown=risk.max_daily_drawdown if can_trade else Decimal("0"),
        max_open_positions=min(risk.max_open_positions, 3) if can_trade else 0,
        max_trades_per_day=6 if can_trade else 0,
        max_leverage=(
            configuration.contract_leverage
            if can_trade and configuration.market_type == "swap"
            else Decimal("1")
        ),
        require_native_protection=True,
        allow_agent_entry=can_trade,
        allow_agent_cancel_entry=True,
        allow_agent_reduce=True,
        allow_agent_exit=True,
        allow_agent_halt=True,
    )


def validate_launch_scope(request: AgentLaunchRequest) -> None:
    configuration = configuration_service.current_configuration()
    supported_modes = {
        AgentAutonomyMode.OBSERVE,
        AgentAutonomyMode.RESEARCH,
        platform_agent_mode(configuration),
    }
    if request.autonomy_mode not in supported_modes:
        raise HTTPException(409, "启动模式与当前交易环境不匹配")
    if not set(request.symbols).issubset(configuration.strategy.watchlist):
        raise HTTPException(409, "启动品种超出系统交易白名单")
    if not set(request.timeframes).issubset(configuration.strategy.timeframes):
        raise HTTPException(409, "启动周期超出系统周期白名单")


def launch_preview_payload(request: AgentLaunchRequest) -> AgentLaunchPreview:
    validate_launch_scope(request)
    policy = launch_policy(request)
    grant_payload = {
        "profile_id": request.profile_id,
        "symbols": sorted(request.symbols),
        "timeframes": sorted(request.timeframes),
        "autonomy_mode": str(request.autonomy_mode),
        "risk_template": request.risk_template,
        "policy_fingerprint": policy.fingerprint,
    }
    grant_fingerprint = stable_fingerprint(grant_payload)
    stored_payload = store.load_agent_launch_profile(request.profile_id)
    stored = AgentLaunchProfile.model_validate(stored_payload) if stored_payload else None
    configuration = configuration_service.current_configuration()
    risk = configuration.risk
    blocking: list[str] = []
    state = risk_state_service.get()
    if state.halted and request.autonomy_mode == platform_agent_mode(configuration):
        blocking.append(state.reason or "全局风险闸已停止新开仓")
    return AgentLaunchPreview(
        profile_id=request.profile_id,
        grant_fingerprint=grant_fingerprint,
        confirmation_required=(
            stored is None or stored.authorized_fingerprint != grant_fingerprint
        ),
        reuses_goal_id=stored.goal_id if stored else None,
        mode=request.autonomy_mode,
        symbols=request.symbols,
        timeframes=request.timeframes,
        risk_summary={
            "交易环境": str(configuration.trading_mode),
            "单笔风险": f"{risk.risk_per_trade * 100:f}%",
            "日内回撤": f"{risk.max_daily_drawdown * 100:f}%",
            "最大持仓": min(risk.max_open_positions, 3),
            "原生保护单": True,
        },
        blocking_reasons=blocking,
    )


def strategy_overview_summary(plan: StrategyPlan) -> AgentStrategySummary:
    return AgentStrategySummary(
        plan_id=plan.id,
        name=plan.name,
        symbol=plan.symbol,
        timeframe=plan.timeframe,
        family=plan.algorithm,
        status=str(plan.status),
        source=("agent" if plan.agent_goal_id else plan.decision_origin),
        fingerprint=plan.fingerprint,
        updated_at=plan.data_as_of or plan.signal_candle_at or plan.created_at,
    )


def build_agent_runtime_overview() -> AgentRuntimeOverview:
    goals = [
        goal
        for goal in agent_store.list_goals(200)
        if goal.status != AgentGoalStatus.ARCHIVED
    ]
    runs = agent_store.list_runs(200)
    episodes = agent_store.list_episodes(500)
    coverage = agent_store.schedule_market_coverage([goal.id for goal in goals])
    risk_state = risk_state_service.get()
    now = datetime.now(UTC)
    goal_views: list[AgentGoalOverview] = []
    latest_heartbeat: datetime | None = None
    for goal in goals:
        goal_runs = [item for item in runs if item.goal_id == goal.id]
        latest_run = goal_runs[0] if goal_runs else None
        last_decision = None
        if latest_run:
            decisions = agent_store.decisions_for_run(latest_run.id, 50)
            last_decision = decisions[-1] if decisions else None
        goal_coverage = {
            (item["symbol"], item["timeframe"]): item
            for item in coverage
            if item["goal_id"] == goal.id
        }
        senses: list[AgentMarketSense] = []
        heartbeat_candidates: list[datetime] = []
        next_scan_candidates: list[datetime] = []
        for symbol in goal.symbols:
            for timeframe in goal.timeframes:
                item = goal_coverage.get((symbol, timeframe)) or {
                    "symbol": symbol,
                    "timeframe": timeframe,
                }
                processed = item.get("event_processed_at") or item.get("event_created_at")
                processed_at = datetime.fromisoformat(processed) if processed else None
                if processed_at:
                    heartbeat_candidates.append(processed_at)
                interval = max(goal.poll_seconds, timeframe_seconds(timeframe))
                bucket = int(now.timestamp()) // interval
                next_scan = datetime.fromtimestamp((bucket + 1) * interval, UTC)
                next_scan_candidates.append(next_scan)
                event_status = item.get("event_status")
                run_status = item.get("last_run_status")
                sense_status = (
                    "paused"
                    if goal.status != AgentGoalStatus.ACTIVE
                    else "healthy"
                    if event_status == str(AgentEventStatus.PROCESSED)
                    and run_status
                    in {
                        str(AgentRunStatus.WAITING),
                        str(AgentRunStatus.COMPLETED),
                        str(AgentRunStatus.HALTED),
                    }
                    else "running"
                    if event_status == str(AgentEventStatus.CLAIMED)
                    else "queued"
                    if event_status in {None, str(AgentEventStatus.PENDING)}
                    else "degraded"
                )
                senses.append(
                    AgentMarketSense(
                        symbol=symbol,
                        timeframe=timeframe,
                        status=sense_status,
                        last_event_at=processed_at,
                        next_scan_at=next_scan,
                        last_run_status=run_status,
                    )
                )
        if latest_run:
            heartbeat_candidates.append(latest_run.updated_at)
        heartbeat = max(heartbeat_candidates, default=None)
        if heartbeat and (latest_heartbeat is None or heartbeat > latest_heartbeat):
            latest_heartbeat = heartbeat
        goal_plans = sorted(
            [plan for plan in plans.values() if plan.agent_goal_id == goal.id],
            key=lambda item: item.data_as_of or item.signal_candle_at or item.created_at,
            reverse=True,
        )
        recent_strategies = [strategy_overview_summary(item) for item in goal_plans[:5]]
        current_plan = next(
            (item for item in goal_plans if item.status != PlanStatus.ARCHIVED),
            None,
        )
        goal_episodes = [item for item in episodes if item.goal_id == goal.id]
        active_episodes = [
            item
            for item in goal_episodes
            if item.status
            in {
                TradeEpisodeStatus.RESERVED,
                TradeEpisodeStatus.PENDING_ENTRY,
                TradeEpisodeStatus.OPEN,
                TradeEpisodeStatus.AWAITING_REVIEW,
            }
        ]
        blocking: list[str] = []
        is_execution_goal = goal.autonomy_mode in {
            AgentAutonomyMode.PAPER,
            AgentAutonomyMode.SANDBOX,
            AgentAutonomyMode.LIVE_CAPPED,
        }
        if risk_state.halted and is_execution_goal:
            blocking.append(risk_state.reason or "全局风险闸已停止新开仓")
        if latest_run and latest_run.last_error:
            blocking.append(latest_run.last_error)
        if not goal_coverage:
            blocking.append("尚无市场调度记录")
        stale_after = max(goal.poll_seconds * 3, 180)
        runtime_status: Literal["running", "waiting", "paused", "blocked", "offline"]
        if goal.status != AgentGoalStatus.ACTIVE:
            runtime_status = "paused"
        elif blocking and is_execution_goal:
            runtime_status = "blocked"
        elif latest_run and latest_run.status == AgentRunStatus.RUNNING:
            runtime_status = "running"
        elif heartbeat and (now - heartbeat).total_seconds() > stale_after:
            runtime_status = "offline"
        else:
            runtime_status = "waiting"
        goal_views.append(
            AgentGoalOverview(
                goal_id=goal.id,
                objective=goal.objective,
                status=str(goal.status),
                autonomy_mode=str(goal.autonomy_mode),
                symbols=goal.symbols,
                timeframes=goal.timeframes,
                runtime_status=runtime_status,
                last_heartbeat_at=heartbeat,
                next_scan_at=min(next_scan_candidates, default=None),
                last_action=str(last_decision.action) if last_decision else None,
                last_action_summary=last_decision.reason if last_decision else None,
                current_strategy=(
                    strategy_overview_summary(current_plan) if current_plan else None
                ),
                recently_generated_strategies=recent_strategies,
                market_sense=senses,
                active_episode_count=len(active_episodes),
                uncertain_episode_count=sum(
                    item.status == TradeEpisodeStatus.UNCERTAIN for item in goal_episodes
                ),
                blocking_reasons=blocking,
            )
        )
    return AgentRuntimeOverview(
        active_goal_count=sum(goal.status == AgentGoalStatus.ACTIVE for goal in goals),
        latest_heartbeat_at=latest_heartbeat,
        risk_halted=risk_state.halted,
        risk_reason=risk_state.reason,
        goals=goal_views,
    )


def sandbox_forward_performance() -> AgentForwardPerformance:
    configuration = configuration_service.current_configuration()
    policies = agent_store.list_policies(500)
    sandbox_policies = [
        policy
        for policy in policies
        if policy.autonomy_mode == AgentAutonomyMode.SANDBOX and policy.allow_agent_entry
    ]
    sandbox_policy_ids = {policy.id for policy in sandbox_policies}
    required_symbols = list(
        dict.fromkeys(symbol for policy in sandbox_policies for symbol in policy.allowed_symbols)
    )
    episodes = [
        episode
        for episode in agent_store.list_episodes(10000)
        if episode.policy_id in sandbox_policy_ids
    ]
    return build_forward_performance(
        episodes,
        configuration.agent_forward_validation,
        scope_mode=AgentAutonomyMode.SANDBOX,
        required_symbols=required_symbols,
    )


def validate_agent_policy_against_platform(policy: AgentPolicy) -> None:
    configuration = configuration_service.current_configuration()
    execution_modes = {
        AgentAutonomyMode.PAPER: TradingMode.PAPER,
        AgentAutonomyMode.SANDBOX: TradingMode.SANDBOX,
        AgentAutonomyMode.LIVE_CAPPED: TradingMode.LIVE,
    }
    expected_mode = execution_modes.get(policy.autonomy_mode)
    if expected_mode is not None and configuration.trading_mode != expected_mode:
        mode_label = "实盘模式" if expected_mode == TradingMode.LIVE else f"{expected_mode} 模式"
        raise HTTPException(
            409,
            f"Agent {policy.autonomy_mode} 授权只能在平台{mode_label}中创建或使用",
        )
    if policy.allow_agent_entry and (
        policy.autonomy_mode not in execution_modes
        or policy.max_risk_per_trade <= 0
        or policy.max_daily_drawdown <= 0
        or policy.max_open_positions <= 0
        or policy.max_trades_per_day <= 0
        or not policy.allowed_symbols
        or not policy.allowed_timeframes
    ):
        raise HTTPException(409, "Agent 自动入场授权必须具有非零且完整的风险预算与市场白名单")
    if policy.max_risk_per_trade > configuration.risk.risk_per_trade:
        raise HTTPException(409, "Agent 单笔风险授权不能超过平台硬风控上限")
    if policy.max_daily_drawdown > configuration.risk.max_daily_drawdown:
        raise HTTPException(409, "Agent 日内回撤授权不能超过平台硬风控上限")
    if policy.max_leverage > configuration.risk.max_leverage:
        raise HTTPException(409, "Agent 杠杆授权不能超过平台硬风控上限")
    if (
        policy.allow_agent_entry
        and str(configuration.market_type) == "swap"
        and policy.max_leverage < configuration.contract_leverage
    ):
        raise HTTPException(409, "Agent 杠杆上限不能低于当前固定执行杠杆，否则授权不可执行")
    if not set(policy.allowed_symbols).issubset(configuration.strategy.watchlist):
        raise HTTPException(409, "Agent 品种授权必须是平台关注列表的子集")
    if not set(policy.allowed_timeframes).issubset(configuration.strategy.timeframes):
        raise HTTPException(409, "Agent 周期授权必须是平台策略周期的子集")
    if (
        policy.autonomy_mode
        in {
            AgentAutonomyMode.SANDBOX,
            AgentAutonomyMode.LIVE_CAPPED,
        }
        and not policy.require_native_protection
    ):
        raise HTTPException(409, "交易所自动交易模式必须强制使用原生保护单")
    if policy.autonomy_mode == AgentAutonomyMode.LIVE_CAPPED and (
        configuration.trading_mode != TradingMode.LIVE or not configuration.live_trading_enabled
    ):
        raise HTTPException(409, "创建 live_capped 授权前必须先显式解锁平台实盘模式")


def validate_agent_plan_grant(plan: StrategyPlan) -> None:
    if plan.decision_origin != "agent":
        return
    if not all(
        (
            plan.agent_goal_id,
            plan.agent_run_id,
            plan.agent_policy_id,
            plan.agent_policy_fingerprint,
        )
    ):
        raise HTTPException(409, "Agent 计划缺少完整目标、运行或权限绑定")
    goal = agent_store.load_goal(plan.agent_goal_id)
    policy = agent_store.load_policy(plan.agent_policy_id)
    if not goal or goal.status != AgentGoalStatus.ACTIVE:
        raise HTTPException(409, "Agent 目标已失效，禁止激活或执行计划")
    if goal.valid_until and goal.valid_until <= datetime.now(UTC):
        raise HTTPException(409, "Agent 目标已过期，禁止激活或执行计划")
    if not policy or policy.fingerprint != plan.agent_policy_fingerprint:
        raise HTTPException(409, "Agent 权限已变化，计划必须重新生成和验证")
    if not policy.allow_agent_entry:
        raise HTTPException(409, "当前 Agent 权限不允许增加账户风险")
    if goal.autonomy_mode != policy.autonomy_mode:
        raise HTTPException(409, "Agent 目标与权限模式不一致")
    validate_plan_research_provenance(plan)
    validate_plan_campaign_provenance(plan)
    validate_agent_policy_against_platform(policy)


def selected_causal_policy_definition(
    report: ResearchCampaignReport,
) -> dict | None:
    if not report.causal_policy_definitions or not report.selected_library_entry_id:
        return None
    return next(
        (
            item
            for item in report.causal_policy_definitions
            if item.get("campaign_entry_id") == report.selected_library_entry_id
        ),
        None,
    )


def build_plan_causal_policy_selection_evidence(
    plan: StrategyPlan,
    report: ResearchCampaignReport,
    *,
    assessment: MarketStateAssessment,
    available_data: set[str],
    selected_library_entry_id: str,
    actionable_library_evidence: list[dict] | None = None,
) -> dict:
    """Freeze the exact causal policy decision that selected a concrete spec."""

    definition = selected_causal_policy_definition(report)
    certificate = report.execution_certificate
    if definition is None or certificate is None:
        raise ValueError("Campaign does not contain a certified causal policy")
    policy_fingerprint = validate_causal_policy_definition(definition)
    if policy_fingerprint != certificate.strategy_fingerprint or policy_fingerprint != next(
        item.strategy_fingerprint
        for item in report.candidates
        if item.library_entry_id == report.selected_library_entry_id
    ):
        raise ValueError("causal policy certificate and Campaign evidence differ")
    if bool(definition["abstain_on_ambiguous_regime"]) and assessment.ambiguous:
        raise ValueError("certified causal policy requires abstention in an ambiguous regime")
    if assessment.confidence < Decimal(str(definition.get("minimum_regime_confidence", "0"))):
        raise ValueError("current regime confidence is below the certified policy threshold")
    priors = [CausalStrategyPrior.model_validate(item) for item in definition["priors"]]
    ranked = rank_causal_strategy_priors(priors, assessment, set(available_data))
    admitted = ranked[: int(definition["maximum_candidates_per_bar"])]
    selected_prior = next(
        (prior for prior, _, _ in admitted if prior.id == selected_library_entry_id),
        None,
    )
    if selected_prior is None:
        raise ValueError("concrete strategy was not admitted by the certified causal policy")
    if selected_prior.spec_fingerprint != plan.strategy_spec_fingerprint:
        raise ValueError("selected public strategy fingerprint differs from the trade plan")
    if plan.signal_candle_at is None or plan.data_as_of is None:
        raise ValueError("causal policy plan must bind signal and market evidence times")
    minimum_signal_confidence = Decimal(str(definition.get("minimum_signal_confidence", "0")))
    raw_consensus_evidence = actionable_library_evidence or [
        {
            "library_entry_id": selected_library_entry_id,
            "strategy_fingerprint": plan.strategy_spec_fingerprint,
            "side": str(plan.side),
            "confidence": str(plan.confidence),
            "signal_candle_at": plan.signal_candle_at.isoformat(),
            "data_as_of": plan.data_as_of.isoformat(),
        }
    ]
    admitted_priors = {prior.id: prior for prior, _, _ in admitted}
    consensus_evidence: list[dict] = []
    for item in raw_consensus_evidence:
        entry_id = str(item.get("library_entry_id") or "")
        prior = admitted_priors.get(entry_id)
        if prior is None or str(item.get("side")) != str(plan.side):
            continue
        if str(item.get("strategy_fingerprint")) != prior.spec_fingerprint:
            continue
        if Decimal(str(item.get("confidence") or "0")) < minimum_signal_confidence:
            continue
        if (
            str(item.get("signal_candle_at")) != plan.signal_candle_at.isoformat()
            or str(item.get("data_as_of")) != plan.data_as_of.isoformat()
        ):
            continue
        consensus_evidence.append(
            {
                "library_entry_id": entry_id,
                "strategy_fingerprint": prior.spec_fingerprint,
                "side": str(plan.side),
                "confidence": str(item["confidence"]),
                "signal_candle_at": str(item["signal_candle_at"]),
                "data_as_of": str(item["data_as_of"]),
            }
        )
    consensus_evidence = sorted(
        {item["library_entry_id"]: item for item in consensus_evidence}.values(),
        key=lambda item: item["library_entry_id"],
    )
    required_consensus = int(definition.get("minimum_directional_consensus", 1))
    if (
        selected_library_entry_id not in {item["library_entry_id"] for item in consensus_evidence}
        or len(consensus_evidence) < required_consensus
    ):
        raise ValueError("current signals do not satisfy certified directional consensus")
    payload = {
        "policy_fingerprint": policy_fingerprint,
        "campaign_entry_id": report.selected_library_entry_id,
        "selected_library_entry_id": selected_library_entry_id,
        "selected_strategy_fingerprint": selected_prior.spec_fingerprint,
        "ranked_admitted_library_entry_ids": [prior.id for prior, _, _ in admitted],
        "directional_consensus_entries": consensus_evidence,
        "directional_consensus_count": len(consensus_evidence),
        "available_data": sorted(available_data),
        "assessment": assessment.model_dump(mode="json"),
        "signal_candle_at": plan.signal_candle_at.isoformat(),
        "data_as_of": plan.data_as_of.isoformat(),
        "selector_version": int(definition["policy_version"]),
        "realized_return_inputs_forbidden": True,
    }
    payload["fingerprint"] = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return payload


def validate_plan_causal_policy_selection(
    plan: StrategyPlan,
    report: ResearchCampaignReport,
) -> bool:
    evidence = getattr(plan, "causal_policy_selection_evidence", None)
    selected_entry_id = getattr(
        plan,
        "causal_policy_selected_library_entry_id",
        None,
    )
    if not evidence or not selected_entry_id:
        return False
    try:
        expected = build_plan_causal_policy_selection_evidence(
            plan,
            report,
            assessment=MarketStateAssessment.model_validate(evidence["assessment"]),
            available_data=set(evidence["available_data"]),
            selected_library_entry_id=selected_entry_id,
            actionable_library_evidence=evidence.get(
                "directional_consensus_entries",
                [],
            ),
        )
    except (KeyError, StopIteration, TypeError, ValueError):
        return False
    return bool(
        expected == evidence
        and getattr(plan, "causal_policy_fingerprint", None) == evidence["policy_fingerprint"]
    )


def matching_execution_campaign(
    plan: StrategyPlan,
    campaign_payloads: list[dict],
    *,
    require_plan_binding: bool = True,
    causal_policy_assessment: MarketStateAssessment | None = None,
    causal_policy_available_data: set[str] | None = None,
    causal_policy_selected_library_entry_id: str | None = None,
    causal_policy_actionable_evidence: list[dict] | None = None,
) -> ResearchCampaignReport | None:
    """Find the exact cross-market admission used for exchange execution."""

    if not plan.strategy_spec_fingerprint:
        return None
    for payload in campaign_payloads:
        report = ResearchCampaignReport.model_validate(payload)
        certificate = report.execution_certificate
        selected = next(
            (
                item
                for item in report.candidates
                if item.library_entry_id == report.selected_library_entry_id
            ),
            None,
        )
        if certificate is None:
            continue
        dataset_fingerprints: dict[str, str] = {}
        certificate_scope_valid = True
        for dataset_id in [*report.research_dataset_ids, *report.blind_dataset_ids]:
            dataset_payload = store.load_research_dataset(dataset_id)
            if not dataset_payload:
                certificate_scope_valid = False
                break
            dataset = ResearchDataset.model_validate(dataset_payload)
            dataset_fingerprints[dataset_id] = dataset.data_fingerprint
        if not certificate_scope_valid:
            continue
        try:
            validate_campaign_execution_certificate(
                report,
                certificate,
                dataset_fingerprints=dataset_fingerprints,
            )
        except ValueError:
            continue
        policy_definition = selected_causal_policy_definition(report)
        if policy_definition is not None:
            if (
                causal_policy_assessment is not None
                and causal_policy_available_data is not None
                and causal_policy_selected_library_entry_id is not None
            ):
                try:
                    selection_evidence = build_plan_causal_policy_selection_evidence(
                        plan,
                        report,
                        assessment=causal_policy_assessment,
                        available_data=causal_policy_available_data,
                        selected_library_entry_id=(causal_policy_selected_library_entry_id),
                        actionable_library_evidence=(causal_policy_actionable_evidence),
                    )
                except (StopIteration, ValueError):
                    strategy_binding_valid = False
                else:
                    strategy_binding_valid = bool(
                        selected is not None
                        and selected.strategy_fingerprint
                        == selection_evidence["policy_fingerprint"]
                        and certificate.strategy_fingerprint
                        == selection_evidence["policy_fingerprint"]
                    )
            else:
                strategy_binding_valid = bool(
                    selected is not None
                    and selected.strategy_fingerprint == certificate.strategy_fingerprint
                    and validate_plan_causal_policy_selection(plan, report)
                )
        else:
            strategy_binding_valid = bool(
                selected is not None
                and selected.strategy_fingerprint == plan.strategy_spec_fingerprint
                and certificate.strategy_fingerprint == plan.strategy_spec_fingerprint
            )
        plan_binding_valid = (
            getattr(plan, "research_campaign_id", None) == report.id
            and getattr(plan, "campaign_certificate_fingerprint", None) == certificate.fingerprint
        )
        if (
            report.autonomous_execution_eligible
            and report.pbo.passed
            and bool(report.blind_dataset_ids)
            and report.blind_selected_candidate_passed
            and selected is not None
            and selected.cross_market_passed
            and strategy_binding_valid
            and (plan_binding_valid or not require_plan_binding)
        ):
            return report
    return None


def validate_plan_campaign_provenance(plan: StrategyPlan) -> None:
    """Never let one backtest authorize an Agent exchange position."""

    if plan.trading_mode == TradingMode.PAPER:
        return
    campaign = matching_execution_campaign(
        plan,
        store.list_research_campaigns(100),
    )
    if campaign is None:
        raise HTTPException(
            409,
            "Agent exchange execution requires an exact-spec immutable Campaign "
            "certificate bound to this plan, with passed PBO, cross-market and "
            "single-use blind admission",
        )


def validate_plan_research_provenance(plan: StrategyPlan) -> None:
    """Revalidate AI/ML Champion status and immutable evidence before execution."""

    source = plan.strategy_spec.get("source") if plan.strategy_spec else None
    if source not in {"ai", "ml"}:
        return
    if not plan.research_candidate_id or not plan.validated_strategy_artifact:
        raise HTTPException(
            409,
            "AI/ML 交易计划缺少已验证 Champion 执行工件。",
        )
    payload = store.load_strategy_candidate(plan.research_candidate_id)
    if not payload:
        raise HTTPException(409, "计划绑定的研究 Champion 已不存在。")
    candidate = parse_strategy_candidate_payload(
        payload,
        audit_context="plan_research_provenance",
    )
    if candidate is None:
        raise HTTPException(
            409,
            "计划绑定的研究 Champion 与当前策略合约不兼容，必须重新研究和编译。",
        )
    if candidate.spec != plan.strategy_spec:
        raise HTTPException(409, "计划 StrategySpec 与绑定的 Champion 不一致。")
    dataset_payload = (
        store.load_research_dataset(candidate.dataset_id) if candidate.dataset_id else None
    )
    if not dataset_payload:
        raise HTTPException(409, "Champion 绑定的不可变研究数据集已不可用。")
    dataset = ResearchDataset.model_validate(dataset_payload)
    try:
        artifact = ValidatedStrategyArtifact.model_validate(plan.validated_strategy_artifact)
        validate_compiled_champion(
            candidate,
            artifact,
            dataset_id=dataset.id,
            data_fingerprint=dataset.data_fingerprint,
        )
    except ValueError as exc:
        raise HTTPException(409, f"Champion 执行工件验证失败：{exc}") from exc


def require_agent_position_management_commissioning() -> None:
    """Prevent an Agent from opening a position it cannot safely manage."""

    configuration = configuration_service.current_configuration()
    if configuration.trading_mode == TradingMode.PAPER:
        return
    commissioning = configuration_service.commissioning_for(configuration)
    if not (commissioning and commissioning.agent_position_management_passed):
        raise RuntimeError(
            "exchange Agent trading is locked until the sandbox "
            "position-management commissioning has passed"
        )


def build_agent_episode_runtime_views(
    episodes: list[TradeEpisode],
) -> list[AgentEpisodeRuntime]:
    records = runtime.order_ledger.records if runtime.order_ledger else {}
    report = runtime.reconciliation_report
    uncertain_ids = set(report.uncertain_order_ids) if report else set()
    unknown_ids = set(report.unknown_open_order_ids) if report else set()
    unprotected_ids = set(report.unprotected_order_ids) if report else set()
    views: list[AgentEpisodeRuntime] = []
    for episode in episodes:
        plan = plans.get(episode.plan_id)
        entry_records = [
            records[order_id] for order_id in episode.entry_order_ids if order_id in records
        ]
        entry_quantity = sum(
            (record.order.filled_quantity for record in entry_records),
            Decimal("0"),
        )
        entry_notional = sum(
            (
                record.order.filled_quantity * record.order.average_fill_price
                for record in entry_records
                if record.order.average_fill_price is not None
            ),
            Decimal("0"),
        )
        priced_quantity = sum(
            (
                record.order.filled_quantity
                for record in entry_records
                if record.order.average_fill_price is not None
            ),
            Decimal("0"),
        )
        average_entry = (
            entry_notional / priced_quantity if priced_quantity > 0 and entry_notional > 0 else None
        )
        protected_order_ids = [
            record.order.id for record in entry_records if record.protection_verified
        ]
        entry_ids = set(episode.entry_order_ids)
        reconciliation_clean = bool(report) and not (
            entry_ids & (uncertain_ids | unknown_ids | unprotected_ids)
        )
        protection_verified = bool(entry_ids) and (
            len(entry_records) == len(entry_ids)
            and len(protected_order_ids) == len(entry_ids)
            and reconciliation_clean
        )
        if episode.status in {
            TradeEpisodeStatus.OPEN,
            TradeEpisodeStatus.PENDING_ENTRY,
            TradeEpisodeStatus.UNCERTAIN,
        }:
            protection_status = "verified" if protection_verified else "uncertain"
        else:
            protection_status = "not_applicable"
        last_reconciled_at = max(
            (record.last_reconciled_at for record in entry_records),
            default=None,
        )
        views.append(
            AgentEpisodeRuntime(
                episode_id=episode.id,
                plan_id=episode.plan_id,
                symbol=episode.symbol,
                plan_status=str(plan.status) if plan else None,
                leverage=plan.leverage if plan else None,
                entry_quantity=entry_quantity,
                average_entry=average_entry,
                entry_fees=sum(
                    (record.order.fee for record in entry_records),
                    Decimal("0"),
                ),
                stop_loss=plan.exit.stop_loss if plan else None,
                take_profits=(
                    [target.model_dump(mode="json") for target in plan.exit.take_profits]
                    if plan
                    else []
                ),
                native_protection_status=protection_status,
                protected_order_ids=protected_order_ids,
                last_reconciled_at=last_reconciled_at,
            )
        )
    return views


@app.get("/api/agent/overview", response_model=AgentRuntimeOverview)
async def agent_runtime_overview():
    return await asyncio.to_thread(build_agent_runtime_overview)


@app.post("/api/agent/launch/preview", response_model=AgentLaunchPreview)
async def preview_agent_launch(request: AgentLaunchRequest):
    return await asyncio.to_thread(launch_preview_payload, request)


@app.post("/api/agent/launch", response_model=AgentLaunchResult)
async def launch_agent(request: AgentLaunchRequest):
    preview = await asyncio.to_thread(launch_preview_payload, request)
    if preview.confirmation_required:
        if not request.authorize:
            raise HTTPException(409, "首次启动或权限范围变化，需要确认授权")
        if request.preview_fingerprint != preview.grant_fingerprint:
            raise HTTPException(409, "启动预览已变化，请重新核对授权摘要")
    policy = launch_policy(request)
    validate_agent_policy_against_platform(policy)
    stored_payload = await asyncio.to_thread(
        store.load_agent_launch_profile,
        request.profile_id,
    )
    stored = AgentLaunchProfile.model_validate(stored_payload) if stored_payload else None
    goal_id = (
        stored.goal_id
        if stored and stored.goal_id
        else f"goal_launch_{stable_fingerprint({'profile_id': request.profile_id})[:16]}"
    )
    existing_goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if existing_goal:
        goal = existing_goal.model_copy(
            deep=True,
            update={
                "objective": request.objective,
                "symbols": request.symbols,
                "timeframes": request.timeframes,
                "autonomy_mode": request.autonomy_mode,
                "risk_policy_id": policy.id,
                "poll_seconds": request.poll_seconds,
                "status": AgentGoalStatus.ACTIVE,
                "updated_at": datetime.now(UTC),
            },
        )
    else:
        goal = AgentGoal(
            id=goal_id,
            objective=request.objective,
            symbols=request.symbols,
            timeframes=request.timeframes,
            autonomy_mode=request.autonomy_mode,
            risk_policy_id=policy.id,
            poll_seconds=request.poll_seconds,
            status=AgentGoalStatus.ACTIVE,
        )
    now = datetime.now(UTC)
    profile = AgentLaunchProfile(
        id=request.profile_id,
        goal_id=goal.id,
        policy_id=policy.id,
        symbols=request.symbols,
        timeframes=request.timeframes,
        autonomy_mode=request.autonomy_mode,
        risk_template=request.risk_template,
        objective=request.objective,
        poll_seconds=request.poll_seconds,
        grant_fingerprint=preview.grant_fingerprint,
        authorized_fingerprint=preview.grant_fingerprint,
        created_at=stored.created_at if stored else now,
        updated_at=now,
    )
    await asyncio.to_thread(agent_store.save_launch_bundle, policy, goal, profile)
    add_audit(
        "agent",
        "自动交易 Agent 已通过快速启动配置启用",
        severity="warning",
        goal_id=goal.id,
        profile_id=profile.id,
        symbols=",".join(goal.symbols),
        timeframes=",".join(goal.timeframes),
        autonomy_mode=str(goal.autonomy_mode),
        reused=bool(existing_goal),
    )
    return AgentLaunchResult(
        profile=profile,
        goal_id=goal.id,
        reused=bool(existing_goal),
        status=str(goal.status),
    )


@app.post("/api/agent/goals/{goal_id}/archive", response_model=AgentGoal)
async def archive_agent_goal(goal_id: str):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    goal.status = AgentGoalStatus.ARCHIVED
    await asyncio.to_thread(agent_store.save_goal, goal)
    add_audit("agent", "Agent 目标已归档", severity="warning", goal_id=goal.id)
    return goal


@app.post("/api/agent/goals/{goal_id}/restore", response_model=AgentGoal)
async def restore_agent_goal(goal_id: str):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    if goal.status != AgentGoalStatus.ARCHIVED:
        raise HTTPException(409, "only an archived Agent goal can be restored")
    goal.status = AgentGoalStatus.PAUSED
    await asyncio.to_thread(agent_store.save_goal, goal)
    add_audit("agent", "Agent 目标已恢复为暂停状态", goal_id=goal.id)
    return goal


@app.websocket("/ws/agent")
async def agent_runtime_stream(websocket: WebSocket):
    await websocket.accept()
    last_payload = ""
    try:
        while True:
            overview = await asyncio.to_thread(build_agent_runtime_overview)
            payload = overview.model_dump_json()
            if payload != last_payload:
                await websocket.send_json(
                    {"type": "agent_overview", "data": overview.model_dump(mode="json")}
                )
                last_payload = payload
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc)[:180]})
            await websocket.close(code=1011)


@app.get("/api/agent/state")
async def agent_control_state():
    goals, policies, runs, episodes, memories, shadow_trials = await asyncio.gather(
        asyncio.to_thread(agent_store.list_goals, 50),
        asyncio.to_thread(agent_store.list_policies, 50),
        asyncio.to_thread(agent_store.list_runs, 50),
        asyncio.to_thread(agent_store.list_episodes, 50),
        asyncio.to_thread(agent_store.recent_memories, 50),
        asyncio.to_thread(agent_store.list_shadow_trials, 500),
    )
    schedule_coverage = await asyncio.to_thread(
        agent_store.schedule_market_coverage,
        [goal.id for goal in goals],
    )
    goal_by_id = {goal.id: goal for goal in goals}
    now = datetime.now(UTC)
    for item in schedule_coverage:
        goal = goal_by_id.get(item["goal_id"])
        interval = max(
            goal.poll_seconds if goal else 60,
            timeframe_seconds(item["timeframe"]),
        )
        bucket = int(now.timestamp()) // interval
        item["next_scheduled_at"] = datetime.fromtimestamp(
            (bucket + 1) * interval,
            UTC,
        ).isoformat()
        item["coverage_status"] = (
            "paused"
            if goal and goal.status != AgentGoalStatus.ACTIVE
            else "healthy"
            if (
                item["event_status"] == str(AgentEventStatus.PROCESSED)
                and item["last_run_status"]
                in {
                    str(AgentRunStatus.WAITING),
                    str(AgentRunStatus.COMPLETED),
                    str(AgentRunStatus.HALTED),
                }
            )
            else "running"
            if item["event_status"] == str(AgentEventStatus.CLAIMED)
            else "queued"
            if item["event_status"] == str(AgentEventStatus.PENDING)
            else "degraded"
        )
    for trial in shadow_trials:
        if trial.backtest_evidence:
            continue
        payload = await asyncio.to_thread(
            store.load_latest_backtest,
            trial.plan.id,
        )
        if not payload:
            continue
        artifact = BacktestResult.model_validate(payload)
        if artifact.strategy_fingerprint != trial.plan.fingerprint or artifact.cost_model.get(
            "direction_filter"
        ) != str(trial.plan.side):
            continue
        trial.backtest_evidence = evidence_summary(artifact)
        await asyncio.to_thread(agent_store.save_shadow_trial, trial)
    return {
        "goals": goals,
        "policies": policies,
        "runs": runs,
        "episodes": episodes,
        "episode_runtime": build_agent_episode_runtime_views(episodes),
        "memories": memories,
        "shadow_trials": shadow_trials[:100],
        "shadow_calibration": summarize_shadow_trials(shadow_trials).model_dump(mode="json"),
        "market_coverage": schedule_coverage,
        "risk_state": risk_state_service.get(),
    }


@app.get("/api/agent/performance", response_model=AgentForwardPerformance)
async def agent_forward_performance(
    autonomy_mode: AgentAutonomyMode = AgentAutonomyMode.SANDBOX,
    goal_id: str | None = None,
):
    policies, episodes = await asyncio.gather(
        asyncio.to_thread(agent_store.list_policies, 500),
        asyncio.to_thread(agent_store.list_episodes, 10000),
    )
    matching_policies = [
        policy
        for policy in policies
        if policy.autonomy_mode == autonomy_mode and policy.allow_agent_entry
    ]
    policy_ids = {policy.id for policy in matching_policies}
    scoped = [
        episode
        for episode in episodes
        if episode.policy_id in policy_ids and (goal_id is None or episode.goal_id == goal_id)
    ]
    selected_goal = await asyncio.to_thread(agent_store.load_goal, goal_id) if goal_id else None
    required_symbols = (
        selected_goal.symbols
        if selected_goal
        else list(
            dict.fromkeys(
                symbol for policy in matching_policies for symbol in policy.allowed_symbols
            )
        )
    )
    configuration = configuration_service.current_configuration()
    return build_forward_performance(
        scoped,
        configuration.agent_forward_validation,
        scope_mode=autonomy_mode,
        required_symbols=required_symbols,
    )


@app.get("/api/agent/runs/{run_id}")
async def agent_run_trace(run_id: str):
    run, decisions, observations = await asyncio.gather(
        asyncio.to_thread(agent_store.load_run, run_id),
        asyncio.to_thread(agent_store.decisions_for_run, run_id, 50),
        asyncio.to_thread(agent_store.observations_for_run, run_id, 50),
    )
    if not run:
        raise HTTPException(404, "Agent run not found")
    return {
        "run": run,
        "decisions": decisions,
        "observations": observations,
    }


@app.get("/api/agent/readiness", response_model=AgentReadiness)
async def agent_trading_readiness() -> AgentReadiness:
    configuration_view = configuration_service.get()
    configuration = configuration_view.configuration
    ai_view = ai_configuration_service.get()
    risk_state = risk_state_service.get()
    reconciliation_health = runtime.reconciliation_health
    reconciliation_entry_allowed = (
        configuration.trading_mode == TradingMode.PAPER or reconciliation_health.entry_allowed
    )
    goals, policies, episodes = await asyncio.gather(
        asyncio.to_thread(agent_store.list_goals, 100),
        asyncio.to_thread(agent_store.list_policies, 100),
        asyncio.to_thread(agent_store.list_episodes, 200),
    )
    mode = {
        TradingMode.PAPER: AgentAutonomyMode.PAPER,
        TradingMode.SANDBOX: AgentAutonomyMode.SANDBOX,
        TradingMode.LIVE: AgentAutonomyMode.LIVE_CAPPED,
    }[configuration.trading_mode]

    ready_ai_providers = []
    for provider in ai_view.configuration.providers:
        credential = ai_view.credentials[provider.provider]
        provider_runtime = ai_view.runtime[provider.provider]
        if (
            provider.enabled
            and credential.api_key_configured
            and provider_runtime.status == "ready"
            and provider_runtime.selected_model_available is not False
        ):
            ready_ai_providers.append(str(provider.provider))

    commissioning = configuration_view.commissioning
    position_management_ready = configuration.trading_mode == TradingMode.PAPER or bool(
        commissioning and commissioning.agent_position_management_passed
    )
    entry_policies = [
        policy
        for policy in policies
        if policy.autonomy_mode == mode
        and policy.allow_agent_entry
        and policy.max_risk_per_trade > 0
        and policy.max_daily_drawdown > 0
        and policy.max_open_positions > 0
        and policy.max_trades_per_day > 0
        and policy.allowed_symbols
        and policy.allowed_timeframes
        and (configuration.trading_mode == TradingMode.PAPER or policy.require_native_protection)
    ]
    entry_policy_ids = {policy.id for policy in entry_policies}
    now = datetime.now(UTC)
    active_goals = [
        goal
        for goal in goals
        if goal.status == AgentGoalStatus.ACTIVE
        and goal.autonomy_mode == mode
        and goal.risk_policy_id in entry_policy_ids
        and (goal.valid_until is None or goal.valid_until > now)
    ]
    uncertain_episodes = [
        episode for episode in episodes if episode.status == TradeEpisodeStatus.UNCERTAIN
    ]
    checks = [
        AgentReadinessCheck(
            code="ai_reasoner",
            passed=bool(ready_ai_providers),
            message=(
                f"可用推理提供商：{', '.join(ready_ai_providers)}"
                if ready_ai_providers
                else "没有同时启用、配置凭证并通过模型测试的 AI 提供商"
            ),
        ),
        AgentReadinessCheck(
            code="execution_gateway",
            passed=configuration_view.execution_ready,
            message=(
                "交易网关已通过执行验收"
                if configuration_view.execution_ready
                else "交易网关尚未通过当前凭证对应的原子保护单验收"
            ),
        ),
        AgentReadinessCheck(
            code="position_management",
            passed=position_management_ready,
            message=(
                "Agent 可验证减仓、保护单再平衡与完全退出"
                if position_management_ready
                else "尚未通过 Agent 减仓、保护单再平衡与完全退出验收"
            ),
        ),
        AgentReadinessCheck(
            code="risk_gate",
            passed=not risk_state.halted and reconciliation_entry_allowed,
            message=(
                "全局风险闸允许交易，订单对账已同步"
                if not risk_state.halted and reconciliation_entry_allowed
                else (
                    f"全局风险闸已停机：{risk_state.reason}"
                    if risk_state.halted
                    else "订单对账暂时不可用，系统正在自动重试，期间禁止新开仓"
                )
            ),
        ),
        AgentReadinessCheck(
            code="entry_policy",
            passed=bool(entry_policies),
            message=(
                f"{len(entry_policies)} 个 {mode} 自动入场授权有效"
                if entry_policies
                else f"没有有效的 {mode} 自动入场授权"
            ),
        ),
        AgentReadinessCheck(
            code="active_goal",
            passed=bool(active_goals),
            message=(
                f"{len(active_goals)} 个活动交易目标已绑定有效授权"
                if active_goals
                else "没有活动交易目标绑定有效自动入场授权"
            ),
        ),
        AgentReadinessCheck(
            code="episode_reconciliation",
            passed=not uncertain_episodes,
            message=(
                "没有待人工对账的不确定交易回合"
                if not uncertain_episodes
                else f"{len(uncertain_episodes)} 个不确定交易回合必须先完成对账"
            ),
        ),
    ]
    if configuration.trading_mode == TradingMode.LIVE:
        sandbox_policy_ids = {
            policy.id for policy in policies if policy.autonomy_mode == AgentAutonomyMode.SANDBOX
        }
        sandbox_symbols = list(
            dict.fromkeys(
                symbol
                for policy in policies
                if policy.autonomy_mode == AgentAutonomyMode.SANDBOX and policy.allow_agent_entry
                for symbol in policy.allowed_symbols
            )
        )
        sandbox_performance = build_forward_performance(
            [episode for episode in episodes if episode.policy_id in sandbox_policy_ids],
            configuration.agent_forward_validation,
            scope_mode=AgentAutonomyMode.SANDBOX,
            required_symbols=sandbox_symbols,
        )
        checks.append(
            AgentReadinessCheck(
                code="sandbox_forward_validation",
                passed=sandbox_performance.qualified_for_live_review,
                message=(
                    "测试网前向收益、近期优势与回撤证据已达到实盘评审门槛"
                    if sandbox_performance.qualified_for_live_review
                    else (
                        "测试网前向证据尚未通过："
                        f"{sandbox_performance.attributable_episodes} 个完整回合，"
                        f"{sandbox_performance.independent_runs} 个独立执行批次，"
                        f"累计 {sandbox_performance.total_r:f}R"
                    )
                ),
            )
        )
    return AgentReadiness(
        ready_for_autonomous_trading=all(check.passed for check in checks),
        mode=mode,
        checks=checks,
    )


@app.put("/api/agent/policies/{policy_id}", response_model=AgentPolicy)
async def save_agent_policy(policy_id: str, update: AgentPolicyUpdate):
    if update.policy.id != policy_id:
        raise HTTPException(422, "policy id does not match request path")
    expected = f"AUTHORIZE AGENT {policy_id} {update.policy.autonomy_mode}"
    if update.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    validate_agent_policy_against_platform(update.policy)
    await asyncio.to_thread(agent_store.save_policy, update.policy)
    add_audit(
        "agent",
        "Agent 权限策略已由操作员保存",
        severity="warning",
        policy_id=policy_id,
        autonomy_mode=str(update.policy.autonomy_mode),
        allow_agent_entry=update.policy.allow_agent_entry,
    )
    return update.policy


@app.post("/api/agent/goals", response_model=AgentGoal)
async def create_agent_goal(goal: AgentGoal):
    policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
    if not policy:
        raise HTTPException(409, "Agent goal references an unknown policy")
    if goal.autonomy_mode != policy.autonomy_mode:
        raise HTTPException(409, "Agent goal and policy autonomy modes must match")
    if not set(goal.symbols).issubset(policy.allowed_symbols):
        raise HTTPException(409, "Agent goal symbols exceed the policy grant")
    if not set(goal.timeframes).issubset(policy.allowed_timeframes):
        raise HTTPException(409, "Agent goal timeframes exceed the policy grant")
    await asyncio.to_thread(agent_store.save_goal, goal)
    add_audit(
        "agent",
        "新的持久化 Agent 目标已创建",
        goal_id=goal.id,
        autonomy_mode=str(goal.autonomy_mode),
    )
    return goal


@app.put("/api/agent/goals/{goal_id}", response_model=AgentGoal)
async def update_agent_goal_scope(goal_id: str, update: AgentGoalScopeUpdate):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    expected = f"UPDATE AGENT GOAL {goal.id}"
    if update.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
    if not policy:
        raise HTTPException(409, "Agent policy not found")
    candidate = AgentGoal.model_validate(
        {
            **goal.model_dump(),
            "objective": update.objective,
            "symbols": update.symbols,
            "timeframes": update.timeframes,
            "poll_seconds": update.poll_seconds,
        }
    )
    if not set(candidate.symbols).issubset(policy.allowed_symbols):
        raise HTTPException(409, "Agent goal symbols exceed the policy grant")
    if not set(candidate.timeframes).issubset(policy.allowed_timeframes):
        raise HTTPException(409, "Agent goal timeframes exceed the policy grant")
    await asyncio.to_thread(agent_store.save_goal, candidate)
    add_audit(
        "agent",
        "Operator updated Agent goal scope",
        severity="warning",
        goal_id=goal.id,
        symbols=",".join(candidate.symbols),
        timeframes=",".join(candidate.timeframes),
        poll_seconds=candidate.poll_seconds,
    )
    return candidate


@app.put("/api/agent/goals/{goal_id}/status", response_model=AgentGoal)
async def update_agent_goal_status(goal_id: str, update: AgentGoalStatusUpdate):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    if update.status not in {AgentGoalStatus.ACTIVE, AgentGoalStatus.PAUSED}:
        raise HTTPException(422, "this endpoint only supports active or paused")
    verb = "RESUME" if update.status == AgentGoalStatus.ACTIVE else "PAUSE"
    expected = f"{verb} AGENT {goal.id}"
    if update.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    if update.status == AgentGoalStatus.ACTIVE:
        policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
        if not policy:
            raise HTTPException(409, "Agent policy not found")
        validate_agent_policy_against_platform(policy)
        if goal.valid_until and goal.valid_until <= datetime.now(UTC):
            raise HTTPException(409, "expired Agent goal cannot be resumed")
    goal.status = update.status
    await asyncio.to_thread(agent_store.save_goal, goal)
    add_audit(
        "agent",
        f"Operator changed Agent goal status to {update.status}",
        severity="warning",
        goal_id=goal.id,
        status=str(update.status),
    )
    return goal


@app.post("/api/agent/goals/{goal_id}/trigger", response_model=AgentEvent)
async def trigger_agent_goal(goal_id: str, request: AgentTriggerRequest):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    event = AgentEvent(
        event_type=request.event_type,
        source="operator",
        goal_id=goal.id,
        dedupe_key=request.dedupe_key or f"manual:{goal.id}:{uuid4().hex}",
        payload=request.payload,
    )
    return await asyncio.to_thread(agent_store.append_event, event)


def build_agent_market_state(
    market,
    candles: list[Candle],
    *,
    symbol: str,
    timeframe: str,
    trigger: AgentEvent,
) -> dict:
    """Separate immutable signal bars from the currently forming bar.

    Exchange OHLCV timestamps identify the start of a candle. The Agent
    reasoner may use the live snapshot and a labelled forming candle as
    context, but only completed bars may be presented as signal evidence.
    """

    as_of = market.timestamp
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    completed = closed_candles(candles, timeframe, as_of)
    forming = next(
        (candle for candle in reversed(candles) if candle_close_time(candle, timeframe) > as_of),
        None,
    )
    return {
        "snapshot": market.model_dump(mode="json"),
        "recent_closed_candles": [candle.model_dump(mode="json") for candle in completed[-3:]],
        "closed_candle_count": len(completed),
        "signal_data_end": (completed[-1].timestamp.isoformat() if completed else None),
        "forming_candle": (
            {
                **forming.model_dump(mode="json"),
                "is_closed": False,
                "close_time": candle_close_time(
                    forming,
                    timeframe,
                ).isoformat(),
                "role": "live_context_only_not_signal_evidence",
            }
            if forming
            else None
        ),
        "symbol": symbol,
        "timeframe": timeframe,
        "trigger": trigger.model_dump(mode="json"),
    }


def memory_relevant_to_market(
    memory: AgentMemory,
    *,
    symbol: str,
    timeframe: str,
) -> bool:
    """Match every market dimension declared by a durable memory.

    Episodic memories currently carry symbol, timeframe, direction and
    strategy-family tags. At state-build time direction and family may not yet
    be known, but symbol and timeframe are. Matching either dimension with OR
    leaks an ETH 1h outcome into BTC/SOL 1h decisions, so each declared market
    dimension must match independently. Memories without market-dimensional
    tags remain global (for example operational reconciliation lessons).
    """

    tags = set(memory.tags)
    symbol_tags = {tag for tag in tags if "/" in tag}
    timeframe_tags = {tag for tag in tags if tag in TIMEFRAME_SECONDS}
    return (not symbol_tags or symbol in symbol_tags) and (
        not timeframe_tags or timeframe in timeframe_tags
    )


async def settle_agent_shadow_trials(
    *,
    goal_id: str,
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    as_of: datetime,
) -> list[ShadowValidationTrial]:
    """Advance pending counterfactuals without ever touching execution APIs."""

    trials = await asyncio.to_thread(
        agent_store.list_shadow_trials,
        10000,
        goal_id=goal_id,
        symbol=symbol,
        timeframe=timeframe,
        terminal=False,
    )
    if not trials:
        return []
    configuration = configuration_service.current_configuration()
    funding_rates: list[FundingRatePoint] = []
    if configuration.market_type == "swap":
        earliest = min(trial.decision_at for trial in trials)
        with suppress(Exception):
            funding_rates = await runtime.funding_history(
                symbol,
                earliest,
                as_of,
            )
    updated: list[ShadowValidationTrial] = []
    for trial in trials:
        evaluation_as_of = min(as_of, trial.horizon_at)
        trial.outcome = evaluate_shadow_plan(
            trial.plan,
            candles,
            decision_at=trial.decision_at,
            as_of=evaluation_as_of,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            funding_rates=funding_rates,
            force_close=as_of >= trial.horizon_at,
        )
        await asyncio.to_thread(agent_store.save_shadow_trial, trial)
        updated.append(trial)
    return updated


def unified_research_strategy_library(family: str | None = None) -> dict:
    """Return every public prior without pretending research-only specs are executable."""

    normalized_family = family.strip().lower() if family else None
    single_leg_families = {item.value for item in StrategyKind}
    research_families = {"relative_value", "relative_momentum", "carry"}
    if normalized_family not in {None, *single_leg_families, *research_families}:
        raise ValueError(
            "strategy family must be trend, mean_reversion, breakout, "
            "relative_value, relative_momentum, or carry"
        )
    selected_single_family = (
        StrategyKind(normalized_family) if normalized_family in single_leg_families else None
    )
    single_entries = (
        list_strategy_library(selected_single_family)
        if normalized_family is None or selected_single_family is not None
        else []
    )
    pair_entries = []
    if normalized_family in {None, "relative_value", "relative_momentum"}:
        for spec in PAIR_STRATEGY_LIBRARY:
            pair_family = (
                "relative_momentum"
                if spec.model == "relative_momentum_equal_dollar"
                else "relative_value"
            )
            if normalized_family is not None and normalized_family != pair_family:
                continue
            pair_entries.append(
                {
                    "id": pair_strategy_id(spec),
                    "scope": "pair_research_only",
                    "family": pair_family,
                    "archetype": spec.model,
                    "version": spec.version,
                    "source_urls": [spec.source_url],
                    "hypothesis": spec.hypothesis,
                    "strategy_spec": spec.model_dump(mode="json"),
                    "execution_eligible": False,
                }
            )
    carry_entries = []
    if normalized_family in {None, "carry"}:
        carry_entries = [
            {
                "id": carry_strategy_id(spec),
                "scope": "spot_perpetual_research_only",
                "family": "carry",
                "archetype": spec.model,
                "version": spec.version,
                "source_urls": [spec.source_url],
                "hypothesis": spec.hypothesis,
                "strategy_spec": spec.model_dump(mode="json"),
                "execution_eligible": False,
            }
            for spec in CARRY_STRATEGY_LIBRARY
        ]
    single_payloads = [
        {
            **entry.model_dump(mode="json"),
            "scope": "single_leg_executable_after_certification",
        }
        for entry in single_entries
    ]
    all_entries = [*single_payloads, *pair_entries, *carry_entries]
    family_counts: dict[str, int] = {}
    for entry in all_entries:
        entry_family = str(entry["family"])
        family_counts[entry_family] = family_counts.get(entry_family, 0) + 1
    archetypes = sorted({str(entry["archetype"]) for entry in all_entries})
    return {
        "version": 3,
        "non_empty": bool(all_entries),
        "entry_count": len(all_entries),
        "family_counts": family_counts,
        "archetype_count": len(archetypes),
        "archetypes": archetypes,
        "counts": {
            "single_leg": len(single_payloads),
            "pair_research": len(pair_entries),
            "carry_research": len(carry_entries),
        },
        "entries": all_entries,
        "indicator_catalog": INDICATOR_CATALOG,
        "profitability_claim": False,
    }


def causal_data_eligible_library_entries(
    entries,
    datasets: list[ResearchDataset],
    features_by_dataset: dict[str, tuple[list, list, list, list, list, list]],
) -> tuple[list[str], dict[str, str]]:
    """Preselect hypotheses using causal data availability, never returns."""

    selected: list[str] = []
    excluded: dict[str, str] = {}
    for entry in entries:
        supported_markets = []
        for dataset in datasets:
            (
                funding,
                open_interest,
                positioning,
                basis,
                order_flow,
                liquidation_flow,
            ) = features_by_dataset[dataset.id]
            reasons = historical_feature_eligibility(
                dataset=dataset,
                required_data=entry.required_data,
                funding=funding,
                open_interest=open_interest,
                positioning=positioning,
                basis=basis,
                order_flow=order_flow,
                liquidation_flow=liquidation_flow,
            )
            if not reasons:
                supported_markets.append(dataset)
        unique_exchanges = {item.exchange for item in supported_markets}
        unique_symbols = {item.symbol for item in supported_markets}
        if len(supported_markets) >= 3 and len(unique_exchanges) >= 2 and len(unique_symbols) >= 3:
            selected.append(entry.id)
        else:
            excluded[entry.id] = (
                f"causal feature coverage supports {len(supported_markets)} markets, "
                f"{len(unique_exchanges)} exchanges and {len(unique_symbols)} symbols"
            )
    return selected, excluded


def build_agent_coordinator(goal: AgentGoal, policy: AgentPolicy) -> AgentCoordinator:
    ai_gateway.service = ai_configuration_service
    ai_gateway.proxy_url = configuration_service.current_configuration().network_proxy_url
    registry = build_core_tool_registry(runtime, risk_state_service, agent_store)

    async def build_state(selected_goal: AgentGoal, event: AgentEvent, run: AgentRun) -> AgentState:
        symbol = str(event.payload.get("symbol") or selected_goal.symbols[0])
        timeframe = str(event.payload.get("timeframe") or selected_goal.timeframes[0])
        market, candles = await runtime.market_context(
            limit=120, symbol=symbol, timeframe=timeframe
        )
        await settle_agent_shadow_trials(
            goal_id=selected_goal.id,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            as_of=datetime.now(UTC),
        )
        portfolio = await runtime.portfolio()
        assert runtime.execution is not None
        open_orders = await runtime.execution.open_orders()
        active_episodes = [
            episode
            for episode in await asyncio.to_thread(
                agent_store.list_episodes,
                500,
            )
            if episode.goal_id == selected_goal.id
            and episode.status
            in {
                TradeEpisodeStatus.RESERVED,
                TradeEpisodeStatus.PENDING_ENTRY,
                TradeEpisodeStatus.OPEN,
                TradeEpisodeStatus.UNCERTAIN,
                TradeEpisodeStatus.AWAITING_REVIEW,
            }
        ]
        memories = await asyncio.to_thread(agent_store.recent_memories, 50)
        relevant_memories = [
            memory
            for memory in memories
            if memory_relevant_to_market(
                memory,
                symbol=symbol,
                timeframe=timeframe,
            )
        ][:10]
        return AgentState(
            goal_id=selected_goal.id,
            run_id=run.id,
            phase="observing",
            market=build_agent_market_state(
                market,
                candles,
                symbol=symbol,
                timeframe=timeframe,
                trigger=event,
            ),
            portfolio=portfolio.model_dump(mode="json"),
            risk=risk_state_service.get().model_dump(mode="json"),
            grant=policy.model_dump(mode="json"),
            open_order_ids=[order.id for order in open_orders],
            position_symbols=[position.symbol for position in portfolio.open_positions],
            active_episodes=[
                {
                    "episode_id": episode.id,
                    "plan_id": episode.plan_id,
                    "symbol": episode.symbol,
                    "side": episode.side,
                    "status": str(episode.status),
                    "planned_risk": str(episode.planned_risk),
                }
                for episode in active_episodes
            ],
            memories=[
                {
                    "id": memory.id,
                    "type": str(memory.memory_type),
                    "content": memory.content,
                    "tags": memory.tags,
                    "confidence": str(memory.confidence),
                    "evidence_refs": memory.evidence_refs,
                }
                for memory in relevant_memories
            ],
            memory_refs=[memory.id for memory in relevant_memories],
        )

    def owned_plan(
        plan_id: str,
        *,
        require_current_policy: bool = True,
    ) -> StrategyPlan:
        plan = plans.get(plan_id)
        if not plan:
            raise LookupError("Agent trade plan not found")
        if plan.decision_origin != "agent" or plan.agent_goal_id != goal.id:
            raise PermissionError("trade plan does not belong to this Agent goal")
        if require_current_policy and (
            plan.agent_policy_id != policy.id or plan.agent_policy_fingerprint != policy.fingerprint
        ):
            raise PermissionError("trade plan is not bound to the current Agent policy")
        return plan

    async def episode_execution_evidence(
        plan: StrategyPlan, episode: TradeEpisode
    ) -> tuple[list[Order], list[Fill]]:
        return await collect_agent_episode_evidence(plan, episode)

    async def assess_owned_position(
        plan_id: str,
        symbol: str,
    ) -> dict:
        plan = owned_plan(plan_id, require_current_policy=False)
        if symbol != plan.symbol:
            raise ValueError("position assessment symbol does not match the owned plan")
        episode = next(
            (
                item
                for item in agent_store.list_episodes(500)
                if item.goal_id == goal.id
                and item.plan_id == plan.id
                and item.status == TradeEpisodeStatus.OPEN
            ),
            None,
        )
        if not episode:
            return {
                "recommendation": "wait",
                "reason": "计划当前没有开放交易回合，不执行持仓动作",
                "execution_arguments": {},
            }
        (market, history), portfolio = await asyncio.gather(
            runtime.market_context(
                limit=300,
                symbol=plan.symbol,
                timeframe=plan.timeframe,
            ),
            runtime.portfolio(),
        )
        entry_orders, plan_fills = await episode_execution_evidence(plan, episode)
        entry_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        entered = sum(
            (fill.quantity for fill in plan_fills if fill.side == entry_side),
            Decimal("0"),
        )
        entry_fills = [fill for fill in plan_fills if fill.side == entry_side and fill.quantity > 0]
        entry_time = min(
            (fill.timestamp for fill in entry_fills),
            default=episode.opened_at,
        )
        average_entry = (
            sum((fill.price * fill.quantity for fill in entry_fills), Decimal("0")) / entered
            if entered > 0
            else (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
        )
        exited = sum(
            (fill.quantity for fill in plan_fills if fill.side != entry_side),
            Decimal("0"),
        )
        attributable = max(entered - exited, Decimal("0"))
        position = next(
            (
                item
                for item in portfolio.open_positions
                if item.symbol == plan.symbol and item.side == plan.side
            ),
            None,
        )
        aligned_quantity = position.quantity if position else Decimal("0")
        expected = min(attributable, aligned_quantity)
        protected = bool(
            expected > 0
            and await runtime.execution.verify_position_protection(
                plan,
                entry_orders,
                expected,
            )
        )
        signal = evaluate_current_plan_signal(plan, history, market)
        opposite_signal = bool(signal.actionable and str(signal.direction) != str(plan.side))
        stop_breached = (plan.side == Side.LONG and market.last <= plan.exit.stop_loss) or (
            plan.side == Side.SHORT and market.last >= plan.exit.stop_loss
        )
        trailing = agent_trailing_state(
            plan,
            history,
            market.timestamp,
            market.last,
            entry_time,
            average_entry,
        )
        time_exit = agent_time_exit_state(
            plan,
            history,
            market.timestamp,
            entry_time,
        )
        if (
            attributable <= 0
            or aligned_quantity <= 0
            or attributable != aligned_quantity
            or not protected
        ):
            recommendation = "halt"
            reason = "Agent 归因仓位、交易所净仓位或原生保护覆盖不一致，禁止继续自主增加风险"
            execution_arguments = {"reason": reason}
        elif opposite_signal or stop_breached or trailing["breached"] or time_exit["due"]:
            recommendation = "exit"
            if opposite_signal:
                reason = "最新已收盘策略信号形成明确反向证据"
            elif stop_breached:
                reason = "市场价格已越过计划止损但仓位仍存在，执行防御性退出"
            elif trailing["breached"]:
                reason = "追踪止损已在声明的激活条件满足后触发，退出剩余仓位"
            else:
                reason = "已达到策略声明的最大持仓 K 线数，退出剩余仓位"
            execution_arguments = {
                "plan_id": plan.id,
                "symbol": plan.symbol,
            }
        else:
            recommendation = "hold"
            reason = "仓位归因与原生保护一致，最新已收盘信号未形成反向证据"
            execution_arguments = {}
        return {
            "episode_id": episode.id,
            "plan_id": plan.id,
            "symbol": plan.symbol,
            "recommendation": recommendation,
            "reason": reason,
            "execution_arguments": execution_arguments,
            "position": (position.model_dump(mode="json") if position else None),
            "attributable_quantity": str(attributable),
            "protection_verified": protected,
            "stop_breached": stop_breached,
            "trailing": trailing,
            "time_exit": time_exit,
            "signal": signal.model_dump(mode="json"),
            "market": {
                "last": str(market.last),
                "timestamp": market.timestamp.isoformat(),
                "funding_rate": (
                    str(market.funding_rate) if market.funding_rate is not None else None
                ),
            },
        }

    async def matching_champion(
        strategy_kind: str,
        symbol: str,
        timeframe: str,
    ) -> StrategyCandidate | None:
        candidate_payloads = await asyncio.to_thread(
            store.list_strategy_candidates,
            200,
        )
        for payload in candidate_payloads:
            candidate = parse_strategy_candidate_payload(
                payload,
                audit_context="agent_matching_champion",
            )
            if candidate is None:
                continue
            if (
                candidate.status != CandidateStatus.CHAMPION
                or candidate.spec.get("family") != strategy_kind
                or not candidate.dataset_id
            ):
                continue
            dataset_payload = await asyncio.to_thread(
                store.load_research_dataset,
                candidate.dataset_id,
            )
            if not dataset_payload:
                continue
            dataset = ResearchDataset.model_validate(dataset_payload)
            if dataset.symbol == symbol and dataset.timeframe == timeframe:
                return candidate
        return None

    async def _build_trade_plan(
        prompt: str,
        strategy_kind: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str | None = None,
        adaptive_candidate: AdaptiveStrategyCandidate | None = None,
        *,
        _agent_run_id: str,
    ) -> StrategyPlan:
        if symbol not in goal.symbols or timeframe not in goal.timeframes:
            raise PermissionError("requested market is outside the Agent goal")
        selected_champion = (
            None
            if adaptive_candidate is not None
            else await matching_champion(strategy_kind, symbol, timeframe)
        )
        plan = await generate_strategy(
            GenerateRequest(
                symbol=symbol,
                timeframe=timeframe,
                risk_fraction=(
                    Decimal(risk_fraction)
                    if risk_fraction
                    else policy.max_risk_per_trade
                    if policy.allow_agent_entry
                    else None
                ),
                prompt=prompt,
                strategy_kind=StrategyKind(strategy_kind),
                strategy_spec=(
                    adaptive_candidate.spec
                    if adaptive_candidate is not None
                    else selected_champion.spec
                    if selected_champion
                    else None
                ),
                research_candidate_id=(selected_champion.id if selected_champion else None),
            )
        )
        plan.decision_origin = "agent"
        plan.agent_goal_id = goal.id
        plan.agent_run_id = _agent_run_id
        plan.agent_policy_id = policy.id
        plan.agent_policy_fingerprint = policy.fingerprint
        if selected_champion:
            plan.reasons.append(
                "采用同品种、同周期研究 Champion "
                f"{selected_champion.id} 的不可变 StrategySpec；"
                "该计划仍必须重新通过最新数据回测"
            )
        if adaptive_candidate is not None:
            plan.reasons.extend(
                [
                    f"当前市场自适应规格：{adaptive_candidate.label}；"
                    "selection_basis=current_market_only，backtest_tuned=false",
                    *adaptive_candidate.rationale,
                ]
            )
        store.save_strategy(plan)
        audit_data = {
            "goal_id": goal.id,
            "run_id": _agent_run_id,
            "plan_id": plan.id,
        }
        if selected_champion:
            audit_data["research_champion_id"] = selected_champion.id
        if adaptive_candidate is not None:
            audit_data.update(
                {
                    "adaptive_label": adaptive_candidate.label,
                    "adaptive_spec_fingerprint": (adaptive_candidate.spec_fingerprint),
                }
            )
        add_audit(
            "agent",
            "Agent 已生成绑定权限指纹的确定性交易计划",
            **audit_data,
        )
        return plan

    async def build_trade_plan(
        prompt: str,
        strategy_kind: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str | None = None,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        del _agent_tool_call_id
        plan = await _build_trade_plan(
            prompt=prompt,
            strategy_kind=strategy_kind,
            symbol=symbol,
            timeframe=timeframe,
            risk_fraction=risk_fraction,
            _agent_run_id=_agent_run_id,
        )
        return plan.model_dump(mode="json")

    async def run_plan_backtest(plan_id: str) -> dict:
        owned_plan(plan_id)
        result = await backtest_strategy(plan_id)
        return result.model_dump(mode="json")

    async def screen_trade_candidates(
        symbol: str,
        timeframe: str,
        prompt: str = "",
        risk_fraction: str | None = None,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        del _agent_tool_call_id
        if symbol not in goal.symbols or timeframe not in goal.timeframes:
            raise PermissionError("requested market is outside the Agent goal")
        analysis_tool = registry.get("analyze_market_regime")
        analysis = await analysis_tool.handler(symbol=symbol, timeframe=timeframe)
        recommendation = analysis["recommendation"]
        actionable, candidate_pool = stratified_actionable_candidates(
            analysis["actionable_candidates"],
            max_total=9,
            max_per_family=4,
        )
        execution_context_eligible = recommendation["action"] == "consider_plan"
        data_quality_acceptable = bool(recommendation.get("data_quality_acceptable"))
        if not actionable or not data_quality_acceptable:
            wait_category = (
                "market_data_unavailable" if not data_quality_acceptable else "no_actionable_signal"
            )
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "selection_status": "wait",
                "wait_category": wait_category,
                "blocking_layer": (
                    "market_data" if wait_category == "market_data_unavailable" else "signal"
                ),
                "selected_plan_id": None,
                "reason": recommendation["reason"],
                "market_evidence": {
                    "assessment": analysis["assessment"],
                    "confirmation": analysis["confirmation"],
                    "recommendation": recommendation,
                    "actionable_candidates": actionable,
                    "candidate_pool": candidate_pool,
                    "execution_context": {
                        "eligible": False,
                        "role": "execution_gate_not_research_gate",
                        "blocking_reason": recommendation["reason"],
                    },
                },
                "candidates": [],
            }

        screened: list[dict] = []
        failures: list[dict] = []
        base_prompt = prompt.strip() or (
            "依据当前已收盘行情与多周期确定性证据生成计划；不追价，证据退化时保持等待。"
        )
        assessment = MarketStateAssessment.model_validate(analysis["assessment"])
        market_evidence = MarketEvidenceSnapshot.model_validate(analysis["market_evidence"])
        current_available_data = {"ohlcv"}
        if market_evidence.funding_rate is not None:
            current_available_data.add("funding_rate")
        if (
            market_evidence.open_interest is not None
            or market_evidence.open_interest_value is not None
        ):
            current_available_data.add("open_interest")
        if market_evidence.long_short_ratio is not None:
            current_available_data.add("positioning")
        if market_evidence.basis_rate is not None:
            current_available_data.add("basis")
        requested_risk_fraction = (
            Decimal(risk_fraction) if risk_fraction is not None else policy.max_risk_per_trade
        )
        if requested_risk_fraction <= 0:
            raise ValueError("candidate risk fraction must be positive")
        policy_capped_risk_fraction = min(
            requested_risk_fraction,
            policy.max_risk_per_trade,
        )
        execution_risk_multiplier = Decimal(
            str(recommendation.get("execution_risk_multiplier") or "1")
        )
        # A hard execution conflict may still be researched, but cannot be
        # executed. A soft single-source headwind produces a smaller plan
        # rather than an arbitrary rejection.
        planning_risk_multiplier = (
            execution_risk_multiplier
            if execution_context_eligible and execution_risk_multiplier > 0
            else Decimal("1")
        )
        effective_risk_fraction = policy_capped_risk_fraction * planning_risk_multiplier
        risk_adjustment = {
            "requested_risk_fraction": requested_risk_fraction,
            "policy_max_risk_fraction": policy.max_risk_per_trade,
            "policy_cap_applied": (requested_risk_fraction > policy.max_risk_per_trade),
            "execution_context_class": recommendation.get(
                "execution_context_class",
                "aligned_or_neutral",
            ),
            "execution_risk_multiplier": execution_risk_multiplier,
            "effective_risk_fraction": effective_risk_fraction,
            "reason": recommendation.get("execution_context_reason", ""),
        }
        proposal_inputs: list[tuple[dict, AdaptiveStrategyCandidate | None, dict]] = []
        seen_specs: set[tuple[str, str]] = set()
        champion_families: set[str] = set()
        for signal in sorted(
            actionable,
            key=lambda item: Decimal(str(item.get("confidence") or "0")),
            reverse=True,
        ):
            strategy_kind = StrategyKind(str(signal["strategy_kind"]))
            if strategy_kind.value not in champion_families:
                champion = await matching_champion(
                    strategy_kind.value,
                    symbol,
                    timeframe,
                )
                champion_families.add(strategy_kind.value)
            else:
                champion = None
            if (
                champion is not None
                and (
                    strategy_kind.value,
                    champion.spec_fingerprint,
                )
                not in seen_specs
            ):
                proposal_inputs.append(
                    (
                        signal,
                        None,
                        {
                            "label": "champion",
                            "selection_basis": "validated_research_champion",
                            "backtest_tuned": False,
                            "rationale": [
                                f"使用已编译 Champion {champion.id} 作为基准",
                                "仍须使用最新行情重新通过完整回测",
                            ],
                            "spec_fingerprint": champion.spec_fingerprint,
                        },
                    )
                )
                seen_specs.add((strategy_kind.value, champion.spec_fingerprint))
            ranked_library_templates = rank_strategy_library(
                list_strategy_library(strategy_kind),
                assessment,
                current_available_data,
            )
            for library_template, regime_fit in ranked_library_templates[:4]:
                library_spec = library_template.validated_spec
                library_identity = (
                    strategy_kind.value,
                    library_spec.fingerprint,
                )
                if library_identity in seen_specs:
                    continue
                library_candidate = AdaptiveStrategyCandidate(
                    label="published_library",
                    selection_basis="public_strategy_prior",
                    backtest_tuned=False,
                    rationale=[
                        f"公开策略库模板：{library_template.name}",
                        f"来源：{library_template.origin}",
                        f"参考：{library_template.source_url}",
                        "公开规则仅作为先验假设，未读取本次回测结果",
                    ],
                    spec=library_template.spec,
                    spec_fingerprint=library_spec.fingerprint,
                )
                proposal_inputs.append(
                    (
                        signal,
                        library_candidate,
                        {
                            **library_candidate.model_dump(
                                mode="json",
                                exclude={"spec"},
                            ),
                            "library_entry_id": library_template.id,
                            "library_name": library_template.name,
                            "expected_profile": (library_template.expected_profile),
                            "regime_fit": regime_fit,
                        },
                    )
                )
                seen_specs.add(library_identity)
            raw_spec = signal.get("strategy_spec")
            if raw_spec:
                adaptive = [
                    AdaptiveStrategyCandidate(
                        label=str(signal.get("adaptive_label") or "adaptive"),
                        selection_basis=str(signal.get("selection_basis") or "current_market_only"),
                        backtest_tuned=bool(signal.get("backtest_tuned", False)),
                        rationale=[str(item) for item in signal.get("rationale", [])],
                        spec=raw_spec,
                        spec_fingerprint=str(signal["spec_fingerprint"]),
                    )
                ]
            else:
                adaptive = build_adaptive_strategy_candidates(
                    strategy_kind,
                    assessment,
                    market_evidence,
                )
            for candidate in adaptive:
                identity = (
                    strategy_kind.value,
                    candidate.spec_fingerprint,
                )
                if identity in seen_specs:
                    continue
                seen_specs.add(identity)
                proposal_inputs.append(
                    (
                        signal,
                        candidate,
                        candidate.model_dump(
                            mode="json",
                            exclude={"spec"},
                        ),
                    )
                )
        family_order = list(
            dict.fromkeys(str(item[0]["strategy_kind"]) for item in proposal_inputs)
        )
        prioritized_inputs: list[tuple[dict, AdaptiveStrategyCandidate | None, dict]] = []
        selected_input_ids: set[int] = set()
        for preferred_label in (
            "champion",
            "published_library",
            "baseline",
            "cost_resilient",
        ):
            for family in family_order:
                match = next(
                    (
                        item
                        for item in proposal_inputs
                        if id(item) not in selected_input_ids
                        and str(item[0]["strategy_kind"]) == family
                        and str(item[2].get("label")) == preferred_label
                    ),
                    None,
                )
                if match is None:
                    continue
                prioritized_inputs.append(match)
                selected_input_ids.add(id(match))
        prioritized_inputs.extend(
            item for item in proposal_inputs if id(item) not in selected_input_ids
        )
        proposal_inputs = prioritized_inputs[:9]
        historical_episodes = await asyncio.to_thread(
            agent_store.list_episodes,
            10000,
        )

        async def evaluate_proposal(
            signal: dict,
            adaptive_candidate: AdaptiveStrategyCandidate | None,
            adaptation: dict,
        ) -> tuple[str, dict]:
            strategy_kind = str(signal["strategy_kind"])
            plan: StrategyPlan | None = None
            try:
                plan = await _build_trade_plan(
                    prompt=base_prompt,
                    strategy_kind=strategy_kind,
                    symbol=symbol,
                    timeframe=timeframe,
                    risk_fraction=str(effective_risk_fraction),
                    adaptive_candidate=adaptive_candidate,
                    _agent_run_id=_agent_run_id,
                )
                current_configuration = configuration_service.current_configuration()
                portfolio_admission = admit_strategy_risk(
                    family=plan.algorithm,
                    symbol=plan.symbol,
                    side=plan.side,
                    strategy_fingerprint=plan.strategy_spec_fingerprint,
                    requested_risk_fraction=plan.sizing.risk_fraction,
                    active_plans=list(plans.values()),
                    maximum_portfolio_risk=(current_configuration.risk.max_portfolio_risk),
                )
                if (
                    portfolio_admission.allowed
                    and portfolio_admission.admitted_risk_fraction < plan.sizing.risk_fraction
                ):
                    plan = rescale_plan_risk(
                        plan,
                        portfolio_admission.admitted_risk_fraction,
                    )
                    plans[plan.id] = plan
                    store.save_strategy(plan)
                result = await backtest_strategy(
                    plan.id,
                    research_trial_count=len(proposal_inputs),
                )
                validation = configuration_service.current_configuration().strategy.validation
                probe_evidence = sandbox_probe_evidence(result, validation)
                if (
                    policy.autonomy_mode == AgentAutonomyMode.SANDBOX
                    and not result.robustness_passed
                    and probe_evidence["eligible"]
                ):
                    probe_cap = sandbox_probe_risk_cap(policy)
                    if plan.sizing.risk_fraction > probe_cap:
                        plan = rescale_plan_risk(plan, probe_cap)
                        plans[plan.id] = plan
                        store.save_strategy(plan)
                        result = await backtest_strategy(
                            plan.id,
                            research_trial_count=len(proposal_inputs),
                        )
                        probe_evidence = sandbox_probe_evidence(
                            result,
                            validation,
                        )
                signal_confidence = Decimal(str(signal.get("confidence") or "0"))
                regime_scores = analysis["assessment"].get("scores", {})
                regime_score = Decimal(str(regime_scores.get(strategy_kind) or "0"))
                forward_evidence = build_plan_forward_outcome_evidence(
                    plan,
                    historical_episodes,
                    plans,
                )
                return (
                    "screened",
                    {
                        "plan": plan,
                        "signal": signal,
                        "backtest": result,
                        "adaptation": adaptation,
                        "signal_confidence": signal_confidence,
                        "regime_score": regime_score,
                        "selection_score": candidate_selection_score(
                            result,
                            signal_confidence=signal_confidence,
                            regime_score=regime_score,
                            forward_expectancy_r=forward_evidence["expectancy_r"],
                            forward_sample=forward_evidence["sample"],
                        ),
                        "forward_outcome_evidence": forward_evidence,
                        "sandbox_probe_evidence": probe_evidence,
                        "portfolio_admission": portfolio_admission,
                    },
                )
            except Exception as exc:
                archived_plan_ids = (
                    archive_unselected_agent_candidate_plans([plan], None)
                    if plan is not None
                    else []
                )
                return (
                    "failure",
                    {
                        "plan_id": plan.id if plan is not None else None,
                        "plan_archived": bool(archived_plan_ids),
                        "strategy_kind": strategy_kind,
                        "adaptive_label": adaptation.get("label", "unknown"),
                        "spec_fingerprint": adaptation.get(
                            "spec_fingerprint",
                            "",
                        ),
                        "signal": signal,
                        "error": str(exc)[:500],
                    },
                )

        evaluated = await asyncio.gather(
            *(
                evaluate_proposal(signal, adaptive_candidate, adaptation)
                for signal, adaptive_candidate, adaptation in proposal_inputs
            )
        )
        for kind, payload in evaluated:
            if kind == "screened":
                screened.append(payload)
            else:
                failures.append(payload)

        parameter_reports: dict[str, dict] = {}
        parameter_groups: dict[str, list[dict]] = {}
        for item in screened:
            plan = item["plan"]
            group_key = f"{plan.algorithm}:{plan.side}"
            parameter_groups.setdefault(group_key, []).append(item)
        for group_key, items in parameter_groups.items():
            fold_evidence = [
                ParameterFoldEvidence(
                    label=str(item["adaptation"].get("label") or "unknown"),
                    spec_fingerprint=str(
                        item["adaptation"].get("spec_fingerprint")
                        or item["plan"].strategy_spec_fingerprint
                        or ""
                    ),
                    windows=item["backtest"].temporal_stability.windows,
                )
                for item in items
                if item["backtest"].temporal_stability is not None
            ]
            report = build_rolling_parameter_report(fold_evidence)
            serialized_report = report.model_dump(mode="json")
            parameter_reports[group_key] = serialized_report
            for item in items:
                item["parameter_walk_forward"] = serialized_report
                item["backtest"] = item["backtest"].model_copy(
                    update={
                        "parameter_walk_forward": serialized_report,
                    }
                )
                store.save_backtest(
                    item["plan"].id,
                    item["backtest"],
                )

        screened.sort(
            key=lambda item: evidence_rank_key(
                item["backtest"],
                signal_confidence=item["signal_confidence"],
                regime_score=item["regime_score"],
                forward_expectancy_r=item["forward_outcome_evidence"]["expectancy_r"],
                forward_sample=item["forward_outcome_evidence"]["sample"],
            ),
            reverse=True,
        )
        campaign_payloads = (
            await asyncio.to_thread(store.list_research_campaigns, 100)
            if policy.autonomy_mode in {AgentAutonomyMode.SANDBOX, AgentAutonomyMode.LIVE_CAPPED}
            else []
        )
        current_policy_actionable_evidence = [
            {
                "library_entry_id": str(item["adaptation"]["library_entry_id"]),
                "strategy_fingerprint": item["plan"].strategy_spec_fingerprint,
                "side": str(item["plan"].side),
                "confidence": str(item["plan"].confidence),
                "signal_candle_at": item["plan"].signal_candle_at.isoformat(),
                "data_as_of": item["plan"].data_as_of.isoformat(),
            }
            for item in screened
            if item["adaptation"].get("library_entry_id")
            and item["plan"].signal_candle_at is not None
            and item["plan"].data_as_of is not None
        ]
        candidates: list[dict] = []
        for rank, item in enumerate(screened, start=1):
            plan = item["plan"]
            result = item["backtest"]
            parameter_report = item.get("parameter_walk_forward") or {}
            robust_eligible = bool(
                result.robustness_passed
                and result.win_loss_quality_passed
                and parameter_report.get("passed") is True
            )
            probe_evidence = item["sandbox_probe_evidence"]
            sandbox_probe_eligible = bool(
                policy.autonomy_mode == AgentAutonomyMode.SANDBOX
                and not result.robustness_passed
                and probe_evidence["eligible"]
                and parameter_report.get("passed") is True
                and plan.sizing.risk_fraction <= sandbox_probe_risk_cap(policy)
            )
            backtest_eligible = robust_eligible or sandbox_probe_eligible
            evidence_tier = (
                "robust"
                if robust_eligible
                else "sandbox_probe"
                if sandbox_probe_eligible
                else "insufficient"
            )
            selected_library_entry_id = item["adaptation"].get("library_entry_id")
            matching_campaign = (
                matching_execution_campaign(
                    plan,
                    campaign_payloads,
                    require_plan_binding=False,
                    causal_policy_assessment=(assessment if selected_library_entry_id else None),
                    causal_policy_available_data=(
                        current_available_data if selected_library_entry_id else None
                    ),
                    causal_policy_selected_library_entry_id=(
                        str(selected_library_entry_id) if selected_library_entry_id else None
                    ),
                    causal_policy_actionable_evidence=(
                        current_policy_actionable_evidence if selected_library_entry_id else None
                    ),
                )
                if policy.autonomy_mode
                in {AgentAutonomyMode.SANDBOX, AgentAutonomyMode.LIVE_CAPPED}
                else None
            )
            if matching_campaign is not None:
                certificate = matching_campaign.execution_certificate
                assert certificate is not None
                if selected_causal_policy_definition(matching_campaign) is not None:
                    assert selected_library_entry_id
                    selection_evidence = build_plan_causal_policy_selection_evidence(
                        plan,
                        matching_campaign,
                        assessment=assessment,
                        available_data=current_available_data,
                        selected_library_entry_id=str(selected_library_entry_id),
                        actionable_library_evidence=(current_policy_actionable_evidence),
                    )
                    plan.causal_policy_fingerprint = certificate.strategy_fingerprint
                    plan.causal_policy_selected_library_entry_id = str(selected_library_entry_id)
                    plan.causal_policy_selection_evidence = selection_evidence
                plan.research_campaign_id = matching_campaign.id
                plan.campaign_certificate_fingerprint = certificate.fingerprint
                store.save_strategy(plan)
                evidence_tier = "campaign_certified"
            campaign_certified = bool(
                policy.autonomy_mode == AgentAutonomyMode.PAPER or matching_campaign is not None
            )
            strategy_admitted = (
                backtest_eligible
                if policy.autonomy_mode == AgentAutonomyMode.PAPER
                else campaign_certified
            )
            execution_eligible = bool(
                strategy_admitted and execution_context_eligible and policy.allow_agent_entry
            )
            execution_eligible = bool(execution_eligible and item["portfolio_admission"].allowed)
            stress_return = (
                result.stress_return_percent if result.stress_return_percent is not None else "N/A"
            )
            candidates.append(
                {
                    "rank": rank,
                    "plan_id": plan.id,
                    "strategy_kind": plan.algorithm,
                    "side": str(plan.side),
                    "confidence": plan.confidence,
                    "backtest_eligible": backtest_eligible,
                    "evidence_tier": evidence_tier,
                    "sandbox_probe_eligible": sandbox_probe_eligible,
                    "sandbox_probe_evidence": probe_evidence,
                    "execution_eligible": execution_eligible,
                    "execution_context_eligible": (execution_context_eligible),
                    "campaign_certified": campaign_certified,
                    "strategy_admitted": strategy_admitted,
                    "risk_adjustment": risk_adjustment,
                    "portfolio_admission": item["portfolio_admission"].model_dump(mode="json"),
                    "execution_blocking_reasons": (
                        ([] if execution_context_eligible else [recommendation["reason"]])
                        + item["portfolio_admission"].hard_conflicts
                    ),
                    "authorization_arguments": {
                        "plan_id": plan.id,
                        "symbol": plan.symbol,
                        "timeframe": plan.timeframe,
                        "risk_fraction": plan.sizing.risk_fraction,
                        "leverage": plan.leverage,
                        "protected": True,
                    },
                    "screening_summary": (
                        f"排名 {rank}/{len(screened)}；"
                        f"稳健性={'通过' if result.robustness_passed else '未通过'}，"
                        f"证据层级={evidence_tier}，"
                        f"PF {result.profit_factor:f}，压力收益 {stress_return}%，"
                        f"综合排序 {item['selection_score']['total']}/100；"
                        f"执行上下文="
                        f"{'允许' if execution_context_eligible else '等待'}"
                    ),
                    "adaptation": item["adaptation"],
                    "strategy_spec_fingerprint": (plan.strategy_spec_fingerprint),
                    "strategy_parameters": (
                        plan.strategy_spec.get("parameters", {}) if plan.strategy_spec else {}
                    ),
                    "parameter_walk_forward": item.get("parameter_walk_forward"),
                    "selection_score": item["selection_score"],
                    "forward_outcome_evidence": item["forward_outcome_evidence"],
                    "entry": plan.entry.model_dump(mode="json"),
                    "sizing": plan.sizing.model_dump(mode="json"),
                    "signal": item["signal"],
                    "backtest": evidence_summary(result),
                }
            )
        selected = next(
            (item for item in candidates if item["execution_eligible"]),
            None,
        )
        best_research_candidate = next(
            (item for item in candidates if item["backtest_eligible"]),
            None,
        )
        decision_at = datetime.now(UTC)
        decision_batch_id = f"{_agent_run_id}:{symbol}:{timeframe}:candidate-screen"
        shadow_trials: list[ShadowValidationTrial] = []
        for item in screened:
            plan = item["plan"]
            candidate = next(
                candidate for candidate in candidates if candidate["plan_id"] == plan.id
            )
            trial = ShadowValidationTrial(
                id=f"shadow_{plan.id}",
                goal_id=goal.id,
                run_id=_agent_run_id,
                decision_batch_id=decision_batch_id,
                plan=plan.model_copy(deep=True),
                decision_at=decision_at,
                horizon_at=decision_at + timeframe_delta(timeframe, 48),
                selected_for_execution=bool(selected and selected["plan_id"] == plan.id),
                execution_eligible=bool(candidate["execution_eligible"]),
                screening_rank=int(candidate["rank"]),
                screening_summary=str(candidate["screening_summary"]),
                selection_score=dict(candidate["selection_score"]),
                adaptation_evidence=dict(candidate["adaptation"]),
                signal_evidence=dict(candidate["signal"]),
                backtest_evidence=dict(candidate["backtest"]),
                outcome=ShadowPlanOutcome(
                    status=ShadowOutcomeStatus.PENDING_ENTRY,
                    plan_id=plan.id,
                    symbol=plan.symbol,
                    timeframe=plan.timeframe,
                    side=plan.side,
                    decision_at=decision_at,
                    original_quantity=plan.sizing.suggested_quantity,
                ),
            )
            await asyncio.to_thread(agent_store.save_shadow_trial, trial)
            shadow_trials.append(trial)
        archived_candidate_plan_ids = archive_unselected_agent_candidate_plans(
            [item["plan"] for item in screened],
            selected["plan_id"] if selected else None,
        )
        selection_status = (
            (
                "sandbox_probe_candidate"
                if selected["evidence_tier"] == "sandbox_probe"
                else "robust_candidate"
            )
            if selected
            else "context_wait"
            if best_research_candidate and not execution_context_eligible
            else "evidence_unavailable"
            if not candidates and failures
            else "wait"
        )
        wait_category = (
            None
            if selected
            else "execution_context_wait"
            if selection_status == "context_wait"
            else "external_evidence_unavailable"
            if selection_status == "evidence_unavailable"
            else "backtest_evidence_insufficient"
        )
        blocking_layer = (
            None
            if selected
            else "execution_context"
            if selection_status == "context_wait"
            else "external"
            if selection_status == "evidence_unavailable"
            else "evidence"
        )
        reason = (
            "同一市场时点的多策略与受约束参数候选已完成公平筛选；"
            + (
                "排名首位候选通过测试网低风险前向证据层，仅以缩小后的风险收集真实成交样本"
                if selected and selected["evidence_tier"] == "sandbox_probe"
                else "排名首位且通过全部稳健性门槛"
            )
            if selected
            else (
                "候选已完成回测研究且存在稳健证据，但当前执行上下文不允许入场："
                f"{recommendation['reason']}"
                if selection_status == "context_wait"
                else (
                    "交易所或账户证据不可用，候选计划未能完成生成和回测，保持等待"
                    if selection_status == "evidence_unavailable"
                    else (
                        "当前存在方向信号，但所有受约束参数候选均未通过完整样本、"
                        "时间稳定性、近期窗口与成本压力证据，保持等待"
                    )
                )
            )
        )
        add_audit(
            "agent",
            "Agent 已完成同市场多策略候选证据筛选",
            goal_id=goal.id,
            run_id=_agent_run_id,
            symbol=symbol,
            timeframe=timeframe,
            screened=len(candidates),
            parameter_hypotheses=len(proposal_inputs),
            actionable_input_count=candidate_pool["input_count"],
            actionable_selected_count=candidate_pool["selected_count"],
            actionable_selected_family_counts=json.dumps(
                candidate_pool["selected_family_counts"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            archived_candidate_plans=len(archived_candidate_plan_ids),
            selected_plan_id=selected["plan_id"] if selected else "",
            failed_families=len(failures),
            shadow_trials=len(shadow_trials),
            shadow_batch_id=decision_batch_id,
            execution_context_eligible=execution_context_eligible,
            best_research_plan_id=(
                best_research_candidate["plan_id"] if best_research_candidate else ""
            ),
        )
        campaign_scope = await latest_sealed_campaign_scope()
        suggested_library_entry_ids = list(
            dict.fromkeys(
                str(item["adaptation"].get("library_entry_id"))
                for item in candidates
                if item["adaptation"].get("library_entry_id")
            )
        )[:12]
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "selection_status": selection_status,
            "wait_category": wait_category,
            "blocking_layer": blocking_layer,
            "selected_plan_id": selected["plan_id"] if selected else None,
            "best_research_plan_id": (
                best_research_candidate["plan_id"] if best_research_candidate else None
            ),
            "reason": reason,
            "market_evidence": {
                "assessment": analysis["assessment"],
                "confirmation": analysis["confirmation"],
                "recommendation": recommendation,
                "actionable_candidates": actionable,
                "candidate_pool": candidate_pool,
                "execution_context": {
                    "eligible": execution_context_eligible,
                    "role": "execution_gate_not_research_gate",
                    "risk_adjustment": risk_adjustment,
                    "blocking_reason": (
                        None if execution_context_eligible else recommendation["reason"]
                    ),
                },
            },
            "candidates": candidates,
            "parameter_walk_forward": parameter_reports,
            "generation_failures": failures,
            "archived_candidate_plan_ids": archived_candidate_plan_ids,
            "shadow_validation": {
                "batch_id": decision_batch_id,
                "trial_count": len(shadow_trials),
                "horizon_bars": 48,
                "causal": True,
                "executable": False,
            },
            "campaign_scope": campaign_scope,
            "suggested_library_entry_ids": suggested_library_entry_ids,
            "next_action": (
                "call execute_validated_trade_plan with selected_authorization exactly"
                if selected and policy.allow_agent_entry
                else "wait"
            ),
            "selected_authorization": (selected["authorization_arguments"] if selected else None),
        }

    async def authorize_trade_plan(
        plan_id: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str,
        leverage: str,
        protected: bool,
    ) -> dict:
        plan = owned_plan(plan_id)
        validate_plan_research_provenance(plan)
        if not policy.allow_agent_entry:
            raise PermissionError("Agent entry permission is disabled")
        if not protected or not policy.require_native_protection:
            raise PermissionError("Agent plans require native exchange protection")
        if symbol != plan.symbol or timeframe != plan.timeframe:
            raise ValueError("authorization arguments do not match the trade plan")
        if Decimal(risk_fraction) != plan.sizing.risk_fraction:
            raise ValueError("authorization risk does not match the trade plan")
        if Decimal(leverage) != plan.leverage:
            raise ValueError("authorization leverage does not match the trade plan")
        artifact_data = store.load_latest_backtest(plan.id)
        if not artifact_data:
            raise RuntimeError("Agent plan has no bound backtest artifact")
        artifact = BacktestResult.model_validate(artifact_data)
        if artifact.strategy_fingerprint != plan.fingerprint:
            raise RuntimeError("Agent plan changed after its latest backtest")
        if artifact.cost_model.get("direction_filter") != str(plan.side):
            raise RuntimeError("Agent plan backtest is not bound to the plan trade direction")
        if artifact.integrity_checks.get("immutable_dataset_binding") is not True:
            raise RuntimeError(
                "Agent plan backtest predates immutable integrity admission; rerun it"
            )
        matching_campaign = None
        if policy.autonomy_mode in {
            AgentAutonomyMode.SANDBOX,
            AgentAutonomyMode.LIVE_CAPPED,
        }:
            campaign_payloads = await asyncio.to_thread(
                store.list_research_campaigns,
                50,
            )
            matching_campaign = matching_execution_campaign(
                plan,
                campaign_payloads,
            )
            if matching_campaign is None:
                raise RuntimeError(
                    "exchange Agent execution requires an exact-spec immutable Campaign "
                    "certificate that passes cumulative multiple-testing correction, "
                    "cross-market validation, PBO and the single-use blind holdout"
                )
        else:
            if not artifact.robustness_passed or not artifact.win_loss_quality_passed:
                raise RuntimeError(
                    "paper Agent plan lacks robust combined win-rate and payoff evidence"
                )
            if not (
                artifact.parameter_walk_forward
                and artifact.parameter_walk_forward.get("passed") is True
            ):
                raise RuntimeError(
                    "paper Agent execution requires a passed prior-fold "
                    "parameter walk-forward report"
                )
        current_configuration = configuration_service.current_configuration()
        portfolio_admission = admit_strategy_risk(
            family=plan.algorithm,
            symbol=plan.symbol,
            side=plan.side,
            strategy_fingerprint=plan.strategy_spec_fingerprint,
            requested_risk_fraction=plan.sizing.risk_fraction,
            active_plans=list(plans.values()),
            maximum_portfolio_risk=(current_configuration.risk.max_portfolio_risk),
        )
        if (
            not portfolio_admission.allowed
            or portfolio_admission.admitted_risk_fraction < plan.sizing.risk_fraction
        ):
            raise RuntimeError(
                "strategy portfolio changed after screening; rerun candidate "
                f"selection: {portfolio_admission.model_dump(mode='json')}"
            )
        plan.approved_at = datetime.now(UTC)
        plan.approved_fingerprint = plan.fingerprint
        plan.status = PlanStatus.APPROVED
        store.save_strategy(plan)
        add_audit(
            "agent",
            "Agent 计划通过不可变策略证书与当前执行上下文授权",
            goal_id=goal.id,
            plan_id=plan.id,
            policy_fingerprint=policy.fingerprint,
            evidence_tier=(
                "campaign_certified" if matching_campaign is not None else "paper_robust"
            ),
            research_campaign_id=(matching_campaign.id if matching_campaign is not None else ""),
        )
        return plan.model_dump(mode="json")

    async def activate_trade_plan(
        plan_id: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str,
        leverage: str,
        protected: bool,
    ) -> dict:
        plan = owned_plan(plan_id)
        if (
            symbol != plan.symbol
            or timeframe != plan.timeframe
            or Decimal(risk_fraction) != plan.sizing.risk_fraction
            or Decimal(leverage) != plan.leverage
            or not protected
        ):
            raise ValueError("activation arguments do not match the authorized plan")
        activated = await activate_strategy(
            plan.id, ActivateRequest(confirmation=f"ACTIVATE {plan.symbol}")
        )
        return activated.model_dump(mode="json")

    async def submit_trade_plan(
        plan_id: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str,
        leverage: str,
        protected: bool,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        del _agent_tool_call_id
        plan = owned_plan(plan_id)
        if (
            symbol != plan.symbol
            or timeframe != plan.timeframe
            or Decimal(risk_fraction) != plan.sizing.risk_fraction
            or Decimal(leverage) != plan.leverage
            or not protected
        ):
            raise ValueError("execution arguments do not match the authorized plan")
        require_agent_position_management_commissioning()
        portfolio = await runtime.portfolio()
        if portfolio.daily_drawdown_fraction >= policy.max_daily_drawdown:
            state = risk_state_service.halt(
                "Agent policy daily drawdown budget reached",
                source="agent_daily_drawdown",
            )
            raise PermissionError(state.reason)
        open_symbols = {position.symbol for position in portfolio.open_positions}
        if plan.symbol not in open_symbols and len(open_symbols) >= policy.max_open_positions:
            raise PermissionError("Agent account-level open-position cap is reached")
        episode = TradeEpisode(
            goal_id=goal.id,
            policy_id=policy.id,
            run_id=_agent_run_id,
            plan_id=plan.id,
            symbol=plan.symbol,
            side=str(plan.side),
            status=TradeEpisodeStatus.RESERVED,
            planned_risk=plan.sizing.risk_amount,
            opened_at=datetime.now(UTC),
        )
        episode = await asyncio.to_thread(
            agent_store.reserve_episode,
            episode,
            max_trades_per_day=policy.max_trades_per_day,
            max_open_positions=policy.max_open_positions,
        )
        try:
            result = await execute_strategy(plan.id)
        except Exception as exc:
            settle_agent_entry_exception(plan, episode, exc)
            raise
        if result.order:
            plan.status = PlanStatus.PAUSED
            store.save_strategy(plan)
            episode.status = (
                TradeEpisodeStatus.OPEN
                if any(order.status == OrderStatus.FILLED for order in result.orders)
                else TradeEpisodeStatus.PENDING_ENTRY
            )
            episode.entry_order_ids = [order.id for order in result.orders]
            episode.opened_at = min(order.created_at for order in result.orders)
            agent_store.save_episode(episode)
        else:
            failed_checks = [
                check for check in result.decision.checks if check.blocking and not check.passed
            ]
            plan.status = PlanStatus.ARCHIVED
            store.save_strategy(plan)
            episode.status = TradeEpisodeStatus.CANCELED
            episode.summary = (
                "entry rejected by hard risk checks: "
                + "; ".join(f"{check.code}: {check.message}" for check in failed_checks)
                if failed_checks
                else "entry produced no order; the stale plan was archived"
            )
            agent_store.save_episode(episode)
        return result.model_dump(mode="json")

    async def cancel_entry_order(order_id: str, symbol: str) -> dict:
        if symbol not in goal.symbols:
            raise PermissionError("order symbol is outside the Agent goal")
        order = await cancel_order(order_id)
        return order.model_dump(mode="json")

    async def decrease_agent_position(
        plan_id: str,
        symbol: str,
        fraction: str,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        del _agent_run_id
        plan = owned_plan(plan_id, require_current_policy=False)
        if symbol != plan.symbol:
            raise ValueError("position symbol does not match the owned plan")
        requested_fraction = Decimal(fraction)
        if requested_fraction <= 0 or requested_fraction > 1:
            raise ValueError("position reduction fraction must be in (0, 1]")
        await runtime.initialize()
        assert runtime.execution is not None
        episode = next(
            (
                item
                for item in agent_store.list_episodes(200)
                if item.plan_id == plan.id and item.goal_id == goal.id
            ),
            None,
        )
        if not episode or episode.status != TradeEpisodeStatus.OPEN:
            raise RuntimeError("owned Agent position is not in an open episode")
        require_agent_position_management_commissioning()
        entry_orders, plan_fills = await episode_execution_evidence(plan, episode)
        entry_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        entry_quantity = sum(
            (fill.quantity for fill in plan_fills if fill.side == entry_side),
            Decimal("0"),
        )
        exit_quantity = sum(
            (fill.quantity for fill in plan_fills if fill.side != entry_side),
            Decimal("0"),
        )
        attributable = max(entry_quantity - exit_quantity, Decimal("0"))
        portfolio = await runtime.portfolio()
        aligned_quantity = sum(
            (
                position.quantity
                for position in portfolio.open_positions
                if position.symbol == plan.symbol and position.side == plan.side
            ),
            Decimal("0"),
        )
        if aligned_quantity <= 0 or attributable <= 0:
            raise RuntimeError("no aligned Agent-attributable position remains")
        attributable = min(attributable, aligned_quantity)
        quantity = attributable * requested_fraction
        quantity = quantity.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            raise RuntimeError("position reduction rounded to zero")
        raw_key = f"{plan.id}:{_agent_tool_call_id}:reduce:{quantity:f}"
        intent = OrderIntent(
            idempotency_key=f"cx{sha256(raw_key.encode()).hexdigest()[:20]}",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.SELL if plan.side == Side.LONG else OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            reduce_only=True,
        )
        episode.exit_client_order_ids = list(
            dict.fromkeys([*episode.exit_client_order_ids, intent.idempotency_key])
        )
        agent_store.save_episode(episode)
        try:
            order = await runtime.execution.reduce_position_safely(
                plan, entry_orders, intent, attributable
            )
        except asyncio.CancelledError:
            episode.status = TradeEpisodeStatus.UNCERTAIN
            episode.summary = (
                "Agent position reduction timed out or was interrupted after intent "
                "persistence; exchange reconciliation is required"
            )
            agent_store.save_episode(episode)
            risk_state_service.halt(episode.summary, source="protection_rebalance")
            agent_store.append_event(
                AgentEvent(
                    event_type=AgentEventType.ORDER_UNCERTAIN,
                    source="agent-position-tool",
                    goal_id=goal.id,
                    dedupe_key=f"episode:{episode.id}:reduction_interrupted",
                    payload={
                        "episode_id": episode.id,
                        "plan_id": plan.id,
                        "symbol": plan.symbol,
                        "client_order_id": intent.idempotency_key,
                    },
                )
            )
            raise
        except PositionProtectionError as exc:
            if exc.order and runtime.order_ledger:
                runtime.order_ledger.record(exc.order, protection_verified=False)
            if exc.order:
                episode.status = TradeEpisodeStatus.UNCERTAIN
                episode.exit_order_ids = list(
                    dict.fromkeys([*episode.exit_order_ids, exc.order.id])
                )
                agent_store.save_episode(episode)
            state = risk_state_service.halt(
                f"Agent position protection is uncertain: {str(exc)[:180]}",
                source="protection_rebalance",
            )
            add_audit(
                "agent",
                "Agent position reduction failed protection verification",
                severity="critical",
                goal_id=goal.id,
                plan_id=plan.id,
                reason=state.reason,
            )
            raise RuntimeError(state.reason) from exc
        if runtime.order_ledger:
            runtime.order_ledger.record(order, protection_verified=True)
        episode.exit_order_ids = list(dict.fromkeys([*episode.exit_order_ids, order.id]))
        remaining = attributable - order.filled_quantity
        if remaining <= 0:
            episode.status = TradeEpisodeStatus.AWAITING_REVIEW
            event_type = AgentEventType.POSITION_CLOSED
            event_suffix = "position_closed"
        else:
            event_type = AgentEventType.POSITION_UPDATED
            event_suffix = f"position_reduced:{order.id}"
        agent_store.save_episode(episode)
        agent_store.append_event(
            AgentEvent(
                event_type=event_type,
                source="agent-position-tool",
                goal_id=goal.id,
                dedupe_key=f"episode:{episode.id}:{event_suffix}",
                payload={
                    "episode_id": episode.id,
                    "plan_id": plan.id,
                    "symbol": plan.symbol,
                    "order_id": order.id,
                    "reduced_quantity": str(order.filled_quantity),
                    "remaining_quantity": str(max(remaining, Decimal("0"))),
                },
            )
        )
        return {
            "order": order.model_dump(mode="json"),
            "episode": episode.model_dump(mode="json"),
        }

    async def reduce_position(
        plan_id: str,
        symbol: str,
        fraction: str,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        return await decrease_agent_position(
            plan_id,
            symbol,
            fraction,
            _agent_run_id=_agent_run_id,
            _agent_tool_call_id=_agent_tool_call_id,
        )

    async def exit_position(
        plan_id: str,
        symbol: str,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        return await decrease_agent_position(
            plan_id,
            symbol,
            "1",
            _agent_run_id=_agent_run_id,
            _agent_tool_call_id=_agent_tool_call_id,
        )

    async def review_closed_trade(
        plan_id: str,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        del _agent_run_id, _agent_tool_call_id
        plan = owned_plan(plan_id, require_current_policy=False)
        existing = next(
            (item for item in agent_store.list_episodes(200) if item.plan_id == plan.id),
            None,
        )
        if existing and existing.status == TradeEpisodeStatus.CLOSED:
            return existing.model_dump(mode="json")
        if not existing:
            raise RuntimeError("Agent trade episode is missing")
        _, plan_fills = await episode_execution_evidence(plan, existing)
        entry_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        entries = [fill for fill in plan_fills if fill.side == entry_side]
        exits = [fill for fill in plan_fills if fill.side != entry_side]
        portfolio = await runtime.portfolio()
        position = next(
            (item for item in portfolio.open_positions if item.symbol == plan.symbol),
            None,
        )
        aligned = position and position.side == plan.side and position.quantity > 0
        if not entries or not exits or aligned:
            raise RuntimeError("trade is not fully closed; deterministic review is unavailable")
        entry_quantity = sum((fill.quantity for fill in entries), Decimal("0"))
        exit_quantity = sum((fill.quantity for fill in exits), Decimal("0"))
        if entry_quantity <= 0:
            raise RuntimeError("closed trade has no verified entry quantity")
        if exit_quantity < entry_quantity:
            raise RuntimeError("attributed exit fills do not fully cover the Agent entry")
        average_entry = (
            sum(
                (fill.price * fill.quantity for fill in entries),
                Decimal("0"),
            )
            / entry_quantity
        )
        average_exit = (
            sum(
                (fill.price * fill.quantity for fill in exits),
                Decimal("0"),
            )
            / exit_quantity
        )
        direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
        gross = sum(
            ((fill.price - average_entry) * fill.quantity * direction for fill in exits),
            Decimal("0"),
        )
        quote_currency = plan.symbol.split("/", 1)[1].split(":", 1)[0]
        unsupported_fees = {
            fill.fee_currency
            for fill in plan_fills
            if fill.fee and fill.fee_currency not in {"", quote_currency}
        }
        if unsupported_fees:
            raise RuntimeError(
                "cannot normalize non-quote fee currencies: " + ",".join(sorted(unsupported_fees))
            )
        fees = sum((fill.fee for fill in plan_fills), Decimal("0"))
        realized = (gross - fees).quantize(Decimal("0.01"))
        return_on_risk = (
            realized / plan.sizing.risk_amount if plan.sizing.risk_amount > 0 else Decimal("0")
        ).quantize(Decimal("0.001"))
        episode = existing
        episode.status = TradeEpisodeStatus.CLOSED
        episode.entry_order_ids = list(
            dict.fromkeys([*episode.entry_order_ids, *(fill.order_id for fill in entries)])
        )
        episode.exit_order_ids = list(
            dict.fromkeys([*episode.exit_order_ids, *(fill.order_id for fill in exits)])
        )
        episode.realized_pnl = realized
        episode.fees = fees
        episode.return_on_risk = return_on_risk
        episode.closed_at = max(fill.timestamp for fill in exits)
        episode.summary = (
            f"{plan.symbol} {plan.side} 已平仓；净收益 {realized:f} USDT，"
            f"风险收益倍数 {return_on_risk:f}R，费用 {fees:f} USDT"
        )
        agent_store.save_episode(episode)
        outcome = "win" if realized > 0 else "loss" if realized < 0 else "breakeven"
        memory_content = (
            f"{episode.summary}；outcome={outcome}；"
            f"strategy_family={plan.algorithm}；timeframe={plan.timeframe}；"
            f"entry_regime={plan.regime}；plan_confidence={plan.confidence:f}；"
            f"average_entry={average_entry:f}；average_exit={average_exit:f}；"
            f"planned_risk={plan.sizing.risk_amount:f} USDT；"
            f"strategy_spec={plan.strategy_spec_fingerprint or 'system-default'}。"
            "该记忆仅可作为同品种、同周期、同方向和同策略家族的前向结果证据，"
            "不能绕过最新行情、回测、成本压力或硬风险闸。"
        )
        memory = AgentMemory(
            memory_type=MemoryType.EPISODIC,
            content=memory_content,
            tags=[plan.symbol, plan.timeframe, str(plan.side), plan.algorithm],
            source_episode_id=episode.id,
            confidence=Decimal("1"),
            evidence_refs=[*episode.entry_order_ids, *episode.exit_order_ids],
        )
        agent_store.save_memory(memory)
        add_audit(
            "agent",
            "Agent 已完成确定性平仓复盘并写入 Episode 记忆",
            goal_id=goal.id,
            plan_id=plan.id,
            episode_id=episode.id,
            realized_pnl=float(realized),
        )
        return {
            "episode": episode.model_dump(mode="json"),
            "memory": memory.model_dump(mode="json"),
        }

    async def latest_sealed_campaign_scope() -> dict | None:
        payloads = await asyncio.to_thread(store.list_research_datasets, 500)
        groups: dict[str, list[ResearchDataset]] = {}
        for payload in payloads:
            dataset = ResearchDataset.model_validate(payload)
            if (
                dataset.blind_set_id
                and dataset.research_role in {"development", "blind"}
                and dataset.symbol in goal.symbols
                and dataset.timeframe in goal.timeframes
            ):
                groups.setdefault(dataset.blind_set_id, []).append(dataset)
        ordered_groups = sorted(
            groups.items(),
            key=lambda item: max(dataset.created_at for dataset in item[1]),
            reverse=True,
        )
        for blind_set_id, datasets in ordered_groups:
            consumption = await asyncio.to_thread(
                store.load_blind_set_consumption,
                blind_set_id,
            )
            if consumption is not None:
                continue
            development = [
                dataset for dataset in datasets if dataset.research_role == "development"
            ]
            blind = [dataset for dataset in datasets if dataset.research_role == "blind"]
            development_sources = {dataset.source_dataset_id for dataset in development}
            blind_sources = {dataset.source_dataset_id for dataset in blind}
            if len(development) < 3 or len(blind) < 3 or development_sources != blind_sources:
                continue
            return {
                "blind_set_id": blind_set_id,
                "dataset_ids": [dataset.id for dataset in development],
                "blind_dataset_ids": [dataset.id for dataset in blind],
                "market_count": len(development),
                "sealed": True,
                "consumed": False,
                "policy": "single-use-chronological-purged-holdout",
            }
        return None

    async def inspect_strategy_library(
        family: str | None = None,
    ) -> dict:
        payload = unified_research_strategy_library(family)
        payload.update(
            {
                "campaign_scope": await latest_sealed_campaign_scope(),
                "next_step": (
                    "select templates by current market fit, then backtest on a fresh "
                    "immutable dataset before any execution"
                ),
            }
        )
        return payload

    async def prepare_goal_research_data(
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        exchange_ids: list[str] | None = None,
        bars: int = 5000,
        max_markets: int = 12,
        selection_mode: str = "explicit",
        universe_snapshot_id: str | None = None,
        market_type: str | None = None,
        feature_mode: Literal["full", "ohlcv_only"] = "full",
        minimum_listing_age_days: int = 90,
        minimum_quote_volume_24h: Decimal = Decimal("0"),
        minimum_history_coverage_ratio: Decimal = Decimal("0.90"),
        maximum_symbols_per_exchange: int = 10,
    ) -> dict:
        requested_symbols = symbols or goal.symbols
        requested_timeframes = timeframes or goal.timeframes
        normalized_symbols = [item.strip().upper().replace("-", "/") for item in requested_symbols]
        normalized_timeframes = [item.strip().lower() for item in requested_timeframes]
        if not set(normalized_symbols).issubset(set(goal.symbols)):
            raise PermissionError("research symbol is outside the Agent goal")
        if not set(normalized_timeframes).issubset(set(goal.timeframes)):
            raise PermissionError("research timeframe is outside the Agent goal")
        request = ResearchUniverseRequest(
            symbols=normalized_symbols,
            timeframes=normalized_timeframes,
            exchange_ids=exchange_ids or [],
            bars=bars,
            max_markets=max_markets,
            selection_mode=selection_mode,
            universe_snapshot_id=universe_snapshot_id,
            market_type=market_type,
            feature_mode=feature_mode,
            minimum_listing_age_days=minimum_listing_age_days,
            minimum_quote_volume_24h=minimum_quote_volume_24h,
            minimum_history_coverage_ratio=minimum_history_coverage_ratio,
            minimum_symbols_per_exchange=min(3, len(normalized_symbols)),
            maximum_symbols_per_exchange=maximum_symbols_per_exchange,
        )
        normalized_request = request.model_dump(mode="json")
        job = ResearchUniverseJob(
            request=normalized_request,
            request_fingerprint=research_universe_request_fingerprint(normalized_request),
            total_markets=min(
                len(normalized_symbols)
                * len(normalized_timeframes)
                * max(len(exchange_ids or []), 1),
                max_markets,
            ),
        )
        await asyncio.to_thread(store.save_research_universe_job, job)
        await run_research_universe_job(job)
        if job.status != ResearchJobStatus.COMPLETED or job.report is None:
            raise RuntimeError(job.error or "durable research data acquisition failed")
        return {
            **job.report.model_dump(mode="json"),
            "durable_job": {
                "id": job.id,
                "request_fingerprint": job.request_fingerprint,
                "status": str(job.status),
            },
        }

    async def seal_goal_research_holdout(
        dataset_ids: list[str],
        universe_snapshot_id: str,
        development_ratio: float = 0.80,
        embargo_days: float = 7.0,
        minimum_development_bars: int = 500,
        minimum_blind_bars: int = 300,
        common_window: bool = True,
        window_start: datetime | None = None,
        window_end_exclusive: datetime | None = None,
    ) -> dict:
        for dataset_id in dataset_ids:
            payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
            if payload is None:
                raise LookupError(f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                raise PermissionError(f"dataset {dataset_id} is outside the Agent goal")
        request = SealedHoldoutRequest(
            dataset_ids=dataset_ids,
            universe_snapshot_id=universe_snapshot_id,
            development_ratio=development_ratio,
            embargo_days=embargo_days,
            minimum_development_bars=minimum_development_bars,
            minimum_blind_bars=minimum_blind_bars,
            common_window=common_window,
            window_start=window_start,
            window_end_exclusive=window_end_exclusive,
        )
        report = await asyncio.to_thread(seal_research_holdout, store, request)
        return report.model_dump(mode="json")

    async def backtest_goal_causal_strategy_policy(
        dataset_id: str,
        library_entry_ids: list[str],
        maximum_candidates_per_bar: int = 3,
        abstain_on_ambiguous_regime: bool = True,
        minimum_regime_confidence: str = "0",
        minimum_signal_confidence: str = "0",
        minimum_directional_consensus: int = 1,
        research_trial_count: int = 1,
    ) -> dict:
        payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
        if payload is None:
            raise LookupError(f"research dataset not found: {dataset_id}")
        dataset = ResearchDataset.model_validate(payload)
        if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
            raise PermissionError(f"dataset {dataset_id} is outside the Agent goal")
        result = await backtest_causal_strategy_policy(
            CausalPolicyBacktestRequest(
                dataset_id=dataset_id,
                library_entry_ids=library_entry_ids,
                maximum_candidates_per_bar=maximum_candidates_per_bar,
                abstain_on_ambiguous_regime=abstain_on_ambiguous_regime,
                minimum_regime_confidence=Decimal(minimum_regime_confidence),
                minimum_signal_confidence=Decimal(minimum_signal_confidence),
                minimum_directional_consensus=minimum_directional_consensus,
                research_trial_count=research_trial_count,
            )
        )
        return result.model_dump(mode="json")

    async def run_goal_research_campaign(
        dataset_ids: list[str],
        blind_dataset_ids: list[str] | None = None,
        universe_snapshot_id: str | None = None,
        library_entry_ids: list[str] | None = None,
        candidate_ids: list[str] | None = None,
        causal_policy_specs: list[dict] | None = None,
        max_backtests: int = 240,
        parent_campaign_id: str | None = None,
    ) -> dict:
        if not dataset_ids:
            raise ValueError("research campaign requires dataset_ids")
        blind_ids = blind_dataset_ids or []
        development_metadata: dict[str, ResearchDataset] = {}
        for dataset_id in [*dataset_ids, *blind_ids]:
            payload = await asyncio.to_thread(
                store.load_research_dataset,
                dataset_id,
            )
            if not payload:
                raise LookupError(f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                raise PermissionError(f"dataset {dataset_id} is outside the Agent goal")
            if dataset_id in dataset_ids:
                development_metadata[dataset_id] = dataset
        selected_library_entry_ids = list(library_entry_ids or [])
        selected_causal_policy_specs = [
            CausalPolicyCampaignSpec.model_validate(item) for item in (causal_policy_specs or [])
        ]
        data_preflight: dict = {
            "applied": False,
            "selection_basis": "explicit",
        }
        if (
            not selected_library_entry_ids
            and not candidate_ids
            and not selected_causal_policy_specs
        ):

            async def load_development_features(dataset_id: str):
                payloads = await asyncio.gather(
                    asyncio.to_thread(store.load_research_dataset_funding, dataset_id),
                    asyncio.to_thread(
                        store.load_research_dataset_open_interest,
                        dataset_id,
                    ),
                    asyncio.to_thread(
                        store.load_research_dataset_positioning,
                        dataset_id,
                    ),
                    asyncio.to_thread(store.load_research_dataset_basis, dataset_id),
                    asyncio.to_thread(
                        store.load_research_dataset_order_flow,
                        dataset_id,
                    ),
                    asyncio.to_thread(
                        store.load_research_dataset_liquidation_flow,
                        dataset_id,
                    ),
                )
                return (
                    [FundingRatePoint.model_validate(item) for item in payloads[0]],
                    [OpenInterestPoint.model_validate(item) for item in payloads[1]],
                    [PositioningPoint.model_validate(item) for item in payloads[2]],
                    [BasisPoint.model_validate(item) for item in payloads[3]],
                    [OrderFlowBar.model_validate(item) for item in payloads[4]],
                    [LiquidationFlowPoint.model_validate(item) for item in payloads[5]],
                )

            feature_rows = await asyncio.gather(
                *(load_development_features(dataset_id) for dataset_id in dataset_ids)
            )
            features_by_dataset = dict(zip(dataset_ids, feature_rows, strict=True))
            selected_library_entry_ids, excluded = causal_data_eligible_library_entries(
                list_strategy_library(),
                [development_metadata[dataset_id] for dataset_id in dataset_ids],
                features_by_dataset,
            )
            if not selected_library_entry_ids:
                raise RuntimeError(
                    "no public strategy has causal feature coverage across three symbols "
                    "and two exchanges; acquire missing development data first"
                )
            data_preflight = {
                "applied": True,
                "selection_basis": "development_data_availability_only",
                "selected_strategy_count": len(selected_library_entry_ids),
                "excluded_strategy_count": len(excluded),
                "excluded": excluded,
                "blind_data_inspected": False,
            }
        report = await run_research_campaign(
            ResearchCampaignRequest(
                dataset_ids=dataset_ids,
                blind_dataset_ids=blind_ids,
                universe_snapshot_id=universe_snapshot_id,
                library_entry_ids=selected_library_entry_ids,
                candidate_ids=candidate_ids or [],
                causal_policy_specs=selected_causal_policy_specs,
                max_backtests=max_backtests,
                parent_campaign_id=parent_campaign_id,
            )
        )
        result = redact_campaign_for_research_agent(report)
        result["causal_data_preflight"] = data_preflight
        result["blind_disclosure_policy"] = (
            "Research Agents receive only PASS, FAIL or SHADOW_REQUIRED with coarse codes; "
            "blind scope and metrics are sealed."
        )
        return result

    async def run_relative_value_research(
        dataset_a_id: str,
        dataset_b_id: str,
    ) -> dict:
        for dataset_id in {dataset_a_id, dataset_b_id}:
            payload = await asyncio.to_thread(
                store.load_research_dataset,
                dataset_id,
            )
            if not payload:
                raise LookupError(f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                raise PermissionError(
                    f"relative-value dataset {dataset_id} is outside the Agent goal"
                )
        reports = await run_relative_value_backtests(
            RelativeValueResearchRequest(
                dataset_a_id=dataset_a_id,
                dataset_b_id=dataset_b_id,
            )
        )
        return {
            "execution_enabled": False,
            "reports": [report.model_dump(mode="json") for report in reports],
        }

    async def run_relative_value_campaign_matrix(
        dataset_ids: list[str],
        blind_dataset_ids: list[str] | None = None,
        fold_count: int = 6,
        formation_fraction: Decimal = Decimal("0.25"),
        strategy_specs: list[dict] | None = None,
        parent_campaign_id: str | None = None,
        generate_failure_revisions: bool = True,
    ) -> dict:
        blind_ids = blind_dataset_ids or []
        for dataset_id in set([*dataset_ids, *blind_ids]):
            payload = await asyncio.to_thread(
                store.load_research_dataset,
                dataset_id,
            )
            if not payload:
                raise LookupError(f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                raise PermissionError(
                    f"relative-value dataset {dataset_id} is outside the Agent goal"
                )
        report = await run_relative_value_campaign(
            RelativeValueCampaignRequest(
                dataset_ids=dataset_ids,
                blind_dataset_ids=blind_ids,
                fold_count=fold_count,
                formation_fraction=formation_fraction,
                strategy_specs=strategy_specs or [],
                parent_campaign_id=parent_campaign_id,
                generate_failure_revisions=generate_failure_revisions,
            )
        )
        return report.model_dump(mode="json")

    async def run_carry_campaign_matrix(
        dataset_ids: list[str],
        blind_dataset_ids: list[str] | None = None,
        fold_count: int = 6,
        formation_fraction: Decimal = Decimal("0.25"),
        strategy_specs: list[dict] | None = None,
        parent_campaign_id: str | None = None,
        generate_failure_revisions: bool = True,
    ) -> dict:
        blind_ids = blind_dataset_ids or []
        for dataset_id in set([*dataset_ids, *blind_ids]):
            payload = await asyncio.to_thread(
                store.load_research_dataset,
                dataset_id,
            )
            if not payload:
                raise LookupError(f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                raise PermissionError(f"carry dataset {dataset_id} is outside the Agent goal")
        report = await run_carry_campaign(
            CarryCampaignRequest(
                dataset_ids=dataset_ids,
                blind_dataset_ids=blind_ids,
                fold_count=fold_count,
                formation_fraction=formation_fraction,
                strategy_specs=strategy_specs or [],
                parent_campaign_id=parent_campaign_id,
                generate_failure_revisions=generate_failure_revisions,
            )
        )
        return report.model_dump(mode="json")

    async def revise_failed_candidate(
        candidate_id: str = "",
        campaign_id: str = "",
        library_entry_id: str = "",
        max_revisions: int = 3,
    ) -> dict:
        if campaign_id:
            campaign_payload = await asyncio.to_thread(
                store.load_research_campaign,
                campaign_id,
            )
            if not campaign_payload:
                raise LookupError("research campaign not found")
            campaign = ResearchCampaignReport.model_validate(campaign_payload)
            for dataset_id in campaign.dataset_ids:
                dataset_payload = await asyncio.to_thread(
                    store.load_research_dataset,
                    dataset_id,
                )
                if not dataset_payload:
                    raise LookupError(f"research dataset not found: {dataset_id}")
                dataset = ResearchDataset.model_validate(dataset_payload)
                if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                    raise PermissionError("campaign research dataset is outside the Agent goal")
            if campaign.causal_policy_definitions:
                response = await revise_campaign_causal_policy(
                    campaign_id,
                    CausalPolicyRevisionRequest(
                        campaign_entry_id=library_entry_id or None,
                        max_revisions=max_revisions,
                    ),
                )
            else:
                response = await revise_campaign_strategy(
                    campaign_id,
                    CampaignRevisionRequest(
                        library_entry_id=library_entry_id or None,
                        max_revisions=max_revisions,
                    ),
                )
            return response.model_dump(mode="json")
        if not candidate_id:
            raise ValueError("candidate_id or campaign_id is required")
        payload = await asyncio.to_thread(
            store.load_strategy_candidate,
            candidate_id,
        )
        if not payload:
            raise LookupError("strategy candidate not found")
        candidate = StrategyCandidate.model_validate(payload)
        if candidate.dataset_id:
            dataset_payload = await asyncio.to_thread(
                store.load_research_dataset,
                candidate.dataset_id,
            )
            if dataset_payload:
                dataset = ResearchDataset.model_validate(dataset_payload)
                if dataset.symbol not in goal.symbols or dataset.timeframe not in goal.timeframes:
                    raise PermissionError("candidate research dataset is outside the Agent goal")
        response = await revise_strategy_candidate(
            candidate_id,
            CandidateRevisionRequest(max_revisions=max_revisions),
        )
        return response.model_dump(mode="json")

    async def detect_research_opportunity(symbol: str, timeframe: str) -> dict:
        market, history = await runtime.market_context(
            limit=300, symbol=symbol, timeframe=timeframe
        )
        candles = closed_candles(history, timeframe, market.timestamp)
        assessment = score_market_state(candles)
        return {
            "snapshot_time": market.timestamp.isoformat(),
            "symbol": symbol,
            "timeframe": timeframe,
            "closed_bar_count": len(candles),
            "market_state": assessment.model_dump(mode="json"),
            "permitted_next_tools": ["propose_hypothesis"],
        }

    def propose_hypothesis(hypothesis: dict) -> dict:
        validated = ResearchHypothesisV2.model_validate(hypothesis)
        return {
            "hypothesis": validated.model_dump(mode="json"),
            "status": "SEMANTIC_PROPOSAL_ONLY",
            "notice": "Certification Service must persist this before empirical evaluation",
        }

    def propose_strategy_dsl(dsl: dict) -> dict:
        validated = StrategyDSLV2.model_validate(dsl)
        ir = compile_strategy_dsl(validated)
        return {
            "dsl": validated.model_dump(mode="json"),
            "canonical_ir_hash": ir.fingerprint,
            "required_features": list(ir.required_features),
            "status": "SHADOW_REQUIRED_UNLESS_CERTIFIED_GENERATOR_PROVENANCE_EXISTS",
        }

    def propose_mutation(
        operation: EvolutionOperation,
        parent_ir_hash: str,
        changed_nodes: list[str],
        reason: str,
    ) -> dict:
        if not changed_nodes or len(changed_nodes) > 8:
            raise ValueError("mutation must name between one and eight changed nodes")
        return {
            "operation": str(operation),
            "parent_ir_hash": parent_ir_hash,
            "changed_nodes": changed_nodes,
            "reason": reason[:500],
            "status": "STRUCTURAL_PROPOSAL_ONLY",
        }

    def review_candidate(dsl: dict) -> dict:
        ir = compile_strategy_dsl(StrategyDSLV2.model_validate(dsl))
        return {
            "canonical_ir_hash": ir.fingerprint,
            "node_count": len(ir.nodes),
            "maximum_warmup": max(node.warmup for node in ir.nodes),
            "required_features": list(ir.required_features),
            "review": "SEMANTICALLY_VALID",
        }

    def summarize_failure(reason_codes: list[str]) -> dict:
        bounded = sorted(set(reason_codes))[:32]
        return {
            "reason_codes": bounded,
            "summary": "Candidate did not advance through one or more coarse research gates.",
            "blind_metrics_disclosed": False,
        }

    async def review_strategy_health() -> dict:
        report = await review_strategy_governance()
        return report.model_dump(mode="json")

    async def execute_validated_trade_plan(
        plan_id: str,
        symbol: str,
        timeframe: str,
        risk_fraction: str,
        leverage: str,
        protected: bool,
        *,
        _agent_run_id: str,
        _agent_tool_call_id: str,
    ) -> dict:
        """Move one admitted plan to protected submission in one resumable call."""

        plan = owned_plan(plan_id)
        arguments = {
            "plan_id": plan_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "risk_fraction": risk_fraction,
            "leverage": leverage,
            "protected": protected,
        }
        stages: list[str] = []
        if plan.status == PlanStatus.PAUSED:
            episode = next(
                (
                    item
                    for item in agent_store.list_episodes(500)
                    if item.goal_id == goal.id and item.plan_id == plan.id
                ),
                None,
            )
            if episode is not None:
                return {
                    "plan_id": plan.id,
                    "status": "already_submitted",
                    "stages": ["submission_previously_completed"],
                    "episode": episode.model_dump(mode="json"),
                }
        if plan.status == PlanStatus.DRAFT:
            await authorize_trade_plan(**arguments)
            stages.append("strategy_quality_admitted")
            plan = owned_plan(plan_id)
        if plan.status == PlanStatus.APPROVED:
            await activate_trade_plan(**arguments)
            stages.append("latest_signal_and_risk_revalidated")
            plan = owned_plan(plan_id)
        if plan.status != PlanStatus.ACTIVE:
            raise RuntimeError(f"validated plan state {plan.status} cannot be submitted")
        result = await submit_trade_plan(
            **arguments,
            _agent_run_id=_agent_run_id,
            _agent_tool_call_id=_agent_tool_call_id,
        )
        stages.append("protected_order_submitted")
        return {
            "plan_id": plan.id,
            "status": "submitted",
            "stages": stages,
            "execution": result,
        }

    for tool in (
        AgentTool(
            name="detect_research_opportunity",
            description=(
                "Read a bounded point-in-time market snapshot and identify whether a "
                "research hypothesis is warranted. Returns no database or execution handle"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=detect_research_opportunity,
        ),
        AgentTool(
            name="propose_hypothesis",
            description=(
                "Validate one ResearchHypothesisV2 semantic proposal. This tool does not "
                "persist, backtest, certify, adjust risk, or place orders"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=propose_hypothesis,
        ),
        AgentTool(
            name="propose_strategy_dsl",
            description=(
                "Parse and semantically compile versioned JSON Strategy DSL v2. Free DSL "
                "remains Shadow-only and this tool cannot issue provenance or certificates"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=propose_strategy_dsl,
        ),
        AgentTool(
            name="propose_mutation",
            description=(
                "Propose exactly one bounded structural evolution operation and changed "
                "node set; budget enforcement remains outside the LLM capability"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=propose_mutation,
        ),
        AgentTool(
            name="review_candidate",
            description=(
                "Review DSL semantic validity and complexity without return or blind metrics"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=review_candidate,
        ),
        AgentTool(
            name="summarize_failure",
            description=(
                "Summarize fixed coarse failure reason codes without sealed blind attribution"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=summarize_failure,
        ),
        AgentTool(
            name="inspect_strategy_library",
            description=(
                "Read the non-empty, versioned public strategy template library and "
                "indicator catalog. Includes single-leg, pair and true spot-perpetual "
                "carry priors. Optionally filter family by trend|mean_reversion|breakout|"
                "relative_value|relative_momentum|carry. Templates are hypotheses and never "
                "bypass fresh causal backtesting"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=inspect_strategy_library,
        ),
        AgentTool(
            name="prepare_research_universe",
            description=(
                "Paginate and persist immutable historical datasets across a bounded "
                "exchange x symbol x timeframe matrix inside the Agent goal. Public "
                "research adapters are isolated from the trading account. Returns "
                "coverage, quality, gaps, funding and open-interest history plus "
                "positioning, basis and order-flow provenance plus dataset ids. "
                "selection_mode=point_in_time first freezes the complete current "
                "venue catalog, listing age, active status, liquidity, exclusions and "
                "the exact in-goal market selection before downloading candles. It "
                "labels historical survivorship as unproven instead of inferring it. "
                "selection_mode=frozen_snapshot plus universe_snapshot_id replays an "
                "already sealed current or archived scope and rejects any mismatched "
                "exchange, symbol, timeframe, or market type. "
                "The acquisition job is durable and resumes after process restart. "
                "Optional exchange_ids (okx|binance|bybit), symbols, timeframes, "
                "market_type (spot|swap|future), "
                "feature_mode (full|ohlcv_only), bars (500-50000), max_markets, "
                "minimum_listing_age_days, "
                "minimum_quote_volume_24h, minimum_history_coverage_ratio, "
                "maximum_symbols_per_exchange. Every frozen market must meet the "
                "same immutable history-coverage contract before Campaign use. "
                "ohlcv_only seals candles first for fast causal screening; derivative "
                "features can be enriched independently after a candidate advances"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=prepare_goal_research_data,
            timeout_seconds=1800,
        ),
        AgentTool(
            name="seal_research_holdout",
            description=(
                "Seal an exact frozen universe into fingerprint-bound development and "
                "single-use blind datasets. Requires general source dataset_ids and "
                "universe_snapshot_id; rejects cherry-picked scope, pre-snapshot data, "
                "non-identical common timestamps, overlapping split windows, and unsafe "
                "time boundaries. Supports development_ratio, embargo_days, inclusive "
                "window_start, and exclusive window_end_exclusive. Returns development "
                "ids for strategy selection and blind ids that remain unopened until the "
                "development gate selects exactly one candidate"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=seal_goal_research_holdout,
        ),
        AgentTool(
            name="backtest_causal_strategy_policy",
            description=(
                "Backtest one pre-registered Agent meta-policy on an immutable non-blind "
                "dataset. At every closed bar it ranks supplied public library_entry_ids "
                "using only the current causal regime and latest-known feature availability, "
                "then evaluates at most maximum_candidates_per_bar strategies. The policy, "
                "selector version, ambiguity-abstention rule and StrategySpec fingerprints "
                "are bound into one fingerprint. Realised and future returns are forbidden "
                "selector inputs; sealed blind datasets are rejected"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=backtest_goal_causal_strategy_policy,
            timeout_seconds=1800,
        ),
        AgentTool(
            name="run_research_campaign",
            description=(
                "Run a complete public-strategy × immutable-dataset backtest "
                "matrix inside the Agent goal. Applies cost stress, temporal "
                "folds, Deflated Sharpe, multiple-testing correction, Monte "
                "Carlo path risk, cross-market gates and PBO. Requires "
                "development dataset_ids and disjoint independent "
                "blind_dataset_ids. Development data ranks candidates; blind "
                "data only accepts or rejects the single selected candidate. "
                "A universe_snapshot_id binds the exact pre-frozen market scope and "
                "is mandatory for any future execution certificate; historical "
                "survivorship must also be proven. Optional library_entry_ids, "
                "candidate_ids, causal_policy_specs and max_backtests. Each causal "
                "policy pre-registers a unique id, public library_entry_ids, maximum "
                "candidates evaluated per closed bar, and ambiguity abstention. Policy "
                "Campaigns require at least two structurally unique hypotheses and cannot "
                "inspect blind data during selection. A parent_campaign_id is mandatory "
                "for the one permitted sealed-lineage successor, which must retain every "
                "parent hypothesis and add only one to three parent-authorized, "
                "single-variable children"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=run_goal_research_campaign,
            timeout_seconds=3600,
        ),
        AgentTool(
            name="run_relative_value_research",
            description=(
                "Run public equal-dollar log-spread and causal rolling-beta residual "
                "pair hypotheses on "
                "two synchronized immutable datasets inside the Agent goal. "
                "Uses next-bar dual-leg fills, fees, slippage, funding and "
                "statistical gates. Research only: multi-leg execution is "
                "explicitly disabled"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=run_relative_value_research,
            timeout_seconds=600,
        ),
        AgentTool(
            name="run_relative_value_campaign",
            description=(
                "Build every same-exchange pair from at least three synchronized symbols "
                "on at least two exchanges, then run the bounded relative-value library "
                "through dual-leg costs, temporal folds, cross-pair quality gates, FDR "
                "and PBO. A frozen formation_fraction is excluded from every trading "
                "metric before the remaining out-of-sample period is evaluated. Accepts "
                "development dataset_ids and optional source-matched blind_dataset_ids, "
                "even fold_count and formation_fraction. A development winner alone may "
                "atomically consume the blind set once; only PASS/HOLD is disclosed. "
                "Optional strategy_specs can rerun only explicitly returned bounded "
                "child revisions, each counted as a new hypothesis. "
                "Research only: multi-leg execution remains disabled"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=run_relative_value_campaign_matrix,
            timeout_seconds=1800,
        ),
        AgentTool(
            name="run_carry_campaign",
            description=(
                "Run true same-exchange spot-perpetual cash-and-carry hypotheses over "
                "exact matched immutable legs for at least three symbols on each of two "
                "exchanges. Uses a frozen formation period, already-published funding, "
                "actual historical funding cashflows, synchronized next-bar dual-leg "
                "fills, capital cost, base and stressed fees/slippage, temporal folds, "
                "cross-market gates, FDR and PBO. Accepts development dataset_ids, optional "
                "source-matched blind_dataset_ids, even fold_count, formation_fraction and "
                "bounded strategy_specs. A development winner alone may atomically consume "
                "the blind set once; only PASS/HOLD is disclosed. "
                "A parent_campaign_id may run the one permitted successor containing only "
                "the parent's frozen hypotheses plus its returned one-variable children. "
                "Research only: multi-leg execution and profitability claims are disabled"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=run_carry_campaign_matrix,
            timeout_seconds=1800,
        ),
        AgentTool(
            name="revise_failed_strategy",
            description=(
                "Attribute why a complete Campaign aggregate or one backtested "
                "candidate failed, then create up to five bounded, model-aware "
                "single-hypothesis child revisions. Prefer campaign_id with an "
                "optional library_entry_id; otherwise use candidate_id. "
                "For a causal policy Campaign, freezes at most three one-control "
                "meta-policy children and permits exactly one successor. Revisions "
                "never inherit validation"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=revise_failed_candidate,
        ),
        AgentTool(
            name="review_strategy_health",
            description=(
                "Re-evaluate Challenger and Champion health from latest "
                "immutable backtests and Campaign evidence. Automatically "
                "retires a Champion only after at least two independent "
                "evidence layers fail"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=review_strategy_health,
        ),
        AgentTool(
            name="assess_owned_position",
            description=(
                "Deterministically assess one owned open position using attributed "
                "fills, current exchange position, exact native protection coverage "
                "and the latest closed strategy signal. Returns hold, exit or halt "
                "with exact execution arguments. Requires plan_id and symbol"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=assess_owned_position,
            timeout_seconds=45,
        ),
        AgentTool(
            name="screen_trade_candidates",
            description=(
                "Generate a bounded current-market parameter mini-ensemble for every "
                "actionable trend, mean-reversion, and breakout family, then backtest all "
                "hypotheses on equivalent fresh evidence. Parameters are selected before "
                "and without reading backtest results. Rank full-sample, chronological "
                "stability, recent-window and cost-stress evidence. Returns selected_plan_id "
                "only when a candidate passes every robustness gate. Arguments: symbol, "
                "timeframe, optional prompt and risk_fraction"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen_trade_candidates,
            timeout_seconds=240,
            include_context=True,
        ),
        AgentTool(
            name="build_trade_plan",
            description=(
                "Build a deterministic StrategyPlan. Arguments: prompt, strategy_kind "
                "(trend|mean_reversion|breakout), symbol, timeframe, optional risk_fraction. "
                "A matching same-market research Champion is applied automatically; "
                "the resulting plan still requires a fresh backtest"
            ),
            risk_class=ToolRiskClass.RESEARCH,
            handler=build_trade_plan,
            include_context=True,
        ),
        AgentTool(
            name="run_plan_backtest",
            description="Run walk-forward and cost-stress validation for an owned plan_id",
            risk_class=ToolRiskClass.RESEARCH,
            handler=run_plan_backtest,
        ),
        AgentTool(
            name="execute_validated_trade_plan",
            description=(
                "The only Agent entry transition for an already selected robust plan. "
                "Atomically resumes strategy-quality admission, latest closed-signal "
                "activation and protected order submission. Requires the exact "
                "selected_authorization object: plan_id, symbol, timeframe, "
                "risk_fraction, leverage, protected=true"
            ),
            risk_class=ToolRiskClass.INCREASE_RISK,
            handler=execute_validated_trade_plan,
            timeout_seconds=120,
            include_context=True,
        ),
        AgentTool(
            name="cancel_entry_order",
            description="Cancel a tracked unfilled entry using order_id and symbol",
            risk_class=ToolRiskClass.DECREASE_RISK,
            handler=cancel_entry_order,
        ),
        AgentTool(
            name="reduce_position",
            description=(
                "Reduce an owned open Agent position while preserving exact protection "
                "coverage. Exchange modes require passed position-management commissioning"
            ),
            risk_class=ToolRiskClass.DECREASE_RISK,
            handler=reduce_position,
            include_context=True,
        ),
        AgentTool(
            name="exit_position",
            description=(
                "Fully close an owned open Agent position and verify protection cleanup. "
                "Exchange modes require passed position-management commissioning"
            ),
            risk_class=ToolRiskClass.DECREASE_RISK,
            handler=exit_position,
            include_context=True,
        ),
        AgentTool(
            name="review_closed_trade",
            description=(
                "Review a fully closed owned trade from attributed fills by plan_id "
                "and write an Episode memory"
            ),
            risk_class=ToolRiskClass.READ_ONLY,
            handler=review_closed_trade,
            include_context=True,
        ),
    ):
        registry.register(tool)
    executor = AgentToolExecutor(registry, agent_store)
    return AgentCoordinator(
        store=agent_store,
        registry=registry,
        executor=executor,
        reasoner=GatewayAgentReasoner(ai_gateway),
        state_builder=build_state,
    )


@app.post("/api/agent/goals/{goal_id}/run-once", response_model=AgentRun)
async def run_agent_goal_once(goal_id: str):
    goal = await asyncio.to_thread(agent_store.load_goal, goal_id)
    if not goal:
        raise HTTPException(404, "Agent goal not found")
    policy = await asyncio.to_thread(agent_store.load_policy, goal.risk_policy_id)
    if not policy:
        raise HTTPException(409, "Agent policy not found")
    coordinator = build_agent_coordinator(goal, policy)
    run = await coordinator.process_next(
        worker_id=f"api-{uuid4().hex[:10]}", goal=goal, policy=policy
    )
    if not run:
        raise HTTPException(409, "Agent goal has no pending event")
    add_audit(
        "agent",
        "Agent 已完成一次有界 Observe-Reason-Act 运行",
        goal_id=goal.id,
        run_id=run.id,
        status=str(run.status),
        steps=run.step_count,
    )
    return run


class PreparedLiveAgentPromotion(BaseModel):
    source_goal_id: str
    live_policy: AgentPolicy
    live_goal: AgentGoal


def prepare_live_agent_promotion(
    update: ConfigurationUpdate,
    previous: UserConfiguration,
) -> PreparedLiveAgentPromotion | None:
    source_goal_id = update.promote_agent_goal_id
    if not source_goal_id:
        return None
    if (
        previous.trading_mode != TradingMode.SANDBOX
        or update.configuration.trading_mode != TradingMode.LIVE
    ):
        raise HTTPException(
            409,
            "Agent 实盘升级只能与 sandbox → live 环境切换在同一次保存中执行",
        )
    source_goal = agent_store.load_goal(source_goal_id)
    if (
        not source_goal
        or source_goal.status != AgentGoalStatus.ACTIVE
        or source_goal.autonomy_mode != AgentAutonomyMode.SANDBOX
    ):
        raise HTTPException(409, "请选择一个仍处于 active 的 sandbox Agent 目标")
    source_policy = agent_store.load_policy(source_goal.risk_policy_id)
    if (
        not source_policy
        or source_policy.autonomy_mode != AgentAutonomyMode.SANDBOX
        or not source_policy.allow_agent_entry
    ):
        raise HTTPException(409, "sandbox 目标没有有效的自动入场授权")
    if not set(source_goal.symbols).issubset(update.configuration.strategy.watchlist):
        raise HTTPException(409, "实盘关注列表不再覆盖 sandbox Agent 目标品种")
    if not set(source_goal.timeframes).issubset(update.configuration.strategy.timeframes):
        raise HTTPException(409, "实盘策略周期不再覆盖 sandbox Agent 目标周期")
    nonterminal = {
        TradeEpisodeStatus.RESERVED,
        TradeEpisodeStatus.PENDING_ENTRY,
        TradeEpisodeStatus.OPEN,
        TradeEpisodeStatus.AWAITING_REVIEW,
        TradeEpisodeStatus.UNCERTAIN,
    }
    active_episodes = [
        episode
        for episode in agent_store.list_episodes(10000)
        if episode.goal_id == source_goal.id and episode.status in nonterminal
    ]
    if active_episodes:
        raise HTTPException(
            409,
            "sandbox Agent 仍有未闭环交易回合，完成平仓、复盘和对账后才能升级",
        )
    target_leverage = min(
        source_policy.max_leverage,
        update.configuration.risk.max_leverage,
    )
    if (
        update.configuration.market_type == "swap"
        and target_leverage < update.configuration.contract_leverage
    ):
        raise HTTPException(
            409,
            "实盘固定执行杠杆高于 sandbox Agent 授权，不能无损迁移",
        )
    live_policy = source_policy.model_copy(
        update={
            "id": f"{source_policy.id}-live",
            "autonomy_mode": AgentAutonomyMode.LIVE_CAPPED,
            "max_risk_per_trade": min(
                source_policy.max_risk_per_trade,
                update.configuration.risk.risk_per_trade,
            ),
            "max_daily_drawdown": min(
                source_policy.max_daily_drawdown,
                update.configuration.risk.max_daily_drawdown,
            ),
            "max_open_positions": min(
                source_policy.max_open_positions,
                update.configuration.risk.max_open_positions,
            ),
            "max_leverage": target_leverage,
        }
    )
    now = datetime.now(UTC)
    live_goal = source_goal.model_copy(
        update={
            "id": f"goal_live_{sha256(source_goal.id.encode()).hexdigest()[:16]}",
            "autonomy_mode": AgentAutonomyMode.LIVE_CAPPED,
            "risk_policy_id": live_policy.id,
            "status": AgentGoalStatus.ACTIVE,
            "created_at": now,
            "updated_at": now,
        }
    )
    return PreparedLiveAgentPromotion(
        source_goal_id=source_goal.id,
        live_policy=live_policy,
        live_goal=live_goal,
    )


@app.put("/api/settings", response_model=ConfigurationView)
async def update_settings(update: ConfigurationUpdate):
    previous = configuration_service.current_configuration()
    promotion = prepare_live_agent_promotion(update, previous)
    previous_context = (
        previous.exchange_id,
        previous.trading_mode,
        previous.market_type,
    )
    next_context = (
        update.configuration.exchange_id,
        update.configuration.trading_mode,
        update.configuration.market_type,
    )
    credential_change = bool(
        update.credentials
        and (
            update.credentials.clear_existing
            or update.credentials.api_key
            or update.credentials.secret
            or update.credentials.passphrase
        )
    )
    previous_target_credentials = (
        configuration_service.credentials_for(update.configuration).copy()
        if credential_change
        else None
    )
    sensitive_change = previous_context != next_context or credential_change
    if sensitive_change and previous.trading_mode != TradingMode.PAPER:
        try:
            await runtime.initialize()
            assert runtime.execution is not None
            open_orders = await runtime.execution.open_orders()
            portfolio = await runtime.portfolio()
        except Exception as exc:
            raise HTTPException(
                409,
                f"无法确认旧执行环境没有风险敞口，拒绝切换设置：{exc}",
            ) from exc
        if open_orders:
            raise HTTPException(409, "旧执行环境仍有挂单，请先撤单并完成对账")
        if previous_context != next_context and portfolio.open_positions:
            raise HTTPException(409, "旧执行环境仍有持仓，请先平仓后再切换")
    try:
        view = configuration_service.save(update)
        await runtime.initialize(force=True)
        if promotion:
            validate_agent_policy_against_platform(promotion.live_policy)
            promoted_view = view.model_copy(
                update={
                    "promoted_agent_goal_id": promotion.live_goal.id,
                    "promoted_agent_policy_id": promotion.live_policy.id,
                }
            )
            await asyncio.to_thread(
                agent_store.promote_goal_to_live,
                promotion.source_goal_id,
                promotion.live_policy,
                promotion.live_goal,
            )
            view = promoted_view
    except Exception as exc:
        store.save_configuration(previous)
        if previous_target_credentials is not None:
            with suppress(Exception):
                configuration_service.restore_credentials(
                    update.configuration, previous_target_credentials
                )
        with suppress(Exception):
            await runtime.initialize(force=True)
        raise HTTPException(400, str(exc)) from exc
    if previous_context != next_context:
        archived = 0
        for plan in plans.values():
            if plan.status in {PlanStatus.ACTIVE, PlanStatus.PAUSED}:
                plan.status = PlanStatus.ARCHIVED
                store.save_strategy(plan)
                archived += 1
        if archived:
            add_audit(
                "strategy",
                "执行环境已切换，旧环境活动策略全部归档",
                severity="warning",
                archived=archived,
            )
    add_audit(
        "settings",
        "运行设置已更新",
        exchange=view.configuration.exchange_id,
        mode=str(view.configuration.trading_mode),
    )
    if promotion:
        add_audit(
            "agent",
            "sandbox Agent 授权与目标已原子升级为 live_capped",
            severity="warning",
            source_goal_id=promotion.source_goal_id,
            live_goal_id=promotion.live_goal.id,
            live_policy_id=promotion.live_policy.id,
        )
    return view


@app.post("/api/settings/test-connection", response_model=ConnectionTestResult)
async def test_connection(update: ConfigurationUpdate):
    started = perf_counter()
    credentials = configuration_service.credentials_for(update.configuration)
    if update.credentials:
        incoming = update.credentials.model_dump(exclude={"clear_existing"})
        credentials.update({key: value for key, value in incoming.items() if value})
    try:
        return await runtime.test_connection(update.configuration, credentials)
    except Exception as exc:
        return ConnectionTestResult(
            ok=False,
            exchange=update.configuration.exchange_id,
            mode=update.configuration.trading_mode,
            public_market_ok=False,
            private_account_ok=False
            if update.configuration.trading_mode != TradingMode.PAPER
            else None,
            latency_ms=round((perf_counter() - started) * 1000),
            symbol=update.configuration.default_symbol,
            failure_code="public_market_unavailable",
            required_action="检查网络、代理、交易所可用性和交易品种后重新测试连接。",
            message=str(exc),
        )


class CommissioningRequest(BaseModel):
    confirmation: str


class MultiLegCommissioningLeg(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    adapter_key: str = Field(min_length=1, max_length=64)
    market_type: Literal["spot", "swap"]
    symbol: str = Field(min_length=3, max_length=64)
    side: OrderSide


class MultiLegCommissioningRequest(BaseModel):
    strategy_kind: Literal["pair", "carry"]
    legs: list[MultiLegCommissioningLeg] = Field(min_length=2, max_length=2)
    confirmation: str

    def validate_protocol_shape(self) -> None:
        if len({leg.id for leg in self.legs}) != 2:
            raise ValueError("multi-leg commissioning requires two distinct leg ids")
        key_scopes: dict[str, str] = {}
        for leg in self.legs:
            existing = key_scopes.get(leg.adapter_key)
            if existing is not None and existing != leg.market_type:
                raise ValueError("one adapter key cannot span market types")
            key_scopes[leg.adapter_key] = leg.market_type
        sides = {leg.side for leg in self.legs}
        if sides != {OrderSide.BUY, OrderSide.SELL}:
            raise ValueError("multi-leg commissioning requires one buy and one sell leg")
        if self.strategy_kind == "pair":
            if any(leg.market_type != "swap" for leg in self.legs):
                raise ValueError("pair commissioning requires two swap legs")
            if len({leg.symbol for leg in self.legs}) != 2:
                raise ValueError("pair commissioning requires two distinct symbols")
        else:
            if {leg.market_type for leg in self.legs} != {"spot", "swap"}:
                raise ValueError("carry commissioning requires one spot and one swap leg")
            if len({leg.symbol for leg in self.legs}) != 1:
                raise ValueError("carry commissioning requires the same canonical symbol")
            spot_leg = next(leg for leg in self.legs if leg.market_type == "spot")
            if spot_leg.side != OrderSide.BUY:
                raise ValueError("carry commissioning requires spot buy and swap sell")


def _multi_leg_scope_exposure(
    portfolio: PortfolioState,
    symbol: str,
) -> Decimal:
    return sum(
        (
            position.quantity if str(position.side) == str(Side.LONG) else -position.quantity
            for position in portfolio.open_positions
            if position.symbol == symbol
        ),
        Decimal("0"),
    )


async def _sandbox_multi_leg_adapters(
    request: MultiLegCommissioningRequest,
) -> tuple[dict[str, CcxtExchangeAdapter], dict[str, OrderLedger]]:
    configuration = configuration_service.current_configuration()
    credentials = configuration_service.credentials_for(configuration)
    adapters: dict[str, CcxtExchangeAdapter] = {}
    ledgers: dict[str, OrderLedger] = {}
    for leg in request.legs:
        if leg.adapter_key in adapters:
            continue
        adapter = CcxtExchangeAdapter(
            configuration.exchange_id,
            credentials,
            sandbox=True,
            live_enabled=False,
            market_type=leg.market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            store=store,
            account_id=(f"{configuration.exchange_id}:sandbox:{leg.market_type}:multi_leg"),
            proxy_url=configuration.network_proxy_url,
        )
        adapters[leg.adapter_key] = adapter
        ledgers[leg.adapter_key] = OrderLedger(
            store,
            f"{configuration.exchange_id}:sandbox:{leg.market_type}:multi_leg",
        )
    return adapters, ledgers


@app.post(
    "/api/settings/commission-multi-leg",
    response_model=MultiLegCommissioningResult,
)
async def commission_multi_leg(request: MultiLegCommissioningRequest):
    if multi_leg_commissioning_lock.locked():
        raise HTTPException(409, "multi-leg commissioning is already running")
    async with multi_leg_commissioning_lock:
        return await _commission_multi_leg(request)


async def _commission_multi_leg(
    request: MultiLegCommissioningRequest,
) -> MultiLegCommissioningResult:
    try:
        request.validate_protocol_shape()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    configuration = configuration_service.current_configuration()
    if configuration.trading_mode != TradingMode.SANDBOX:
        raise HTTPException(409, "multi-leg commissioning can run only in sandbox mode")
    expected = (
        "TEST MULTI LEG "
        + request.strategy_kind.upper()
        + " "
        + ",".join(sorted(leg.symbol for leg in request.legs))
    )
    if request.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    if risk_state_service.get().halted:
        raise HTTPException(409, "global risk halt must be cleared before commissioning")
    credential_fingerprint = configuration_service.credential_fingerprint(configuration)
    for market_type in sorted({leg.market_type for leg in request.legs}):
        scoped_configuration = configuration.model_copy(update={"market_type": market_type})
        existing = configuration_service.commissioning_for(scoped_configuration)
        if not existing or not existing.passed:
            raise HTTPException(
                409,
                f"single-market sandbox commissioning has not passed for {market_type}",
            )
        if existing.credential_fingerprint != credential_fingerprint:
            raise HTTPException(409, f"{market_type} commissioning used other credentials")
        if market_type == "swap" and not existing.agent_position_management_passed:
            raise HTTPException(
                409,
                "swap position-management commissioning must pass first",
            )

    adapters, ledgers = await _sandbox_multi_leg_adapters(request)
    scope_map = {
        leg.id: f"{configuration.exchange_id}:{leg.market_type}:{leg.symbol}"
        for leg in request.legs
    }
    starting_exposure: dict[str, Decimal] = {}
    try:
        for adapter in adapters.values():
            await adapter.validate_account_configuration()
        portfolios = {
            key: portfolio
            for key, portfolio in zip(
                adapters,
                await asyncio.gather(*(item.portfolio() for item in adapters.values())),
                strict=True,
            )
        }
        open_orders_by_adapter = {
            key: orders
            for key, orders in zip(
                adapters,
                await asyncio.gather(*(item.open_orders() for item in adapters.values())),
                strict=True,
            )
        }
        for leg in request.legs:
            scope = scope_map[leg.id]
            starting_exposure[scope] = _multi_leg_scope_exposure(
                portfolios[leg.adapter_key],
                leg.symbol,
            )
        no_preexisting_scope_orders = not any(
            order.symbol == leg.symbol
            for leg in request.legs
            for order in open_orders_by_adapter[leg.adapter_key]
        )
        if any(value != 0 for value in starting_exposure.values()):
            raise HTTPException(
                409,
                "commissioning scopes must start flat; existing positions were found",
            )
        if not no_preexisting_scope_orders:
            raise HTTPException(
                409,
                "commissioning scopes must have no pre-existing open orders",
            )
        snapshots = await asyncio.gather(
            *(adapters[leg.adapter_key].market_snapshot(leg.symbol) for leg in request.legs)
        )
        quantities = await asyncio.gather(
            *(
                adapters[leg.adapter_key].minimum_commission_quantity(
                    leg.symbol,
                    snapshot.ask if leg.side == OrderSide.BUY else snapshot.bid,
                )
                for leg, snapshot in zip(request.legs, snapshots, strict=True)
            )
        )
        run_id = f"multileg_commission_{uuid4().hex}"
        execution_request = MultiLegExecutionRequest(
            id=run_id,
            strategy_kind=request.strategy_kind,
            campaign_id=f"commissioning_{uuid4().hex}",
            strategy_fingerprint=sha256(run_id.encode()).hexdigest(),
            campaign_certificate_fingerprint=sha256(f"certificate:{run_id}".encode()).hexdigest(),
            blind_evidence_fingerprint=sha256(
                f"not-research-evidence:{run_id}".encode()
            ).hexdigest(),
            execution_purpose="commissioning",
            trading_mode="sandbox",
            legs=[
                MultiLegOrderSpec(
                    id=leg.id,
                    adapter_key=leg.adapter_key,
                    exchange=configuration.exchange_id,
                    market_type=leg.market_type,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=quantity.quantize(
                        Decimal("0.00000001"),
                        rounding=ROUND_UP,
                    ),
                    compensation_reduce_only=leg.market_type != "spot",
                )
                for leg, quantity in zip(request.legs, quantities, strict=True)
            ],
        )
        coordinator = MultiLegExecutionCoordinator(
            store,
            adapters,
            risk_state=risk_state_service,
            reconciliation_attempts=8,
            reconciliation_delay_seconds=0.5,
            order_sink=lambda adapter_key, order: ledgers[adapter_key].record(
                order,
                protection_verified=False,
            ),
        )

        def validate_commissioning(candidate: MultiLegExecutionRequest) -> None:
            if (
                candidate.execution_purpose != "commissioning"
                or candidate.fingerprint != execution_request.fingerprint
            ):
                raise ValueError("commissioning request identity changed")

        entered = await coordinator.execute(
            execution_request,
            validate_certificate=validate_commissioning,
        )
        if entered.status != MultiLegExecutionStatus.HEDGED:
            raise RuntimeError(f"multi-leg sandbox entry did not reach HEDGED: {entered.status}")
        closed = await coordinator.close(
            entered.id,
            validate_certificate=validate_commissioning,
        )
        ending_portfolios = {
            key: portfolio
            for key, portfolio in zip(
                adapters,
                await asyncio.gather(*(item.portfolio() for item in adapters.values())),
                strict=True,
            )
        }
        ending_exposure = {
            scope_map[leg.id]: _multi_leg_scope_exposure(
                ending_portfolios[leg.adapter_key],
                leg.symbol,
            )
            for leg in request.legs
        }
        remaining_orders = await asyncio.gather(*(item.open_orders() for item in adapters.values()))
        no_remaining_scope_orders = not any(
            order.symbol in {leg.symbol for leg in request.legs}
            for orders in remaining_orders
            for order in orders
        )
        recorded_order_ids = {
            record.order.id for ledger in ledgers.values() for record in ledger.records.values()
        }
        expected_order_ids = {
            order.id
            for state in closed.legs
            for order in (state.entry_order, state.compensation_order)
            if order is not None
        }
        result = build_multi_leg_commissioning_result(
            exchange=configuration.exchange_id,
            credential_fingerprint=credential_fingerprint,
            strategy_kind=request.strategy_kind,
            run=entered,
            closed_run=closed,
            starting_exposure=starting_exposure,
            ending_exposure=ending_exposure,
            no_preexisting_scope_orders=no_preexisting_scope_orders,
            no_remaining_scope_orders=no_remaining_scope_orders,
            order_ledger_recorded=expected_order_ids <= recorded_order_ids,
            message=(
                "sandbox entry, hedge reconciliation, coordinated close, flat-position "
                "recheck, and durable order recording completed"
            ),
        )
        if not result.passed:
            risk_state_service.halt(
                "multi-leg commissioning ended without complete flat-state proof",
                source="multi_leg_commissioning",
            )
        await asyncio.to_thread(store.save_multi_leg_commissioning, result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        risk_state_service.halt(
            f"multi-leg commissioning failed; manually reconcile venue: {str(exc)[:180]}",
            source="multi_leg_commissioning",
        )
        add_audit(
            "commissioning",
            "multi-leg sandbox commissioning failed",
            severity="critical",
            strategy_kind=request.strategy_kind,
            reason=str(exc)[:240],
        )
        raise HTTPException(502, str(exc)) from exc
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )


@app.get(
    "/api/settings/commission-multi-leg",
    response_model=list[MultiLegCommissioningResult],
)
async def list_multi_leg_commissioning():
    payloads = await asyncio.to_thread(store.list_multi_leg_commissioning)
    return [MultiLegCommissioningResult.model_validate(payload) for payload in payloads]


@app.post(
    "/api/settings/commission-protection",
    response_model=CommissioningResult,
)
async def commission_protection(request: CommissioningRequest):
    configuration = configuration_service.current_configuration()
    if configuration.trading_mode != TradingMode.SANDBOX:
        raise HTTPException(409, "原子保护单验收只能在交易所测试网模式执行")
    expected = f"TEST PROTECTED ORDER {configuration.default_symbol}"
    if request.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    await runtime.initialize(force=True)
    execution = runtime.execution
    if not isinstance(execution, CcxtExchangeAdapter):
        raise HTTPException(409, "当前执行网关不是交易所测试网")
    orders: list[Order] = []
    canceled_ids: set[str] = set()
    try:
        market, history = await runtime.market_context()
        strategy_history = closed_candles(
            history,
            configuration.default_timeframe,
            market.timestamp,
        )
        await execution.validate_account_configuration()
        entry_price = market.last * Decimal("0.80")
        stop_price = market.last * Decimal("0.75")
        target_price = market.last * Decimal("1.20")
        quantity = await execution.minimum_commission_quantity(
            configuration.default_symbol, entry_price
        )
        quantity = quantity.quantize(Decimal("0.00000001"), rounding=ROUND_UP)
        risk_amount = (quantity * (entry_price - stop_price)).quantize(
            Decimal("0.00000001"), rounding=ROUND_UP
        )
        notional = (quantity * entry_price).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
        template = strategy_factory.generate_unchecked(
            StrategyKind.TREND,
            candles=strategy_history,
            snapshot=market,
            equity=max(configuration.paper_equity, Decimal("100")),
            risk_fraction=Decimal("0.001"),
            exchange=configuration.exchange_id,
            timeframe=configuration.default_timeframe,
            minimum_reward_risk=Decimal("1"),
        )
        plan = template.model_copy(
            update={
                "id": f"commission-{uuid4().hex}",
                "name": "交易所原子保护单验收",
                "side": Side.LONG,
                "trading_mode": configuration.trading_mode,
                "market_type": configuration.market_type,
                "margin_mode": str(configuration.margin_mode),
                "leverage": configuration.contract_leverage,
                "status": PlanStatus.ACTIVE,
                "entry": EntryRule(
                    order_type=OrderType.LIMIT,
                    zone=PriceZone(lower=entry_price, upper=entry_price),
                    trigger="仅用于测试网验收；限价远离市价，验证后立即撤销",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                ),
                "exit": ExitPlan(
                    stop_loss=stop_price,
                    take_profits=[
                        ProfitTarget(
                            price=target_price,
                            close_fraction=Decimal("1"),
                            rationale="测试交易所原生止盈保护",
                        )
                    ],
                    invalidation="验收结束立即撤销",
                ),
                "sizing": PositionSizing(
                    risk_fraction=Decimal("0.001"),
                    risk_amount=risk_amount,
                    suggested_quantity=quantity,
                    notional=notional,
                ),
                "minimum_reward_risk": Decimal("1"),
                "thesis": "测试网原子保护单协议验收，不属于交易策略信号。",
                "reasons": ["确认入场、止盈和止损由交易所作为一个受保护结构接受"],
            }
        )
        intent = OrderIntent(
            idempotency_key=f"cxtest{uuid4().hex[:16]}",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=entry_price,
        )
        orders = await execution.place_protected_order(plan, intent)
        for order in orders:
            if not await execution.verify_protection(order):
                raise RuntimeError(f"订单 {order.id} 未查询到完整止盈止损保护")
        for order in reversed(orders):
            canceled = await execution.cancel_order(order.id, order.symbol)
            if canceled.status != OrderStatus.CANCELED:
                canceled = await execution.fetch_order(order.id, order.symbol)
            if canceled.status != OrderStatus.CANCELED:
                raise RuntimeError(f"测试订单 {order.id} 未确认撤销，当前状态 {canceled.status}")
            canceled_ids.add(order.id)
    except Exception as exc:
        for order in reversed(orders):
            if order.id not in canceled_ids:
                with suppress(Exception):
                    await execution.cancel_order(order.id, order.symbol)
        risk_state_service.halt(
            f"测试网保护单验收失败，必须人工核对交易所挂单：{str(exc)[:160]}",
            source="commissioning",
        )
        add_audit(
            "commissioning",
            "测试网原子保护单验收失败",
            severity="critical",
            reason=str(exc)[:180],
        )
        raise HTTPException(502, str(exc)) from exc
    result = CommissioningResult(
        exchange=configuration.exchange_id,
        market_type=configuration.market_type,
        symbol=configuration.default_symbol,
        credential_fingerprint=configuration_service.credential_fingerprint(configuration),
        protection_verified=True,
        cancellation_confirmed=len(canceled_ids) == len(orders),
        order_ids=[order.id for order in orders],
        message="测试网原子保护单已由交易所确认，且所有测试订单均已撤销",
    )
    store.save_commissioning(configuration.exchange_id, str(configuration.market_type), result)
    add_audit(
        "commissioning",
        "测试网原子保护单验收通过",
        exchange=configuration.exchange_id,
        market_type=str(configuration.market_type),
        order_count=len(orders),
    )
    return result


@app.post(
    "/api/settings/commission-agent-position-management",
    response_model=CommissioningResult,
)
async def commission_agent_position_management(request: CommissioningRequest):
    if agent_commissioning_lock.locked():
        raise HTTPException(409, "Agent 仓位管理验收正在执行，请等待当前验收完成")
    async with agent_commissioning_lock:
        return await _commission_agent_position_management(request)


async def _commission_agent_position_management(request: CommissioningRequest):
    """Exercise an actual OKX Demo fill, partial reduction, and final exit."""

    configuration = configuration_service.current_configuration()
    if (
        configuration.trading_mode != TradingMode.SANDBOX
        or configuration.exchange_id != "okx"
        or str(configuration.market_type) != "swap"
    ):
        raise HTTPException(409, "Agent 仓位管理验收只能在 OKX USDT 永续测试网执行")
    expected = f"TEST AGENT REDUCTION {configuration.default_symbol}"
    if request.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    risk_state = risk_state_service.get()
    if risk_state.halted:
        raise HTTPException(
            409,
            "全局交易仍处于 HALT；请先稳定通过对账、人工核对账户并解除停机",
        )
    existing = configuration_service.commissioning_for(configuration)
    if not existing or not existing.passed:
        raise HTTPException(409, "请先通过测试网原子保护挂单与撤单验收")
    if existing.credential_fingerprint != configuration_service.credential_fingerprint(
        configuration
    ):
        raise HTTPException(409, "当前测试网凭证与原子保护单验收凭证不一致")

    await runtime.initialize(force=True)
    execution = runtime.execution
    if not isinstance(execution, CcxtExchangeAdapter):
        raise HTTPException(409, "当前执行网关不是 OKX 测试网")
    portfolio = await runtime.portfolio()
    if any(
        position.symbol == configuration.default_symbol and position.quantity > 0
        for position in portfolio.open_positions
    ):
        raise HTTPException(409, "验收前默认品种必须没有任何已有持仓")
    open_orders = await execution.open_orders()
    if any(order.symbol == configuration.default_symbol for order in open_orders):
        raise HTTPException(409, "验收前默认品种必须没有任何已有普通挂单")

    plan: StrategyPlan | None = None
    submitted_orders: list[Order] = []
    entry_orders: list[Order] = []
    reduction: Order | None = None
    final_exit: Order | None = None
    try:
        market, history = await runtime.market_context()
        strategy_history = closed_candles(
            history, configuration.default_timeframe, market.timestamp
        )
        await execution.validate_account_configuration()
        entry_price = market.ask * Decimal("1.01")
        stop_price = market.bid * Decimal("0.95")
        first_target = market.ask * Decimal("1.05")
        second_target = market.ask * Decimal("1.10")
        minimum = await execution.minimum_commission_quantity(
            configuration.default_symbol, entry_price
        )
        quantity = (minimum * Decimal("8")).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
        risk_amount = (quantity * (entry_price - stop_price)).quantize(
            Decimal("0.00000001"), rounding=ROUND_UP
        )
        notional = (quantity * entry_price).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
        template = strategy_factory.generate_unchecked(
            StrategyKind.TREND,
            candles=strategy_history,
            snapshot=market,
            equity=max(configuration.paper_equity, Decimal("100")),
            risk_fraction=Decimal("0.001"),
            exchange=configuration.exchange_id,
            timeframe=configuration.default_timeframe,
            minimum_reward_risk=Decimal("1"),
        )
        plan = template.model_copy(
            update={
                "id": f"agent-commission-{uuid4().hex}",
                "name": "OKX Agent 仓位管理验收",
                "side": Side.LONG,
                "trading_mode": TradingMode.SANDBOX,
                "market_type": configuration.market_type,
                "margin_mode": str(configuration.margin_mode),
                "leverage": configuration.contract_leverage,
                "status": PlanStatus.ACTIVE,
                "entry": EntryRule(
                    order_type=OrderType.LIMIT,
                    zone=PriceZone(lower=entry_price, upper=entry_price),
                    trigger="仅用于 OKX Demo 成交后保护单重整验收",
                    expires_at=datetime.now(UTC) + timedelta(minutes=2),
                ),
                "exit": ExitPlan(
                    stop_loss=stop_price,
                    take_profits=[
                        ProfitTarget(
                            price=first_target,
                            close_fraction=Decimal("0.5"),
                            rationale="验收第一组附加保护单",
                        ),
                        ProfitTarget(
                            price=second_target,
                            close_fraction=Decimal("0.5"),
                            rationale="验收第二组附加保护单",
                        ),
                    ],
                    invalidation="验收结束后必须完全平仓",
                ),
                "sizing": PositionSizing(
                    risk_fraction=Decimal("0.001"),
                    risk_amount=risk_amount,
                    suggested_quantity=quantity,
                    notional=notional,
                ),
                "minimum_reward_risk": Decimal("1"),
                "thesis": "测试网仓位管理协议验收，不属于交易策略信号。",
                "reasons": ["验证部分减仓、保护单重整、全平和空仓回查"],
            }
        )
        intent = OrderIntent(
            idempotency_key=f"cxagenttest{uuid4().hex[:14]}",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=entry_price,
        )
        submitted_orders = await execution.place_protected_order(plan, intent)

        async def wait_for_fill(order: Order) -> Order:
            latest = order
            for attempt in range(8):
                if latest.status == OrderStatus.FILLED:
                    return latest
                if latest.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
                    break
                if attempt < 7:
                    await asyncio.sleep(0.5)
                    latest = await execution.fetch_order(order.id, order.symbol)
            raise RuntimeError(f"测试网入场订单 {order.id} 未完整成交，状态 {latest.status}")

        entry_orders = list(
            await asyncio.gather(*(wait_for_fill(item) for item in submitted_orders))
        )
        current_quantity = sum((order.filled_quantity for order in entry_orders), Decimal("0"))
        if current_quantity <= 0:
            raise RuntimeError("测试网验收没有取得可核验的成交数量")
        partial_quantity = (entry_orders[-1].filled_quantity / Decimal("2")).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        if partial_quantity <= 0:
            raise RuntimeError("测试仓位不足以执行一次有效的部分减仓")
        reduction = await execution.reduce_position_safely(
            plan,
            entry_orders,
            OrderIntent(
                idempotency_key=f"cxagentreduce{uuid4().hex[:12]}",
                plan_id=plan.id,
                plan_fingerprint=plan.fingerprint,
                symbol=plan.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=partial_quantity,
                reduce_only=True,
            ),
            current_quantity,
        )
        remaining = current_quantity - reduction.filled_quantity
        if remaining <= 0:
            raise RuntimeError("部分减仓意外关闭了全部测试仓位")
        if not await execution.verify_position_protection(plan, entry_orders, remaining):
            raise RuntimeError("部分减仓后的原生保护数量未通过独立回查")
        final_exit = await execution.reduce_position_safely(
            plan,
            entry_orders,
            OrderIntent(
                idempotency_key=f"cxagentexit{uuid4().hex[:14]}",
                plan_id=plan.id,
                plan_fingerprint=plan.fingerprint,
                symbol=plan.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=remaining,
                reduce_only=True,
            ),
            remaining,
        )
        final_portfolio = await runtime.portfolio()
        position_flat = not any(
            position.symbol == plan.symbol and position.quantity > 0
            for position in final_portfolio.open_positions
        )
        protection_flat = await execution.verify_position_protection(
            plan, entry_orders, Decimal("0")
        )
        if not position_flat or not protection_flat:
            raise RuntimeError("最终空仓或保护单清理未通过独立回查")
    except Exception as exc:
        cleanup_errors: list[str] = []
        for submitted in submitted_orders:
            try:
                latest = await execution.fetch_order(submitted.id, submitted.symbol)
                if latest.status in {
                    OrderStatus.OPEN,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.PENDING,
                }:
                    await execution.cancel_order(latest.id, latest.symbol)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"cancel {submitted.id}: {str(cleanup_exc)[:80]}")
        if plan and submitted_orders:
            try:
                cleanup_portfolio = await runtime.portfolio()
                cleanup_quantity = sum(
                    (
                        position.quantity
                        for position in cleanup_portfolio.open_positions
                        if position.symbol == plan.symbol and position.side == plan.side
                    ),
                    Decimal("0"),
                )
                if cleanup_quantity > 0:
                    try:
                        await execution.reduce_position_safely(
                            plan,
                            submitted_orders,
                            OrderIntent(
                                idempotency_key=f"cxagentcleanup{uuid4().hex[:11]}",
                                plan_id=plan.id,
                                plan_fingerprint=plan.fingerprint,
                                symbol=plan.symbol,
                                side=OrderSide.SELL,
                                order_type=OrderType.MARKET,
                                quantity=cleanup_quantity,
                                reduce_only=True,
                            ),
                            cleanup_quantity,
                        )
                    except Exception as protected_cleanup_exc:
                        cleanup_errors.append(
                            f"protected cleanup: {str(protected_cleanup_exc)[:100]}"
                        )
                        refreshed = await runtime.portfolio()
                        cleanup_quantity = sum(
                            (
                                position.quantity
                                for position in refreshed.open_positions
                                if position.symbol == plan.symbol and position.side == plan.side
                            ),
                            Decimal("0"),
                        )
                        if cleanup_quantity > 0:
                            emergency = await execution.place_order(
                                OrderIntent(
                                    idempotency_key=(f"cxagentemergency{uuid4().hex[:9]}"),
                                    plan_id=plan.id,
                                    plan_fingerprint=plan.fingerprint,
                                    symbol=plan.symbol,
                                    side=OrderSide.SELL,
                                    order_type=OrderType.MARKET,
                                    quantity=cleanup_quantity,
                                    reduce_only=True,
                                )
                            )
                            if emergency.status != OrderStatus.FILLED:
                                emergency = await execution.fetch_order(
                                    emergency.id, emergency.symbol
                                )
                            if emergency.status != OrderStatus.FILLED:
                                cleanup_errors.append(f"emergency exit status: {emergency.status}")
                if not await execution.clear_position_protection(plan, submitted_orders):
                    cleanup_errors.append("native protection cleanup unconfirmed")
                cleanup_portfolio = await runtime.portfolio()
                if any(
                    position.symbol == plan.symbol and position.quantity > 0
                    for position in cleanup_portfolio.open_positions
                ):
                    cleanup_errors.append("position cleanup unconfirmed")
            except Exception as cleanup_exc:
                cleanup_errors.append(f"cleanup failure: {str(cleanup_exc)[:120]}")
        cleanup_status = (
            "；自动清理已确认"
            if not cleanup_errors
            else "；自动清理未完全确认：" + " | ".join(cleanup_errors)[:240]
        )
        risk_state_service.halt(
            "Agent 测试网仓位管理验收失败，必须人工核对持仓和 Algo 单："
            f"{str(exc)[:150]}{cleanup_status}",
            source="agent_commissioning",
        )
        add_audit(
            "commissioning",
            "Agent 测试网仓位管理验收失败",
            severity="critical",
            reason=str(exc)[:180],
            entry_order_ids=",".join(order.id for order in submitted_orders),
            reduction_order_id=reduction.id if reduction else "",
            exit_order_id=final_exit.id if final_exit else "",
            cleanup_status=cleanup_status,
        )
        raise HTTPException(502, str(exc)) from exc

    result = existing.model_copy(
        update={
            "symbol": configuration.default_symbol,
            "agent_reduction_verified": True,
            "agent_exit_verified": True,
            "position_flat_confirmed": True,
            "order_ids": list(
                dict.fromkeys(
                    [
                        *existing.order_ids,
                        *(order.id for order in entry_orders),
                        reduction.id,
                        final_exit.id,
                    ]
                )
            ),
            "message": ("OKX Demo 已确认真实成交、部分减仓保护重整、最终平仓和空仓状态"),
            "tested_at": datetime.now(UTC),
        }
    )
    store.save_commissioning(configuration.exchange_id, str(configuration.market_type), result)
    add_audit(
        "commissioning",
        "Agent 测试网仓位管理验收通过",
        exchange=configuration.exchange_id,
        market_type=str(configuration.market_type),
        order_count=len(entry_orders) + 2,
    )
    return result


@app.get("/api/dashboard")
async def dashboard():
    configuration = configuration_service.current_configuration()
    try:
        market, history = await runtime.market_context(limit=configuration.strategy.research_bars)
        portfolio = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"真实行情不可用：{exc}") from exc
    strategy_history = closed_candles(
        history,
        configuration.default_timeframe,
        market.timestamp,
    )
    current_strategy_kind = configured_strategy_kind(configuration, strategy_history)
    active_plan = ensure_plan(history, market, portfolio)
    signal_strategy_kind = (
        StrategyKind(active_plan.algorithm) if active_plan else current_strategy_kind
    )
    signal_kwargs = dict(
        candles=strategy_history,
        snapshot=market,
        timeframe=configuration.default_timeframe,
        allow_short=configuration.market_type == "swap",
        confirmation_candles=confirmation_history(
            strategy_history,
            configuration.default_timeframe,
        ),
        require_confirmation=requires_higher_confirmation(configuration),
    )
    if active_plan or not configuration.strategy.automatic_regime_selection:
        current_signal = strategy_factory.evaluate(
            signal_strategy_kind,
            **signal_kwargs,
            strategy_spec=(active_plan.strategy_spec or None) if active_plan else None,
        )
    else:
        signal_strategy_kind, current_signal, _ = strategy_factory.evaluate_ensemble(
            **signal_kwargs
        )
    if active_plan:
        decision = add_signal_gate(
            risk_engine().evaluate(active_plan, portfolio, market),
            active_plan,
            current_signal,
        )
    else:
        decision = RiskDecision(
            approved=False,
            checks=[
                RiskCheck(
                    code="actionable_plan",
                    passed=False,
                    message="当前没有满足条件的可执行计划，系统保持观望",
                )
            ],
        )
    execution = runtime.execution
    assert execution is not None
    if runtime.order_ledger and runtime.order_ledger.records:
        recent_orders = runtime.order_ledger.recent_orders(10)
    elif isinstance(execution, PaperExchange):
        recent_orders = list(execution.orders.values())[-10:][::-1]
    else:
        recent_orders = (await execution.open_orders())[-10:][::-1]
    backtest_history = closed_candles(
        history,
        active_plan.timeframe if active_plan else configuration.default_timeframe,
        market.timestamp,
    )
    backtest_exchange = active_plan.exchange if active_plan else configuration.exchange_id
    backtest_symbol = active_plan.symbol if active_plan else configuration.default_symbol
    backtest_timeframe = active_plan.timeframe if active_plan else configuration.default_timeframe
    dataset, funding_rates, open_interest, positioning, basis = await materialize_research_dataset(
        backtest_history,
        exchange=backtest_exchange,
        market_type=str(configuration.market_type),
        symbol=backtest_symbol,
        timeframe=backtest_timeframe,
    )
    strategy_spec = (active_plan.strategy_spec or None) if active_plan else None
    cache_key = (
        dataset.id,
        active_plan.fingerprint if active_plan else str(signal_strategy_kind),
        configuration.fee_bps,
        configuration.slippage_bps,
        configuration.risk.risk_per_trade,
        configuration.risk.minimum_reward_risk,
        requires_higher_confirmation(configuration),
        configuration.strategy.automatic_regime_selection,
        configuration.strategy.validation.model_dump_json(),
    )
    backtest = await execute_research_backtest(
        dataset,
        backtest_history,
        cache_key=cache_key,
        initial_equity=portfolio.equity,
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        strategy_kind=(
            StrategyKind(active_plan.algorithm) if active_plan else signal_strategy_kind
        ),
        symbol=backtest_symbol,
        timeframe=backtest_timeframe,
        exchange=backtest_exchange,
        risk_fraction=(
            active_plan.sizing.risk_fraction if active_plan else configuration.risk.risk_per_trade
        ),
        minimum_reward_risk=(
            active_plan.minimum_reward_risk
            if active_plan
            else configuration.risk.minimum_reward_risk
        ),
        allow_short=configuration.market_type == "swap",
        require_confirmation=requires_higher_confirmation(configuration),
        validation=configuration.strategy.validation,
        strategy_spec=strategy_spec,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        automatic_selection=(
            configuration.strategy.automatic_regime_selection and active_plan is None
        ),
        side_filter=Side(active_plan.side) if active_plan else None,
    )
    if active_plan:
        backtest.strategy_fingerprint = active_plan.fingerprint
    latency_ms = max(round((datetime.now(UTC) - market.timestamp).total_seconds() * 1000), 0)
    quality = runtime.market_quality
    return {
        "market": market,
        "candles": history[-CHART_CANDLE_LIMIT:],
        "signal": current_signal,
        "portfolio": portfolio,
        "plan": active_plan,
        "risk": decision,
        "orders": recent_orders,
        "audit": audit[:12],
        "backtest": backtest,
        "system": {
            "mode": configuration.trading_mode,
            "market_type": configuration.market_type,
            "margin_mode": configuration.margin_mode,
            "contract_leverage": configuration.contract_leverage,
            "live_locked": not configuration.live_trading_enabled,
            "data_latency_ms": latency_ms,
            "strategy_workers": 1,
            "market_data_real": True,
            "exchange": configuration.exchange_id,
            "market_quality": quality,
            "risk_state": risk_state_service.get(),
            "reconciliation": runtime.reconciliation_report,
            "reconciliation_health": runtime.reconciliation_health,
            "private_order_stream": runtime.private_stream_status,
        },
    }


@app.get("/api/market")
async def market_window(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 300,
):
    configuration = configuration_service.current_configuration()
    selected_symbol = (symbol or configuration.default_symbol).upper()
    selected_timeframe = timeframe or configuration.default_timeframe
    if selected_symbol not in configuration.strategy.watchlist:
        raise HTTPException(400, "交易品种不在已配置的观察列表中")
    if selected_timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(400, "不支持的 K 线周期")
    if not 60 <= limit <= 500:
        raise HTTPException(400, "K 线数量必须在 60–500 之间")
    try:
        market, history = await runtime.market_context(
            limit=limit,
            symbol=selected_symbol,
            timeframe=selected_timeframe,
        )
    except Exception as exc:
        raise HTTPException(503, f"真实行情不可用：{exc}") from exc
    return {
        "market": market,
        "candles": history[-limit:],
        "timeframe": selected_timeframe,
        "quality": runtime.market_quality,
    }


@app.get("/api/market-data/liquidations/status")
async def liquidation_collection_status():
    if liquidation_collector is None:
        return {
            "running": False,
            "historical_backfill": False,
            "sampling": "latest_per_symbol_within_1000ms",
            "message": "collector has not started",
        }
    sessions_payload, event_summaries = await asyncio.gather(
        asyncio.to_thread(
            partial(
                store.list_market_data_capture_sessions,
                source="binance",
                stream=BINANCE_LIQUIDATION_STREAM,
            )
        ),
        asyncio.to_thread(store.summarize_liquidation_events),
    )
    sessions = [MarketDataCaptureSession.model_validate(payload) for payload in sessions_payload]
    now = datetime.now(UTC)
    coverage = [
        calculate_capture_coverage(
            sessions,
            source="binance",
            stream=BINANCE_LIQUIDATION_STREAM,
            symbol=symbol,
            now=now,
        ).model_dump(mode="json")
        for symbol in sorted(liquidation_collector.symbols)
    ]
    return {
        **liquidation_collector.status(),
        "durable_capture_coverage": coverage,
        "event_summaries": event_summaries,
    }


@app.get(
    "/api/market-data/liquidations",
    response_model=list[LiquidationEvent],
)
async def liquidation_events(symbol: str | None = None, limit: int = 1000):
    normalized = symbol.upper().replace("-", "/") if symbol else None
    payloads = await asyncio.to_thread(
        partial(
            store.list_liquidation_events,
            symbol=normalized,
            limit=limit,
        )
    )
    return [LiquidationEvent.model_validate(item) for item in payloads]


@app.websocket("/api/stream/market")
async def stream_market(websocket: WebSocket):
    """Push the current exchange tick and forming candle to the browser."""

    await websocket.accept()
    queue = runtime.market_data.subscribe()
    try:
        configuration = configuration_service.current_configuration()
        symbol = websocket.query_params.get("symbol", configuration.default_symbol).upper()
        timeframe = websocket.query_params.get("timeframe", configuration.default_timeframe)
        if symbol not in configuration.strategy.watchlist or timeframe not in TIMEFRAME_SECONDS:
            await websocket.close(code=1008, reason="unsupported market subscription")
            return
        await runtime.market_context(
            limit=300,
            symbol=symbol,
            timeframe=timeframe,
        )
        initial = runtime.market_data.stream_event(symbol, timeframe)
        if initial:
            await websocket.send_json(initial)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10)
                candle = event["candles"].get(timeframe)
                if candle and event["market"]["symbol"] == symbol:
                    await websocket.send_json(event)
            except TimeoutError:
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "server_time": datetime.now(UTC).isoformat(),
                    }
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc)[:180]})
            await websocket.close(code=1011)
    finally:
        runtime.market_data.unsubscribe(queue)


@app.get("/api/strategies", response_model=list[StrategyPlan])
async def list_strategies():
    return list(plans.values())


@app.get("/api/research/strategy-spec")
async def strategy_spec_contract():
    return {
        "schema": strategy_spec_json_schema(),
        "defaults": {
            kind.value: default_strategy_spec(kind).model_dump(mode="json") for kind in StrategyKind
        },
    }


@app.post("/api/research/autonomous-runs", response_model=ResearchRunResult)
async def create_autonomous_research_run(request: AutonomousResearchRunRequest):
    envelope = None
    if request.generator_envelope_id:
        envelope_payload = await asyncio.to_thread(
            store.load_generator_envelope, request.generator_envelope_id
        )
        if envelope_payload is None:
            raise HTTPException(404, "generator envelope not found")
        envelope = GeneratorEnvelopeCertificate.model_validate(envelope_payload)
    engine = FastGateEngine(
        store,
        runtime_build_identity,
        envelope=envelope,
    )
    return await asyncio.to_thread(engine.run, request)


@app.get(
    "/api/research/autonomous-runs/{run_id}",
    response_model=ResearchRunResult,
)
async def autonomous_research_run_result(run_id: str):
    payload = await asyncio.to_thread(store.load_autonomous_research_event, run_id)
    if payload is None:
        raise HTTPException(404, "autonomous research run not found")
    if "disposition" not in payload:
        raise HTTPException(409, "autonomous research decision is not committed")
    return ResearchRunResult.model_validate(payload)


@app.get("/api/research/trials")
async def permanent_trial_ledger(family_id: str | None = None, limit: int = 100):
    trials = await asyncio.to_thread(
        partial(
            store.list_research_trials,
            family_id=family_id,
            limit=min(max(limit, 1), 1_000),
        )
    )
    # Never expose return statistics or sealed blind attribution through the
    # public/Agent-facing ledger DTO.
    return [
        {
            "id": trial["id"],
            "research_event_id": trial["research_event_id"],
            "hypothesis_id": trial["hypothesis_id"],
            "family_id": trial["family_id"],
            "candidate_id": trial["candidate_id"],
            "trial_type": trial["trial_type"],
            "result": trial.get("result", "pending"),
            "reason_codes": trial.get("reason_codes", []),
            "created_at": trial["created_at"],
        }
        for trial in trials
    ]


@app.get("/api/research/certificates/{certificate_id}")
async def research_certificate_status(certificate_id: str):
    payload = await asyncio.to_thread(store.load_research_certificate_status, certificate_id)
    if payload is None:
        raise HTTPException(404, "research certificate not found")
    return payload


@app.get("/api/research/datasets", response_model=list[ResearchDataset])
async def research_datasets(limit: int = 50):
    bounded_limit = min(max(limit, 1), 200)
    payloads = await asyncio.to_thread(store.list_research_datasets, bounded_limit)
    return [ResearchDataset.model_validate(payload) for payload in payloads]


def ai_market_summary(market, history, symbol: str, timeframe: str) -> dict:
    candles = closed_candles(history, timeframe, market.timestamp)[-300:]
    if len(candles) < 50:
        raise ValueError("至少需要 50 根已收盘 K 线才能生成 AI 策略候选")
    closes = [candle.close for candle in candles]
    returns = [
        (current / previous - Decimal("1"))
        for previous, current in zip(closes, closes[1:], strict=False)
        if previous
    ]
    average_return = sum(returns, Decimal("0")) / max(len(returns), 1)
    average_abs_deviation = sum((value - average_return).copy_abs() for value in returns) / max(
        len(returns), 1
    )
    recent_volume = sum((item.volume for item in candles[-20:]), Decimal("0")) / 20
    baseline_volume = sum((item.volume for item in candles[-100:]), Decimal("0")) / min(
        len(candles), 100
    )
    selected_kind = select_strategy_kind(candles)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(candles),
        "data_start": candles[0].timestamp.isoformat(),
        "data_end": candles[-1].timestamp.isoformat(),
        "market_timestamp": market.timestamp.isoformat(),
        "last_price": str(market.last),
        "spread_bps": str(market.spread_bps),
        "funding_rate": str(market.funding_rate) if market.funding_rate is not None else None,
        "return_20_bars": str(closes[-1] / closes[-20] - Decimal("1")),
        "return_100_bars": str(closes[-1] / closes[-100] - Decimal("1")),
        "mean_absolute_return": str(average_abs_deviation),
        "recent_high": str(max(item.high for item in candles[-50:])),
        "recent_low": str(min(item.low for item in candles[-50:])),
        "volume_ratio_20_to_100": str(
            recent_volume / baseline_volume if baseline_volume else Decimal("0")
        ),
        "deterministic_regime": str(selected_kind),
    }


@app.get("/api/research/strategy-library")
async def builtin_strategy_library(family: str | None = None):
    try:
        return unified_research_strategy_library(family)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def normalize_personal_strategy_spec(raw_spec: dict):
    payload = dict(raw_spec)
    payload.pop("fingerprint", None)
    payload["source"] = "operator"
    return validate_strategy_spec(payload)


def personal_strategy_view(record: PersonalStrategyRecord) -> StrategyLibraryItem:
    referenced = any(
        plan.causal_policy_selected_library_entry_id == record.id for plan in plans.values()
    )
    return StrategyLibraryItem(
        id=record.id,
        name=record.name,
        description=record.description,
        origin="personal",
        family=record.family,
        status=record.status,
        version=record.version,
        strategy_spec=record.strategy_spec,
        spec_fingerprint=record.spec_fingerprint,
        executable=False,
        can_edit=record.status == "draft",
        can_archive=record.status != "archived",
        can_restore=record.status == "archived",
        can_delete=record.status == "draft" and not referenced,
        evidence_summary=(
            "已被交易计划引用，仅允许归档" if referenced else "个人草稿，执行前必须完成回测认证"
        ),
        updated_at=record.updated_at,
    )


def unified_operator_strategy_library() -> list[StrategyLibraryItem]:
    hidden = store.hidden_strategy_library_items()
    items: list[StrategyLibraryItem] = []
    for entry in list_strategy_library():
        validated = entry.validated_spec
        items.append(
            StrategyLibraryItem(
                id=entry.id,
                name=entry.name,
                description=entry.summary,
                origin="builtin",
                family=str(entry.family),
                status="available",
                version=entry.version,
                strategy_spec=validated.model_dump(mode="json", exclude={"fingerprint"}),
                spec_fingerprint=validated.fingerprint,
                executable=False,
                hidden=entry.id in hidden,
                can_archive=entry.id not in hidden,
                can_restore=entry.id in hidden,
                can_delete=False,
                evidence_summary="公开先验；使用前必须通过不可变数据集与稳健性验证",
            )
        )
    for payload in store.list_personal_strategy_library_items(1000):
        items.append(personal_strategy_view(PersonalStrategyRecord.model_validate(payload)))
    for payload in store.list_strategy_candidates(500):
        try:
            candidate = StrategyCandidate.model_validate(payload)
        except ValidationError:
            continue
        if candidate.source == "system":
            continue
        items.append(
            StrategyLibraryItem(
                id=candidate.id,
                name=f"{str(candidate.spec.get('family', 'strategy'))} · {candidate.id[-8:]}",
                description=str(candidate.spec.get("hypothesis") or "Agent 研究候选"),
                origin="agent",
                family=str(candidate.spec.get("family") or "unknown"),
                status=str(candidate.status),
                version=1,
                strategy_spec=candidate.spec,
                spec_fingerprint=candidate.spec_fingerprint,
                executable=bool(
                    candidate.status == CandidateStatus.CHAMPION
                    and candidate.execution_artifact is not None
                ),
                can_archive=candidate.status
                not in {CandidateStatus.RETIRED, CandidateStatus.CHAMPION},
                can_restore=candidate.status == CandidateStatus.RETIRED,
                can_delete=False,
                evidence_summary=(
                    f"PF {candidate.backtest.profit_factor:f} · "
                    f"收益 {candidate.backtest.return_percent:f}%"
                    if candidate.backtest
                    else "尚未绑定不可变数据集回测"
                ),
                updated_at=candidate.updated_at,
            )
        )
    origin_order = {"personal": 0, "agent": 1, "builtin": 2}
    return sorted(
        items,
        key=lambda item: (
            origin_order[item.origin],
            -(item.updated_at.timestamp() if item.updated_at else 0),
            item.name,
        ),
    )


@app.get("/api/strategy-library", response_model=StrategyLibraryPage)
async def operator_strategy_library(
    origin: Literal["builtin", "personal", "agent"] | None = None,
    status: str | None = None,
    family: str | None = None,
    include_hidden: bool = False,
    search: str | None = None,
):
    items = await asyncio.to_thread(unified_operator_strategy_library)
    if origin:
        items = [item for item in items if item.origin == origin]
    if status:
        items = [item for item in items if item.status == status]
    if family:
        items = [item for item in items if item.family == family]
    if not include_hidden:
        items = [item for item in items if not item.hidden]
    if search:
        needle = search.casefold()
        items = [
            item
            for item in items
            if needle in item.name.casefold()
            or needle in item.description.casefold()
            or needle in item.id.casefold()
        ]
    return StrategyLibraryPage(items=items, total=len(items))


@app.post("/api/strategy-library", response_model=StrategyLibraryItem)
async def create_personal_strategy(request: PersonalStrategyCreate):
    try:
        spec = normalize_personal_strategy_spec(request.strategy_spec)
    except ValueError as exc:
        raise HTTPException(422, f"StrategySpec 无效：{exc}") from exc
    now = datetime.now(UTC)
    record = PersonalStrategyRecord(
        name=request.name.strip(),
        description=request.description.strip(),
        family=str(spec.family),
        strategy_spec=spec.model_dump(mode="json", exclude={"fingerprint"}),
        spec_fingerprint=spec.fingerprint,
        origin_ref=request.origin_ref,
        created_at=now,
        updated_at=now,
    )
    await asyncio.to_thread(store.save_personal_strategy_library_item, record)
    add_audit(
        "strategy",
        "个人策略已加入策略库",
        item_id=record.id,
        family=record.family,
        origin_ref=record.origin_ref,
    )
    return personal_strategy_view(record)


@app.patch("/api/strategy-library/{item_id}", response_model=StrategyLibraryItem)
async def update_personal_strategy(item_id: str, request: PersonalStrategyUpdate):
    payload = await asyncio.to_thread(store.load_personal_strategy_library_item, item_id)
    if not payload:
        raise HTTPException(404, "personal strategy not found")
    current = PersonalStrategyRecord.model_validate(payload)
    if current.status != "draft":
        raise HTTPException(409, "只有个人草稿可以直接编辑")
    spec = normalize_personal_strategy_spec(request.strategy_spec or current.strategy_spec)
    updated = current.model_copy(
        deep=True,
        update={
            "name": request.name.strip() if request.name is not None else current.name,
            "description": (
                request.description.strip()
                if request.description is not None
                else current.description
            ),
            "family": str(spec.family),
            "strategy_spec": spec.model_dump(mode="json", exclude={"fingerprint"}),
            "spec_fingerprint": spec.fingerprint,
            "updated_at": datetime.now(UTC),
        },
    )
    await asyncio.to_thread(store.save_personal_strategy_library_item, updated)
    add_audit("strategy", "个人策略草稿已更新", item_id=updated.id)
    return personal_strategy_view(updated)


@app.post("/api/strategy-library/{item_id}/archive", response_model=StrategyLibraryItem)
async def archive_strategy_library_item(item_id: str):
    personal_payload = await asyncio.to_thread(
        store.load_personal_strategy_library_item,
        item_id,
    )
    if personal_payload:
        record = PersonalStrategyRecord.model_validate(personal_payload)
        record.status = "archived"
        record.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_personal_strategy_library_item, record)
        add_audit("strategy", "个人策略已归档", severity="warning", item_id=item_id)
        return personal_strategy_view(record)
    candidate_payload = await asyncio.to_thread(store.load_strategy_candidate, item_id)
    if candidate_payload:
        candidate = StrategyCandidate.model_validate(candidate_payload)
        if candidate.source == "system":
            raise HTTPException(409, "内置候选不可归档，请隐藏对应公开模板")
        if candidate.status == CandidateStatus.CHAMPION:
            raise HTTPException(409, "当前 Champion 必须先由新的 Champion 替代")
        candidate.status = CandidateStatus.RETIRED
        candidate.execution_artifact = None
        candidate.updated_at = datetime.now(UTC)
        candidate.decision_reasons.append("操作员从策略库归档该候选")
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
        add_audit("strategy", "Agent 策略候选已归档", severity="warning", item_id=item_id)
        return next(
            item
            for item in await asyncio.to_thread(unified_operator_strategy_library)
            if item.id == item_id
        )
    try:
        library_entry(item_id)
    except LookupError as exc:
        raise HTTPException(404, "strategy library item not found") from exc
    await asyncio.to_thread(store.set_strategy_library_hidden, item_id, True)
    return next(
        item
        for item in await asyncio.to_thread(unified_operator_strategy_library)
        if item.id == item_id
    )


@app.post("/api/strategy-library/{item_id}/restore", response_model=StrategyLibraryItem)
async def restore_strategy_library_item(item_id: str):
    personal_payload = await asyncio.to_thread(
        store.load_personal_strategy_library_item,
        item_id,
    )
    if personal_payload:
        record = PersonalStrategyRecord.model_validate(personal_payload)
        if record.status != "archived":
            raise HTTPException(409, "strategy is not archived")
        record.status = "draft"
        record.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_personal_strategy_library_item, record)
        add_audit("strategy", "个人策略已恢复", item_id=item_id)
        return personal_strategy_view(record)
    candidate_payload = await asyncio.to_thread(store.load_strategy_candidate, item_id)
    if candidate_payload:
        candidate = StrategyCandidate.model_validate(candidate_payload)
        if candidate.status != CandidateStatus.RETIRED or candidate.source == "system":
            raise HTTPException(409, "strategy candidate cannot be restored")
        candidate.status = CandidateStatus.PROPOSED
        candidate.updated_at = datetime.now(UTC)
        candidate.decision_reasons.append("操作员恢复候选；必须重新完成评估")
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
        return next(
            item
            for item in await asyncio.to_thread(unified_operator_strategy_library)
            if item.id == item_id
        )
    try:
        library_entry(item_id)
    except LookupError as exc:
        raise HTTPException(404, "strategy library item not found") from exc
    await asyncio.to_thread(store.set_strategy_library_hidden, item_id, False)
    return next(
        item
        for item in await asyncio.to_thread(unified_operator_strategy_library)
        if item.id == item_id
    )


@app.put("/api/strategy-library/{item_id}/preference", response_model=StrategyLibraryItem)
async def set_strategy_library_preference(
    item_id: str,
    request: StrategyLibraryPreference,
):
    try:
        library_entry(item_id)
    except LookupError as exc:
        raise HTTPException(404, "built-in strategy not found") from exc
    await asyncio.to_thread(store.set_strategy_library_hidden, item_id, request.hidden)
    return next(
        item
        for item in await asyncio.to_thread(unified_operator_strategy_library)
        if item.id == item_id
    )


@app.delete("/api/strategy-library/{item_id}")
async def delete_personal_strategy(item_id: str):
    payload = await asyncio.to_thread(store.load_personal_strategy_library_item, item_id)
    if not payload:
        raise HTTPException(404, "personal strategy not found")
    record = PersonalStrategyRecord.model_validate(payload)
    view = personal_strategy_view(record)
    if not view.can_delete:
        raise HTTPException(409, "该策略已有证据引用，只能归档")
    await asyncio.to_thread(store.delete_personal_strategy_library_item, item_id)
    add_audit(
        "strategy",
        "未引用的个人策略草稿已永久删除",
        severity="warning",
        item_id=item_id,
    )
    return {"deleted": True, "item_id": item_id}


ResearchDatasetBundle = tuple[
    ResearchDataset,
    list[Candle],
    list[FundingRatePoint],
    list[OpenInterestPoint],
    list[PositioningPoint],
    list[BasisPoint],
    list[OrderFlowBar],
    list[LiquidationFlowPoint],
]


async def load_verified_research_dataset(dataset_id: str) -> ResearchDatasetBundle:
    dataset_payload = await asyncio.to_thread(
        store.load_research_dataset,
        dataset_id,
    )
    if not dataset_payload:
        raise HTTPException(404, f"research dataset not found: {dataset_id}")
    dataset = ResearchDataset.model_validate(dataset_payload)
    (
        candle_payloads,
        funding_payloads,
        open_interest_payloads,
        positioning_payloads,
        basis_payloads,
        order_flow_payloads,
        liquidation_flow_payloads,
    ) = await asyncio.gather(
        asyncio.to_thread(store.load_research_dataset_candles, dataset_id),
        asyncio.to_thread(store.load_research_dataset_funding, dataset_id),
        asyncio.to_thread(store.load_research_dataset_open_interest, dataset_id),
        asyncio.to_thread(store.load_research_dataset_positioning, dataset_id),
        asyncio.to_thread(store.load_research_dataset_basis, dataset_id),
        asyncio.to_thread(store.load_research_dataset_order_flow, dataset_id),
        asyncio.to_thread(store.load_research_dataset_liquidation_flow, dataset_id),
    )
    candles = [Candle.model_validate(item) for item in candle_payloads]
    funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
    open_interest = [OpenInterestPoint.model_validate(item) for item in open_interest_payloads]
    positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
    basis = [BasisPoint.model_validate(item) for item in basis_payloads]
    order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
    liquidation_flow = [
        LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
    ]
    verification = verify_research_dataset(
        dataset,
        candles,
        funding,
        open_interest,
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    )
    if not verification["verified"]:
        raise HTTPException(409, f"research dataset integrity failed: {dataset_id}")
    return (
        dataset,
        candles,
        funding,
        open_interest,
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    )


def causal_strategy_prior_from_library_entry(entry) -> CausalStrategyPrior:
    return CausalStrategyPrior(
        id=entry.id,
        family=entry.family,
        archetype=entry.archetype,
        preferred_regimes=entry.preferred_regimes,
        avoid_regimes=entry.avoid_regimes,
        required_data=entry.required_data,
        spec=entry.spec,
        spec_fingerprint=entry.validated_spec.fingerprint,
    )


@dataclass(frozen=True)
class _CampaignPolicyFamily:
    value: str = "agent_policy"


@dataclass(frozen=True)
class _CampaignPolicyFingerprint:
    fingerprint: str


@dataclass(frozen=True)
class CausalPolicyCampaignEntry:
    id: str
    policy_id: str
    priors: tuple[CausalStrategyPrior, ...]
    maximum_candidates_per_bar: int
    abstain_on_ambiguous_regime: bool
    minimum_regime_confidence: Decimal
    minimum_signal_confidence: Decimal
    minimum_directional_consensus: int
    policy_fingerprint: str

    @property
    def family(self) -> _CampaignPolicyFamily:
        return _CampaignPolicyFamily()

    @property
    def archetype(self) -> str:
        return "causal_regime_library_meta_policy"

    @property
    def required_data(self) -> list[str]:
        # A certificate must not enable a branch that Campaign evidence never
        # exercised. Therefore every feature required by any frozen prior is a
        # policy-wide historical coverage requirement. The single-dataset
        # development screen may still explore partial availability, but a
        # formal Campaign cell becomes ineligible when this union is missing.
        return sorted({feature for prior in self.priors for feature in prior.required_data})

    @property
    def validated_spec(self) -> _CampaignPolicyFingerprint:
        return _CampaignPolicyFingerprint(self.policy_fingerprint)

    @property
    def frozen_definition(self) -> dict:
        return {
            "campaign_entry_id": self.id,
            "policy_id": self.policy_id,
            "policy_version": CAUSAL_LIBRARY_POLICY_VERSION,
            "maximum_candidates_per_bar": self.maximum_candidates_per_bar,
            "abstain_on_ambiguous_regime": self.abstain_on_ambiguous_regime,
            "minimum_regime_confidence": str(self.minimum_regime_confidence),
            "minimum_signal_confidence": str(self.minimum_signal_confidence),
            "minimum_directional_consensus": self.minimum_directional_consensus,
            "selection_inputs": ("closed_bar_regime_and_latest_known_causal_features_only"),
            "realized_return_inputs_forbidden": True,
            "priors": [
                prior.model_dump(mode="json")
                for prior in sorted(self.priors, key=lambda item: item.id)
            ],
            "policy_fingerprint": self.policy_fingerprint,
        }


def build_causal_policy_campaign_entry(
    policy: CausalPolicyCampaignSpec,
) -> CausalPolicyCampaignEntry:
    entries = [library_entry(entry_id) for entry_id in policy.library_entry_ids]
    priors = tuple(causal_strategy_prior_from_library_entry(entry) for entry in entries)
    policy_fingerprint = causal_library_policy_fingerprint(
        list(priors),
        maximum_candidates_per_bar=policy.maximum_candidates_per_bar,
        abstain_on_ambiguous_regime=policy.abstain_on_ambiguous_regime,
        minimum_regime_confidence=policy.minimum_regime_confidence,
        minimum_signal_confidence=policy.minimum_signal_confidence,
        minimum_directional_consensus=policy.minimum_directional_consensus,
    )
    return CausalPolicyCampaignEntry(
        id=f"causal_policy:{policy.id}",
        policy_id=policy.id,
        priors=priors,
        maximum_candidates_per_bar=policy.maximum_candidates_per_bar,
        abstain_on_ambiguous_regime=policy.abstain_on_ambiguous_regime,
        minimum_regime_confidence=policy.minimum_regime_confidence,
        minimum_signal_confidence=policy.minimum_signal_confidence,
        minimum_directional_consensus=policy.minimum_directional_consensus,
        policy_fingerprint=policy_fingerprint,
    )


async def backtest_causal_policy_entry_on_dataset(
    entry: CausalPolicyCampaignEntry,
    dataset: ResearchDataset,
    candles: list[Candle],
    funding_rates: list[FundingRatePoint],
    open_interest: list[OpenInterestPoint],
    *,
    positioning: list[PositioningPoint],
    basis: list[BasisPoint],
    order_flow: list[OrderFlowBar],
    liquidation_flow: list[LiquidationFlowPoint],
    research_trial_count: int,
) -> BacktestResult:
    configuration = configuration_service.current_configuration()
    result = await execute_research_backtest(
        dataset,
        candles,
        initial_equity=configuration.paper_equity,
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        exchange=dataset.exchange,
        risk_fraction=configuration.risk.risk_per_trade,
        minimum_reward_risk=configuration.risk.minimum_reward_risk,
        allow_short=dataset.market_type == "swap",
        require_confirmation=False,
        validation=configuration.strategy.validation,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        causal_strategy_priors=list(entry.priors),
        causal_policy_max_candidates_per_bar=(entry.maximum_candidates_per_bar),
        causal_policy_abstain_on_ambiguous_regime=(entry.abstain_on_ambiguous_regime),
        causal_policy_minimum_regime_confidence=(entry.minimum_regime_confidence),
        causal_policy_minimum_signal_confidence=(entry.minimum_signal_confidence),
        causal_policy_minimum_directional_consensus=(entry.minimum_directional_consensus),
        research_trial_count=research_trial_count,
        require_regime_family_fit=True,
    )
    if result.strategy_fingerprint != entry.policy_fingerprint:
        raise RuntimeError("causal policy backtest fingerprint binding failed")
    result.integrity_checks.update(
        {
            "causal_policy_campaign_hypothesis_pre_registered": True,
            "causal_policy_campaign_certificate_eligible_path": True,
        }
    )
    return result


@app.post(
    "/api/research/causal-policy/backtest",
    response_model=BacktestResult,
)
async def backtest_causal_strategy_policy(
    request: CausalPolicyBacktestRequest,
) -> BacktestResult:
    (
        dataset,
        candles,
        funding,
        open_interest,
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    ) = await load_verified_research_dataset(request.dataset_id)
    if dataset.research_role == "blind" or dataset.blind_sealed:
        raise HTTPException(422, "sealed blind datasets cannot be used for policy research")
    try:
        entry = build_causal_policy_campaign_entry(
            CausalPolicyCampaignSpec(
                id="single_dataset_development_screen",
                library_entry_ids=request.library_entry_ids,
                maximum_candidates_per_bar=request.maximum_candidates_per_bar,
                abstain_on_ambiguous_regime=(request.abstain_on_ambiguous_regime),
                minimum_regime_confidence=request.minimum_regime_confidence,
                minimum_signal_confidence=request.minimum_signal_confidence,
                minimum_directional_consensus=(request.minimum_directional_consensus),
            )
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    historical_campaigns = await asyncio.to_thread(store.list_research_campaigns, 1000)
    minimum_trial_count = (
        max(
            (
                int(item.get("effective_trial_count") or item.get("trial_count") or 1)
                for item in historical_campaigns
            ),
            default=0,
        )
        + 1
    )
    effective_trial_count = max(request.research_trial_count, minimum_trial_count)
    result = await backtest_causal_policy_entry_on_dataset(
        entry,
        dataset,
        candles,
        funding,
        open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        research_trial_count=effective_trial_count,
    )
    result.cost_model["causal_policy_effective_trial_count"] = str(effective_trial_count)
    result.cost_model["causal_policy_evidence_role"] = (
        "single_dataset_development_screen_not_certificate_eligible"
    )
    result.integrity_checks["causal_policy_campaign_certificate_eligible_path"] = False
    result.integrity_checks["causal_policy_campaign_required_for_certificate"] = True
    add_audit(
        "research",
        "Agent因果动态选策回测完成",
        dataset_id=dataset.id,
        policy_fingerprint=result.strategy_fingerprint,
        prior_count=len(entry.priors),
        effective_trial_count=effective_trial_count,
        total_trades=result.total_trades,
        robustness_passed=result.robustness_passed,
    )
    return result


def campaign_blind_opening_allowed(
    *,
    provisional_selected,
    research_pbo,
    blind_dataset_ids: list[str],
    survivorship_bias_control_passed: bool,
) -> bool:
    """Keep sealed evidence closed until selection bias is controlled.

    Development-only exploration may omit a point-in-time universe. A sealed
    blind set is a one-shot evidence asset, however, so a development winner
    and PBO pass are insufficient unless the exact market universe also proves
    point-in-time selection and historical/prospective survivorship control.
    """

    return bool(
        provisional_selected is not None
        and research_pbo.passed
        and blind_dataset_ids
        and survivorship_bias_control_passed
    )


async def execute_research_campaign(
    request: ResearchCampaignRequest,
    *,
    progress_callback=None,
) -> ResearchCampaignReport:
    """Run a complete matrix with durable, versioned cell reuse."""

    research_dataset_ids = list(dict.fromkeys(request.dataset_ids))
    blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
    overlap = set(research_dataset_ids) & set(blind_dataset_ids)
    if overlap:
        raise HTTPException(
            422,
            "research and blind dataset IDs must be disjoint",
        )
    dataset_ids = [*research_dataset_ids, *blind_dataset_ids]
    if len(dataset_ids) > 60:
        raise HTTPException(422, "campaign supports at most 60 total datasets")
    dataset_metadata: dict[str, ResearchDataset] = {}
    for dataset_id in dataset_ids:
        payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
        if not payload:
            raise HTTPException(404, f"research dataset not found: {dataset_id}")
        dataset_metadata[dataset_id] = ResearchDataset.model_validate(payload)
    if any(
        dataset_metadata[dataset_id].research_role == "blind" for dataset_id in research_dataset_ids
    ):
        raise HTTPException(422, "sealed blind datasets cannot be used for strategy selection")
    invalid_blind_ids = [
        dataset_id
        for dataset_id in blind_dataset_ids
        if dataset_metadata[dataset_id].research_role != "blind"
        or not dataset_metadata[dataset_id].blind_sealed
        or not dataset_metadata[dataset_id].blind_set_id
    ]
    if invalid_blind_ids:
        raise HTTPException(
            422,
            "blind datasets must be role-bound, fingerprinted and sealed: "
            + ", ".join(invalid_blind_ids),
        )
    blind_set_ids = {dataset_metadata[dataset_id].blind_set_id for dataset_id in blind_dataset_ids}
    if len(blind_set_ids) > 1:
        raise HTTPException(422, "blind datasets must belong to one sealed blind set")
    blind_set_id: str | None = None
    if blind_dataset_ids:
        blind_set_id = next(iter(blind_set_ids))
        invalid_development_ids = [
            dataset_id
            for dataset_id in research_dataset_ids
            if dataset_metadata[dataset_id].research_role != "development"
            or dataset_metadata[dataset_id].blind_set_id != blind_set_id
        ]
        if invalid_development_ids:
            raise HTTPException(
                422,
                "blind Campaign research datasets must be development splits "
                "from the same sealed set: " + ", ".join(invalid_development_ids),
            )
        development_sources = {
            dataset_metadata[dataset_id].source_dataset_id for dataset_id in research_dataset_ids
        }
        blind_sources = {
            dataset_metadata[dataset_id].source_dataset_id for dataset_id in blind_dataset_ids
        }
        if development_sources != blind_sources:
            raise HTTPException(
                422,
                "development and blind datasets must contain matching source markets",
            )
    universe_snapshot = None
    point_in_time_selection_passed = False
    historical_survivorship_complete = False
    survivorship_bias_control_passed = False
    if request.universe_snapshot_id:
        universe_payload = await asyncio.to_thread(
            store.load_research_universe_snapshot,
            request.universe_snapshot_id,
        )
        if universe_payload is None:
            raise HTTPException(404, "research universe snapshot not found")
        universe_snapshot = ResearchUniverseSnapshot.model_validate(universe_payload)
        try:
            validate_universe_snapshot(universe_snapshot)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        expected_market_keys = {
            (
                item.entry.exchange,
                item.entry.symbol,
                timeframe,
                item.entry.market_type,
            )
            for item in universe_snapshot.instruments
            if item.selected
            for timeframe in universe_snapshot.policy.timeframes
        }
        actual_research_keys = {
            (
                dataset_metadata[dataset_id].exchange,
                dataset_metadata[dataset_id].symbol,
                dataset_metadata[dataset_id].timeframe,
                dataset_metadata[dataset_id].market_type,
            )
            for dataset_id in research_dataset_ids
        }
        if actual_research_keys != expected_market_keys or len(research_dataset_ids) != len(
            expected_market_keys
        ):
            raise HTTPException(
                422,
                "campaign datasets must cover the exact frozen point-in-time universe",
            )
        if blind_dataset_ids:
            actual_blind_keys = {
                (
                    dataset_metadata[dataset_id].exchange,
                    dataset_metadata[dataset_id].symbol,
                    dataset_metadata[dataset_id].timeframe,
                    dataset_metadata[dataset_id].market_type,
                )
                for dataset_id in blind_dataset_ids
            }
            if actual_blind_keys != expected_market_keys or len(blind_dataset_ids) != len(
                expected_market_keys
            ):
                raise HTTPException(
                    422,
                    "blind datasets must cover the exact frozen point-in-time universe",
                )
        insufficient_history: list[str] = []
        for market_key in sorted(expected_market_keys):
            timeframe = market_key[2]
            required_bars = universe_snapshot.policy.required_bars_by_timeframe[timeframe]
            matching_research = [
                dataset_metadata[dataset_id]
                for dataset_id in research_dataset_ids
                if (
                    dataset_metadata[dataset_id].exchange,
                    dataset_metadata[dataset_id].symbol,
                    dataset_metadata[dataset_id].timeframe,
                    dataset_metadata[dataset_id].market_type,
                )
                == market_key
            ]
            matching_blind = [
                dataset_metadata[dataset_id]
                for dataset_id in blind_dataset_ids
                if (
                    dataset_metadata[dataset_id].exchange,
                    dataset_metadata[dataset_id].symbol,
                    dataset_metadata[dataset_id].timeframe,
                    dataset_metadata[dataset_id].market_type,
                )
                == market_key
            ]
            coverage_datasets = [*matching_research, *matching_blind]
            covered_bars = sum(item.candle_count for item in coverage_datasets)
            coverage_ratio = Decimal(covered_bars) / Decimal(required_bars)
            quality_passed = all(
                item.gap_count == 0 and item.invalid_count == 0 for item in coverage_datasets
            )
            if (
                coverage_ratio < universe_snapshot.policy.minimum_history_coverage_ratio
                or not quality_passed
            ):
                exchange, symbol, _, market_type = market_key
                insufficient_history.append(
                    f"{exchange}:{symbol}:{timeframe}:{market_type}={covered_bars}/{required_bars}"
                )
        if insufficient_history:
            raise HTTPException(
                422,
                "campaign datasets do not satisfy the frozen history coverage "
                "contract: " + ", ".join(insufficient_history),
            )
        pre_snapshot_datasets = [
            dataset_id
            for dataset_id in dataset_ids
            if dataset_metadata[dataset_id].data_start < universe_snapshot.as_of
        ]
        if pre_snapshot_datasets:
            raise HTTPException(
                422,
                "campaign datasets must start at or after the frozen point-in-time "
                "universe snapshot: " + ", ".join(pre_snapshot_datasets),
            )
        point_in_time_selection_passed = bool(
            universe_snapshot.point_in_time_selection_passed and universe_snapshot.catalog_complete
        )
        prospective_coverage = True
        historical_survivorship_complete = bool(
            universe_snapshot.historical_survivorship_complete or prospective_coverage
        )
        survivorship_bias_control_passed = bool(
            point_in_time_selection_passed and historical_survivorship_complete
        )
    entry_ids = list(dict.fromkeys(request.library_entry_ids))
    candidate_ids = list(dict.fromkeys(request.candidate_ids))
    causal_policy_mode = bool(request.causal_policy_specs)
    causal_policy_definitions: list[dict] = []
    if causal_policy_mode:
        try:
            entries = [
                build_causal_policy_campaign_entry(policy) for policy in request.causal_policy_specs
            ]
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        policy_fingerprints = [entry.policy_fingerprint for entry in entries]
        if len(policy_fingerprints) != len(set(policy_fingerprints)):
            raise HTTPException(
                422,
                "causal policy Campaign hypotheses must be structurally unique",
            )
        causal_policy_definitions = [entry.frozen_definition for entry in entries]
    else:
        try:
            entries = (
                [library_entry(entry_id) for entry_id in entry_ids]
                if entry_ids
                else list_strategy_library()
                if not candidate_ids
                else []
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        for candidate_id in candidate_ids:
            payload = await asyncio.to_thread(
                store.load_strategy_candidate,
                candidate_id,
            )
            if not payload:
                raise HTTPException(
                    404,
                    f"strategy candidate not found: {candidate_id}",
                )
            candidate = parse_strategy_candidate_payload(
                payload,
                audit_context="research_campaign",
            )
            if candidate is None:
                raise HTTPException(
                    409,
                    f"strategy candidate is stale: {candidate_id}",
                )
            family = StrategyKind(candidate.spec["family"])
            entries.append(
                StrategyLibraryEntry(
                    id=f"candidate:{candidate.id}",
                    name=f"Candidate {candidate.id}",
                    family=family,
                    archetype="agent_created_candidate",
                    origin=f"{candidate.source} generated strategy candidate",
                    source_url="https://local.invalid/research-candidate",
                    summary="Agent 创建或修改的可执行策略假设。",
                    indicators=[],
                    preferred_regimes=[],
                    required_data=infer_required_data(candidate.spec),
                    spec=candidate.spec,
                )
            )
    research_matrix_size = len(research_dataset_ids) * len(entries)
    maximum_cell_count = research_matrix_size + len(blind_dataset_ids)
    if research_matrix_size == 0:
        raise HTTPException(422, "campaign matrix is empty")
    if maximum_cell_count > request.max_backtests:
        raise HTTPException(
            422,
            f"campaign requires at most {maximum_cell_count} cells; "
            "increase max_backtests or reduce scope",
        )
    datasets: list[ResearchDatasetBundle] = list(
        await asyncio.gather(
            *(load_verified_research_dataset(dataset_id) for dataset_id in research_dataset_ids)
        )
    )
    configuration = configuration_service.current_configuration()
    research_configuration = {
        "fee_bps": str(configuration.fee_bps),
        "slippage_bps": str(configuration.slippage_bps),
        "risk_fraction": str(configuration.risk.risk_per_trade),
        "minimum_reward_risk": str(configuration.risk.minimum_reward_risk),
        "validation": configuration.strategy.validation.model_dump(mode="json"),
        "campaign_engine_version": CAMPAIGN_ENGINE_VERSION,
        "campaign_hypothesis_mode": (
            "causal_agent_policy" if causal_policy_mode else "static_strategy"
        ),
        "causal_policy_definitions_fingerprint": (
            causal_policy_definitions_fingerprint(causal_policy_definitions)
        ),
        "universe_snapshot_fingerprint": (
            universe_snapshot.fingerprint if universe_snapshot is not None else None
        ),
        "survivorship_bias_control_passed": survivorship_bias_control_passed,
    }
    research_configuration_fingerprint = sha256(
        json.dumps(
            research_configuration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    campaign_id = f"campaign_{uuid4().hex[:16]}"
    lineage_id = research_lineage_id(
        [dataset_metadata[dataset_id] for dataset_id in research_dataset_ids],
        blind_set_id=blind_set_id,
    )
    trial_family_id = research_trial_family_id(
        [dataset_metadata[dataset_id] for dataset_id in research_dataset_ids]
    )

    # Backfill the permanent ledger from durable reports before charging the
    # current experiment. Historical reports did not persist the configuration
    # fingerprint, so they share a conservative legacy configuration identity.
    historical_reports = await asyncio.to_thread(store.list_research_campaigns, 1000)
    current_research_ids = set(research_dataset_ids)
    ledger_dataset_payloads = await asyncio.to_thread(store.list_research_datasets, 5000)
    ledger_dataset_metadata = {
        str(payload["id"]): ResearchDataset.model_validate(payload)
        for payload in ledger_dataset_payloads
    }

    def source_lineage_keys(dataset_id_values: set[str]) -> set[tuple[str, ...]]:
        keys: set[tuple[str, ...]] = set()
        for dataset_id_value in dataset_id_values:
            dataset = ledger_dataset_metadata.get(dataset_id_value)
            if dataset is None:
                continue
            keys.add(
                (
                    dataset.source_dataset_id or dataset.id,
                    dataset.exchange,
                    dataset.market_type,
                    dataset.symbol,
                    dataset.timeframe,
                )
            )
        return keys

    current_source_keys = source_lineage_keys(current_research_ids)
    lineage_history: list[dict] = []
    for historical in reversed(historical_reports):
        if historical.get("status") != "completed":
            continue
        historical_research_ids = set(
            historical.get("research_dataset_ids")
            or [
                dataset_id
                for dataset_id in historical.get("dataset_ids", [])
                if dataset_id not in set(historical.get("blind_dataset_ids", []))
            ]
        )
        historical_datasets = [
            ledger_dataset_metadata[dataset_id]
            for dataset_id in historical_research_ids
            if dataset_id in ledger_dataset_metadata
        ]
        historical_trial_family_id = (
            research_trial_family_id(historical_datasets) if historical_datasets else ""
        )
        same_lineage = historical.get("research_lineage_id") == lineage_id
        historical_source_keys = source_lineage_keys(historical_research_ids)
        same_lineage = same_lineage or bool(
            current_source_keys
            and historical_source_keys == current_source_keys
            and len(historical_source_keys) == len(historical_research_ids)
        )
        if blind_set_id:
            same_lineage = same_lineage or historical.get("blind_set_id") == blind_set_id
        else:
            same_lineage = same_lineage or historical_research_ids == current_research_ids
        if same_lineage:
            lineage_history.append(historical)
        historical_fingerprints = sorted(
            {
                str(cell.get("strategy_fingerprint"))
                for cell in historical.get("cells", [])
                if cell.get("strategy_fingerprint")
                and cell.get("dataset_id") in historical_research_ids
                and cell.get("dataset_role", "research") != "blind"
            }
        )
        if not historical_fingerprints:
            historical_fingerprints = sorted(
                {
                    str(candidate.get("strategy_fingerprint"))
                    for candidate in historical.get("candidates", [])
                    if candidate.get("strategy_fingerprint")
                }
            )
        if not historical_fingerprints:
            continue
        historical_configuration_fingerprint = str(
            historical.get("research_configuration_fingerprint") or "legacy_unknown"
        )
        historical_experiment_id = str(
            historical.get("research_experiment_id")
            or research_experiment_fingerprint(
                lineage_id=str(historical.get("research_lineage_id") or "legacy_unknown_lineage"),
                engine_version=int(historical.get("engine_version") or 0),
                research_configuration_fingerprint=(historical_configuration_fingerprint),
                strategy_fingerprints=historical_fingerprints,
            )
        )
        if historical_trial_family_id == trial_family_id:
            await asyncio.to_thread(
                store.register_research_trial_family_experiment,
                family_id=trial_family_id,
                experiment_id=historical_experiment_id,
                campaign_id=str(historical.get("id") or historical_experiment_id),
                engine_version=int(historical.get("engine_version") or 0),
                research_configuration_fingerprint=(historical_configuration_fingerprint),
                strategy_fingerprints=historical_fingerprints,
            )
        if not same_lineage:
            continue
        await asyncio.to_thread(
            store.register_research_campaign_experiment,
            lineage_id=lineage_id,
            experiment_id=historical_experiment_id,
            campaign_id=str(historical.get("id") or historical_experiment_id),
            engine_version=int(historical.get("engine_version") or 0),
            research_configuration_fingerprint=(historical_configuration_fingerprint),
            strategy_fingerprints=historical_fingerprints,
            blind_set_id=blind_set_id,
        )
        historical_blind_consumption = historical.get("blind_consumption_id")
        if historical_blind_consumption:
            await asyncio.to_thread(
                store.mark_research_lineage_blind_opened,
                lineage_id=lineage_id,
                blind_consumption_id=str(historical_blind_consumption),
            )

    strategy_fingerprints = [entry.validated_spec.fingerprint for entry in entries]
    experiment_id = research_experiment_fingerprint(
        lineage_id=lineage_id,
        engine_version=CAMPAIGN_ENGINE_VERSION,
        research_configuration_fingerprint=research_configuration_fingerprint,
        strategy_fingerprints=strategy_fingerprints,
    )
    parent_report: ResearchCampaignReport | None = None
    if request.parent_campaign_id:
        parent_payload = await asyncio.to_thread(
            store.load_research_campaign,
            request.parent_campaign_id,
        )
        if parent_payload is None:
            raise HTTPException(404, "parent research campaign not found")
        parent_report = ResearchCampaignReport.model_validate(parent_payload)
        if parent_report.parent_campaign_id is not None:
            raise HTTPException(
                409,
                "bounded research permits only one successor on this lineage",
            )
        if parent_report.blind_consumption_id:
            raise HTTPException(
                409,
                "a consumed blind Campaign is terminal evidence and cannot be revised",
            )
        existing_successors = [
            payload
            for payload in lineage_history
            if payload.get("parent_campaign_id") == parent_report.id
        ]
        if existing_successors and not any(
            payload.get("research_experiment_id") == experiment_id
            for payload in existing_successors
        ):
            raise HTTPException(
                409,
                "bounded research permits only one successor per research phase",
            )
        if parent_report.research_lineage_id != lineage_id:
            raise HTTPException(
                422,
                "successor must use the exact parent evidence lineage",
            )
        if (
            set(parent_report.research_dataset_ids) != set(research_dataset_ids)
            or set(parent_report.blind_dataset_ids) != set(blind_dataset_ids)
            or parent_report.universe_snapshot_id != request.universe_snapshot_id
        ):
            raise HTTPException(
                422,
                "successor must reuse the parent's exact development, blind, "
                "and frozen-universe scope",
            )
        parent_fingerprints = {
            cell.strategy_fingerprint
            for cell in parent_report.cells
            if cell.dataset_role != "blind"
        }
        current_fingerprints = set(strategy_fingerprints)
        if not parent_fingerprints.issubset(current_fingerprints):
            raise HTTPException(422, "successor must retain every frozen parent hypothesis")
        new_fingerprints = current_fingerprints - parent_fingerprints
        if not new_fingerprints or len(new_fingerprints) > 3:
            raise HTTPException(422, "successor requires one to three bounded child hypotheses")
        if causal_policy_mode:
            if not parent_report.causal_policy_definitions:
                raise HTTPException(422, "causal policy successor requires a policy parent")
            allowed_definitions = {
                str(item.get("policy_fingerprint")): item
                for item in parent_report.authorized_policy_revision_definitions
            }
            if not new_fingerprints.issubset(
                set(parent_report.authorized_policy_revision_fingerprints)
            ):
                raise HTTPException(
                    422,
                    "causal policy successor contains a child not frozen by its parent",
                )
            for entry in entries:
                if entry.policy_fingerprint not in new_fingerprints:
                    continue
                if allowed_definitions.get(entry.policy_fingerprint) != entry.frozen_definition:
                    raise HTTPException(
                        422,
                        "causal policy successor definition differs from its authorization",
                    )
        else:
            allowed_fingerprints = set(parent_report.authorized_revision_fingerprints)
            if not new_fingerprints.issubset(allowed_fingerprints):
                raise HTTPException(422, "successor contains a child not frozen by its parent")
            new_candidate_ids = {
                entry.id.removeprefix("candidate:")
                for entry in entries
                if entry.validated_spec.fingerprint in new_fingerprints
                and entry.id.startswith("candidate:")
            }
            if len(new_candidate_ids) != len(new_fingerprints) or not new_candidate_ids.issubset(
                set(parent_report.authorized_revision_candidate_ids)
            ):
                raise HTTPException(
                    422,
                    "successor children must use the exact candidate IDs authorized by its parent",
                )
    elif blind_set_id and lineage_history:
        exact_replay = any(
            payload.get("research_experiment_id") == experiment_id for payload in lineage_history
        )
        same_frozen_hypotheses = any(
            payload.get("parent_campaign_id") is None
            and {
                str(cell.get("strategy_fingerprint"))
                for cell in payload.get("cells", [])
                if cell.get("strategy_fingerprint")
                and cell.get("dataset_role", "research") != "blind"
            }
            == set(strategy_fingerprints)
            for payload in lineage_history
        )
        if not exact_replay and not same_frozen_hypotheses:
            raise HTTPException(
                409,
                "a new hypothesis set on a sealed evidence lineage requires "
                "parent_campaign_id; only one bounded successor is permitted",
            )
    try:
        ledger_summary = await asyncio.to_thread(
            store.register_research_campaign_experiment,
            lineage_id=lineage_id,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            engine_version=CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
            blind_set_id=blind_set_id,
            trial_family_id=trial_family_id,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    replayed_report: ResearchCampaignReport | None = None
    if ledger_summary.get("replayed"):
        campaign_id = str(ledger_summary["campaign_id"])
        replayed_payload = await asyncio.to_thread(
            store.load_research_campaign,
            campaign_id,
        )
        if replayed_payload is not None:
            replayed_report = ResearchCampaignReport.model_validate(replayed_payload)
    effective_trial_count = int(ledger_summary["effective_trial_count"])
    if parent_report is not None:
        experiment_kind = "bounded_policy_revision" if causal_policy_mode else "bounded_revision"
        predecessor_campaign_id = parent_report.id
    elif lineage_history:
        experiment_kind = "structural_extension"
        predecessor_campaign_id = str(lineage_history[-1].get("id") or "") or None
    else:
        experiment_kind = "initial"
        predecessor_campaign_id = None

    # Complete all non-return data gates before reserving empirical trials.
    # Every eligible hypothesis is then charged exactly once before a cache or
    # backtest result can expose return-bearing evidence.
    feature_eligibility: dict[tuple[str, str], list[str]] = {}
    research_dataset_id_set = set(research_dataset_ids)
    for entry in entries:
        for (
            dataset,
            _candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        ) in datasets:
            if dataset.id not in research_dataset_id_set:
                continue
            feature_eligibility[(entry.id, dataset.id)] = historical_feature_eligibility(
                dataset=dataset,
                required_data=entry.required_data,
                funding=funding,
                open_interest=open_interest,
                positioning=positioning,
                basis=basis,
                order_flow=order_flow,
                liquidation_flow=liquidation_flow,
            )

    fdr_controller = OnlineFDRController(store)
    permanent_trials: dict[str, PermanentTrialRecord] = {}
    fdr_allocations: dict[str, tuple[object, object]] = {}
    for entry in entries:
        eligible_dataset_fingerprints = sorted(
            dataset.data_fingerprint
            for dataset, *_rest in datasets
            if dataset.id in research_dataset_id_set
            and not feature_eligibility[(entry.id, dataset.id)]
        )
        if not eligible_dataset_fingerprints:
            continue
        dataset_scope_fingerprint = sha256(
            json.dumps(
                eligible_dataset_fingerprints,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        artifact_fingerprint = sha256(
            json.dumps(
                {
                    "engine_version": CAMPAIGN_ENGINE_VERSION,
                    "strategy_fingerprint": entry.validated_spec.fingerprint,
                    "dataset_scope_fingerprint": dataset_scope_fingerprint,
                    "validation_fingerprint": research_configuration_fingerprint,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        trial = PermanentTrialRecord.empirical(
            research_event_id=experiment_id,
            campaign_id=campaign_id,
            hypothesis_id=f"legacy_hypothesis:{entry.validated_spec.fingerprint}",
            family_id=f"{trial_family_id}:{entry.family.value}",
            candidate_id=entry.id,
            dataset_fingerprint=dataset_scope_fingerprint,
            validation_fingerprint=research_configuration_fingerprint,
            artifact_fingerprint=artifact_fingerprint,
        )
        persisted_trial = PermanentTrialRecord.model_validate(
            await asyncio.to_thread(store.reserve_research_trial, trial)
        )
        global_allocation = await asyncio.to_thread(
            fdr_controller.allocate_alpha,
            "research:global",
            scope="global",
            idempotency_key=f"{persisted_trial.idempotency_key}:global",
        )
        family_allocation = await asyncio.to_thread(
            fdr_controller.allocate_alpha,
            f"research:family:{persisted_trial.family_id}",
            scope=persisted_trial.family_id,
            idempotency_key=f"{persisted_trial.idempotency_key}:family",
        )
        permanent_trials[entry.id] = persisted_trial
        fdr_allocations[entry.id] = (global_allocation, family_allocation)

    semaphore = asyncio.Semaphore(CAMPAIGN_CELL_CONCURRENCY)
    completed_cells = 0
    cache_hits = 0
    computed_cells = 0

    async def report_progress() -> None:
        if progress_callback is not None:
            await progress_callback(
                completed_cells,
                maximum_cell_count,
                cache_hits,
                computed_cells,
            )

    await report_progress()

    async def evaluate_cell(
        entry,
        dataset: ResearchDataset,
        candles: list[Candle],
        funding: list[FundingRatePoint],
        open_interest: list[OpenInterestPoint],
        positioning: list[PositioningPoint],
        basis: list[BasisPoint],
        order_flow: list[OrderFlowBar],
        liquidation_flow: list[LiquidationFlowPoint],
        dataset_role: str,
        statistical_trial_count: int,
    ):
        nonlocal completed_cells, cache_hits, computed_cells
        async with semaphore:
            cache_key = campaign_cell_cache_key(
                strategy_fingerprint=entry.validated_spec.fingerprint,
                dataset_fingerprint=dataset.data_fingerprint,
                dataset_id=dataset.id,
                trial_count=statistical_trial_count,
                research_configuration_fingerprint=(research_configuration_fingerprint),
            )
            data_ineligibility_reasons = feature_eligibility.get((entry.id, dataset.id), [])
            if data_ineligibility_reasons:
                cell = ineligible_campaign_cell(
                    library_entry=entry,
                    dataset=dataset,
                    reasons=data_ineligibility_reasons,
                    dataset_role=dataset_role,
                )
                await asyncio.to_thread(
                    store.save_research_campaign_cell,
                    cache_key=cache_key,
                    strategy_fingerprint=entry.validated_spec.fingerprint,
                    dataset_fingerprint=dataset.data_fingerprint,
                    engine_version=CAMPAIGN_ENGINE_VERSION,
                    cell=cell,
                )
                computed_cells += 1
                completed_cells += 1
                await report_progress()
                return cell
            cached_payload = await asyncio.to_thread(
                store.load_research_campaign_cell,
                cache_key,
            )
            if cached_payload is not None:
                cell = ResearchCampaignCell.model_validate(cached_payload).model_copy(
                    update={"dataset_role": dataset_role}
                )
                cache_hits += 1
                completed_cells += 1
                await report_progress()
                return cell
            if isinstance(entry, CausalPolicyCampaignEntry):
                result = await backtest_causal_policy_entry_on_dataset(
                    entry,
                    dataset,
                    candles,
                    funding,
                    open_interest,
                    positioning=positioning,
                    basis=basis,
                    order_flow=order_flow,
                    liquidation_flow=liquidation_flow,
                    research_trial_count=statistical_trial_count,
                )
            else:
                candidate = create_candidate(entry.validated_spec)
                result = await backtest_candidate_on_dataset(
                    candidate,
                    dataset,
                    candles,
                    funding,
                    open_interest,
                    positioning=positioning,
                    basis=basis,
                    order_flow=order_flow,
                    liquidation_flow=liquidation_flow,
                    research_trial_count=statistical_trial_count,
                )
            cell = campaign_cell(
                library_entry=entry,
                dataset=dataset,
                result=result,
                backtest_fingerprint=backtest_artifact_fingerprint(result),
                dataset_role=dataset_role,
            )
            await asyncio.to_thread(
                store.save_research_campaign_cell,
                cache_key=cache_key,
                strategy_fingerprint=entry.validated_spec.fingerprint,
                dataset_fingerprint=dataset.data_fingerprint,
                engine_version=CAMPAIGN_ENGINE_VERSION,
                cell=cell,
            )
            computed_cells += 1
            completed_cells += 1
            await report_progress()
            return cell

    research_tasks = [
        asyncio.create_task(
            evaluate_cell(
                entry,
                dataset,
                candles,
                funding,
                open_interest,
                positioning,
                basis,
                order_flow,
                liquidation_flow,
                "research",
                effective_trial_count,
            )
        )
        for entry in entries
        for (
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        ) in datasets
        if dataset.id in set(research_dataset_ids)
    ]
    try:
        research_cells = await asyncio.gather(*research_tasks)
    except BaseException:
        for task in research_tasks:
            task.cancel()
        await asyncio.gather(*research_tasks, return_exceptions=True)
        raise
    research_candidates, research_pbo = aggregate_campaign(
        research_cells,
        cumulative_hypothesis_count=effective_trial_count,
    )
    online_fdr_results: dict[str, dict] = {}
    for candidate in research_candidates:
        allocations = fdr_allocations.get(candidate.library_entry_id)
        trial = permanent_trials.get(candidate.library_entry_id)
        if allocations is None or trial is None:
            continue
        global_allocation, family_allocation = allocations
        await asyncio.to_thread(
            fdr_controller.record_result,
            global_allocation,
            p_value=candidate.strategy_p_value,
        )
        await asyncio.to_thread(
            fdr_controller.record_result,
            family_allocation,
            p_value=candidate.strategy_p_value,
        )
        online_alpha = min(global_allocation.alpha, family_allocation.alpha)
        online_passed = candidate.strategy_p_value <= online_alpha
        online_fdr_results[candidate.library_entry_id] = {
            "alpha": str(online_alpha),
            "passed": online_passed,
        }
        if candidate.expected_r_testable_market_count == 0:
            trial_result = TrialResult.UNTESTABLE
        elif candidate.cross_market_passed and online_passed and research_pbo.passed:
            trial_result = TrialResult.PASS
        else:
            trial_result = TrialResult.FAIL
        await asyncio.to_thread(
            store.append_research_trial_result,
            trial.id,
            result=str(trial_result),
            p_value=candidate.strategy_p_value,
            adjusted_p_value=candidate.multiple_testing_adjusted_p_value,
            dsr=candidate.median_deflated_sharpe_probability,
            pbo=research_pbo.upper_confidence_bound,
            global_alpha=global_allocation.alpha,
            family_alpha=family_allocation.alpha,
            reason_codes=([] if online_passed else ["online_fdr_budget_not_met"]),
        )
    research_candidates = [
        candidate.model_copy(
            update={
                "online_fdr_alpha": Decimal(
                    str(online_fdr_results.get(candidate.library_entry_id, {}).get("alpha", "0"))
                ),
                "online_fdr_passed": bool(
                    online_fdr_results.get(candidate.library_entry_id, {}).get("passed", False)
                ),
                "cross_market_passed": candidate.cross_market_passed
                and bool(
                    online_fdr_results.get(candidate.library_entry_id, {}).get("passed", False)
                ),
            }
        )
        for candidate in research_candidates
    ]
    provisional_selected = next(
        (candidate for candidate in research_candidates if candidate.cross_market_passed),
        None,
    )
    blind_cells: list[ResearchCampaignCell] = []
    blind_consumption_id: str | None = None
    blind_trial: PermanentTrialRecord | None = None
    if campaign_blind_opening_allowed(
        provisional_selected=provisional_selected,
        research_pbo=research_pbo,
        blind_dataset_ids=blind_dataset_ids,
        survivorship_bias_control_passed=survivorship_bias_control_passed,
    ):
        selected_entry = next(
            entry for entry in entries if entry.id == provisional_selected.library_entry_id
        )
        blind_consumption_id = f"blind_eval_{uuid4().hex[:16]}"
        reserved = await asyncio.to_thread(
            store.reserve_blind_set,
            blind_set_id=blind_set_id,
            strategy_fingerprint=selected_entry.validated_spec.fingerprint,
            evaluation_id=blind_consumption_id,
        )
        if not reserved:
            existing = await asyncio.to_thread(
                store.load_blind_set_consumption,
                blind_set_id,
            )
            raise HTTPException(
                409,
                f"sealed blind set has already been opened and cannot be reused: {existing}",
            )
        await asyncio.to_thread(
            store.mark_research_lineage_blind_opened,
            lineage_id=lineage_id,
            blind_consumption_id=blind_consumption_id,
        )
        blind_scope_fingerprint = sha256(
            json.dumps(
                sorted(dataset_metadata[item].data_fingerprint for item in blind_dataset_ids),
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        blind_trial_candidate = PermanentTrialRecord.empirical(
            research_event_id=experiment_id,
            campaign_id=campaign_id,
            hypothesis_id=(f"legacy_hypothesis:{selected_entry.validated_spec.fingerprint}"),
            family_id=f"{trial_family_id}:{selected_entry.family.value}",
            candidate_id=selected_entry.id,
            dataset_fingerprint=blind_scope_fingerprint,
            validation_fingerprint=research_configuration_fingerprint,
            artifact_fingerprint=sha256(
                f"blind:{experiment_id}:{selected_entry.validated_spec.fingerprint}:"
                f"{blind_scope_fingerprint}".encode()
            ).hexdigest(),
        ).model_copy(
            update={
                "trial_type": TrialType.BLIND,
                "touched_blind": True,
            }
        )
        blind_trial = PermanentTrialRecord.model_validate(
            await asyncio.to_thread(store.reserve_research_trial, blind_trial_candidate)
        )
        blind_datasets = await asyncio.gather(
            *(load_verified_research_dataset(dataset_id) for dataset_id in blind_dataset_ids)
        )
        blind_cells = list(
            await asyncio.gather(
                *(
                    evaluate_cell(
                        selected_entry,
                        dataset,
                        candles,
                        funding,
                        open_interest,
                        positioning,
                        basis,
                        order_flow,
                        liquidation_flow,
                        "blind",
                        1,
                    )
                    for (
                        dataset,
                        candles,
                        funding,
                        open_interest,
                        positioning,
                        basis,
                        order_flow,
                        liquidation_flow,
                    ) in blind_datasets
                )
            )
        )
    elif progress_callback is not None:
        # The blind seal remains unopened when development evidence cannot nominate
        # one candidate. Report the actual terminal cell count to durable jobs.
        maximum_cell_count = len(research_cells)
        await report_progress()
    cells = [*research_cells, *blind_cells]
    report = finalize_campaign(
        dataset_ids=dataset_ids,
        blind_dataset_ids=blind_dataset_ids,
        blind_set_id=blind_set_id,
        blind_consumption_id=blind_consumption_id,
        library_entry_ids=[entry.id for entry in entries],
        causal_policy_definitions=causal_policy_definitions,
        cells=cells,
        campaign_id=campaign_id,
        research_lineage_id_value=lineage_id,
        research_trial_family_id_value=trial_family_id,
        research_experiment_id=experiment_id,
        research_configuration_fingerprint=research_configuration_fingerprint,
        cumulative_hypothesis_count=int(ledger_summary["cumulative_hypothesis_count"]),
        sequential_experiment_count=int(ledger_summary["sequential_experiment_count"]),
        effective_trial_count=effective_trial_count,
        permanent_trial_ids=[trial.id for trial in permanent_trials.values()],
        online_fdr_results=online_fdr_results,
        parent_campaign_id=(parent_report.id if parent_report is not None else None),
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        universe_snapshot_id=(universe_snapshot.id if universe_snapshot is not None else None),
        universe_snapshot_fingerprint=(
            universe_snapshot.fingerprint if universe_snapshot is not None else None
        ),
        point_in_time_selection_passed=point_in_time_selection_passed,
        historical_survivorship_complete=historical_survivorship_complete,
        survivorship_bias_control_passed=survivorship_bias_control_passed,
    )
    if replayed_report is not None:
        report = report.model_copy(
            update={
                "parent_campaign_id": replayed_report.parent_campaign_id,
                "predecessor_campaign_id": replayed_report.predecessor_campaign_id,
                "experiment_kind": replayed_report.experiment_kind,
                "authorized_revision_candidate_ids": (
                    replayed_report.authorized_revision_candidate_ids
                ),
                "authorized_revision_fingerprints": (
                    replayed_report.authorized_revision_fingerprints
                ),
                "authorized_policy_revision_definitions": (
                    replayed_report.authorized_policy_revision_definitions
                ),
                "authorized_policy_revision_fingerprints": (
                    replayed_report.authorized_policy_revision_fingerprints
                ),
                "revision_source_library_entry_id": (
                    replayed_report.revision_source_library_entry_id
                ),
                "created_at": replayed_report.created_at,
            }
        )
    if report.autonomous_execution_eligible:
        report.execution_certificate = build_campaign_execution_certificate(
            report,
            dataset_fingerprints={
                dataset_id: dataset_metadata[dataset_id].data_fingerprint
                for dataset_id in dataset_ids
            },
        )
    await asyncio.to_thread(store.save_research_campaign, report)
    if blind_consumption_id is not None:
        await asyncio.to_thread(
            store.complete_blind_set_consumption,
            evaluation_id=blind_consumption_id,
            campaign_id=report.id,
            passed=report.blind_selected_candidate_passed,
        )
    if blind_trial is not None:
        await asyncio.to_thread(
            store.append_research_trial_result,
            blind_trial.id,
            result=str(
                TrialResult.PASS if report.blind_selected_candidate_passed else TrialResult.FAIL
            ),
            reason_codes=[
                "blind_passed" if report.blind_selected_candidate_passed else "blind_not_admitted"
            ],
        )
    add_audit(
        "research",
        "策略研究 Campaign 已完成",
        campaign_id=report.id,
        trial_count=report.trial_count,
        effective_trial_count=report.effective_trial_count,
        cumulative_hypothesis_count=report.cumulative_hypothesis_count,
        selected_library_entry_id=report.selected_library_entry_id,
        autonomous_execution_eligible=(report.autonomous_execution_eligible),
    )
    return report


@app.post(
    "/api/research/campaigns/run",
    response_model=ResearchCampaignReport,
)
async def run_research_campaign(
    request: ResearchCampaignRequest,
) -> ResearchCampaignReport:
    """Run synchronously for Agent tools and backwards-compatible callers."""

    return await execute_research_campaign(request)


async def run_research_campaign_job(job: ResearchCampaignJob) -> None:
    job.status = ResearchCampaignJobStatus.RUNNING
    job.started_at = job.started_at or datetime.now(UTC)
    job.completed_at = None
    job.error = None
    job.updated_at = datetime.now(UTC)
    await asyncio.to_thread(store.save_research_campaign_job, job)
    last_progress_persisted_at = perf_counter()

    async def persist_progress(
        completed: int,
        total: int,
        cache_hits: int,
        computed: int,
    ) -> None:
        nonlocal last_progress_persisted_at
        job.total_cells = total
        job.completed_cells = completed
        job.cache_hits = cache_hits
        job.computed_cells = computed
        job.updated_at = datetime.now(UTC)
        now = perf_counter()
        if completed not in {0, total} and now - last_progress_persisted_at < 0.5:
            return
        await asyncio.to_thread(store.save_research_campaign_job, job)
        last_progress_persisted_at = now

    try:
        report = await execute_research_campaign(
            ResearchCampaignRequest.model_validate(job.request),
            progress_callback=persist_progress,
        )
        job.report_id = report.id
        job.status = ResearchCampaignJobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)
    except asyncio.CancelledError:
        job.status = ResearchCampaignJobStatus.QUEUED
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_research_campaign_job, job)
        raise
    except Exception as exc:
        job.status = ResearchCampaignJobStatus.FAILED
        job.error = str(getattr(exc, "detail", exc))[:1000]
        job.failed_cells = max(job.total_cells - job.completed_cells, 0)
        job.completed_at = datetime.now(UTC)
    finally:
        if job.status != ResearchCampaignJobStatus.QUEUED:
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_research_campaign_job, job)


def schedule_research_campaign_job(job: ResearchCampaignJob) -> None:
    task = asyncio.create_task(
        run_research_campaign_job(job),
        name=f"research-campaign:{job.id}",
    )
    campaign_job_tasks.add(task)
    task.add_done_callback(campaign_job_tasks.discard)


@app.post(
    "/api/research/campaign-jobs",
    response_model=ResearchCampaignJob,
    status_code=202,
)
async def queue_research_campaign(
    request: ResearchCampaignRequest,
) -> ResearchCampaignJob:
    normalized_request = request.model_dump(mode="json")
    normalized_request["dataset_ids"] = sorted(set(request.dataset_ids))
    normalized_request["blind_dataset_ids"] = sorted(set(request.blind_dataset_ids))
    normalized_request["library_entry_ids"] = sorted(set(request.library_entry_ids))
    normalized_request["candidate_ids"] = sorted(set(request.candidate_ids))
    normalized_request["causal_policy_specs"] = sorted(
        [
            {
                **item.model_dump(mode="json"),
                "library_entry_ids": sorted(set(item.library_entry_ids)),
            }
            for item in request.causal_policy_specs
        ],
        key=lambda item: item["id"],
    )
    request_fingerprint = campaign_request_fingerprint(normalized_request)
    recent_payloads = await asyncio.to_thread(
        store.list_research_campaign_jobs,
        100,
    )
    for payload in recent_payloads:
        existing = ResearchCampaignJob.model_validate(payload)
        if existing.request_fingerprint == request_fingerprint and existing.status in {
            ResearchCampaignJobStatus.QUEUED,
            ResearchCampaignJobStatus.RUNNING,
        }:
            return existing
    job = ResearchCampaignJob(
        request=normalized_request,
        request_fingerprint=request_fingerprint,
    )
    await asyncio.to_thread(store.save_research_campaign_job, job)
    schedule_research_campaign_job(job)
    return job


@app.get(
    "/api/research/campaign-jobs",
    response_model=list[ResearchCampaignJob],
)
async def list_research_campaign_jobs(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_research_campaign_jobs,
        min(max(limit, 1), 200),
    )
    return [ResearchCampaignJob.model_validate(payload) for payload in payloads]


@app.get(
    "/api/research/campaign-jobs/{job_id}",
    response_model=ResearchCampaignJob,
)
async def get_research_campaign_job(job_id: str):
    payload = await asyncio.to_thread(
        store.load_research_campaign_job,
        job_id,
    )
    if not payload:
        raise HTTPException(404, "research campaign job not found")
    return ResearchCampaignJob.model_validate(payload)


@app.get(
    "/api/research/campaigns",
    response_model=list[ResearchCampaignReport],
)
async def list_research_campaign_reports():
    payloads = await asyncio.to_thread(store.list_research_campaigns, 20)
    return [ResearchCampaignReport.model_validate(payload) for payload in payloads]


@app.get(
    "/api/research/campaigns/{campaign_id}",
    response_model=ResearchCampaignReport,
)
async def get_research_campaign_report(campaign_id: str):
    payload = await asyncio.to_thread(
        store.load_research_campaign,
        campaign_id,
    )
    if not payload:
        raise HTTPException(404, "research campaign not found")
    return ResearchCampaignReport.model_validate(payload)


@app.get("/api/research/relative-value/library")
async def relative_value_strategy_library():
    return {
        "execution_enabled": False,
        "sources": sorted({spec.source_url for spec in PAIR_STRATEGY_LIBRARY}),
        "profitability_claim": False,
        "entries": [
            {
                "id": pair_strategy_id(spec),
                "spec": spec.model_dump(mode="json"),
                "strategy_fingerprint": spec.fingerprint,
            }
            for spec in PAIR_STRATEGY_LIBRARY
        ],
    }


@app.post(
    "/api/research/relative-value/backtests",
    response_model=list[PairBacktestReport],
)
async def run_relative_value_backtests(
    request: RelativeValueResearchRequest,
):
    if request.dataset_a_id == request.dataset_b_id:
        raise HTTPException(422, "relative-value datasets must be different")

    async def load_dataset(dataset_id: str):
        payload = await asyncio.to_thread(
            store.load_research_dataset,
            dataset_id,
        )
        if not payload:
            raise HTTPException(404, f"research dataset not found: {dataset_id}")
        (
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
            order_flow_payloads,
            liquidation_flow_payloads,
        ) = await asyncio.gather(
            asyncio.to_thread(
                store.load_research_dataset_candles,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_funding,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_open_interest,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_positioning,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_basis,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_order_flow,
                dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_liquidation_flow,
                dataset_id,
            ),
        )
        dataset = ResearchDataset.model_validate(payload)
        candles = [Candle.model_validate(item) for item in candle_payloads]
        funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
        open_interest = [OpenInterestPoint.model_validate(item) for item in open_interest_payloads]
        positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
        basis = [BasisPoint.model_validate(item) for item in basis_payloads]
        order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
        ]
        verification = verify_research_dataset(
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )
        if not verification["verified"]:
            raise HTTPException(
                409,
                f"research dataset integrity failed: {dataset_id}",
            )
        return dataset, candles, funding

    first, second = await asyncio.gather(
        load_dataset(request.dataset_a_id),
        load_dataset(request.dataset_b_id),
    )
    specs = (
        [PairStrategySpec.model_validate(item) for item in request.strategy_specs]
        if request.strategy_specs
        else list(PAIR_STRATEGY_LIBRARY)
    )
    configuration = configuration_service.current_configuration()
    reports: list[PairBacktestReport] = []
    for spec in specs:
        report = await asyncio.to_thread(
            run_pair_backtest,
            dataset_a=first[0],
            candles_a=first[1],
            funding_a=first[2],
            dataset_b=second[0],
            candles_b=second[1],
            funding_b=second[2],
            strategy_spec=spec,
            initial_equity=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            research_trial_count=len(specs),
        )
        await asyncio.to_thread(
            store.save_relative_value_report,
            report,
        )
        reports.append(report)
    add_audit(
        "research",
        "多腿相对价值研究完成；执行保持禁用",
        dataset_a_id=request.dataset_a_id,
        dataset_b_id=request.dataset_b_id,
        report_ids=",".join(report.id for report in reports),
        execution_enabled=False,
    )
    return reports


@app.get(
    "/api/research/relative-value/backtests",
    response_model=list[PairBacktestReport],
)
async def list_relative_value_backtests(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_relative_value_reports,
        min(max(limit, 1), 200),
    )
    return [PairBacktestReport.model_validate(payload) for payload in payloads]


@app.post(
    "/api/research/relative-value/campaigns",
    response_model=PairResearchCampaignReport,
)
async def run_relative_value_campaign(request: RelativeValueCampaignRequest):
    dataset_ids = list(dict.fromkeys(request.dataset_ids))
    if len(dataset_ids) != len(request.dataset_ids):
        raise HTTPException(422, "relative-value campaign datasets must be unique")
    blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
    if len(blind_dataset_ids) != len(request.blind_dataset_ids):
        raise HTTPException(422, "relative-value blind datasets must be unique")
    if set(dataset_ids) & set(blind_dataset_ids):
        raise HTTPException(422, "development and blind datasets must be disjoint")

    metadata_payloads = await asyncio.gather(
        *(
            asyncio.to_thread(store.load_research_dataset, dataset_id)
            for dataset_id in [*dataset_ids, *blind_dataset_ids]
        )
    )
    if any(payload is None for payload in metadata_payloads):
        missing = [
            dataset_id
            for dataset_id, payload in zip(
                [*dataset_ids, *blind_dataset_ids], metadata_payloads, strict=True
            )
            if payload is None
        ]
        raise HTTPException(404, f"research dataset not found: {missing}")
    metadata = [ResearchDataset.model_validate(payload) for payload in metadata_payloads]
    try:
        blind_set_id = _validate_sealed_blind_scope(
            metadata[: len(dataset_ids)],
            metadata[len(dataset_ids) :],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    async def load_dataset(dataset_id: str, *, blind: bool = False):
        payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
        if not payload:
            raise HTTPException(404, f"research dataset not found: {dataset_id}")
        dataset = ResearchDataset.model_validate(payload)
        if not blind and (dataset.research_role == "blind" or dataset.blind_sealed):
            raise HTTPException(
                422,
                "sealed blind datasets cannot be used for relative-value selection",
            )
        if blind and (dataset.research_role != "blind" or not dataset.blind_sealed):
            raise HTTPException(422, "relative-value blind dataset is not sealed")
        (
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
            order_flow_payloads,
            liquidation_flow_payloads,
        ) = await asyncio.gather(
            asyncio.to_thread(store.load_research_dataset_candles, dataset_id),
            asyncio.to_thread(store.load_research_dataset_funding, dataset_id),
            asyncio.to_thread(store.load_research_dataset_open_interest, dataset_id),
            asyncio.to_thread(store.load_research_dataset_positioning, dataset_id),
            asyncio.to_thread(store.load_research_dataset_basis, dataset_id),
            asyncio.to_thread(store.load_research_dataset_order_flow, dataset_id),
            asyncio.to_thread(store.load_research_dataset_liquidation_flow, dataset_id),
        )
        candles = [Candle.model_validate(item) for item in candle_payloads]
        funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
        open_interest = [OpenInterestPoint.model_validate(item) for item in open_interest_payloads]
        positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
        basis = [BasisPoint.model_validate(item) for item in basis_payloads]
        order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
        ]
        verification = verify_research_dataset(
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )
        if not verification["verified"]:
            raise HTTPException(409, f"research dataset integrity failed: {dataset_id}")
        return dataset, candles, funding

    def build_pair_markets(loaded_items: list[tuple], *, scope: str) -> list[PairMarketData]:
        grouped: dict[tuple[str, str], list[tuple]] = {}
        for item in loaded_items:
            grouped.setdefault((item[0].exchange, item[0].timeframe), []).append(item)
        if len({key[0] for key in grouped}) < 2:
            raise HTTPException(
                422,
                f"relative-value {scope} requires at least two exchanges",
            )
        result: list[PairMarketData] = []
        for (exchange, timeframe), items in sorted(grouped.items()):
            by_symbol = {item[0].symbol: item for item in items}
            if len(by_symbol) != len(items):
                raise HTTPException(
                    422,
                    f"duplicate {exchange} {timeframe} symbols in relative-value {scope}",
                )
            if len(by_symbol) < 3:
                raise HTTPException(
                    422,
                    f"{exchange} {timeframe} requires at least three synchronized symbols",
                )
            for first_symbol, second_symbol in combinations(sorted(by_symbol), 2):
                first = by_symbol[first_symbol]
                second = by_symbol[second_symbol]
                result.append(
                    PairMarketData(
                        dataset_a=first[0],
                        candles_a=first[1],
                        funding_a=first[2],
                        dataset_b=second[0],
                        candles_b=second[1],
                        funding_b=second[2],
                    )
                )
        return result

    loaded = list(await asyncio.gather(*(load_dataset(dataset_id) for dataset_id in dataset_ids)))
    markets = build_pair_markets(loaded, scope="campaign")
    specs = (
        [PairStrategySpec.model_validate(item) for item in request.strategy_specs]
        if request.strategy_specs
        else list(PAIR_STRATEGY_LIBRARY)
    )
    lineage_id = pair_research_lineage_id(markets)
    historical_payloads = await asyncio.to_thread(
        store.list_relative_value_campaigns,
        10000,
    )
    pair_ids = {market.id for market in markets}
    lineage_history = [
        payload
        for payload in historical_payloads
        if payload.get("research_lineage_id") == lineage_id
        or set(payload.get("research_pair_ids") or []) == pair_ids
    ]
    parent_report = None
    if request.parent_campaign_id:
        parent_payload = await asyncio.to_thread(
            store.load_relative_value_campaign,
            request.parent_campaign_id,
        )
        if parent_payload is None:
            raise HTTPException(404, "parent relative-value campaign not found")
        parent_report = PairResearchCampaignReport.model_validate(parent_payload)
        if not parent_report.strategy_fingerprints or not parent_report.research_lineage_id:
            raise HTTPException(409, "legacy parent campaign lacks immutable lineage evidence")
        if parent_report.parent_campaign_id is not None:
            raise HTTPException(
                409,
                "bounded relative-value research permits only one successor on this lineage",
            )
        if any(
            payload.get("parent_campaign_id") == parent_report.id for payload in lineage_history
        ):
            raise HTTPException(
                409,
                "bounded relative-value research permits only one successor per research phase",
            )
        if pair_ids != set(parent_report.research_pair_ids):
            raise HTTPException(
                422,
                "relative-value successor must use the exact parent research lineage",
            )
        current_fingerprints = {spec.fingerprint for spec in specs}
        parent_fingerprints = set(parent_report.strategy_fingerprints)
        if not parent_fingerprints.issubset(current_fingerprints):
            raise HTTPException(422, "successor must retain every frozen parent hypothesis")
        new_fingerprints = current_fingerprints - parent_fingerprints
        allowed_children = {
            item.strategy_spec.fingerprint for item in parent_report.recommended_revisions
        }
        if not new_fingerprints or len(new_fingerprints) > 3:
            raise HTTPException(422, "successor requires one to three bounded child hypotheses")
        if not new_fingerprints.issubset(allowed_children):
            raise HTTPException(422, "successor contains a child not frozen by its parent")
    configuration = configuration_service.current_configuration()
    research_configuration = {
        "fee_bps": str(configuration.fee_bps),
        "slippage_bps": str(configuration.slippage_bps),
        "fold_count": request.fold_count,
        "formation_fraction": str(request.formation_fraction),
        "pair_campaign_engine_version": PAIR_CAMPAIGN_ENGINE_VERSION,
        "pair_backtest_engine_version": PAIR_BACKTEST_ENGINE_VERSION,
        "blind_set_id": blind_set_id,
    }
    research_configuration_fingerprint = sha256(
        json.dumps(
            research_configuration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]

    # Older pair campaigns predate the permanent SQL lineage ledger. Backfill
    # them before registering the current experiment so repeated use of the
    # same development data can never reset multiplicity or sequence penalties.
    registered_experiments = {
        str(item["experiment_id"])
        for item in await asyncio.to_thread(
            store.list_research_campaign_experiments,
            lineage_id,
        )
    }
    prior_fingerprints: set[str] = set()
    for historical in reversed(lineage_history):
        historical_fingerprints = sorted(
            {str(item) for item in historical.get("strategy_fingerprints") or []}
        )
        prior_fingerprints.update(historical_fingerprints)
        if not historical_fingerprints:
            continue
        historical_configuration_fingerprint = str(
            historical.get("research_configuration_fingerprint") or "legacy_pair_unknown"
        )
        historical_experiment_id = str(
            historical.get("research_experiment_id")
            or research_experiment_fingerprint(
                lineage_id=lineage_id,
                engine_version=int(historical.get("engine_version") or 0),
                research_configuration_fingerprint=(historical_configuration_fingerprint),
                strategy_fingerprints=historical_fingerprints,
            )
        )
        if historical_experiment_id in registered_experiments:
            continue
        await asyncio.to_thread(
            store.register_research_campaign_experiment,
            lineage_id=lineage_id,
            experiment_id=historical_experiment_id,
            campaign_id=str(historical.get("id") or historical_experiment_id),
            engine_version=int(historical.get("engine_version") or 0),
            research_configuration_fingerprint=(historical_configuration_fingerprint),
            strategy_fingerprints=historical_fingerprints,
            blind_set_id=(
                str(historical.get("blind_set_id"))
                if historical.get("blind_set_id")
                else blind_set_id
            ),
        )
        registered_experiments.add(historical_experiment_id)

    strategy_fingerprints = [spec.fingerprint for spec in specs]
    experiment_id = research_experiment_fingerprint(
        lineage_id=lineage_id,
        engine_version=PAIR_CAMPAIGN_ENGINE_VERSION,
        research_configuration_fingerprint=research_configuration_fingerprint,
        strategy_fingerprints=strategy_fingerprints,
    )
    campaign_id = f"paircampaign_{uuid4().hex[:16]}"
    try:
        ledger_summary = await asyncio.to_thread(
            store.register_research_campaign_experiment,
            lineage_id=lineage_id,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            engine_version=PAIR_CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
            blind_set_id=blind_set_id,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if ledger_summary.get("replayed"):
        existing_payload = await asyncio.to_thread(
            store.load_relative_value_campaign,
            str(ledger_summary["campaign_id"]),
        )
        if existing_payload is not None:
            return PairResearchCampaignReport.model_validate(existing_payload)
        campaign_id = str(ledger_summary["campaign_id"])

    if parent_report is not None:
        experiment_kind = "bounded_revision"
        predecessor_campaign_id = parent_report.id
    elif lineage_history:
        experiment_kind = "structural_extension"
        predecessor_campaign_id = str(lineage_history[0].get("id") or "") or None
    else:
        experiment_kind = "initial"
        predecessor_campaign_id = None
    report = await asyncio.to_thread(
        run_pair_research_campaign,
        markets,
        strategy_specs=specs,
        initial_equity=configuration.paper_equity,
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        fold_count=request.fold_count,
        formation_fraction=request.formation_fraction,
        report_id=campaign_id,
        report_sink=store.save_relative_value_report,
        parent_campaign_id=(parent_report.id if parent_report is not None else None),
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        research_experiment_id=experiment_id,
        research_configuration_fingerprint=research_configuration_fingerprint,
        prior_hypothesis_fingerprints=sorted(prior_fingerprints),
        cumulative_hypothesis_count=int(ledger_summary["cumulative_hypothesis_count"]),
        sequential_experiment_count=int(ledger_summary["sequential_experiment_count"]),
        effective_trial_count=int(ledger_summary["effective_trial_count"]),
        new_trial_variant_count=int(ledger_summary["new_trial_variant_count"]),
        generate_failure_revisions=(
            request.generate_failure_revisions if parent_report is None else False
        ),
        include_blind_pending_reason=not bool(blind_dataset_ids),
    )
    if blind_dataset_ids and report.provisional_selected_strategy_id is None:
        report = report.model_copy(
            update={
                "blind_set_id": blind_set_id,
                "selection_reasons": [
                    *report.selection_reasons,
                    "sealed blind set remained unopened because development "
                    "evidence did not nominate one candidate",
                ],
            }
        )
    elif blind_dataset_ids:
        selected_spec = next(
            spec
            for spec in specs
            if pair_strategy_id(spec) == report.provisional_selected_strategy_id
        )
        blind_consumption_id = f"blind_eval_{uuid4().hex[:16]}"
        reserved = await asyncio.to_thread(
            store.reserve_blind_set,
            blind_set_id=blind_set_id,
            strategy_fingerprint=selected_spec.fingerprint,
            evaluation_id=blind_consumption_id,
        )
        if not reserved:
            existing = await asyncio.to_thread(
                store.load_blind_set_consumption,
                blind_set_id,
            )
            raise HTTPException(
                409,
                f"sealed blind set has already been opened and cannot be reused: {existing}",
            )
        await asyncio.to_thread(
            store.mark_research_lineage_blind_opened,
            lineage_id=lineage_id,
            blind_consumption_id=blind_consumption_id,
        )
        blind_loaded = list(
            await asyncio.gather(
                *(load_dataset(dataset_id, blind=True) for dataset_id in blind_dataset_ids)
            )
        )
        blind_markets = build_pair_markets(blind_loaded, scope="sealed blind evaluation")
        blind_report = await asyncio.to_thread(
            run_pair_research_campaign,
            blind_markets,
            strategy_specs=[selected_spec],
            initial_equity=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            fold_count=request.fold_count,
            formation_fraction=request.formation_fraction,
            cumulative_hypothesis_count=report.cumulative_hypothesis_count,
            sequential_experiment_count=report.sequential_experiment_count,
            effective_trial_count=report.effective_trial_count,
            generate_failure_revisions=False,
            include_blind_pending_reason=False,
        )
        blind_candidate = blind_report.candidates[0]
        blind_passed = blind_candidate.cross_market_passed
        blind_metadata = metadata[len(dataset_ids) :]
        report = report.model_copy(
            update={
                "blind_set_id": blind_set_id,
                "blind_consumption_id": blind_consumption_id,
                "independent_blind_test_completed": True,
                "blind_selected_candidate_passed": blind_passed,
                "blind_market_count": len(blind_markets),
                "blind_evidence_fingerprint": _sealed_blind_evidence_fingerprint(
                    strategy_fingerprint=selected_spec.fingerprint,
                    candidate=blind_candidate,
                    datasets=blind_metadata,
                ),
                "selection_reasons": [
                    *report.selection_reasons,
                    (
                        "the frozen development candidate passed the one-time "
                        "sealed blind evaluation"
                        if blind_passed
                        else "the frozen development candidate failed the one-time "
                        "sealed blind evaluation"
                    ),
                    "blind performance metrics are intentionally withheld; only "
                    "pass/fail evidence is disclosed",
                ],
            }
        )
    if report.blind_selected_candidate_passed:
        selected_spec = next(
            spec
            for spec in specs
            if pair_strategy_id(spec) == report.provisional_selected_strategy_id
        )
        development_candidate = next(
            candidate
            for candidate in report.candidates
            if candidate.library_entry_id == report.provisional_selected_strategy_id
        )
        certificate = build_multi_leg_execution_certificate(
            strategy_kind="pair",
            campaign_id=report.id,
            campaign_engine_version=report.engine_version,
            strategy_id=report.provisional_selected_strategy_id,
            strategy_fingerprint=selected_spec.fingerprint,
            research_lineage_id=report.research_lineage_id,
            blind_set_id=report.blind_set_id,
            blind_consumption_id=report.blind_consumption_id,
            blind_evidence_fingerprint=report.blind_evidence_fingerprint,
            development_evidence={
                "candidate": development_candidate.model_dump(mode="json"),
                "pbo": report.pbo.model_dump(mode="json"),
                "research_pair_ids": report.research_pair_ids,
                "effective_trial_count": report.effective_trial_count,
                "research_configuration_fingerprint": (report.research_configuration_fingerprint),
            },
            strategy_spec_payload=selected_spec.model_dump(mode="json"),
            allowed_execution_groups=[
                [
                    f"{market.dataset_a.exchange}:"
                    f"{market.dataset_a.market_type}:{market.dataset_a.symbol}",
                    f"{market.dataset_b.exchange}:"
                    f"{market.dataset_b.market_type}:{market.dataset_b.symbol}",
                ]
                for market in markets
            ],
            allowed_timeframes=sorted({market.dataset_a.timeframe for market in markets}),
            development_passed=development_candidate.cross_market_passed,
            pbo_passed=report.pbo.passed,
            independent_blind_test_completed=report.independent_blind_test_completed,
            blind_selected_candidate_passed=report.blind_selected_candidate_passed,
        )
        report = report.model_copy(
            update={
                "execution_eligible": True,
                "execution_certificate": certificate,
                "selection_reasons": [
                    reason for reason in report.selection_reasons if "双腿原子下单" not in reason
                ]
                + [
                    "sealed-blind PASS is bound to a fail-closed multi-leg "
                    "execution certificate; venue commissioning remains mandatory"
                ],
            }
        )
    await asyncio.to_thread(store.save_relative_value_campaign, report)
    if report.blind_consumption_id:
        await asyncio.to_thread(
            store.complete_blind_set_consumption,
            evaluation_id=report.blind_consumption_id,
            campaign_id=report.id,
            passed=report.blind_selected_candidate_passed,
        )
    add_audit(
        "research",
        "相对价值跨配对 Campaign 完成",
        campaign_id=report.id,
        pair_markets=len(markets),
        strategies=len(specs),
        pbo_passed=report.pbo.passed,
        selected=report.provisional_selected_strategy_id or "none",
        execution_enabled=report.execution_eligible,
    )
    return report


@app.get(
    "/api/research/relative-value/campaigns",
    response_model=list[PairResearchCampaignReport],
)
async def list_relative_value_campaigns(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_relative_value_campaigns,
        min(max(limit, 1), 200),
    )
    return [PairResearchCampaignReport.model_validate(payload) for payload in payloads]


@app.get(
    "/api/research/relative-value/campaigns/{campaign_id}",
    response_model=PairResearchCampaignReport,
)
async def get_relative_value_campaign(campaign_id: str):
    payload = await asyncio.to_thread(store.load_relative_value_campaign, campaign_id)
    if payload is None:
        raise HTTPException(404, "relative-value campaign not found")
    return PairResearchCampaignReport.model_validate(payload)


async def _load_verified_carry_dataset(dataset_id: str, *, blind: bool = False):
    payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
    if not payload:
        raise HTTPException(404, f"research dataset not found: {dataset_id}")
    dataset = ResearchDataset.model_validate(payload)
    if not blind and (dataset.research_role == "blind" or dataset.blind_sealed):
        raise HTTPException(422, "sealed blind datasets cannot be used for carry selection")
    if blind and (dataset.research_role != "blind" or not dataset.blind_sealed):
        raise HTTPException(422, "carry blind dataset is not sealed")
    (
        candle_payloads,
        funding_payloads,
        open_interest_payloads,
        positioning_payloads,
        basis_payloads,
        order_flow_payloads,
        liquidation_flow_payloads,
    ) = await asyncio.gather(
        asyncio.to_thread(store.load_research_dataset_candles, dataset_id),
        asyncio.to_thread(store.load_research_dataset_funding, dataset_id),
        asyncio.to_thread(store.load_research_dataset_open_interest, dataset_id),
        asyncio.to_thread(store.load_research_dataset_positioning, dataset_id),
        asyncio.to_thread(store.load_research_dataset_basis, dataset_id),
        asyncio.to_thread(store.load_research_dataset_order_flow, dataset_id),
        asyncio.to_thread(store.load_research_dataset_liquidation_flow, dataset_id),
    )
    candles = [Candle.model_validate(item) for item in candle_payloads]
    funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
    verification = verify_research_dataset(
        dataset,
        candles,
        funding,
        [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
        [PositioningPoint.model_validate(item) for item in positioning_payloads],
        [BasisPoint.model_validate(item) for item in basis_payloads],
        [OrderFlowBar.model_validate(item) for item in order_flow_payloads],
        [LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads],
    )
    if not verification["verified"]:
        raise HTTPException(409, f"research dataset integrity failed: {dataset_id}")
    return dataset, candles, funding


@app.get(
    "/api/research/carry/strategy-library",
    response_model=list[CarryStrategySpec],
)
async def get_carry_strategy_library():
    """Expose immutable public-prior carry hypotheses; none are pre-approved."""
    return list(CARRY_STRATEGY_LIBRARY)


@app.post(
    "/api/research/carry/backtests",
    response_model=list[CarryBacktestReport],
)
async def run_carry_backtests(request: CarryResearchRequest):
    spot, perpetual = await asyncio.gather(
        _load_verified_carry_dataset(request.spot_dataset_id),
        _load_verified_carry_dataset(request.perpetual_dataset_id),
    )
    specs = (
        [CarryStrategySpec.model_validate(item) for item in request.strategy_specs]
        if request.strategy_specs
        else list(CARRY_STRATEGY_LIBRARY)
    )
    aligned_count = len(
        {item.timestamp for item in spot[1]} & {item.timestamp for item in perpetual[1]}
    )
    reports = []
    configuration = configuration_service.current_configuration()
    for spec in specs:
        report = await asyncio.to_thread(
            run_carry_backtest,
            spot_dataset=spot[0],
            spot_candles=spot[1],
            perpetual_dataset=perpetual[0],
            perpetual_candles=perpetual[1],
            funding_rates=perpetual[2],
            strategy_spec=spec,
            initial_equity=configuration.paper_equity,
            spot_fee_bps=configuration.fee_bps,
            perpetual_fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            research_trial_count=len(specs),
            evaluation_start_index=max(
                spec.confirmation_bars,
                math.ceil(aligned_count * float(request.formation_fraction)),
            ),
        )
        await asyncio.to_thread(store.save_carry_report, report)
        reports.append(report)
    return reports


@app.get(
    "/api/research/carry/backtests",
    response_model=list[CarryBacktestReport],
)
async def list_carry_backtests(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_carry_reports,
        min(max(limit, 1), 200),
    )
    return [CarryBacktestReport.model_validate(payload) for payload in payloads]


@app.post(
    "/api/research/carry/campaigns",
    response_model=CarryResearchCampaignReport,
)
async def run_carry_campaign(request: CarryCampaignRequest):
    dataset_ids = list(dict.fromkeys(request.dataset_ids))
    if len(dataset_ids) != len(request.dataset_ids):
        raise HTTPException(422, "carry campaign datasets must be unique")
    blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
    if len(blind_dataset_ids) != len(request.blind_dataset_ids):
        raise HTTPException(422, "carry blind datasets must be unique")
    if set(dataset_ids) & set(blind_dataset_ids):
        raise HTTPException(422, "development and blind datasets must be disjoint")
    metadata_payloads = await asyncio.gather(
        *(
            asyncio.to_thread(store.load_research_dataset, dataset_id)
            for dataset_id in [*dataset_ids, *blind_dataset_ids]
        )
    )
    if any(payload is None for payload in metadata_payloads):
        missing = [
            dataset_id
            for dataset_id, payload in zip(
                [*dataset_ids, *blind_dataset_ids], metadata_payloads, strict=True
            )
            if payload is None
        ]
        raise HTTPException(404, f"research dataset not found: {missing}")
    metadata = [ResearchDataset.model_validate(payload) for payload in metadata_payloads]
    try:
        blind_set_id = _validate_sealed_blind_scope(
            metadata[: len(dataset_ids)],
            metadata[len(dataset_ids) :],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    loaded = await asyncio.gather(
        *(_load_verified_carry_dataset(dataset_id) for dataset_id in dataset_ids)
    )

    def build_carry_markets(loaded_items: list[tuple], *, scope: str) -> list[CarryMarketData]:
        grouped: dict[tuple[str, str, str], dict[str, tuple]] = {}
        for item in loaded_items:
            dataset = item[0]
            if dataset.market_type not in {"spot", "swap"}:
                raise HTTPException(422, f"{scope} accepts only spot and swap datasets")
            key = (dataset.exchange, dataset.symbol, dataset.timeframe)
            market_types = grouped.setdefault(key, {})
            if dataset.market_type in market_types:
                raise HTTPException(422, f"duplicate carry leg: {key} {dataset.market_type}")
            market_types[dataset.market_type] = item
        incomplete = [key for key, legs in grouped.items() if set(legs) != {"spot", "swap"}]
        if incomplete:
            raise HTTPException(422, f"carry markets require exact spot+swap legs: {incomplete}")
        scope_counts: dict[tuple[str, str], int] = {}
        for exchange, _symbol, timeframe in grouped:
            key = (exchange, timeframe)
            scope_counts[key] = scope_counts.get(key, 0) + 1
        if len({exchange for exchange, _ in scope_counts}) < 2:
            raise HTTPException(422, f"{scope} requires at least two exchanges")
        if any(count < 3 for count in scope_counts.values()):
            raise HTTPException(
                422,
                f"each {scope} exchange/timeframe scope requires at least three symbols",
            )
        return [
            CarryMarketData(
                spot_dataset=legs["spot"][0],
                spot_candles=legs["spot"][1],
                perpetual_dataset=legs["swap"][0],
                perpetual_candles=legs["swap"][1],
                funding_rates=legs["swap"][2],
            )
            for _key, legs in sorted(grouped.items())
        ]

    markets = build_carry_markets(list(loaded), scope="carry campaign")
    specs = (
        [CarryStrategySpec.model_validate(item) for item in request.strategy_specs]
        if request.strategy_specs
        else list(CARRY_STRATEGY_LIBRARY)
    )
    lineage_id = carry_research_lineage_id(markets)
    history = [
        item
        for item in await asyncio.to_thread(store.list_carry_campaigns, 10000)
        if item.get("research_lineage_id") == lineage_id
    ]
    parent_report = None
    market_ids = {market.id for market in markets}
    if request.parent_campaign_id:
        parent_payload = await asyncio.to_thread(
            store.load_carry_campaign,
            request.parent_campaign_id,
        )
        if parent_payload is None:
            raise HTTPException(404, "parent carry campaign not found")
        parent_report = CarryResearchCampaignReport.model_validate(parent_payload)
        if parent_report.parent_campaign_id is not None:
            raise HTTPException(
                409,
                "bounded carry research permits only one successor on this lineage",
            )
        if any(item.get("parent_campaign_id") == parent_report.id for item in history):
            raise HTTPException(
                409,
                "bounded carry research permits only one successor per research phase",
            )
        if market_ids != set(parent_report.research_market_ids):
            raise HTTPException(422, "carry successor must use the exact parent lineage")
        current_fingerprints = {spec.fingerprint for spec in specs}
        parent_fingerprints = set(parent_report.strategy_fingerprints)
        if not parent_fingerprints.issubset(current_fingerprints):
            raise HTTPException(422, "carry successor must retain every parent hypothesis")
        new_fingerprints = current_fingerprints - parent_fingerprints
        allowed_children = {
            item.strategy_spec.fingerprint for item in parent_report.recommended_revisions
        }
        if not new_fingerprints or len(new_fingerprints) > 3:
            raise HTTPException(422, "carry successor requires one to three bounded children")
        if not new_fingerprints.issubset(allowed_children):
            raise HTTPException(422, "carry successor contains a child not frozen by its parent")
    prior_fingerprints = {
        str(fingerprint)
        for item in history
        for fingerprint in item.get("strategy_fingerprints") or []
    }
    configuration = configuration_service.current_configuration()
    research_configuration = {
        "spot_fee_bps": str(configuration.fee_bps),
        "perpetual_fee_bps": str(configuration.fee_bps),
        "slippage_bps": str(configuration.slippage_bps),
        "fold_count": request.fold_count,
        "formation_fraction": str(request.formation_fraction),
        "carry_campaign_engine_version": CARRY_CAMPAIGN_ENGINE_VERSION,
        "carry_backtest_engine_version": CARRY_BACKTEST_ENGINE_VERSION,
        "blind_set_id": blind_set_id,
    }
    configuration_fingerprint = sha256(
        json.dumps(
            research_configuration,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    strategy_fingerprints = [spec.fingerprint for spec in specs]
    experiment_id = research_experiment_fingerprint(
        lineage_id=lineage_id,
        engine_version=CARRY_CAMPAIGN_ENGINE_VERSION,
        research_configuration_fingerprint=configuration_fingerprint,
        strategy_fingerprints=strategy_fingerprints,
    )
    campaign_id = f"carrycampaign_{uuid4().hex[:16]}"
    try:
        ledger = await asyncio.to_thread(
            store.register_research_campaign_experiment,
            lineage_id=lineage_id,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            engine_version=CARRY_CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
            blind_set_id=blind_set_id,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if ledger.get("replayed"):
        payload = await asyncio.to_thread(
            store.load_carry_campaign,
            str(ledger["campaign_id"]),
        )
        if payload is not None:
            return CarryResearchCampaignReport.model_validate(payload)
        campaign_id = str(ledger["campaign_id"])
    if parent_report is not None:
        experiment_kind = "bounded_revision"
        predecessor_campaign_id = parent_report.id
    elif history:
        experiment_kind = "structural_extension"
        predecessor_campaign_id = str(history[0].get("id") or "") or None
    else:
        experiment_kind = "initial"
        predecessor_campaign_id = None
    report = await asyncio.to_thread(
        run_carry_research_campaign,
        markets,
        strategy_specs=specs,
        initial_equity=configuration.paper_equity,
        spot_fee_bps=configuration.fee_bps,
        perpetual_fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        fold_count=request.fold_count,
        formation_fraction=request.formation_fraction,
        cumulative_hypothesis_count=max(
            int(ledger["cumulative_hypothesis_count"]),
            len(prior_fingerprints | set(strategy_fingerprints)),
        ),
        sequential_experiment_count=int(ledger["sequential_experiment_count"]),
        effective_trial_count=int(ledger["effective_trial_count"]),
        new_trial_variant_count=int(ledger["new_trial_variant_count"]),
        research_experiment_id=experiment_id,
        research_configuration_fingerprint=configuration_fingerprint,
        report_id=campaign_id,
        report_sink=store.save_carry_report,
        parent_campaign_id=(parent_report.id if parent_report is not None else None),
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        generate_failure_revisions=request.generate_failure_revisions,
        include_blind_pending_reason=not bool(blind_dataset_ids),
    )
    if blind_dataset_ids and report.provisional_selected_strategy_id is None:
        report = report.model_copy(
            update={
                "blind_set_id": blind_set_id,
                "selection_reasons": [
                    *report.selection_reasons,
                    "sealed blind set remained unopened because development "
                    "evidence did not nominate one candidate",
                ],
            }
        )
    elif blind_dataset_ids:
        selected_spec = next(
            spec
            for spec in specs
            if carry_strategy_id(spec) == report.provisional_selected_strategy_id
        )
        blind_consumption_id = f"blind_eval_{uuid4().hex[:16]}"
        reserved = await asyncio.to_thread(
            store.reserve_blind_set,
            blind_set_id=blind_set_id,
            strategy_fingerprint=selected_spec.fingerprint,
            evaluation_id=blind_consumption_id,
        )
        if not reserved:
            existing = await asyncio.to_thread(
                store.load_blind_set_consumption,
                blind_set_id,
            )
            raise HTTPException(
                409,
                f"sealed blind set has already been opened and cannot be reused: {existing}",
            )
        await asyncio.to_thread(
            store.mark_research_lineage_blind_opened,
            lineage_id=lineage_id,
            blind_consumption_id=blind_consumption_id,
        )
        blind_loaded = list(
            await asyncio.gather(
                *(
                    _load_verified_carry_dataset(dataset_id, blind=True)
                    for dataset_id in blind_dataset_ids
                )
            )
        )
        blind_markets = build_carry_markets(
            blind_loaded,
            scope="sealed blind carry evaluation",
        )
        blind_report = await asyncio.to_thread(
            run_carry_research_campaign,
            blind_markets,
            strategy_specs=[selected_spec],
            initial_equity=configuration.paper_equity,
            spot_fee_bps=configuration.fee_bps,
            perpetual_fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            fold_count=request.fold_count,
            formation_fraction=request.formation_fraction,
            cumulative_hypothesis_count=report.cumulative_hypothesis_count,
            sequential_experiment_count=report.sequential_experiment_count,
            effective_trial_count=report.effective_trial_count,
            generate_failure_revisions=False,
            include_blind_pending_reason=False,
        )
        blind_candidate = blind_report.candidates[0]
        blind_passed = blind_candidate.cross_market_passed
        blind_metadata = metadata[len(dataset_ids) :]
        report = report.model_copy(
            update={
                "blind_set_id": blind_set_id,
                "blind_consumption_id": blind_consumption_id,
                "independent_blind_test_completed": True,
                "blind_selected_candidate_passed": blind_passed,
                "blind_market_count": len(blind_markets),
                "blind_evidence_fingerprint": _sealed_blind_evidence_fingerprint(
                    strategy_fingerprint=selected_spec.fingerprint,
                    candidate=blind_candidate,
                    datasets=blind_metadata,
                ),
                "selection_reasons": [
                    *report.selection_reasons,
                    (
                        "the frozen carry candidate passed the one-time sealed blind evaluation"
                        if blind_passed
                        else "the frozen carry candidate failed the one-time "
                        "sealed blind evaluation"
                    ),
                    "blind performance metrics are intentionally withheld; only "
                    "pass/fail evidence is disclosed",
                ],
            }
        )
    if report.blind_selected_candidate_passed:
        selected_spec = next(
            spec
            for spec in specs
            if carry_strategy_id(spec) == report.provisional_selected_strategy_id
        )
        development_candidate = next(
            candidate
            for candidate in report.candidates
            if candidate.library_entry_id == report.provisional_selected_strategy_id
        )
        certificate = build_multi_leg_execution_certificate(
            strategy_kind="carry",
            campaign_id=report.id,
            campaign_engine_version=report.engine_version,
            strategy_id=report.provisional_selected_strategy_id,
            strategy_fingerprint=selected_spec.fingerprint,
            research_lineage_id=report.research_lineage_id,
            blind_set_id=report.blind_set_id,
            blind_consumption_id=report.blind_consumption_id,
            blind_evidence_fingerprint=report.blind_evidence_fingerprint,
            development_evidence={
                "candidate": development_candidate.model_dump(mode="json"),
                "pbo": report.pbo.model_dump(mode="json"),
                "research_market_ids": report.research_market_ids,
                "effective_trial_count": report.effective_trial_count,
                "research_configuration_fingerprint": (report.research_configuration_fingerprint),
            },
            strategy_spec_payload=selected_spec.model_dump(mode="json"),
            allowed_execution_groups=[
                [
                    f"{market.spot_dataset.exchange}:spot:{market.spot_dataset.symbol}",
                    f"{market.perpetual_dataset.exchange}:swap:{market.perpetual_dataset.symbol}",
                ]
                for market in markets
            ],
            allowed_timeframes=sorted({market.spot_dataset.timeframe for market in markets}),
            development_passed=development_candidate.cross_market_passed,
            pbo_passed=report.pbo.passed,
            independent_blind_test_completed=report.independent_blind_test_completed,
            blind_selected_candidate_passed=report.blind_selected_candidate_passed,
        )
        report = report.model_copy(
            update={
                "execution_eligible": True,
                "execution_certificate": certificate,
                "selection_reasons": [
                    reason
                    for reason in report.selection_reasons
                    if "partial-fill compensation" not in reason
                    and "cannot issue an execution certificate" not in reason
                ]
                + [
                    "sealed-blind PASS is bound to a fail-closed multi-leg "
                    "execution certificate; venue commissioning remains mandatory"
                ],
            }
        )
    await asyncio.to_thread(store.save_carry_campaign, report)
    if report.blind_consumption_id:
        await asyncio.to_thread(
            store.complete_blind_set_consumption,
            evaluation_id=report.blind_consumption_id,
            campaign_id=report.id,
            passed=report.blind_selected_candidate_passed,
        )
    add_audit(
        "research",
        "Spot-perpetual carry Campaign completed",
        campaign_id=report.id,
        markets=len(markets),
        strategies=len(specs),
        pbo_passed=report.pbo.passed,
        selected=report.provisional_selected_strategy_id or "none",
        execution_enabled=report.execution_eligible,
    )
    return report


@app.get(
    "/api/research/carry/campaigns",
    response_model=list[CarryResearchCampaignReport],
)
async def list_carry_campaigns(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_carry_campaigns,
        min(max(limit, 1), 200),
    )
    return [CarryResearchCampaignReport.model_validate(payload) for payload in payloads]


@app.get(
    "/api/research/carry/campaigns/{campaign_id}",
    response_model=CarryResearchCampaignReport,
)
async def get_carry_campaign(campaign_id: str):
    payload = await asyncio.to_thread(store.load_carry_campaign, campaign_id)
    if payload is None:
        raise HTTPException(404, "carry campaign not found")
    return CarryResearchCampaignReport.model_validate(payload)


@app.post(
    "/api/research/campaigns/{campaign_id}/revise",
    response_model=CandidateRevisionResponse,
)
async def revise_campaign_strategy(
    campaign_id: str,
    request: CampaignRevisionRequest,
):
    payload = await asyncio.to_thread(
        store.load_research_campaign,
        campaign_id,
    )
    if not payload:
        raise HTTPException(404, "research campaign not found")
    report = ResearchCampaignReport.model_validate(payload)
    if report.blind_consumption_id:
        raise HTTPException(
            409,
            "a consumed blind Campaign is terminal evidence; retire the hypothesis "
            "lineage and use a newly sealed holdout for any independent successor",
        )
    if report.parent_campaign_id is not None:
        raise HTTPException(
            409,
            "a bounded successor cannot generate a second revision generation",
        )
    if report.authorized_revision_candidate_ids:
        raise HTTPException(
            409,
            "this Campaign has already frozen its one permitted revision generation",
        )
    lineage_history = await asyncio.to_thread(store.list_research_campaigns, 1000)
    if any(item.get("parent_campaign_id") == report.id for item in lineage_history):
        raise HTTPException(
            409,
            "this Campaign already has its one permitted successor",
        )
    aggregate = (
        next(
            (
                item
                for item in report.candidates
                if item.library_entry_id == request.library_entry_id
            ),
            None,
        )
        if request.library_entry_id
        else report.candidates[0]
        if report.candidates
        else None
    )
    if aggregate is None:
        raise HTTPException(404, "campaign strategy aggregate not found")
    if aggregate.library_entry_id.startswith("candidate:"):
        base_candidate_id = aggregate.library_entry_id.removeprefix("candidate:")
    else:
        base_candidate_id = f"cand_{aggregate.library_entry_id}"
    candidate_payload = await asyncio.to_thread(
        store.load_strategy_candidate,
        base_candidate_id,
    )
    candidate = (
        parse_strategy_candidate_payload(
            candidate_payload,
            audit_context="campaign_failure_revision",
        )
        if candidate_payload
        else None
    )
    if candidate is None:
        if aggregate.library_entry_id.startswith("candidate:"):
            raise HTTPException(409, "campaign candidate is stale or missing")
        try:
            entry = library_entry(aggregate.library_entry_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        candidate = create_candidate(entry.validated_spec)
        candidate.id = base_candidate_id
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
    cells = [cell for cell in report.cells if cell.library_entry_id == aggregate.library_entry_id]
    attribution, revisions = generate_campaign_failure_revisions(
        candidate,
        aggregate,
        cells,
        max_revisions=request.max_revisions,
    )
    for revision in revisions:
        await asyncio.to_thread(
            store.save_strategy_candidate,
            revision.candidate,
        )
    report = report.model_copy(
        update={
            "authorized_revision_candidate_ids": [revision.candidate.id for revision in revisions],
            "authorized_revision_fingerprints": [
                revision.candidate.spec_fingerprint for revision in revisions
            ],
            "revision_source_library_entry_id": aggregate.library_entry_id,
        }
    )
    await asyncio.to_thread(store.save_research_campaign, report)
    add_audit(
        "research",
        "Campaign 聚合失败已生成单变量策略修订",
        campaign_id=campaign_id,
        library_entry_id=aggregate.library_entry_id,
        parent_candidate_id=candidate.id,
        failure_codes=",".join(attribution.codes),
        revision_count=len(revisions),
    )
    return CandidateRevisionResponse(
        parent_candidate_id=candidate.id,
        attribution=attribution,
        revisions=revisions,
    )


@app.post(
    "/api/research/campaigns/{campaign_id}/revise-causal-policy",
    response_model=CausalPolicyRevisionResponse,
)
async def revise_campaign_causal_policy(
    campaign_id: str,
    request: CausalPolicyRevisionRequest,
) -> CausalPolicyRevisionResponse:
    payload = await asyncio.to_thread(store.load_research_campaign, campaign_id)
    if not payload:
        raise HTTPException(404, "research campaign not found")
    report = ResearchCampaignReport.model_validate(payload)
    if not report.causal_policy_definitions:
        raise HTTPException(422, "Campaign does not contain causal policy hypotheses")
    if report.blind_consumption_id:
        raise HTTPException(409, "a consumed blind Campaign is terminal evidence")
    if report.parent_campaign_id is not None:
        raise HTTPException(409, "a bounded successor cannot create another generation")
    if report.authorized_policy_revision_fingerprints:
        raise HTTPException(
            409,
            "this Campaign already froze its one permitted causal policy revision generation",
        )
    lineage_history = await asyncio.to_thread(store.list_research_campaigns, 1000)
    if any(item.get("parent_campaign_id") == report.id for item in lineage_history):
        raise HTTPException(409, "this Campaign already has its one permitted successor")
    aggregate = (
        next(
            (
                item
                for item in report.candidates
                if item.library_entry_id == request.campaign_entry_id
            ),
            None,
        )
        if request.campaign_entry_id
        else report.candidates[0]
        if report.candidates
        else None
    )
    if aggregate is None:
        raise HTTPException(404, "causal policy aggregate not found")
    definition = next(
        (
            item
            for item in report.causal_policy_definitions
            if item.get("campaign_entry_id") == aggregate.library_entry_id
        ),
        None,
    )
    if definition is None:
        raise HTTPException(409, "causal policy definition is missing from the Campaign")
    parent_spec = CausalPolicyCampaignSpec(
        id=str(definition["policy_id"]),
        library_entry_ids=[str(item["id"]) for item in definition["priors"]],
        maximum_candidates_per_bar=int(definition["maximum_candidates_per_bar"]),
        abstain_on_ambiguous_regime=bool(definition["abstain_on_ambiguous_regime"]),
        minimum_regime_confidence=Decimal(str(definition.get("minimum_regime_confidence", "0"))),
        minimum_signal_confidence=Decimal(str(definition.get("minimum_signal_confidence", "0"))),
        minimum_directional_consensus=int(definition.get("minimum_directional_consensus", 1)),
    )
    proposed_changes: list[tuple[str, object, str]] = []
    next_signal_confidence = min(
        parent_spec.minimum_signal_confidence + Decimal("0.10"),
        Decimal("0.90"),
    )
    if next_signal_confidence != parent_spec.minimum_signal_confidence:
        proposed_changes.append(
            ("minimum_signal_confidence", next_signal_confidence, "signal_confidence")
        )
    next_regime_confidence = min(
        parent_spec.minimum_regime_confidence + Decimal("0.10"),
        Decimal("0.90"),
    )
    if next_regime_confidence != parent_spec.minimum_regime_confidence:
        proposed_changes.append(
            ("minimum_regime_confidence", next_regime_confidence, "regime_confidence")
        )
    if parent_spec.maximum_candidates_per_bar < 20:
        proposed_changes.append(
            (
                "maximum_candidates_per_bar",
                parent_spec.maximum_candidates_per_bar + 1,
                "candidate_breadth",
            )
        )
    revisions: list[CausalPolicyCampaignSpec] = []
    for field_name, value, label in proposed_changes[: request.max_revisions]:
        token = str(value).replace(".", "")
        revision = parent_spec.model_copy(
            update={
                "id": f"{parent_spec.id}_{label}_{token}",
                field_name: value,
            }
        )
        changed_fields = {
            key
            for key, current in parent_spec.model_dump(mode="json").items()
            if key != "id" and revision.model_dump(mode="json")[key] != current
        }
        if changed_fields != {field_name}:
            raise RuntimeError("causal policy revision must change exactly one control")
        revisions.append(CausalPolicyCampaignSpec.model_validate(revision))
    if not revisions:
        raise HTTPException(409, "causal policy has no remaining bounded revision")
    frozen_entries = [build_causal_policy_campaign_entry(item) for item in revisions]
    report = report.model_copy(
        update={
            "authorized_policy_revision_definitions": [
                item.frozen_definition for item in frozen_entries
            ],
            "authorized_policy_revision_fingerprints": [
                item.policy_fingerprint for item in frozen_entries
            ],
            "revision_source_library_entry_id": aggregate.library_entry_id,
        }
    )
    await asyncio.to_thread(store.save_research_campaign, report)
    add_audit(
        "research",
        "Campaign failure froze one bounded causal policy revision generation",
        campaign_id=campaign_id,
        campaign_entry_id=aggregate.library_entry_id,
        parent_policy_fingerprint=str(definition["policy_fingerprint"]),
        revision_count=len(revisions),
    )
    return CausalPolicyRevisionResponse(
        parent_campaign_entry_id=aggregate.library_entry_id,
        parent_policy_fingerprint=str(definition["policy_fingerprint"]),
        failure_reasons=aggregate.reasons,
        revisions=revisions,
    )


@app.post(
    "/api/research/datasets/prepare",
    response_model=ResearchUniverseReport,
)
async def prepare_research_datasets(request: ResearchUniverseRequest):
    try:
        report = await prepare_research_universe(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    add_audit(
        "research",
        "批量研究数据集抓取完成",
        requested_markets=report.requested_markets,
        completed_markets=report.completed_markets,
        healthy_markets=report.healthy_markets,
        total_candles=report.total_candles,
    )
    return report


@app.post(
    "/api/research/holdouts/seal",
    response_model=SealedHoldoutReport,
)
async def seal_research_holdout_endpoint(
    request: SealedHoldoutRequest,
) -> SealedHoldoutReport:
    try:
        report = await asyncio.to_thread(seal_research_holdout, store, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    add_audit(
        "research",
        "冻结研究范围已切分为开发集与单次盲测集",
        blind_set_id=report.blind_set_id,
        universe_snapshot_id=report.universe_snapshot_id,
        market_count=len(report.datasets),
        development_dataset_count=len(report.development_dataset_ids),
        blind_dataset_count=len(report.blind_dataset_ids),
    )
    return report


async def run_research_universe_job(job: ResearchUniverseJob) -> None:
    job.status = ResearchJobStatus.RUNNING
    job.started_at = job.started_at or datetime.now(UTC)
    job.completed_at = None
    job.error = None
    job.completed_markets = 0
    job.healthy_markets = 0
    job.total_candles = 0
    job.last_cell = None
    job.report = None
    job.updated_at = datetime.now(UTC)
    await asyncio.to_thread(store.save_research_universe_job, job)

    async def persist_progress(
        completed: int,
        total: int,
        cell: ResearchUniverseCell,
    ) -> None:
        job.completed_markets = completed
        job.total_markets = total
        job.healthy_markets += int(cell.ok)
        job.total_candles += cell.candle_count
        job.last_cell = cell
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_research_universe_job, job)

    try:
        report = await prepare_research_universe(
            ResearchUniverseRequest.model_validate(job.request),
            progress_callback=persist_progress,
        )
        job.report = report
        job.total_markets = report.requested_markets
        job.completed_markets = report.completed_markets
        job.healthy_markets = report.healthy_markets
        job.total_candles = report.total_candles
        job.status = ResearchJobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)
    except asyncio.CancelledError:
        job.status = ResearchJobStatus.QUEUED
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_research_universe_job, job)
        raise
    except Exception as exc:
        job.status = ResearchJobStatus.FAILED
        job.error = str(getattr(exc, "detail", exc))[:1000]
        job.completed_at = datetime.now(UTC)
    finally:
        if job.status != ResearchJobStatus.QUEUED:
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_research_universe_job, job)


def schedule_research_universe_job(job: ResearchUniverseJob) -> None:
    task = asyncio.create_task(
        run_research_universe_job(job),
        name=f"research-universe:{job.id}",
    )
    universe_job_tasks.add(task)
    task.add_done_callback(universe_job_tasks.discard)


@app.post(
    "/api/research/universe-jobs",
    response_model=ResearchUniverseJob,
    status_code=202,
)
async def queue_research_universe_job(
    request: ResearchUniverseRequest,
) -> ResearchUniverseJob:
    normalized = request.model_dump(mode="json")
    normalized["symbols"] = sorted(
        {symbol.strip().upper().replace("-", "/") for symbol in request.symbols}
    )
    normalized["timeframes"] = sorted(
        {timeframe.strip().lower() for timeframe in request.timeframes}
    )
    normalized["exchange_ids"] = sorted(
        {exchange.strip().lower() for exchange in request.exchange_ids}
    )
    fingerprint = research_universe_request_fingerprint(normalized)
    for payload in await asyncio.to_thread(store.list_research_universe_jobs, 100):
        existing = ResearchUniverseJob.model_validate(payload)
        if existing.request_fingerprint == fingerprint and existing.status in {
            ResearchJobStatus.QUEUED,
            ResearchJobStatus.RUNNING,
        }:
            return existing
    total_markets = min(
        len(normalized["symbols"])
        * len(normalized["timeframes"])
        * max(len(normalized["exchange_ids"]), 1),
        request.max_markets,
    )
    job = ResearchUniverseJob(
        request=normalized,
        request_fingerprint=fingerprint,
        total_markets=total_markets,
    )
    await asyncio.to_thread(store.save_research_universe_job, job)
    schedule_research_universe_job(job)
    return job


@app.get(
    "/api/research/universe-snapshots",
    response_model=list[ResearchUniverseSnapshot],
)
async def list_research_universe_snapshots(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_research_universe_snapshots,
        min(max(limit, 1), 200),
    )
    snapshots: list[ResearchUniverseSnapshot] = []
    for payload in payloads:
        snapshot = ResearchUniverseSnapshot.model_validate(payload)
        try:
            validate_universe_snapshot(snapshot)
        except ValueError:
            # Preserve corrupt/tampered legacy evidence in storage and keep the
            # direct endpoint fail-closed with HTTP 409, but quarantine it from
            # the collection of usable snapshots so one bad artifact cannot
            # make every valid snapshot undiscoverable.
            continue
        snapshots.append(snapshot)
    return snapshots


@app.post(
    "/api/research/universe-snapshots/import-archive",
    response_model=ResearchUniverseSnapshot,
)
async def import_historical_universe_archive(
    request: HistoricalUniverseArchiveImportRequest,
):
    """Import an operator-supplied, hash-bound historical catalog artifact."""

    if request.as_of.tzinfo is None:
        raise HTTPException(422, "historical archive as_of must be timezone-aware")
    if set(request.catalogs) - SUPPORTED_RESEARCH_EXCHANGES:
        raise HTTPException(422, "historical archive contains an unsupported exchange")
    record_count = sum(len(items) for items in request.catalogs.values())
    if record_count > 5000:
        raise HTTPException(422, "historical archive supports at most 5000 instruments")
    try:
        snapshot = build_point_in_time_universe_snapshot(
            request.catalogs,
            policy=request.policy,
            as_of=request.as_of,
            archive_evidence=request.archive_evidence,
        )
        validate_universe_snapshot(snapshot)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(store.save_research_universe_snapshot, snapshot)
    add_audit(
        "research",
        "Historical point-in-time market catalog archive imported",
        universe_snapshot_id=snapshot.id,
        exchanges=len(snapshot.exchange_ids),
        instruments=len(snapshot.instruments),
        historical_survivorship_complete=snapshot.historical_survivorship_complete,
    )
    return snapshot


@app.post(
    "/api/research/catalog-journal/capture",
    response_model=list[CatalogJournalCaptureResult],
)
async def capture_market_catalog_journal(request: CatalogJournalCaptureRequest):
    exchanges = sorted({item.strip().lower() for item in request.exchange_ids})
    if set(exchanges) - SUPPORTED_RESEARCH_EXCHANGES:
        raise HTTPException(422, "catalog journal contains an unsupported exchange")
    unsupported_scopes = [
        f"{exchange}:{market_type}"
        for exchange in exchanges
        for market_type in request.market_types
        if (exchange, market_type) not in CATALOG_JOURNAL_SOURCE_URLS
    ]
    if unsupported_scopes:
        raise HTTPException(
            422,
            "catalog journal source is not configured: " + ",".join(unsupported_scopes),
        )
    results: list[CatalogJournalCaptureResult] = []
    for exchange in exchanges:
        for market_type in sorted(set(request.market_types)):
            try:
                results.append(await capture_catalog_journal_scope(exchange, market_type))
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(503, str(exc)) from exc
    return results


@app.post(
    "/api/research/official-archive-index/capture",
    response_model=list[OfficialArchiveIndex],
)
async def capture_official_archive_indexes(request: OfficialArchiveCaptureRequest):
    """Fetch exchange-owned archive directories on the server and seal raw hashes."""

    proxy_url = configuration_service.current_configuration().network_proxy_url
    archives: list[OfficialArchiveIndex] = []
    for exchange in sorted(set(request.exchange_ids)):
        try:
            archive = await asyncio.to_thread(
                download_official_archive_index,
                exchange,
                proxy_url=proxy_url,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                503,
                f"unable to capture {exchange} official archive index: {exc}",
            ) from exc
        await asyncio.to_thread(store.save_official_archive_index, archive)
        archives.append(archive)
        add_audit(
            "research",
            "Official historical archive index captured",
            archive_id=archive.id,
            exchange=archive.exchange,
            instruments=archive.normalized_usdt_perpetual_count,
            source_payload_sha256=archive.source_payload_sha256,
            historical_point_in_time_complete=False,
        )
    return archives


@app.get(
    "/api/research/official-archive-indexes",
    response_model=list[OfficialArchiveIndex],
)
async def list_official_archive_indexes(
    exchange: Literal["binance", "bybit"] | None = None,
    limit: int = 100,
):
    payloads = await asyncio.to_thread(
        partial(
            store.list_official_archive_indexes,
            exchange=exchange,
            limit=min(max(limit, 1), 1000),
        )
    )
    return [OfficialArchiveIndex.model_validate(item) for item in payloads]


@app.post(
    "/api/research/historical-anchor-scan",
    response_model=HistoricalAnchorScan,
)
async def create_historical_anchor_scan(request: HistoricalAnchorScanRequest):
    payload = await asyncio.to_thread(
        store.load_official_archive_index,
        request.official_archive_index_id,
    )
    if payload is None:
        raise HTTPException(404, "official archive index not found")
    archive = OfficialArchiveIndex.model_validate(payload)
    try:
        scan = await asyncio.to_thread(
            build_binance_historical_anchor_scan,
            archive,
            anchor_month=request.anchor_month,
            proxy_url=configuration_service.current_configuration().network_proxy_url,
            maximum_workers=request.maximum_workers,
        )
        await asyncio.to_thread(store.save_historical_anchor_scan, scan)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc
    add_audit(
        "research",
        "Official historical anchor candidate scan completed",
        scan_id=scan.id,
        archive_id=archive.id,
        anchor_month=scan.anchor_month.isoformat(),
        candidates=scan.candidate_count,
        observed=scan.observed_count,
        absent=scan.absent_count,
        historical_point_in_time_complete=False,
    )
    return scan


@app.get(
    "/api/research/historical-anchor-scans",
    response_model=list[HistoricalAnchorScan],
)
async def list_historical_anchor_scans(
    exchange: Literal["binance"] | None = None,
    limit: int = 100,
):
    payloads = await asyncio.to_thread(
        partial(
            store.list_historical_anchor_scans,
            exchange=exchange,
            limit=min(max(limit, 1), 1000),
        )
    )
    return [HistoricalAnchorScan.model_validate(item) for item in payloads]


@app.get(
    "/api/research/catalog-journal/snapshots",
    response_model=list[MarketCatalogJournalSnapshot],
)
async def list_market_catalog_journal_snapshots(
    exchange: str | None = None,
    market_type: Literal["spot", "swap"] | None = None,
    limit: int = 100,
):
    payloads = await asyncio.to_thread(
        partial(
            store.list_market_catalog_journal_snapshots,
            exchange=exchange.lower() if exchange else None,
            market_type=market_type,
            limit=min(max(limit, 1), 1000),
        )
    )
    return [MarketCatalogJournalSnapshot.model_validate(item) for item in payloads]


@app.get(
    "/api/research/catalog-journal/events",
    response_model=list[MarketCatalogChangeEvent],
)
async def list_market_catalog_journal_events(
    exchange: str | None = None,
    market_type: Literal["spot", "swap"] | None = None,
    limit: int = 1000,
):
    payloads = await asyncio.to_thread(
        partial(
            store.list_market_catalog_change_events,
            exchange=exchange.lower() if exchange else None,
            market_type=market_type,
            limit=min(max(limit, 1), 10000),
        )
    )
    return [MarketCatalogChangeEvent.model_validate(item) for item in payloads]


@app.get(
    "/api/research/catalog-journal/coverage",
    response_model=list[MarketCatalogJournalCoverage],
)
async def market_catalog_journal_coverage():
    snapshot_payloads, event_payloads = await asyncio.gather(
        asyncio.to_thread(store.list_market_catalog_journal_snapshots, limit=100000),
        asyncio.to_thread(store.list_market_catalog_change_events, limit=100000),
    )
    snapshots = [MarketCatalogJournalSnapshot.model_validate(item) for item in snapshot_payloads]
    events = [MarketCatalogChangeEvent.model_validate(item) for item in event_payloads]
    now = datetime.now(UTC)
    return [
        evaluate_catalog_journal_coverage(
            snapshots,
            events,
            exchange=exchange,
            market_type=market_type,
            now=now,
        )
        for exchange in ("binance", "okx", "bybit")
        for market_type in ("spot", "swap")
    ]


@app.get(
    "/api/research/universe-snapshots/{snapshot_id}",
    response_model=ResearchUniverseSnapshot,
)
async def get_research_universe_snapshot(snapshot_id: str):
    payload = await asyncio.to_thread(store.load_research_universe_snapshot, snapshot_id)
    if payload is None:
        raise HTTPException(404, "research universe snapshot not found")
    snapshot = ResearchUniverseSnapshot.model_validate(payload)
    try:
        validate_universe_snapshot(snapshot)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return snapshot


@app.get(
    "/api/research/universe-jobs",
    response_model=list[ResearchUniverseJob],
)
async def list_research_universe_jobs(limit: int = 50):
    payloads = await asyncio.to_thread(
        store.list_research_universe_jobs,
        min(max(limit, 1), 200),
    )
    return [ResearchUniverseJob.model_validate(payload) for payload in payloads]


@app.get(
    "/api/research/universe-jobs/{job_id}",
    response_model=ResearchUniverseJob,
)
async def get_research_universe_job(job_id: str):
    payload = await asyncio.to_thread(store.load_research_universe_job, job_id)
    if not payload:
        raise HTTPException(404, "research universe job not found")
    return ResearchUniverseJob.model_validate(payload)


@app.post("/api/research/ai-candidates", response_model=AIProposalResponse)
async def propose_ai_strategy_candidates(request: AIProposalRequest):
    configuration = configuration_service.current_configuration()
    symbol = request.symbol or configuration.default_symbol
    timeframe = request.timeframe or configuration.default_timeframe
    try:
        market, history = await runtime.market_context(
            limit=300,
            symbol=symbol,
            timeframe=timeframe,
        )
        summary = ai_market_summary(market, history, symbol, timeframe)
    except (LookupError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    ai_gateway.service = ai_configuration_service
    ai_gateway.proxy_url = configuration.network_proxy_url
    ai_view = ai_configuration_service.get()
    system_prompt = (
        "You are a quantitative research assistant. Propose one parameterized strategy "
        "candidate only. Never propose order execution, leverage changes, credentials, or "
        "guaranteed returns. Use only the supplied bounded market summary. The output is a "
        "research hypothesis that must pass deterministic validation and backtesting."
    )
    requested_family = (
        request.family.value if request.family else "select the best supported family"
    )
    user_prompt = (
        f"Operator hypothesis: {request.hypothesis}\n"
        f"Requested family: {requested_family}\n"
        f"Market summary: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
        "Return exactly one StrategySpec object. Set source to ai and explain the hypothesis "
        "briefly. Keep every parameter inside the schema limits."
    )
    mode = ai_view.configuration.routing_mode
    providers: list[AIProvider | None]
    if request.provider:
        providers = [request.provider]
        mode = "manual"
    elif mode == "consensus":
        route = ai_gateway.routing_order()
        if len(route) < 2:
            raise HTTPException(409, "双模型复核至少需要启用两个已配置的 AI 厂商")
        providers = route[:2]
    else:
        providers = [None]

    proposals: list[AIProposalItem] = []
    try:
        for provider in providers:
            completion, attempted = await ai_gateway.generate_json(
                system=system_prompt,
                prompt=user_prompt,
                schema=strategy_spec_json_schema(),
                schema_name="strategy_spec",
                requested_provider=provider,
                max_tokens=3200,
                operation="strategy_candidate_generation",
                prompt_version="strategy_candidate_v2",
                schema_version="strategy_spec_v1",
            )
            raw_spec = {
                **completion.data,
                "source": "ai",
                "hypothesis": request.hypothesis,
            }
            spec = validate_strategy_spec(raw_spec, expected_kind=request.family)
            candidate = create_candidate(spec)
            await asyncio.to_thread(store.save_strategy_candidate, candidate)
            proposals.append(
                AIProposalItem(
                    provider=completion.provider,
                    model=completion.model,
                    candidate=candidate,
                    usage=completion.usage,
                    latency_ms=completion.latency_ms,
                    route=attempted,
                )
            )
    except AIProviderError as exc:
        raise HTTPException(502, {"code": exc.code, "message": exc.public_message}) from exc
    except ValueError as exc:
        raise HTTPException(422, f"AI 策略候选未通过 StrategySpec 校验：{exc}") from exc

    add_audit(
        "research",
        "AI 已生成隔离的策略研究候选；候选尚不可执行",
        candidate_ids=",".join(item.candidate.id for item in proposals),
        providers=",".join(str(item.provider) for item in proposals),
        mode=mode,
    )
    return AIProposalResponse(mode=mode, proposals=proposals)


@app.get("/api/research/datasets/{dataset_id}", response_model=ResearchDataset)
async def research_dataset(dataset_id: str):
    payload = await asyncio.to_thread(store.load_research_dataset, dataset_id)
    if not payload:
        raise HTTPException(404, "research dataset not found")
    return ResearchDataset.model_validate(payload)


@app.post(
    "/api/research/backtests/{plan_id}",
    response_model=BacktestJob,
    status_code=202,
)
async def queue_research_backtest(plan_id: str):
    if plan_id not in plans:
        raise HTTPException(404, "strategy plan not found")
    recent = await asyncio.to_thread(store.list_research_jobs, 50)
    for payload in recent:
        existing = BacktestJob.model_validate(payload)
        if existing.plan_id == plan_id and existing.status in {
            ResearchJobStatus.QUEUED,
            ResearchJobStatus.RUNNING,
        }:
            return existing
    job = BacktestJob(id=f"btj_{uuid4().hex[:16]}", plan_id=plan_id)
    await asyncio.to_thread(store.save_research_job, job)
    task = asyncio.create_task(run_backtest_job(job), name=f"backtest:{job.id}")
    backtest_job_tasks.add(task)
    task.add_done_callback(backtest_job_tasks.discard)
    return job


@app.get("/api/research/backtests/{job_id}", response_model=BacktestJob)
async def research_backtest_job(job_id: str):
    payload = await asyncio.to_thread(store.load_research_job, job_id)
    if not payload:
        raise HTTPException(404, "research backtest job not found")
    return BacktestJob.model_validate(payload)


@app.get("/api/research/backtests", response_model=list[BacktestJob])
async def research_backtest_jobs(limit: int = 50):
    bounded_limit = min(max(limit, 1), 200)
    payloads = await asyncio.to_thread(store.list_research_jobs, bounded_limit)
    return [BacktestJob.model_validate(payload) for payload in payloads]


@app.post("/api/research/candidates", response_model=StrategyCandidate)
async def propose_strategy_candidate(request: CandidateRequest):
    try:
        candidate = create_candidate(
            request.strategy_spec,
            parent_candidate_id=request.parent_candidate_id,
        )
    except ValueError as exc:
        raise HTTPException(422, f"candidate strategy spec is invalid: {exc}") from exc
    await asyncio.to_thread(store.save_strategy_candidate, candidate)
    add_audit(
        "research",
        "受约束策略候选已登记，尚不可执行",
        candidate_id=candidate.id,
        source=candidate.source,
    )
    return candidate


@app.get("/api/research/candidates", response_model=list[StrategyCandidate])
async def research_candidates(limit: int = 100):
    bounded_limit = min(max(limit, 1), 200)
    payloads = await asyncio.to_thread(store.list_strategy_candidates, bounded_limit)
    return [
        candidate
        for payload in payloads
        if (
            candidate := parse_strategy_candidate_payload(
                payload,
                audit_context="research_candidate_list",
            )
        )
        is not None
    ]


@app.post(
    "/api/research/governance/review",
    response_model=StrategyGovernanceReport,
)
async def review_strategy_governance():
    candidate_payloads, campaign_payloads = await asyncio.gather(
        asyncio.to_thread(store.list_strategy_candidates, 500),
        asyncio.to_thread(store.list_research_campaigns, 50),
    )
    campaigns = [ResearchCampaignReport.model_validate(payload) for payload in campaign_payloads]
    health_reports: list[StrategyHealthReport] = []
    retired: list[str] = []
    for payload in candidate_payloads:
        candidate = parse_strategy_candidate_payload(
            payload,
            audit_context="strategy_governance",
        )
        if (
            candidate is None
            or candidate.backtest is None
            or candidate.status not in {CandidateStatus.CHAMPION, CandidateStatus.CHALLENGER}
        ):
            continue
        campaign_candidate = next(
            (
                aggregate
                for campaign in campaigns
                for aggregate in campaign.candidates
                if aggregate.strategy_fingerprint == candidate.spec_fingerprint
            ),
            None,
        )
        health = assess_strategy_health(
            candidate,
            campaign_candidate=campaign_candidate,
        )
        health_reports.append(health)
        if health.retirement_recommended:
            candidate.status = CandidateStatus.RETIRED
            candidate.execution_artifact = None
            candidate.updated_at = datetime.now(UTC)
            candidate.decision_reasons.extend(health.reasons)
            await asyncio.to_thread(
                store.save_strategy_candidate,
                candidate,
            )
            retired.append(candidate.id)
    if retired:
        add_audit(
            "research",
            "策略治理已自动退休失去多项独立证据的 Champion",
            severity="warning",
            retired_candidate_ids=",".join(retired),
        )
    return StrategyGovernanceReport(
        reviewed=len(health_reports),
        retired_candidate_ids=retired,
        health=health_reports,
    )


@app.post(
    "/api/research/candidates/{candidate_id}/evaluate",
    response_model=StrategyCandidate,
)
async def evaluate_strategy_candidate(candidate_id: str, dataset_id: str):
    candidate_payload, dataset_payload = await asyncio.gather(
        asyncio.to_thread(store.load_strategy_candidate, candidate_id),
        asyncio.to_thread(store.load_research_dataset, dataset_id),
    )
    if not candidate_payload:
        raise HTTPException(404, "strategy candidate not found")
    if not dataset_payload:
        raise HTTPException(404, "research dataset not found")
    candidate = parse_strategy_candidate_payload(
        candidate_payload,
        audit_context="candidate_evaluation",
    )
    if candidate is None:
        raise HTTPException(
            409,
            "strategy candidate uses a stale StrategySpec contract; create a new candidate",
        )
    dataset = ResearchDataset.model_validate(dataset_payload)
    if candidate.status not in {CandidateStatus.PROPOSED, CandidateStatus.REJECTED}:
        raise HTTPException(
            409,
            f"candidate status {candidate.status} cannot be evaluated again",
        )
    candle_payloads, funding_payloads, open_interest_payloads = await asyncio.gather(
        asyncio.to_thread(store.load_research_dataset_candles, dataset_id),
        asyncio.to_thread(store.load_research_dataset_funding, dataset_id),
        asyncio.to_thread(
            store.load_research_dataset_open_interest,
            dataset_id,
        ),
    )
    candles = [Candle.model_validate(payload) for payload in candle_payloads]
    funding_rates = [FundingRatePoint.model_validate(payload) for payload in funding_payloads]
    open_interest = [
        OpenInterestPoint.model_validate(payload) for payload in open_interest_payloads
    ]
    result = await backtest_candidate_on_dataset(
        candidate,
        dataset,
        candles,
        funding_rates,
        open_interest,
    )
    candidate = attach_backtest(candidate, dataset.id, result)
    await asyncio.to_thread(store.save_strategy_candidate, candidate)
    return candidate


@app.post(
    "/api/research/candidates/{candidate_id}/revise",
    response_model=CandidateRevisionResponse,
)
async def revise_strategy_candidate(
    candidate_id: str,
    request: CandidateRevisionRequest,
):
    payload = await asyncio.to_thread(
        store.load_strategy_candidate,
        candidate_id,
    )
    if not payload:
        raise HTTPException(404, "strategy candidate not found")
    candidate = parse_strategy_candidate_payload(
        payload,
        audit_context="candidate_revision",
    )
    if candidate is None:
        raise HTTPException(409, "strategy candidate uses a stale contract")
    if candidate.backtest is None:
        raise HTTPException(
            409,
            "strategy candidate must be backtested before failure-driven revision",
        )
    attribution, revisions = generate_failure_driven_revisions(
        candidate,
        max_revisions=request.max_revisions,
    )
    for revision in revisions:
        await asyncio.to_thread(
            store.save_strategy_candidate,
            revision.candidate,
        )
    add_audit(
        "research",
        "候选策略已完成失败归因并生成单变量修订",
        candidate_id=candidate.id,
        failure_codes=",".join(attribution.codes),
        revision_count=len(revisions),
    )
    return CandidateRevisionResponse(
        parent_candidate_id=candidate.id,
        attribution=attribution,
        revisions=revisions,
    )


@app.post(
    "/api/research/candidates/{candidate_id}/promote",
    response_model=PromotionDecision,
)
async def promote_strategy_candidate(
    candidate_id: str,
    request: CandidatePromotionRequest,
):
    payload = await asyncio.to_thread(store.load_strategy_candidate, candidate_id)
    if not payload:
        raise HTTPException(404, "strategy candidate not found")
    candidate = parse_strategy_candidate_payload(
        payload,
        audit_context="candidate_promotion",
    )
    if candidate is None:
        raise HTTPException(
            409,
            "strategy candidate uses a stale StrategySpec contract and cannot be promoted",
        )
    if candidate.status != CandidateStatus.CHALLENGER:
        raise HTTPException(409, "only a validated Challenger can be promoted")
    if request.confirmation != f"PROMOTE {candidate.spec_fingerprint}":
        raise HTTPException(409, "promotion confirmation phrase does not match")
    all_candidates = [
        parsed
        for item in await asyncio.to_thread(store.list_strategy_candidates, 200)
        if (
            parsed := parse_strategy_candidate_payload(
                item,
                audit_context="promotion_champion_lookup",
            )
        )
        is not None
    ]
    champion = next(
        (
            item
            for item in all_candidates
            if item.status == CandidateStatus.CHAMPION
            and item.spec.get("family") == candidate.spec.get("family")
        ),
        None,
    )
    comparison_champion = champion
    comparison_candles: list[Candle] | None = None
    incumbent_retested = False
    drift = None
    if (
        champion
        and candidate.dataset_id
        and (champion.dataset_id != candidate.dataset_id or champion.backtest is None)
    ):
        dataset_payload = await asyncio.to_thread(
            store.load_research_dataset,
            candidate.dataset_id,
        )
        if not dataset_payload:
            raise HTTPException(409, "challenger research dataset is unavailable")
        dataset = ResearchDataset.model_validate(dataset_payload)
        candle_payloads, funding_payloads, open_interest_payloads = await asyncio.gather(
            asyncio.to_thread(
                store.load_research_dataset_candles,
                candidate.dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_funding,
                candidate.dataset_id,
            ),
            asyncio.to_thread(
                store.load_research_dataset_open_interest,
                candidate.dataset_id,
            ),
        )
        comparison_candles = [Candle.model_validate(item) for item in candle_payloads]
        funding_rates = [FundingRatePoint.model_validate(item) for item in funding_payloads]
        open_interest = [OpenInterestPoint.model_validate(item) for item in open_interest_payloads]
        incumbent_result = await backtest_candidate_on_dataset(
            champion,
            dataset,
            comparison_candles,
            funding_rates,
            open_interest,
        )
        comparison_champion = champion.model_copy(
            deep=True,
            update={
                "dataset_id": dataset.id,
                "backtest": incumbent_result,
            },
        )
        incumbent_retested = True
    decision = compare_for_promotion(candidate, comparison_champion)
    if incumbent_retested:
        decision.checks["incumbent_retested_on_challenger_dataset"] = True
        decision.reasons.append("现任 Champion 已在 Challenger 的同一不可变数据集上临时复测")
    if decision.approved and candidate.dataset_id:
        if comparison_candles is None:
            candle_payloads = await asyncio.to_thread(
                store.load_research_dataset_candles, candidate.dataset_id
            )
            comparison_candles = [Candle.model_validate(item) for item in candle_payloads]
        try:
            drift = detect_return_drift(comparison_candles)
        except ValueError:
            drift = None
        decision.checks["dataset_drift_available"] = drift is not None
        decision.checks["dataset_drift_not_critical"] = (
            drift is not None and drift.status != "critical"
        )
        if drift is None:
            decision.approved = False
            decision.reasons.append("数据集不足以计算漂移，禁止晋升")
        elif drift.status == "critical":
            decision.approved = False
            decision.reasons.append("数据分布漂移达到 critical，禁止自动晋升")
    if decision.approved:
        if champion:
            champion.status = CandidateStatus.RETIRED
            champion.updated_at = datetime.now(UTC)
        candidate.status = CandidateStatus.CHAMPION
        candidate.updated_at = datetime.now(UTC)
        candidate.decision_reasons.extend(decision.reasons)
        if candidate.source in {"ai", "ml"}:
            dataset_payload = await asyncio.to_thread(
                store.load_research_dataset,
                candidate.dataset_id,
            )
            if not dataset_payload or drift is None:
                raise HTTPException(
                    409,
                    "AI/ML Champion 缺少数据集或漂移证据，无法编译执行工件。",
                )
            dataset = ResearchDataset.model_validate(dataset_payload)
            try:
                candidate.execution_artifact = compile_champion(
                    candidate,
                    dataset_id=dataset.id,
                    data_fingerprint=dataset.data_fingerprint,
                    drift_status=drift.status,
                )
            except ValueError as exc:
                raise HTTPException(409, f"AI/ML Champion 执行工件编译失败：{exc}") from exc
        try:
            await asyncio.to_thread(
                store.promote_strategy_candidate,
                candidate,
                champion,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        add_audit(
            "research",
            "研究候选晋升为 Champion；不会自动激活交易计划",
            candidate_id=candidate.id,
        )
    return decision


@app.post("/api/research/ml-challenger", response_model=MLChallengerReport)
async def create_ml_challenger():
    configuration = configuration_service.current_configuration()
    try:
        market, history = await runtime.market_context(limit=configuration.strategy.research_bars)
    except Exception as exc:
        raise HTTPException(503, f"research market data unavailable: {exc}") from exc
    candles = closed_candles(history, configuration.default_timeframe, market.timestamp)
    report = await asyncio.to_thread(train_ml_challenger, candles)
    if report.accepted and report.candidate_spec:
        candidate = create_candidate(report.candidate_spec)
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
    return report


@app.get("/api/research/drift")
async def research_drift():
    configuration = configuration_service.current_configuration()
    try:
        market, history = await runtime.market_context(limit=configuration.strategy.research_bars)
    except Exception as exc:
        raise HTTPException(503, f"research market data unavailable: {exc}") from exc
    candles = closed_candles(history, configuration.default_timeframe, market.timestamp)
    try:
        return await asyncio.to_thread(detect_return_drift, candles)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/strategies/generate", response_model=StrategyPlan)
async def generate_strategy(request: GenerateRequest):
    configuration = configuration_service.current_configuration()
    if (
        request.strategy_spec
        and request.strategy_spec.get("source") in {"ai", "ml"}
        and not request.research_candidate_id
    ):
        raise HTTPException(
            422,
            "AI/ML StrategySpec 只能进入研究候选库，不能直接生成交易计划",
        )
    compiled_candidate: StrategyCandidate | None = None
    spec_source = request.strategy_spec.get("source") if request.strategy_spec else None
    if spec_source in {"ai", "ml"}:
        payload = await asyncio.to_thread(
            store.load_strategy_candidate,
            request.research_candidate_id,
        )
        if not payload:
            raise HTTPException(422, "指定的研究 Champion 不存在。")
        compiled_candidate = parse_strategy_candidate_payload(
            payload,
            audit_context="compiled_candidate_plan_generation",
        )
        if compiled_candidate is None:
            raise HTTPException(
                422,
                "指定的研究 Champion 使用旧版策略合约，必须重新研究和编译。",
            )
        if compiled_candidate.spec != request.strategy_spec:
            raise HTTPException(422, "请求 StrategySpec 与研究 Champion 不一致。")
        dataset_payload = (
            await asyncio.to_thread(
                store.load_research_dataset,
                compiled_candidate.dataset_id,
            )
            if compiled_candidate.dataset_id
            else None
        )
        if not dataset_payload or compiled_candidate.execution_artifact is None:
            raise HTTPException(422, "研究 Champion 尚未形成可执行验证工件。")
        dataset = ResearchDataset.model_validate(dataset_payload)
        try:
            validate_compiled_champion(
                compiled_candidate,
                compiled_candidate.execution_artifact,
                dataset_id=dataset.id,
                data_fingerprint=dataset.data_fingerprint,
            )
        except ValueError as exc:
            raise HTTPException(422, f"研究 Champion 验证失败：{exc}") from exc
    elif request.research_candidate_id:
        raise HTTPException(422, "只有 AI/ML Champion 需要研究执行工件。")
    requested_symbol = request.symbol or configuration.default_symbol
    requested_timeframe = request.timeframe or configuration.default_timeframe
    try:
        market, history = await runtime.market_context(
            limit=configuration.strategy.research_bars,
            symbol=requested_symbol,
            timeframe=requested_timeframe,
        )
        portfolio = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"无法基于真实行情生成策略：{exc}") from exc
    strategy_history = closed_candles(
        history,
        requested_timeframe,
        market.timestamp,
    )
    if len(strategy_history) < 55:
        raise HTTPException(409, "已收盘 K 线不足 55 根，不能生成策略")
    try:
        plan = strategy_factory.generate(
            request.strategy_kind,
            candles=strategy_history,
            snapshot=market,
            equity=strategy_sizing_equity(configuration, portfolio, request.equity),
            risk_fraction=request.risk_fraction or configuration.risk.risk_per_trade,
            exchange=configuration.exchange_id,
            timeframe=requested_timeframe,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
            allow_short=configuration.market_type == "swap",
            confirmation_candles=confirmation_history(strategy_history, requested_timeframe),
            require_confirmation=requires_higher_confirmation(configuration),
            strategy_spec=request.strategy_spec,
        )
    except NoTradeSignal as exc:
        raise HTTPException(
            409,
            detail={
                "code": "no_trade",
                "message": "当前市场条件未达到策略门槛，系统建议观望",
                "signal": exc.signal.model_dump(mode="json"),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, f"策略规格无效：{exc}") from exc
    parent = plans.get(request.revision_of) if request.revision_of else None
    if request.revision_of and not parent:
        raise HTTPException(404, "要修订的策略版本不存在")
    plan.lineage_id = parent.lineage_id or parent.id if parent else plan.id
    plan.supersedes_plan_id = parent.id if parent else None
    plan.version = parent.version + 1 if parent else 1
    plan.algorithm = request.strategy_kind.value
    if compiled_candidate is not None:
        plan.research_candidate_id = compiled_candidate.id
        plan.validated_strategy_artifact = compiled_candidate.execution_artifact.model_dump(
            mode="json"
        )
        plan.reasons.append(
            "采用已通过不可变数据集、稳健性与漂移验证的 AI/ML Champion；"
            f"候选 {compiled_candidate.id}，执行工件 "
            f"{compiled_candidate.execution_artifact.fingerprint}。"
        )
    plan.trading_mode = configuration.trading_mode
    plan.market_type = configuration.market_type
    plan.margin_mode = (
        str(configuration.margin_mode) if configuration.market_type == "swap" else "cash"
    )
    plan.leverage = (
        configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
    )
    if configuration.market_type == "swap":
        plan.reasons.append(
            f"USDT 永续采用 {plan.leverage:f}x "
            f"{'全仓' if plan.margin_mode == 'cross' else '逐仓'}保证金；"
            "仓位按交易所账户权益和止损距离计算"
        )
    plan = constrain_plan_to_portfolio(plan, portfolio, configuration)
    plan.source_prompt = request.prompt.strip()
    plan.thesis = f"{request.prompt.strip()} {plan.thesis}".strip()
    plan.minimum_reward_risk = configuration.risk.minimum_reward_risk
    plans[plan.id] = plan
    store.save_strategy(plan)
    add_audit("strategy", "已基于真实行情生成新策略", plan_id=plan.id)
    return plan


@app.post("/api/strategies/{plan_id}/approve", response_model=StrategyPlan)
async def approve_strategy(plan_id: str, request: ApproveRequest):
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    if plan.status != PlanStatus.DRAFT:
        raise HTTPException(409, "only a draft strategy can be approved")
    validate_plan_research_provenance(plan)
    expected = f"APPROVE {plan.fingerprint}"
    if request.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    artifact_data = store.load_latest_backtest(plan.id)
    if not artifact_data:
        raise HTTPException(409, "策略必须先完成一次绑定当前版本的真实数据回测")
    artifact = BacktestResult.model_validate(artifact_data)
    if artifact.strategy_fingerprint != plan.fingerprint:
        raise HTTPException(409, "策略已发生变化，必须重新回测后再审核")
    if artifact.cost_model.get("direction_filter") != str(plan.side):
        raise HTTPException(409, "回测证据未绑定当前计划方向，必须重新回测")
    if not artifact.robustness_passed:
        reasons = "；".join(artifact.validation_reasons)
        raise HTTPException(409, f"策略稳健性验收未通过：{reasons}")
    if artifact.trade_sample_strongly_corroborated and not (
        artifact.parameter_walk_forward and artifact.parameter_walk_forward.get("passed") is True
    ):
        raise HTTPException(
            409,
            "强证据低样本策略还必须通过“过去分段选参、下一分段验证”的参数前推报告",
        )
    plan.approved_at = datetime.now(UTC)
    plan.approved_fingerprint = plan.fingerprint
    plan.status = PlanStatus.APPROVED
    store.save_strategy(plan)
    add_audit(
        "strategy",
        "策略版本已人工审核",
        plan_id=plan.id,
        fingerprint=plan.fingerprint,
        data_fingerprint=artifact.data_fingerprint,
    )
    return plan


@app.post("/api/strategies/{plan_id}/activate", response_model=StrategyPlan)
async def activate_strategy(plan_id: str, request: ActivateRequest):
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    validate_agent_plan_grant(plan)
    validate_plan_research_provenance(plan)
    if plan.status != PlanStatus.APPROVED:
        raise HTTPException(409, "strategy must be approved before activation")
    if plan.approved_fingerprint != plan.fingerprint:
        raise HTTPException(409, "approved strategy fingerprint no longer matches")
    if plan.entry.expires_at:
        expires_at = plan.entry.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise HTTPException(409, "strategy entry window has expired; generate a new version")
    expected = f"ACTIVATE {plan.symbol}"
    if request.confirmation != expected:
        raise HTTPException(400, f"confirmation must be exactly: {expected}")
    configuration = configuration_service.current_configuration()
    expected_margin_mode = (
        str(configuration.margin_mode) if configuration.market_type == "swap" else "cash"
    )
    expected_leverage = (
        configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
    )
    if (
        plan.exchange != configuration.exchange_id
        or plan.trading_mode != configuration.trading_mode
        or plan.market_type != configuration.market_type
        or plan.margin_mode != expected_margin_mode
        or plan.leverage != expected_leverage
    ):
        raise HTTPException(409, "策略交易所、模式、市场、保证金模式或杠杆与当前环境不一致")
    try:
        market, history = await runtime.market_context(
            limit=configuration_service.current_configuration().strategy.research_bars,
            symbol=plan.symbol,
            timeframe=plan.timeframe,
        )
        portfolio = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"激活前行情或账户检查失败：{exc}") from exc
    signal = evaluate_current_plan_signal(plan, history, market)
    lifecycle = assess_entry_signal(plan, signal)
    decision = add_signal_gate(risk_engine().evaluate(plan, portfolio, market), plan, signal)
    if not decision.approved:
        failures = "；".join(check.message for check in decision.checks if not check.passed)
        raise HTTPException(409, f"策略未通过激活风控：{failures}")
    if lifecycle.state == EntrySignalState.DEGRADED:
        add_audit(
            "strategy",
            "计划信号暂时转为 flat，但未形成反向证据；保留有效入场计划。",
            severity="warning",
            plan_id=plan.id,
            lifecycle_state=str(lifecycle.state),
        )
    for previous in plans.values():
        if (
            previous.id != plan.id
            and previous.lineage_id == plan.lineage_id
            and previous.status == PlanStatus.ACTIVE
        ):
            previous.status = PlanStatus.ARCHIVED
            store.save_strategy(previous)
    plan.status = PlanStatus.ACTIVE
    store.save_strategy(plan)
    add_audit("strategy", "策略已激活，等待入场条件", plan_id=plan.id)
    return plan


def _multi_leg_certificate_for_request(request: MultiLegExecutionRequest):
    payload = (
        store.load_relative_value_campaign(request.campaign_id)
        if request.strategy_kind == "pair"
        else store.load_carry_campaign(request.campaign_id)
    )
    if payload is None:
        raise HTTPException(404, "multi-leg research Campaign not found")
    report = (
        PairResearchCampaignReport.model_validate(payload)
        if request.strategy_kind == "pair"
        else CarryResearchCampaignReport.model_validate(payload)
    )
    certificate = report.execution_certificate
    if not report.execution_eligible or certificate is None:
        raise HTTPException(409, "multi-leg Campaign has no sealed-blind execution certificate")
    try:
        validate_multi_leg_execution_certificate(certificate, request=request)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return certificate


def _validated_multi_leg_strategy_spec(
    request: MultiLegExecutionRequest,
    certificate,
) -> PairStrategySpec | CarryStrategySpec:
    if certificate.version < 2 or not certificate.strategy_spec_payload:
        raise HTTPException(
            409,
            "multi-leg execution requires a v2 certificate with a frozen strategy spec",
        )
    if request.strategy_kind == "pair":
        spec: PairStrategySpec | CarryStrategySpec = PairStrategySpec.model_validate(
            certificate.strategy_spec_payload
        )
        if {leg.role for leg in request.legs} != {"pair_a", "pair_b"}:
            raise HTTPException(422, "pair execution requires pair_a and pair_b roles")
        if any(leg.market_type != "swap" for leg in request.legs):
            raise HTTPException(422, "pair execution requires swap legs")
    else:
        spec = CarryStrategySpec.model_validate(certificate.strategy_spec_payload)
        if {leg.role for leg in request.legs} != {"spot", "perpetual"}:
            raise HTTPException(422, "carry execution requires spot and perpetual roles")
        spot = next(leg for leg in request.legs if leg.role == "spot")
        perpetual = next(leg for leg in request.legs if leg.role == "perpetual")
        if (
            spot.market_type != "spot"
            or spot.side != OrderSide.BUY
            or perpetual.market_type != "swap"
            or perpetual.side != OrderSide.SELL
            or spot.symbol != perpetual.symbol
        ):
            raise HTTPException(
                422,
                "carry execution requires same-symbol spot buy and perpetual sell",
            )
    if spec.fingerprint != certificate.strategy_fingerprint:
        raise HTTPException(409, "certificate strategy spec fingerprint mismatch")
    return spec


async def _paper_multi_leg_adapters(
    request: MultiLegExecutionRequest,
) -> dict[str, PaperExchange]:
    configuration = configuration_service.current_configuration()
    if configuration.trading_mode != TradingMode.PAPER or request.trading_mode != "paper":
        raise HTTPException(
            409,
            "multi-leg exchange execution remains locked; use paper commissioning first",
        )
    if any(leg.exchange != configuration.exchange_id for leg in request.legs):
        raise HTTPException(409, "multi-leg request exchange differs from the active environment")
    adapters: dict[str, PaperExchange] = {}
    for adapter_key in sorted({leg.adapter_key for leg in request.legs}):
        scoped_legs = [leg for leg in request.legs if leg.adapter_key == adapter_key]
        scope = scoped_legs[0]
        paper = PaperExchange(
            initial_cash=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            market_type=scope.market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            store=store,
            account_id=(f"paper:{scope.exchange}:{scope.market_type}:multi_leg:{adapter_key}"),
        )
        public = CcxtExchangeAdapter(
            scope.exchange,
            {},
            sandbox=False,
            live_enabled=False,
            public_only=True,
            market_type=scope.market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            proxy_url=configuration.network_proxy_url,
        )
        try:
            snapshots = await asyncio.gather(
                *(public.market_snapshot(leg.symbol) for leg in scoped_legs)
            )
        finally:
            await public.close()
        for snapshot in snapshots:
            paper.update_market(snapshot)
        adapters[adapter_key] = paper
    return adapters


async def _sandbox_strategy_multi_leg_adapters(
    request: MultiLegExecutionRequest,
) -> tuple[dict[str, CcxtExchangeAdapter], dict[str, OrderLedger]]:
    configuration = configuration_service.current_configuration()
    if configuration.trading_mode != TradingMode.SANDBOX or request.trading_mode != "sandbox":
        raise HTTPException(409, "multi-leg exchange execution requires sandbox mode")
    if any(leg.exchange != configuration.exchange_id for leg in request.legs):
        raise HTTPException(409, "multi-leg request exchange differs from active sandbox")
    credential_fingerprint = configuration_service.credential_fingerprint(configuration)
    commissioning_payload = store.load_multi_leg_commissioning(
        configuration.exchange_id,
        request.strategy_kind,
    )
    if commissioning_payload is None:
        raise HTTPException(409, "matching multi-leg sandbox commissioning is missing")
    commissioning = MultiLegCommissioningResult.model_validate(commissioning_payload)
    scopes = sorted(f"{leg.exchange}:{leg.market_type}:{leg.symbol}" for leg in request.legs)
    try:
        validate_multi_leg_commissioning_result(
            commissioning,
            exchange=configuration.exchange_id,
            credential_fingerprint=credential_fingerprint,
            strategy_kind=request.strategy_kind,
            execution_scopes=scopes,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    credentials = configuration_service.credentials_for(configuration)
    adapters: dict[str, CcxtExchangeAdapter] = {}
    ledgers: dict[str, OrderLedger] = {}
    for adapter_key in sorted({leg.adapter_key for leg in request.legs}):
        scoped_legs = [leg for leg in request.legs if leg.adapter_key == adapter_key]
        scope = scoped_legs[0]
        adapter = CcxtExchangeAdapter(
            configuration.exchange_id,
            credentials,
            sandbox=True,
            live_enabled=False,
            market_type=scope.market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            store=store,
            account_id=(f"{configuration.exchange_id}:sandbox:{scope.market_type}:multi_leg"),
            proxy_url=configuration.network_proxy_url,
        )
        adapters[adapter_key] = adapter
        ledgers[adapter_key] = OrderLedger(
            store,
            f"{configuration.exchange_id}:sandbox:{scope.market_type}:multi_leg",
        )
    return adapters, ledgers


@app.post(
    "/api/execution/multi-leg/paper",
    response_model=MultiLegExecutionRun,
)
async def execute_paper_multi_leg(request: MultiLegExecutionRequest):
    certificate = _multi_leg_certificate_for_request(request)
    _validated_multi_leg_strategy_spec(request, certificate)
    if risk_state_service.get().halted:
        raise HTTPException(409, "global risk halt blocks multi-leg entry")
    adapters = await _paper_multi_leg_adapters(request)
    coordinator = MultiLegExecutionCoordinator(
        store,
        adapters,
        risk_state=risk_state_service,
    )
    return await coordinator.execute(
        request,
        validate_certificate=lambda candidate: validate_multi_leg_execution_certificate(
            certificate,
            request=candidate,
        ),
    )


@app.post(
    "/api/execution/multi-leg/sandbox",
    response_model=MultiLegExecutionRun,
)
async def execute_sandbox_multi_leg(request: MultiLegExecutionRequest):
    certificate = _multi_leg_certificate_for_request(request)
    _validated_multi_leg_strategy_spec(request, certificate)
    if risk_state_service.get().halted:
        raise HTTPException(409, "global risk halt blocks multi-leg entry")
    adapters, ledgers = await _sandbox_strategy_multi_leg_adapters(request)
    try:
        coordinator = MultiLegExecutionCoordinator(
            store,
            adapters,
            risk_state=risk_state_service,
            reconciliation_attempts=8,
            reconciliation_delay_seconds=0.5,
            order_sink=lambda adapter_key, order: ledgers[adapter_key].record(
                order,
                protection_verified=False,
            ),
        )
        return await coordinator.execute(
            request,
            validate_certificate=lambda candidate: validate_multi_leg_execution_certificate(
                certificate,
                request=candidate,
            ),
        )
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )


@app.post(
    "/api/execution/multi-leg/{run_id}/close",
    response_model=MultiLegExecutionRun,
)
async def close_paper_multi_leg(run_id: str):
    payload = await asyncio.to_thread(store.load_multi_leg_execution_run, run_id)
    if payload is None:
        raise HTTPException(404, "multi-leg execution run not found")
    run = MultiLegExecutionRun.model_validate(payload)
    certificate = _multi_leg_certificate_for_request(run.request)
    _validated_multi_leg_strategy_spec(run.request, certificate)
    if run.request.trading_mode == "paper":
        adapters = await _paper_multi_leg_adapters(run.request)
        ledgers: dict[str, OrderLedger] = {}
    elif run.request.trading_mode == "sandbox":
        adapters, ledgers = await _sandbox_strategy_multi_leg_adapters(run.request)
    else:
        raise HTTPException(
            409,
            "live multi-leg execution is locked until profitability certification",
        )
    try:
        coordinator = MultiLegExecutionCoordinator(
            store,
            adapters,
            risk_state=risk_state_service,
            reconciliation_attempts=(8 if run.request.trading_mode == "sandbox" else 3),
            reconciliation_delay_seconds=(0.5 if run.request.trading_mode == "sandbox" else 0.05),
            order_sink=(
                (
                    lambda adapter_key, order: ledgers[adapter_key].record(
                        order,
                        protection_verified=False,
                    )
                )
                if ledgers
                else None
            ),
        )
        return await coordinator.close(
            run_id,
            validate_certificate=lambda candidate: validate_multi_leg_execution_certificate(
                certificate,
                request=candidate,
            ),
        )
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )


async def _evaluate_multi_leg_exit(run_id: str) -> MultiLegExecutionRun:
    payload = await asyncio.to_thread(store.load_multi_leg_execution_run, run_id)
    if payload is None:
        raise HTTPException(404, "multi-leg execution run not found")
    run = MultiLegExecutionRun.model_validate(payload)
    if run.status != MultiLegExecutionStatus.HEDGED:
        return run
    certificate = _multi_leg_certificate_for_request(run.request)
    spec = _validated_multi_leg_strategy_spec(run.request, certificate)
    if run.request.trading_mode == "paper":
        execution_adapters = await _paper_multi_leg_adapters(run.request)
        ledgers: dict[str, OrderLedger] = {}
        configuration = configuration_service.current_configuration()
        market_adapters = {
            key: CcxtExchangeAdapter(
                legs[0].exchange,
                {},
                sandbox=False,
                live_enabled=False,
                public_only=True,
                market_type=legs[0].market_type,
                margin_mode=str(configuration.margin_mode),
                contract_leverage=configuration.contract_leverage,
                proxy_url=configuration.network_proxy_url,
            )
            for key in sorted({leg.adapter_key for leg in run.request.legs})
            for legs in [[leg for leg in run.request.legs if leg.adapter_key == key]]
        }
    elif run.request.trading_mode == "sandbox":
        execution_adapters, ledgers = await _sandbox_strategy_multi_leg_adapters(run.request)
        market_adapters = execution_adapters
    else:
        raise HTTPException(409, "live multi-leg monitoring remains locked")
    separate_market_adapters = run.request.trading_mode == "paper"
    try:
        bar_seconds = timeframe_seconds(run.request.timeframe)
        if run.request.strategy_kind == "pair":
            pair_a = next(leg for leg in run.request.legs if leg.role == "pair_a")
            pair_b = next(leg for leg in run.request.legs if leg.role == "pair_b")
            limit = min(max(spec.formation_window + 8, 80), 1200)
            candles_a, candles_b, snapshot_a, snapshot_b = await asyncio.gather(
                market_adapters[pair_a.adapter_key].fetch_candles(
                    pair_a.symbol,
                    run.request.timeframe,
                    limit,
                ),
                market_adapters[pair_b.adapter_key].fetch_candles(
                    pair_b.symbol,
                    run.request.timeframe,
                    limit,
                ),
                market_adapters[pair_a.adapter_key].market_snapshot(pair_a.symbol),
                market_adapters[pair_b.adapter_key].market_snapshot(pair_b.symbol),
            )
            as_of = min(snapshot_a.timestamp, snapshot_b.timestamp)
            signal = latest_causal_pair_signal(
                closed_candles(candles_a, run.request.timeframe, as_of),
                closed_candles(candles_b, run.request.timeframe, as_of),
                spec,
            )
            if signal is None:
                raise RuntimeError("pair exit monitor lacks a causal synchronized signal")
            market_data_at, signal_value, hedge_ratio = signal
            held_bars = max(
                int((market_data_at - run.created_at).total_seconds() // bar_seconds),
                0,
            )
            should_close, reason = evaluate_pair_exit(
                spec_payload=certificate.strategy_spec_payload,
                pair_a_side=pair_a.side,
                zscore=signal_value,
                held_bars=held_bars,
            )
            signal_name = "pair_zscore"
        else:
            spot = next(leg for leg in run.request.legs if leg.role == "spot")
            perpetual = next(leg for leg in run.request.legs if leg.role == "perpetual")
            spot_market, perpetual_market = await asyncio.gather(
                market_adapters[spot.adapter_key].market_snapshot(spot.symbol),
                market_adapters[perpetual.adapter_key].market_snapshot(perpetual.symbol),
            )
            market_data_at = min(spot_market.timestamp, perpetual_market.timestamp)
            signal_value = perpetual_market.last / spot_market.last - Decimal("1")
            hedge_ratio = None
            held_bars = max(
                int((market_data_at - run.created_at).total_seconds() // bar_seconds),
                0,
            )
            should_close, reason = evaluate_carry_exit(
                spec_payload=certificate.strategy_spec_payload,
                basis_rate=signal_value,
                funding_rate=perpetual_market.funding_rate,
                held_bars=held_bars,
            )
            signal_name = "carry_basis_rate"
        run.exit_monitor = MultiLegExitMonitorState(
            market_data_at=market_data_at,
            held_bars=held_bars,
            signal_name=signal_name,
            signal_value=signal_value,
            hedge_ratio=hedge_ratio,
            should_close=should_close,
            reason=reason,
            consecutive_errors=0,
        )
        await asyncio.to_thread(store.save_multi_leg_execution_run, run)
        if not should_close:
            return run
        coordinator = MultiLegExecutionCoordinator(
            store,
            execution_adapters,
            risk_state=risk_state_service,
            reconciliation_attempts=(8 if run.request.trading_mode == "sandbox" else 3),
            reconciliation_delay_seconds=(0.5 if run.request.trading_mode == "sandbox" else 0.05),
            order_sink=(
                (
                    lambda adapter_key, order: ledgers[adapter_key].record(
                        order,
                        protection_verified=False,
                    )
                )
                if ledgers
                else None
            ),
        )
        closed_run = await coordinator.close(
            run.id,
            validate_certificate=lambda candidate: validate_multi_leg_execution_certificate(
                certificate,
                request=candidate,
            ),
        )
        add_audit(
            "execution",
            "multi-leg exit monitor completed coordinated close",
            run_id=run.id,
            reason=reason,
            signal_value=str(signal_value),
            held_bars=held_bars,
        )
        return closed_run
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in execution_adapters.values()),
            return_exceptions=True,
        )
        if separate_market_adapters:
            await asyncio.gather(
                *(adapter.close() for adapter in market_adapters.values()),
                return_exceptions=True,
            )


async def _resume_interrupted_multi_leg_run(
    run: MultiLegExecutionRun,
) -> MultiLegExecutionRun:
    certificate = _multi_leg_certificate_for_request(run.request)
    _validated_multi_leg_strategy_spec(run.request, certificate)
    if run.request.trading_mode == "paper":
        adapters = await _paper_multi_leg_adapters(run.request)
        ledgers: dict[str, OrderLedger] = {}
    elif run.request.trading_mode == "sandbox":
        adapters, ledgers = await _sandbox_strategy_multi_leg_adapters(run.request)
    else:
        raise RuntimeError("live multi-leg restart recovery remains locked")
    try:
        coordinator = MultiLegExecutionCoordinator(
            store,
            adapters,
            risk_state=risk_state_service,
            reconciliation_attempts=(8 if run.request.trading_mode == "sandbox" else 3),
            reconciliation_delay_seconds=(0.5 if run.request.trading_mode == "sandbox" else 0.05),
            order_sink=(
                (
                    lambda adapter_key, order: ledgers[adapter_key].record(
                        order,
                        protection_verified=False,
                    )
                )
                if ledgers
                else None
            ),
        )

        def validator(candidate: MultiLegExecutionRequest) -> None:
            validate_multi_leg_execution_certificate(
                certificate,
                request=candidate,
            )

        if run.status == MultiLegExecutionStatus.CLOSING:
            return await coordinator.close(
                run.id,
                validate_certificate=validator,
            )
        return await coordinator.execute(
            run.request,
            validate_certificate=validator,
        )
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )


async def multi_leg_exit_monitor_loop() -> None:
    while True:
        payloads = await asyncio.to_thread(store.list_multi_leg_execution_runs, 1000)
        for payload in payloads:
            run = MultiLegExecutionRun.model_validate(payload)
            if run.status in {
                MultiLegExecutionStatus.RESERVED,
                MultiLegExecutionStatus.SUBMITTING,
                MultiLegExecutionStatus.RECONCILING,
                MultiLegExecutionStatus.COMPENSATING,
                MultiLegExecutionStatus.CLOSING,
            }:
                try:
                    await _resume_interrupted_multi_leg_run(run)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    risk_state_service.halt(
                        "multi-leg restart recovery failed; manual venue reconciliation "
                        f"is required for {run.id}: {str(exc)[:160]}",
                        source="multi_leg_restart_recovery",
                    )
                    metrics.increment("multi_leg_restart_recovery_failures_total")
                continue
            if run.status != MultiLegExecutionStatus.HEDGED:
                continue
            try:
                await _evaluate_multi_leg_exit(run.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                latest_payload = await asyncio.to_thread(
                    store.load_multi_leg_execution_run,
                    run.id,
                )
                latest = MultiLegExecutionRun.model_validate(latest_payload or payload)
                previous_errors = (
                    latest.exit_monitor.consecutive_errors if latest.exit_monitor is not None else 0
                )
                latest.exit_monitor = MultiLegExitMonitorState(
                    reason=str(exc)[:500],
                    consecutive_errors=previous_errors + 1,
                )
                await asyncio.to_thread(store.save_multi_leg_execution_run, latest)
                metrics.increment("multi_leg_exit_monitor_failures_total")
                if latest.exit_monitor.consecutive_errors >= 3:
                    risk_state_service.halt(
                        "multi-leg exit monitoring failed repeatedly; manual position "
                        f"reconciliation is required for {latest.id}",
                        source="multi_leg_exit_monitor",
                    )
        await asyncio.sleep(30)


@app.post(
    "/api/execution/multi-leg/{run_id}/evaluate-exit",
    response_model=MultiLegExecutionRun,
)
async def evaluate_multi_leg_exit(run_id: str):
    return await _evaluate_multi_leg_exit(run_id)


@app.get(
    "/api/execution/multi-leg",
    response_model=list[MultiLegExecutionRun],
)
async def list_multi_leg_execution_runs(limit: int = 100):
    payloads = await asyncio.to_thread(
        store.list_multi_leg_execution_runs,
        min(max(limit, 1), 1000),
    )
    return [MultiLegExecutionRun.model_validate(payload) for payload in payloads]


@app.post("/api/strategies/{plan_id}/execute", response_model=ExecuteResponse)
async def execute_strategy(plan_id: str):
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    validate_agent_plan_grant(plan)
    validate_plan_research_provenance(plan)
    if plan.status != PlanStatus.ACTIVE:
        raise HTTPException(409, "strategy must be active before execution")
    configuration = configuration_service.current_configuration()
    if configuration.trading_mode != TradingMode.PAPER and not runtime.reconciliation_entry_allowed:
        health = runtime.reconciliation_health
        raise HTTPException(
            409,
            "订单对账尚未恢复，暂时禁止新开仓；"
            f"连续失败 {health.consecutive_failures}/{health.failure_threshold} 次，"
            "后台会自动重试",
        )
    if (
        plan.exchange != configuration.exchange_id
        or plan.trading_mode != configuration.trading_mode
        or plan.market_type != configuration.market_type
        or plan.margin_mode
        != (str(configuration.margin_mode) if configuration.market_type == "swap" else "cash")
        or plan.leverage
        != (
            configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
        )
    ):
        raise HTTPException(409, "策略交易所、模式、市场、保证金模式或杠杆与当前执行环境不一致")
    if (
        configuration.trading_mode != TradingMode.PAPER
        and not configuration_service.is_execution_ready(configuration)
    ):
        raise HTTPException(409, "真实交易网关尚未通过测试网原子保护单验收")
    try:
        market, history = await runtime.market_context(symbol=plan.symbol, timeframe=plan.timeframe)
        portfolio = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"执行前行情或账户检查失败：{exc}") from exc
    assert runtime.execution is not None
    signal = evaluate_current_plan_signal(plan, history, market)
    lifecycle = assess_entry_signal(plan, signal)
    if not lifecycle.execution_allowed:
        plan.status = PlanStatus.ARCHIVED
        store.save_strategy(plan)
        raise HTTPException(
            409,
            f"最新已收盘信号为 {signal.direction}，与计划 {plan.side} 不一致，拒绝下单",
        )
    if lifecycle.state == EntrySignalState.DEGRADED:
        add_audit(
            "execution",
            "最新信号为 flat 且无反向证据；计划仍在有效期和价格区间内，继续硬风险复核。",
            severity="warning",
            plan_id=plan.id,
            lifecycle_state=str(lifecycle.state),
        )
    engine = ExecutionEngine(runtime.execution, risk_engine(), risk_state_service)
    try:
        # Reuse the same complete snapshot used by signal validation. In swap
        # mode runtime.market_context enriches it with funding data; fetching a
        # second bare ticker here would silently discard that mandatory input.
        result = await engine.submit_plan(plan, portfolio, market)
    except Exception as exc:
        if isinstance(exc, ProtectionVerificationError) and runtime.order_ledger:
            for submitted_order in exc.orders:
                runtime.order_ledger.record(submitted_order, protection_verified=False)
        state = risk_state_service.halt(
            f"订单提交异常，等待对账：{str(exc)[:160]}",
            source="execution_uncertain",
        )
        add_audit(
            "execution",
            "订单提交异常，已自动停机并等待对账",
            severity="critical",
            plan_id=plan.id,
            reason=state.reason,
        )
        raise HTTPException(502, state.reason) from exc
    if result.order:
        if runtime.order_ledger:
            for submitted_order in result.orders:
                runtime.order_ledger.record(submitted_order, protection_verified=True)
        add_audit(
            "execution",
            f"已提交 {len(result.orders)} 个原子保护入场订单",
            order_id=result.order.id,
            plan_id=plan.id,
            mode=str(configuration_service.current_configuration().trading_mode),
        )
    else:
        failed = [check.code for check in result.decision.checks if not check.passed]
        add_audit(
            "risk",
            "订单被风险引擎拒绝",
            severity="warning",
            plan_id=plan.id,
            failed_checks=",".join(failed),
        )
    return ExecuteResponse(
        decision=result.decision,
        order=result.order,
        orders=result.orders,
    )


@app.post("/api/orders/{order_id}/cancel", response_model=Order)
async def cancel_order(order_id: str):
    """Cancel a tracked working order without changing any open position."""

    await runtime.initialize()
    if not runtime.execution or not runtime.order_ledger:
        raise HTTPException(503, "订单网关尚未就绪")
    record = runtime.order_ledger.records.get(order_id)
    if not record:
        raise HTTPException(404, "订单不在当前交易环境的受控账本中")
    if record.order.status not in {
        OrderStatus.PENDING,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
    }:
        raise HTTPException(409, f"订单当前状态 {record.order.status} 不允许撤销")
    try:
        canceled = await runtime.execution.cancel_order(record.order.id, record.order.symbol)
        if canceled.status != OrderStatus.CANCELED:
            canceled = await runtime.execution.fetch_order(record.order.id, record.order.symbol)
        if canceled.status != OrderStatus.CANCELED:
            raise RuntimeError(f"交易所返回状态 {canceled.status}，撤单尚未确认")
    except Exception as exc:
        add_audit(
            "execution",
            "人工撤单失败",
            severity="warning",
            order_id=record.order.id,
            reason=str(exc)[:180],
        )
        raise HTTPException(502, f"交易所撤单失败：{exc}") from exc
    runtime.order_ledger.record(canceled, protection_verified=record.protection_verified)
    add_audit(
        "execution",
        "操作者已撤销未成交订单",
        order_id=canceled.id,
        symbol=canceled.symbol,
    )
    return canceled


@app.post("/api/strategies/{plan_id}/backtest", response_model=BacktestResult)
async def backtest_strategy(
    plan_id: str,
    research_trial_count: int = 1,
):
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    configuration = configuration_service.current_configuration()
    try:
        market, history = await runtime.market_context(
            limit=effective_research_bars(
                configuration.strategy.research_bars,
                plan.timeframe,
            ),
            symbol=plan.symbol,
            timeframe=plan.timeframe,
        )
    except Exception as exc:
        raise HTTPException(503, f"真实历史行情不可用：{exc}") from exc
    history = closed_candles(history, plan.timeframe, market.timestamp)
    dataset, funding_rates, open_interest, positioning, basis = await materialize_research_dataset(
        history,
        exchange=plan.exchange,
        market_type=plan.market_type,
        symbol=plan.symbol,
        timeframe=plan.timeframe,
    )
    result = await execute_research_backtest(
        dataset,
        history,
        initial_equity=(plan.sizing.risk_amount / plan.sizing.risk_fraction),
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        strategy_kind=StrategyKind(plan.algorithm),
        symbol=plan.symbol,
        timeframe=plan.timeframe,
        exchange=plan.exchange,
        risk_fraction=plan.sizing.risk_fraction,
        minimum_reward_risk=plan.minimum_reward_risk,
        allow_short=plan.market_type == "swap",
        require_confirmation=requires_higher_confirmation(configuration, plan.trading_mode),
        validation=configuration.strategy.validation,
        strategy_spec=plan.strategy_spec or None,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        side_filter=Side(plan.side),
        research_trial_count=max(research_trial_count, 1),
    )
    result.strategy_fingerprint = plan.fingerprint
    store.save_backtest(plan.id, result)
    add_audit("backtest", "回测已完成", plan_id=plan.id, trades=result.total_trades)
    return result


@app.get(
    "/api/strategies/{plan_id}/backtest",
    response_model=BacktestResult | None,
)
async def latest_strategy_backtest(plan_id: str):
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    payload = store.load_latest_backtest(plan.id)
    if not payload:
        return None
    artifact = BacktestResult.model_validate(payload)
    if artifact.strategy_fingerprint != plan.fingerprint:
        return None
    if artifact.cost_model.get("direction_filter") != str(plan.side):
        return None
    return artifact


@app.get("/api/strategies/{plan_id}/market-fit")
async def strategy_market_fit(plan_id: str):
    """Explain whether a fixed strategy still fits the latest closed market data."""

    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(404, "strategy plan not found")
    configuration = configuration_service.current_configuration()
    try:
        market, history = await runtime.market_context(
            limit=min(configuration.strategy.research_bars, 500),
            symbol=plan.symbol,
            timeframe=plan.timeframe,
        )
    except Exception as exc:
        raise HTTPException(503, f"最新市场适配检查所需行情不可用：{exc}") from exc
    completed = closed_candles(history, plan.timeframe, market.timestamp)
    assessment = score_market_state(completed)
    signal = evaluate_current_plan_signal(plan, completed, market)
    lifecycle = assess_entry_signal(plan, signal)
    direction_matches = signal.actionable and str(signal.direction) == str(plan.side)
    family_matches = assessment.selected_kind.value == plan.algorithm
    if direction_matches and family_matches and not assessment.ambiguous:
        verdict = "aligned"
        summary = "策略家族、方向与最新已收盘行情一致，可继续交由风险闸门判断"
    elif lifecycle.state == EntrySignalState.INVALIDATED:
        verdict = "conflict"
        summary = "最新确定性信号与计划方向冲突，不应执行该版本"
    else:
        verdict = "expired" if lifecycle.state == EntrySignalState.EXPIRED else "degraded"
        summary = "最新行情尚未形成与该计划一致的清晰信号，保持等待"
    quality = runtime.market_quality
    return {
        "plan_id": plan.id,
        "strategy_fingerprint": plan.fingerprint,
        "symbol": plan.symbol,
        "timeframe": plan.timeframe,
        "checked_at": datetime.now(UTC),
        "data_as_of": market.timestamp,
        "verdict": verdict,
        "summary": summary,
        "entry_lifecycle": lifecycle,
        "direction_matches": direction_matches,
        "family_matches": family_matches,
        "signal": signal,
        "assessment": assessment,
        "market_quality": quality,
    }


@app.get("/api/risk", response_model=RiskDecision)
async def current_risk():
    try:
        market, history = await runtime.market_context()
        portfolio: PortfolioState = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"风险检查所需数据不可用：{exc}") from exc
    return risk_engine().evaluate(ensure_plan(history, market, portfolio), portfolio, market)


@app.get("/api/risk/state", response_model=RiskControlState)
async def get_risk_state():
    return risk_state_service.get()


@app.post("/api/risk/halt", response_model=RiskControlState)
async def halt_trading(request: HaltRequest):
    state = risk_state_service.halt(request.reason)
    add_audit("risk", "已触发全局交易停机", severity="critical", reason=state.reason)
    return state


@app.post("/api/risk/resume", response_model=RiskControlState)
async def resume_trading(request: ResumeRequest):
    try:
        state = risk_state_service.resume(request.confirmation)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    add_audit("risk", "已人工恢复新开仓权限", severity="warning")
    return state


@app.get("/api/audit", response_model=list[AuditEvent])
async def audit_events():
    return audit


@app.get("/api/audit/events", response_model=AuditEventPage)
async def paged_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: int | None = Query(default=None, ge=1),
    category: str | None = None,
    severity: str | None = None,
    search: str | None = Query(default=None, max_length=120),
):
    items, next_cursor = await asyncio.to_thread(
        store.page_audit,
        limit=limit,
        before_sequence=cursor,
        category=category,
        severity=severity,
        search=search.strip() if search else None,
    )
    return AuditEventPage(
        items=[AuditEvent.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


web_distribution = Path(__file__).resolve().parent.parent.parent / "apps" / "web" / "dist"
if web_distribution.exists():
    app.mount(
        "/",
        StaticFiles(directory=web_distribution, html=True),
        name="cosmos-web",
    )
