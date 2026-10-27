from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from functools import partial
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Literal
from uuid import uuid4

from anyio import CapacityLimiter, to_process
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator

from .acceptance import ProductAcceptanceReport
from .agent.performance import build_forward_performance
from .agent.schemas import (
    AgentAutonomyMode,
    TradeEpisodeStatus,
)
from .agent.store import AgentStore
from .ai_config_service import AIConfigurationService
from .ai_configuration import (
    AIConfigurationUpdate,
    AIConfigurationView,
    AIProvider,
)
from .ai_gateway import (
    AIErrorCode,
    AIGateway,
    AIProviderError,
    AIProviderTestResult,
    AIUsage,
)
from .ai_usage import AIUsageReport
from .alpha_factory import (
    ALPHA_MARKETS,
    AlphaModelArtifactV1,
    DevelopmentAuditCertificateV1,
    DevelopmentAuditEvidenceV1,
    FullStrategyCertificateV2,
    SandboxAdmissionCertificateV1,
    SandboxExecutionRoundEvidenceV1,
    SandboxMaturityEvidenceV1,
    alpha_transaction_cost_fingerprint,
    assess_crypto_5m_contract,
    dependency_versions,
)
from .alpha_runtime import (
    estimate_alpha_round_trip_cost,
    evaluate_certified_alpha_signal,
)
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
    StrategyKind,
    UserConfiguration,
)
from .decision_attribution import DecisionOutcomeV2, build_decision_attribution
from .domain import (
    AuditEvent,
    BasisPoint,
    Candle,
    EntryRule,
    ExitPlan,
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
    PositionSide,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
    TradingMode,
)
from .entry_profiles import DEFAULT_ENTRY_PROFILE_ID, get_entry_profile
from .entry_runtime import EntryOrderService
from .exchanges.ccxt_adapter import CcxtExchangeAdapter
from .exchanges.paper import PaperExchange
from .historical_anchor_scan import (
    HistoricalAnchorScan,
    build_binance_historical_anchor_scan,
)
from .indicators import INDICATOR_CATALOG, atr
from .liquidation_stream import (
    BINANCE_LIQUIDATION_STREAM,
    BinanceLiquidationCollector,
)
from .local_access import LocalAccess
from .market_data import TIMEFRAME_SECONDS, validate_candles
from .market_data_capture import MarketDataCaptureSession, calculate_capture_coverage
from .market_state_v2 import build_cross_market_context, build_market_state_v2
from .ml_challenger import MLChallengerReport, detect_return_drift, train_ml_challenger
from .multi_leg_execution import (
    MultiLegCommissioningResult,
    MultiLegExecutionCoordinator,
    MultiLegExecutionRequest,
    MultiLegExecutionStatus,
    MultiLegOrderSpec,
    build_multi_leg_commissioning_result,
    build_multi_leg_execution_certificate,
)
from .observability import Metrics
from .official_archive_index import (
    OfficialArchiveIndex,
    download_official_archive_index,
)
from .official_events import (
    ETF_FLOW_STATUS,
    OfficialEvent,
    OfficialEventClient,
    active_risk_windows,
)
from .operator_workspace import (
    AuditEventPage,
    PersonalStrategyCreate,
    PersonalStrategyRecord,
    PersonalStrategyUpdate,
    ReviewEvidenceItem,
    ReviewJobView,
    ReviewOverview,
    StrategyLibraryItem,
    StrategyLibraryPage,
    StrategyLibraryPreference,
    stable_fingerprint,
)
from .order_ledger import OrderLedger
from .plan_lifecycle import EntrySignalState, assess_entry_signal
from .position_decisions import resolve_position_decision
from .quant_intelligence import (
    LightGBMQuantIntelligenceV1,
    QuantModelArtifactV1,
    market_state_feature_vector,
    quant_model_is_eligible,
)
from .relative_value import (
    PAIR_BACKTEST_ENGINE_VERSION,
    PAIR_STRATEGY_LIBRARY,
    PairBacktestReport,
    PairStrategySpec,
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
    research_experiment_fingerprint,
    research_lineage_id,
    research_trial_family_id,
)
from .research_certificates import CertificateRevocationEvent, GeneratorEnvelopeCertificate
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
    CandidateResearchContext,
    CandidateStatus,
    PromotionDecision,
    StrategyCandidate,
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
from .review_pipeline import (
    ReviewScopeResult,
    StrategyReviewJob,
    StrategyReviewJobStatus,
    review_failure_detail,
    should_retry_review_after_restart,
    strategy_review_fingerprint,
)
from .risk import RiskCheck, RiskDecision, RiskEngine, RiskLimits
from .risk_state import RiskControlState, RiskStateService
from .runtime import RuntimeManager
from .secret_store import SecretStore
from .settings import Settings
from .signals import (
    NoTradeSignal,
    score_market_state,
    select_strategy_kind,
)
from .storage import SqliteStore
from .strategy_dsl import compile_strategy_dsl
from .strategy_factory import StrategyFactory
from .strategy_library import (
    StrategyLibraryEntry,
    infer_required_data,
    library_entry,
    list_strategy_library,
    seed_strategy_library_candidates,
)
from .strategy_portfolio import (
    StrategyHealthReport,
    assess_strategy_health,
)
from .strategy_spec import (
    default_strategy_spec,
    strategy_spec_json_schema,
    validate_strategy_spec,
)
from .timeframes import (
    closed_candles,
    confirmation_timeframe,
    periods_per_year,
    resample_closed_candles,
)
from .trade_history import TradeHistoryResponseV2, build_trade_history_v2
from .trading_intelligence import (
    V2_SYMBOLS,
    AutomationRuntimeStatus,
    AutomationRuntimeV2,
    DecisionEpisode,
    DeltaContext,
    HumanLiveGrant,
    MarketStateV2,
    PositionRuntimeV2,
    PositionStateV2,
    RiskAllocation,
    ThesisState,
    TradingModeV2,
    WaitRuntimeV2,
    validate_market_state_time,
)
from .trading_intelligence import (
    PlanState as TradingIntelligencePlanState,
)
from .trading_intelligence_automation import (
    TradingIntelligenceAutomation,
    is_expected_automated_entry_rejection,
)
from .trading_intelligence_engine import (
    DeterministicRiskEngine,
    DualLLMDecisionService,
    EventDrivenTradingEngine,
    ExistingPositionRisk,
    resolve_trading_mode,
)
from .trading_intelligence_engine import (
    RiskContext as TradingIntelligenceRiskContext,
)
from .trading_intelligence_engine import (
    RiskLimits as TradingIntelligenceRiskLimits,
)
from .trading_intelligence_engine import (
    configuration_fingerprint as v2_configuration_fingerprint,
)
from .trading_intelligence_store import TradingIntelligenceStore
from .v2_execution import EntryRiskBlocked, V2ExecutionEngine, V2ExecutionResult
from .v2_position_runtime import (
    PositionReconciliationIntegrityError,
    V2PositionReconciler,
)

logger = logging.getLogger(__name__)

env_settings = Settings()
store = SqliteStore(env_settings.cosmos_data_dir / "cosmos.db")
trading_intelligence_store = TradingIntelligenceStore(store)
runtime_build_identity = capture_runtime_build_identity()
secret_store = SecretStore(env_settings.cosmos_data_dir / "cosmos.db")
configuration_service = ConfigurationService(store, secret_store)
ai_configuration_service = AIConfigurationService(store, secret_store)
ai_gateway = AIGateway(ai_configuration_service)
dual_llm_decision_service = DualLLMDecisionService(ai_gateway)
trading_intelligence_engine = EventDrivenTradingEngine(trading_intelligence_store)
trading_intelligence_automation: TradingIntelligenceAutomation | None = None
official_event_client = OfficialEventClient()
quant_model_cache: dict[str, LightGBMQuantIntelligenceV1] = {}
quant_model_load_lock = RLock()
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
review_worker_task: asyncio.Task | None = None
v2_position_worker_task: asyncio.Task | None = None
review_worker_wakeup = asyncio.Event()
review_worker_health: dict[str, object] = {
    "status": "stopped",
    "activity": "idle",
    "heartbeat_at": None,
    "current_job_id": None,
    "last_error": None,
}
agent_stream_snapshot_lock = asyncio.Lock()
agent_stream_snapshot_cache: dict[str, object] = {}
AGENT_STREAM_SNAPSHOT_TTL_SECONDS = 2.0
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
QUANT_V1_MODEL_DIR = env_settings.cosmos_data_dir / "models" / "quant-intelligence-v1"


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
    # Immutable datasets are content addressed. Reuse an identical stored copy
    # instead of creating one row per scan, which previously inflated the
    # research inventory with overlapping 5m datasets.
    existing_payload = next(
        (
            item
            for item in await asyncio.to_thread(store.list_research_datasets, 1000)
            if item.get("data_fingerprint") == dataset.data_fingerprint
            and item.get("exchange") == dataset.exchange
            and item.get("market_type") == dataset.market_type
            and item.get("symbol") == dataset.symbol
            and item.get("timeframe") == dataset.timeframe
        ),
        None,
    )
    if existing_payload:
        return (
            ResearchDataset.model_validate(existing_payload),
            funding_rates,
            open_interest,
            positioning,
            basis,
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
    dataset_verification = await to_process.run_sync(
        partial(
            verify_research_dataset,
            dataset,
            candles,
            kwargs.get("funding_rates"),
            kwargs.get("open_interest"),
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        ),
        cancellable=True,
        limiter=research_process_limiter,
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
        require_confirmation=(
            configuration.strategy.validation.require_higher_timeframe_confirmation
        ),
        validation=configuration.strategy.validation,
        strategy_spec=candidate.spec,
        funding_rates=funding_rates,
        open_interest=open_interest or [],
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        research_trial_count=research_trial_count,
        require_regime_family_fit=True,
        maximum_gross_exposure=configuration.risk.max_gross_exposure,
        maximum_leverage=min(
            configuration.risk.max_leverage,
            configuration.contract_leverage,
        ),
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


def _certified_alpha_model_trio(
    admission: SandboxAdmissionCertificateV1,
) -> tuple[AlphaModelArtifactV1, AlphaModelArtifactV1, AlphaModelArtifactV1]:
    artifacts: list[AlphaModelArtifactV1] = []
    for model_id in admission.model_artifact_ids:
        payload = store.load_alpha_model_artifact(model_id)
        if payload is None:
            raise RuntimeError(f"certified alpha model artifact is missing: {model_id}")
        artifacts.append(AlphaModelArtifactV1.model_validate(payload))
    linear = next(
        (
            item
            for item in artifacts
            if item.model_family in {"elastic_net_regression", "logistic_classification"}
        ),
        None,
    )
    gbdt = next(
        (
            item
            for item in artifacts
            if item.model_family in {"lightgbm_regression", "lightgbm_classification"}
        ),
        None,
    )
    ensemble = next(
        (item for item in artifacts if item.model_family == "ensemble_50_50"),
        None,
    )
    if linear is None or gbdt is None or ensemble is None:
        raise RuntimeError("certified alpha model trio is incomplete")
    if ensemble.fingerprint != admission.artifact_fingerprint:
        raise RuntimeError("certified alpha ensemble fingerprint drifted")
    return linear, gbdt, ensemble


def _development_audit_for_admission(
    admission: SandboxAdmissionCertificateV1,
) -> DevelopmentAuditCertificateV1:
    if not admission.development_audit_certificate_id:
        raise RuntimeError("sandbox admission predates server-bound development audit evidence")
    status = store.load_research_certificate_status(admission.development_audit_certificate_id)
    if not status or status["certificate_type"] != "development_audit_v1" or status["revoked"]:
        raise RuntimeError("valid DevelopmentAuditCertificateV1 is unavailable")
    audit = DevelopmentAuditCertificateV1.model_validate(status["certificate"])
    if (
        audit.strategy_ir_hash != admission.strategy_ir_hash
        or audit.artifact_fingerprint != admission.artifact_fingerprint
        or audit.data_contract_fingerprint != admission.data_contract_fingerprint
        or set(audit.model_artifact_ids) != set(admission.model_artifact_ids)
        or audit.evidence != admission.audit_evidence
    ):
        raise RuntimeError("sandbox admission development audit binding drifted")
    return audit


def _alpha_admission_for_plan(plan: StrategyPlan) -> SandboxAdmissionCertificateV1:
    admission_id = plan.sandbox_admission_certificate_id
    if not admission_id and plan.full_strategy_certificate_id:
        full_status = store.load_research_certificate_status(plan.full_strategy_certificate_id)
        if (
            not full_status
            or full_status["certificate_type"] != "full_strategy_v2"
            or full_status["revoked"]
        ):
            raise RuntimeError("valid FullStrategyCertificateV2 is unavailable")
        full = FullStrategyCertificateV2.model_validate(full_status["certificate"])
        admission_id = full.sandbox_admission_certificate_id
    status = store.load_research_certificate_status(admission_id or "")
    if not status or status["certificate_type"] != "sandbox_admission_v1" or status["revoked"]:
        raise RuntimeError("valid SandboxAdmissionCertificateV1 is unavailable")
    admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
    _development_audit_for_admission(admission)
    return admission


async def evaluate_certified_alpha_current_signal(
    admission: SandboxAdmissionCertificateV1,
    history: list[Candle],
    market,
    *,
    exchange: str,
    notional: Decimal,
):
    linear, gbdt, ensemble = await asyncio.to_thread(
        _certified_alpha_model_trio,
        admission,
    )
    required = tuple(
        sorted(
            {
                feature
                for item in (linear, gbdt, ensemble)
                for feature in item.required_data_features
            }
        )
    )
    datasets = await asyncio.to_thread(store.list_research_datasets, 2_000)
    contract = assess_crypto_5m_contract(datasets, required_features=required)
    if contract.status != "READY" or contract.fingerprint != admission.data_contract_fingerprint:
        raise RuntimeError("certified alpha data contract changed or is no longer READY")
    market_key = f"{exchange}:{market.symbol}"
    readiness = next((item for item in contract.markets if item.market == market_key), None)
    if readiness is None or readiness.status != "READY" or not readiness.dataset_id:
        raise RuntimeError(f"certified alpha market is not READY: {market_key}")
    tail = await asyncio.to_thread(
        partial(
            store.load_alpha_runtime_feature_tail,
            readiness.dataset_id,
            limit=5_000,
        )
    )
    completed = closed_candles(history, "5m", market.timestamp)[-4_096:]
    if len(completed) < 55:
        raise RuntimeError("certified alpha runtime requires at least 55 closed 5m candles")
    latest_quote_volume = completed[-1].close * completed[-1].volume
    configuration = configuration_service.current_configuration()
    estimated_cost = estimate_alpha_round_trip_cost(
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        spread_bps=market.spread_bps,
        notional=max(notional, Decimal("0")),
        latest_quote_volume=latest_quote_volume,
    )
    return await asyncio.to_thread(
        partial(
            evaluate_certified_alpha_signal,
            linear,
            gbdt,
            ensemble,
            candles=completed,
            feature_tail=tail,
            snapshot=market,
            estimated_round_trip_cost=estimated_cost,
        )
    )


def evaluate_current_plan_signal(plan: StrategyPlan, history, market):
    configuration = configuration_service.current_configuration()
    completed = closed_candles(history, plan.timeframe, market.timestamp)
    if plan.research_profile == "crypto_5m_v1":
        if plan.timeframe != "5m":
            raise RuntimeError("crypto_5m_v1 execution is frozen to the 5m timeframe")

        async def evaluate_alpha():
            admission = await asyncio.to_thread(_alpha_admission_for_plan, plan)
            signal, _ = await evaluate_certified_alpha_current_signal(
                admission,
                completed,
                market,
                exchange=plan.exchange,
                notional=plan.sizing.notional,
            )
            return signal

        return evaluate_alpha()
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


async def resolve_current_plan_signal(plan: StrategyPlan, history, market):
    result = evaluate_current_plan_signal(plan, history, market)
    return await result if inspect.isawaitable(result) else result


def add_signal_gate(decision: RiskDecision, plan: StrategyPlan, signal) -> RiskDecision:
    assessment = assess_entry_signal(plan, signal)
    alpha_requires_current_direction = (
        plan.research_profile == "crypto_5m_v1" and not signal.actionable
    )
    aligned = assessment.execution_allowed and not alpha_requires_current_direction
    check = RiskCheck(
        code="signal_lifecycle",
        passed=aligned,
        blocking=assessment.blocking or alpha_requires_current_direction,
        message=(
            "crypto_5m_v1 当前集成信号为 flat；方向一致、两倍成本优势和七类状态门"
            "必须逐笔同时通过，禁止新增风险。"
            if alpha_requires_current_direction
            else assessment.reason
        ),
    )
    return RiskDecision(
        approved=decision.approved and aligned,
        checks=[*decision.checks, check],
    )


async def process_sandbox_alpha_handoffs() -> int:
    """Materialize at most one current-signal plan for each admitted strategy."""

    configuration = configuration_service.current_configuration()
    if configuration.exchange_id != "okx" or configuration.trading_mode != TradingMode.SANDBOX:
        return 0
    handoffs = await asyncio.to_thread(store.list_sandbox_admission_handoffs, limit=200)
    retryable = {
        "admitted_waiting_current_signal",
        "queued_until_okx_sandbox",
        "waiting_current_signal",
        "runtime_data_blocked",
        "active_instance_exists",
    }
    created = 0
    for handoff in handoffs:
        if handoff.get("status") not in retryable:
            continue
        certificate_id = str(handoff.get("certificate_id") or "")
        status = await asyncio.to_thread(store.load_research_certificate_status, certificate_id)
        if not status or status["certificate_type"] != "sandbox_admission_v1" or status["revoked"]:
            continue
        admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
        try:
            await asyncio.to_thread(_development_audit_for_admission, admission)
        except RuntimeError as exc:
            await asyncio.to_thread(
                store.save_sandbox_admission_handoff,
                admission,
                status="development_audit_invalid",
                details={"reason": str(exc)},
            )
            continue
        existing = [
            item
            for item in plans.values()
            if item.sandbox_admission_certificate_id == admission.id
            and item.status != PlanStatus.ARCHIVED
        ]
        if existing:
            await asyncio.to_thread(
                partial(
                    store.save_sandbox_admission_handoff,
                    admission,
                    status="active_instance_exists",
                    details={"plan_id": existing[0].id},
                )
            )
            continue
        try:
            portfolio = await runtime.portfolio()
            candidates: list[tuple[Decimal, StrategyPlan, dict]] = []
            symbols = [
                symbol
                for symbol in configuration.strategy.watchlist
                if symbol in {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
            ]
            for symbol in symbols:
                market, history = await runtime.market_context(
                    limit=4_096,
                    symbol=symbol,
                    timeframe="5m",
                )
                completed = closed_candles(history, "5m", market.timestamp)
                if len(completed) < 55:
                    continue
                volatility = atr(completed, 14)
                stop_distance = max(volatility * Decimal("1.8"), market.last * Decimal("0.002"))
                provisional_notional = (
                    portfolio.equity
                    * admission.maximum_risk_per_trade
                    / stop_distance
                    * market.last
                )
                signal, inference = await evaluate_certified_alpha_current_signal(
                    admission,
                    completed,
                    market,
                    exchange="okx",
                    notional=provisional_notional,
                )
                if not signal.actionable:
                    continue
                if configuration.market_type == "spot" and signal.direction == "short":
                    continue
                linear, _, _ = await asyncio.to_thread(_certified_alpha_model_trio, admission)
                ir_payload = linear.canonical_ir_payload or {}
                family = str(ir_payload.get("family") or StrategyKind.TREND.value)
                try:
                    strategy_kind = StrategyKind(family)
                except ValueError:
                    strategy_kind = StrategyKind.TREND
                plan = strategy_factory.generate_unchecked(
                    strategy_kind,
                    candles=completed,
                    snapshot=market,
                    equity=portfolio.equity,
                    risk_fraction=admission.maximum_risk_per_trade,
                    exchange="okx",
                    timeframe="5m",
                    minimum_reward_risk=configuration.risk.minimum_reward_risk,
                    allow_short=configuration.market_type == "swap",
                    side_override=Side(str(signal.direction)),
                )
                plan = plan.model_copy(
                    update={
                        "lineage_id": admission.id,
                        "algorithm": strategy_kind.value,
                        "trading_mode": TradingMode.SANDBOX,
                        "market_type": configuration.market_type,
                        "margin_mode": (
                            str(configuration.margin_mode)
                            if configuration.market_type == "swap"
                            else "cash"
                        ),
                        "leverage": (
                            configuration.contract_leverage
                            if configuration.market_type == "swap"
                            else Decimal("1")
                        ),
                        "status": PlanStatus.ACTIVE,
                        "regime": signal.regime,
                        "confidence": signal.confidence,
                        "signal_candle_at": signal.signal_candle_at,
                        "data_as_of": signal.data_as_of,
                        "strategy_spec": {},
                        "strategy_spec_fingerprint": admission.strategy_ir_hash,
                        "research_profile": "crypto_5m_v1",
                        "alpha_artifact_fingerprint": admission.artifact_fingerprint,
                        "sandbox_admission_certificate_id": admission.id,
                        "validated_strategy_artifact": {
                            "profile": "crypto_5m_v1",
                            "model_artifact_ids": list(admission.model_artifact_ids),
                            "ensemble_artifact_fingerprint": admission.artifact_fingerprint,
                            "data_contract_fingerprint": admission.data_contract_fingerprint,
                            "current_inference": inference.model_dump(mode="json"),
                        },
                        "exit": plan.exit.model_copy(
                            update={"maximum_holding_bars": inference.horizon_bars}
                        ),
                        "reasons": [*signal.reasons, *plan.reasons],
                        "source_prompt": "certified_alpha_runtime_only",
                        "decision_origin": "alpha_factory",
                        "approved_at": datetime.now(UTC),
                    }
                )
                plan.approved_fingerprint = plan.fingerprint
                candidates.append((signal.confidence, plan, inference.model_dump(mode="json")))
            if not candidates:
                await asyncio.to_thread(
                    partial(
                        store.save_sandbox_admission_handoff,
                        admission,
                        status="waiting_current_signal",
                        details={"last_checked_at": datetime.now(UTC).isoformat()},
                    )
                )
                continue
            _, plan, inference_payload = max(candidates, key=lambda item: item[0])
            plans[plan.id] = plan
            await asyncio.to_thread(store.save_strategy, plan)
            await asyncio.to_thread(
                partial(
                    store.save_sandbox_admission_handoff,
                    admission,
                    status="active_instance_exists",
                    details={
                        "plan_id": plan.id,
                        "symbol": plan.symbol,
                        "inference": inference_payload,
                    },
                )
            )
            add_audit(
                "research",
                "已认证 crypto_5m_v1 当前信号自动物化为 OKX 模拟盘活动计划",
                certificate_id=admission.id,
                plan_id=plan.id,
                symbol=plan.symbol,
            )
            created += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                partial(
                    store.save_sandbox_admission_handoff,
                    admission,
                    status="runtime_data_blocked",
                    details={
                        "last_checked_at": datetime.now(UTC).isoformat(),
                        "error": str(exc)[:500],
                    },
                )
            )
    return created


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
            await asyncio.to_thread(MarketCatalogJournalSnapshot.model_validate, previous_payload)
            if previous_payload
            else None
        )
        snapshot, events = await asyncio.to_thread(
            build_catalog_journal_observation,
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


async def official_event_collection_loop() -> None:
    """Collect normalized events; only newly active major windows may recall the LLM."""

    while True:
        try:
            await collect_official_events_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment("trading_intelligence_official_event_failures_total")
        await asyncio.sleep(300)


def load_quant_v1_artifact(symbol: str) -> QuantModelArtifactV1 | None:
    """Read display metadata without importing or constructing native predictors."""

    cached = quant_model_cache.get(symbol)
    if cached is not None:
        return cached.artifact
    filename = {
        "BTC/USDT:USDT": "btc-usdt-swap.json",
        "ETH/USDT:USDT": "eth-usdt-swap.json",
        "SOL/USDT:USDT": "sol-usdt-swap.json",
    }.get(symbol)
    if filename is None:
        return None
    path = QUANT_V1_MODEL_DIR / filename
    if not path.is_file():
        return None
    artifact = QuantModelArtifactV1.model_validate_json(path.read_text(encoding="utf-8"))
    if artifact.symbol != symbol or artifact.timeframe != "5m":
        raise ValueError("Quant model artifact scope does not match BTC/ETH/SOL 5m v1")
    return artifact


def load_quant_v1_model(symbol: str) -> LightGBMQuantIntelligenceV1 | None:
    # UI requests and minute workers can arrive together during cold startup.
    # Only one native predictor per symbol may be created.
    with quant_model_load_lock:
        cached = quant_model_cache.get(symbol)
        if cached is not None:
            return cached
        artifact = load_quant_v1_artifact(symbol)
        if artifact is None:
            return None
        model = LightGBMQuantIntelligenceV1(artifact)
        quant_model_cache[symbol] = model
        return model


async def infer_quant_v1_on_five_minute_close(state: MarketStateV2) -> None:
    model = await asyncio.to_thread(load_quant_v1_model, state.symbol)
    if model is None:
        return
    bucket = state.data_as_of.replace(
        minute=(state.data_as_of.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    latest = await asyncio.to_thread(
        trading_intelligence_store.latest_quant_prediction,
        state.symbol,
    )
    if latest and latest.data_as_of >= bucket:
        return
    features = market_state_feature_vector(
        state.model_dump(mode="json", exclude_none=True),
        model.artifact.feature_names,
    )
    prediction = await asyncio.to_thread(
        model.infer,
        data_as_of=bucket,
        features=features,
    )
    await asyncio.to_thread(trading_intelligence_store.save_quant_prediction, prediction)
    await trading_intelligence_engine.ingest_external_event(
        symbol=state.symbol,
        event_type="QUANT_PREDICTION_UPDATED",
        event_key=prediction.prediction_id,
        payload={"quant_prediction_id": prediction.prediction_id},
    )


async def trading_intelligence_market_state_loop() -> None:
    """Update v2 state on minute closes; ordinary updates never invoke an LLM."""

    async def symbol_loop(symbol: str) -> None:
        while True:
            started = datetime.now(UTC)
            try:
                await refresh_trading_intelligence_market_state(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.increment(
                    "trading_intelligence_market_state_failures_total",
                    symbol=symbol,
                )
            elapsed = (datetime.now(UTC) - started).total_seconds()
            await asyncio.sleep(max(60 - elapsed, 1))

    async with asyncio.TaskGroup() as workers:
        for symbol in V2_SYMBOLS:
            workers.create_task(symbol_loop(symbol), name=f"market-state:{symbol}")
            workers.create_task(
                trading_intelligence_decision_queue_loop(symbol), name=f"decision-queue:{symbol}"
            )
        workers.create_task(trading_intelligence_price_loop(), name="trading-intelligence-prices")


async def trading_intelligence_decision_queue_loop(symbol: str) -> None:
    while True:
        try:
            if not risk_state_service.get().halted:
                await require_v2_automation().drain_decisions(symbol)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Deferred decision failed for %s", symbol)
        await asyncio.sleep(1)


async def trading_intelligence_price_loop() -> None:
    """Read ticker cache; full LLM features stay on their own cadence."""
    while True:
        for symbol in V2_SYMBOLS:
            try:
                tick = runtime.market_data.websocket_snapshot_for(symbol.removesuffix(":USDT"))
                if tick:
                    await require_v2_automation().on_price_tick(tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.increment("trading_intelligence_price_tick_failures_total", symbol=symbol)
        await asyncio.sleep(1)


async def enqueue_v2_trade_completed(symbol: str, event_id: str) -> None:
    require_v2_automation().enqueue_decision(symbol, "TRADE_COMPLETED", event_id)


async def reconcile_v2_demo_state(exchange, *, cancel_all: bool = False) -> dict:
    # Hold the account lock across venue awaits and ledger commits. Startup
    # recovery, the position worker, and new entries must not manage one pool
    # from different position versions or partially reconciled entry fills.
    async with v2_entry_allocation_lock:
        configuration = configuration_service.current_configuration()
        automation = require_v2_automation()
        service = EntryOrderService(
            trading_intelligence_store,
            on_terminal=automation.on_entry_terminal,
            margin_mode=str(configuration.margin_mode),
            execution_leverage=configuration.contract_leverage,
        )
        counters = await service.reconcile(
            exchange,
            cancel_all=(
                cancel_all or not automation.runtime().enabled or risk_state_service.get().halted
            ),
        )
        if counters.get("uncertain"):
            raise PositionReconciliationIntegrityError("pending entry order remains uncertain")
        result = await V2PositionReconciler(
            trading_intelligence_store,
            halt=risk_state_service.halt,
            on_trade_completed=enqueue_v2_trade_completed,
            margin_mode=str(configuration.margin_mode),
            commissioned_max_leverage=configuration.risk.max_leverage,
        ).reconcile(exchange)
        risk_state_service.recover_protection_reconciliation()
        return result


async def reconcile_v2_before_resume() -> None:
    automation = require_v2_automation().runtime()
    entries = await asyncio.to_thread(
        trading_intelligence_store.list_entry_orders,
        needs_reconciliation=True,
        mode="OKX_DEMO",
    )
    needs_demo = any(item.mode == "OKX_DEMO" and not item.terminal_notified for item in entries)
    risk_state = risk_state_service.get()
    protection_recovery_pending = (
        risk_state.halted and risk_state.source == "protection_reconciliation"
    )
    if (
        not needs_demo
        and not trading_intelligence_store.list_positions()
        and not protection_recovery_pending
    ):
        return
    adapter = build_v2_demo_adapter()
    try:
        await reconcile_v2_demo_state(adapter, cancel_all=not automation.enabled)
    finally:
        await adapter.close()


def build_v2_demo_adapter() -> CcxtExchangeAdapter:
    view = configuration_service.get()
    configuration = view.configuration
    return CcxtExchangeAdapter(
        "okx",
        secret_store.get_credentials("okx:sandbox"),
        sandbox=True,
        live_enabled=False,
        public_only=False,
        market_type="swap",
        margin_mode=configuration.margin_mode,
        contract_leverage=configuration.contract_leverage,
        store=store,
        account_id="okx:demo:swap:v2",
        proxy_url=configuration.network_proxy_url,
    )


async def trading_intelligence_position_loop() -> None:
    adapter: CcxtExchangeAdapter | None = None
    adapter_generation = -1
    try:
        while True:
            service = trading_intelligence_automation
            automation = service.runtime() if service else AutomationRuntimeV2()
            pending = trading_intelligence_store.list_entry_orders(active_only=True)
            positions = trading_intelligence_store.list_positions()
            risk_state = risk_state_service.get()
            protection_recovery_pending = (
                risk_state.halted and risk_state.source == "protection_reconciliation"
            )
            if (
                not positions
                and not any(item.mode == "OKX_DEMO" for item in pending)
                and (not automation.enabled or automation.requested_mode != TradingModeV2.OKX_DEMO)
                and not protection_recovery_pending
            ):
                if adapter is not None:
                    await adapter.close()
                    adapter = None
                await asyncio.sleep(5)
                continue
            if adapter is not None and adapter_generation != v2_configuration_generation:
                await adapter.close()
                adapter = None
            if adapter is None:
                adapter = build_v2_demo_adapter()
                adapter_generation = v2_configuration_generation
            try:
                await reconcile_v2_demo_state(
                    adapter,
                    cancel_all=not automation.enabled or risk_state_service.get().halted,
                )
                current = service.runtime()
                trading_intelligence_store.save_automation_runtime(
                    current.model_copy(
                        update={
                            "status": (
                                AutomationRuntimeStatus.STOPPED
                                if not current.enabled
                                else AutomationRuntimeStatus.DEGRADED
                                if current.symbol_failures
                                else AutomationRuntimeStatus.RUNNING
                            ),
                            "last_reconciliation_at": datetime.now(UTC),
                            "last_error_code": next(
                                iter(current.symbol_error_codes.values()),
                                None,
                            ),
                            "last_error_hash": (
                                current.last_error_hash if current.symbol_error_codes else None
                            ),
                            "updated_at": datetime.now(UTC),
                        }
                    ),
                    expected_updated_at=current.updated_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics.increment("trading_intelligence_position_loop_failures_total")
                managed_positions = trading_intelligence_store.list_positions()
                if managed_positions or isinstance(
                    exc,
                    PositionReconciliationIntegrityError,
                ):
                    risk_state_service.halt(
                        "Trading Intelligence v2 Demo position worker failed while "
                        f"managing an attributable position: {type(exc).__name__}",
                        source="protection_reconciliation",
                    )
                    current = service.runtime()
                    trading_intelligence_store.save_automation_runtime(
                        current.model_copy(
                            update={
                                "status": AutomationRuntimeStatus.HALTED,
                                "last_error_code": (f"position_integrity:{type(exc).__name__}"),
                                "last_error_hash": sha256(str(exc).encode()).hexdigest(),
                                "updated_at": datetime.now(UTC),
                            }
                        ),
                        expected_updated_at=current.updated_at,
                    )
                else:
                    current = service.runtime()
                    trading_intelligence_store.save_automation_runtime(
                        current.model_copy(
                            update={
                                "status": AutomationRuntimeStatus.DEGRADED,
                                "last_error_code": (f"position_transport:{type(exc).__name__}"),
                                "last_error_hash": sha256(str(exc).encode()).hexdigest(),
                                "updated_at": datetime.now(UTC),
                            }
                        ),
                        expected_updated_at=current.updated_at,
                    )
                if adapter is not None:
                    await adapter.close()
                    adapter = None
            await asyncio.sleep(1)
    finally:
        if adapter is not None:
            await adapter.close()


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


def ensure_default_entry_profile_timeframes() -> list[str]:
    """Add required internal timeframe roles to legacy user configurations."""

    configuration = configuration_service.current_configuration()
    required = get_entry_profile(DEFAULT_ENTRY_PROFILE_ID).context_timeframes
    missing = [item for item in required if item not in configuration.strategy.timeframes]
    if not missing:
        return []
    strategy = configuration.strategy.model_copy(
        update={"timeframes": [*configuration.strategy.timeframes, *missing]}
    )
    store.save_configuration(configuration.model_copy(update={"strategy": strategy}))
    return missing


@asynccontextmanager
async def lifespan(_: FastAPI):
    global liquidation_collector, review_worker_task, trading_intelligence_automation
    global v2_position_worker_task
    stored_configuration = configuration_service.current_configuration()
    if (
        stored_configuration.live_trading_enabled
        or stored_configuration.automatic_execution_enabled
    ):
        await asyncio.to_thread(
            store.save_configuration,
            stored_configuration.model_copy(
                update={
                    "live_trading_enabled": False,
                    "automatic_execution_enabled": False,
                }
            ),
        )
        add_audit(
            "trading_intelligence",
            "retired legacy automatic/live configuration switches during v2 startup",
            severity="warning",
        )
    previous_risk_state = risk_state_service.get()
    migrated_risk_state = risk_state_service.recover_legacy_strategy_governance_halt()
    if previous_risk_state.halted and not migrated_risk_state.halted:
        add_audit(
            "risk",
            "application startup migrated obsolete strategy-governance halt "
            "to a strategy-level entry gate",
            severity="warning",
            previous_reason=previous_risk_state.reason,
            previous_source=previous_risk_state.source,
        )
    recovered_position_overflows = await asyncio.to_thread(
        trading_intelligence_store.recover_legacy_position_quantity_overflows
    )
    if recovered_position_overflows:
        risk_state_service.halt(
            "Recovered invalid v2 position quantities; manual Demo account "
            "reconciliation is required before automated management can resume",
            source="position_integrity",
        )
        add_audit(
            "trading_intelligence",
            "Recovered legacy aggregate-position quantity overflow and halted automation",
            severity="critical",
            position_ids=[item["position_id"] for item in recovered_position_overflows],
        )
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
    interrupted_reviews = await asyncio.to_thread(store.list_strategy_review_jobs, 1000)
    for payload in interrupted_reviews:
        review_job = StrategyReviewJob.model_validate(payload)
        if review_job.review_contract_version > 1 and should_retry_review_after_restart(review_job):
            review_job.status = StrategyReviewJobStatus.QUEUED
            review_job.stage = "queued"
            review_job.error = "应用重启后已从持久化检查点恢复并自动重试"
            review_job.completed_at = None
            review_job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_strategy_review_job, review_job)
    await initialize_runtime_on_startup()
    configuration = configuration_service.current_configuration()
    trading_intelligence_automation = TradingIntelligenceAutomation(
        trading_intelligence_store,
        trading_intelligence_engine,
        decide=invoke_automated_trading_intelligence_decision,
        execute=execute_automated_trading_intelligence_plan,
        reconcile=reconcile_v2_before_resume,
    )
    if env_settings.cosmos_automation_start_paused:
        previous_automation = trading_intelligence_automation.runtime()
        trading_intelligence_automation.configure(
            enabled=False,
            requested_mode=previous_automation.requested_mode,
            symbols=previous_automation.symbols,
        )
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
    official_event_worker = asyncio.create_task(
        official_event_collection_loop(),
        name="trading-intelligence-official-events",
    )
    market_state_worker = asyncio.create_task(
        trading_intelligence_market_state_loop(),
        name="trading-intelligence-market-state",
    )
    v2_position_worker_task = asyncio.create_task(
        trading_intelligence_position_loop(),
        name="trading-intelligence-position-reconciliation",
    )
    review_worker_task = asyncio.create_task(
        strategy_review_worker_loop(),
        name="durable-strategy-review",
    )
    critical_workers.clear()
    for task in (official_event_worker, market_state_worker, v2_position_worker_task):
        critical_workers[task.get_name()] = task
        task.add_done_callback(on_critical_worker_exit)
    try:
        if not risk_state_service.get().halted:
            await trading_intelligence_automation.resume(evaluate_market_states=False)
        yield
    finally:
        critical_workers.clear()
        maintenance.cancel()
        catalog_worker.cancel()
        official_event_worker.cancel()
        market_state_worker.cancel()
        if v2_position_worker_task is not None:
            v2_position_worker_task.cancel()
        if review_worker_task is not None:
            review_worker_task.cancel()
        for task in list(backtest_job_tasks):
            task.cancel()
        for task in list(campaign_job_tasks):
            task.cancel()
        for task in list(universe_job_tasks):
            task.cancel()
        await asyncio.gather(
            maintenance,
            catalog_worker,
            official_event_worker,
            market_state_worker,
            return_exceptions=True,
        )
        if v2_position_worker_task is not None:
            await asyncio.gather(v2_position_worker_task, return_exceptions=True)
            v2_position_worker_task = None
        if review_worker_task is not None:
            await asyncio.gather(review_worker_task, return_exceptions=True)
            review_worker_task = None
        review_worker_health.update(
            status="stopped",
            activity="shutdown",
            current_job_id=None,
        )
        if backtest_job_tasks:
            await asyncio.gather(*backtest_job_tasks, return_exceptions=True)
        if campaign_job_tasks:
            await asyncio.gather(*campaign_job_tasks, return_exceptions=True)
        if universe_job_tasks:
            await asyncio.gather(*universe_job_tasks, return_exceptions=True)
        if liquidation_collector is not None:
            await liquidation_collector.close()
            liquidation_collector = None
        trading_intelligence_automation = None
        await runtime.close()


app = FastAPI(
    title="COSMOS Quant API",
    version="2.0.0",
    description="Event-driven Trading Intelligence, deterministic risk, and offline research API",
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

local_access = LocalAccess(
    [*env_settings.cors_origins, "http://127.0.0.1:5173", "http://localhost:5173"],
)


@app.middleware("http")
async def require_local_origin(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    protected = path.startswith("/api/") or path in {
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
    if protected and not local_access.trusted_request(
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        str(request.url),
    ):
        return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
    return await call_next(request)


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
    configuration = configuration_service.current_configuration()
    if (
        runtime.order_ledger is not None
        and configuration.exchange_id == "okx"
        and configuration.trading_mode == TradingMode.SANDBOX
    ):
        for position in trading_intelligence_store.list_positions():
            plan_ids.add(position.plan_id)
            runtime.order_ledger.managed_auxiliary_client_ids.update(
                V2PositionReconciler.close_client_ids(position)
            )
            for order_id in position.exit_order_ids:
                artifact = trading_intelligence_store.get_artifact(order_id)
                if not artifact or artifact["artifact_type"] != "ExchangeOrder":
                    continue
                exit_order = Order.model_validate(artifact["payload"])
                if exit_order.client_order_id:
                    runtime.order_ledger.managed_auxiliary_client_ids.add(
                        exit_order.client_order_id
                    )
            for order_id in position.entry_order_ids:
                artifact = trading_intelligence_store.get_artifact(order_id)
                if not artifact or artifact["artifact_type"] != "ExchangeOrder":
                    continue
                order = Order.model_validate(artifact["payload"])
                if order.plan_id != position.plan_id:
                    order = order.model_copy(update={"plan_id": position.plan_id})
                    trading_intelligence_store.save_artifact(
                        order.id,
                        "ExchangeOrder",
                        order,
                    )
                existing = runtime.order_ledger.records.get(order.id)
                if (
                    existing
                    and existing.order.plan_id == order.plan_id
                    and existing.protection_verified
                ):
                    continue
                runtime.order_ledger.record(order, protection_verified=True)
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
    preflight: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


class AIProposalResponse(BaseModel):
    mode: str
    proposals: list[AIProposalItem]


class ResearchUniverseRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=100)
    timeframes: list[str] = Field(min_length=1, max_length=12)
    exchange_ids: list[str] = Field(default_factory=list, max_length=3)
    bars: int = Field(default=5000, ge=500, le=220000)
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
    research_profile: Literal["crypto_5m_v1"] | None = None
    horizons: tuple[Literal[3], Literal[6], Literal[12]] = (3, 6, 12)

    @model_validator(mode="after")
    def enforce_crypto_5m_profile(self):
        if self.research_profile != "crypto_5m_v1":
            return self
        if set(self.symbols) != {"BTC/USDT", "ETH/USDT", "SOL/USDT"}:
            raise ValueError("crypto_5m_v1 requires BTC/ETH/SOL")
        if set(self.exchange_ids) != {"binance", "okx"}:
            raise ValueError("crypto_5m_v1 requires Binance and OKX")
        if self.timeframes != ["5m"]:
            raise ValueError("crypto_5m_v1 history is frozen at 5m")
        if self.bars < 210_240:
            raise ValueError("crypto_5m_v1 requires at least 730 days of 5m bars")
        return self


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
        # Unauthenticated and unmatched paths must not create unbounded metric labels.
        path = getattr(route, "path", "<unmatched>")
        method = (
            request.method
            if request.method
            in {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
                "TRACE",
                "CONNECT",
            }
            else "OTHER"
        )
        metrics.increment(
            "http_requests_total",
            method=method,
            path=path,
            status=str(status),
        )
        metrics.increment(
            "http_request_duration_seconds_total",
            perf_counter() - started,
            method=method,
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
        # PRAGMA integrity_check scans the complete database and can take minutes
        # for multi-gigabyte research ledgers. Keep the API event loop responsive
        # while the explicit readiness probe performs that deep check.
        "database": await asyncio.to_thread(store.integrity_check),
        "configuration": True,
    }
    try:
        await runtime.initialize()
        checks["runtime"] = True
    except Exception as exc:
        checks["runtime"] = str(exc)
    state = risk_state_service.get()
    checks["trading_halted"] = state.halted
    checks["critical_workers"] = bool(critical_workers) and all(
        not task.done() for task in critical_workers.values()
    )
    ready = (
        checks["database"] == "ok"
        and checks.get("runtime") is True
        and checks["critical_workers"] is True
    )
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
    return {
        "status": "ok",
        "version": app.version,
        "server_time": datetime.now(UTC),
    }


@app.get("/api/readiness")
async def worker_readiness():
    workers = {name: not task.done() for name, task in critical_workers.items()}
    ready = bool(workers) and all(workers.values())
    return JSONResponse(
        {
            "status": "ready" if ready else "degraded",
            "workers": workers,
            "risk_halted": risk_state_service.get().halted,
        },
        status_code=200 if ready else 503,
    )


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


@app.get("/api/ai/health")
async def get_ai_health():
    return {
        "generated_at": datetime.now(UTC),
        "providers": await asyncio.to_thread(ai_gateway.health_snapshot),
        "research_fallback": "deterministic",
        "review_llm_calls": 0,
        "execution_llm_calls": 0,
        "pipeline_continues_when_ai_unavailable": True,
        "role_isolation": {
            "research": "optional_ai_generation_with_quality_gate",
            "review": "deterministic_only",
            "trading": "deterministic_only",
        },
    }


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


class SandboxAdmissionIssueRequest(BaseModel):
    strategy_ir_hash: str = Field(min_length=16)
    ensemble_model_id: str
    model_artifact_ids: tuple[str, ...] = Field(min_length=3)
    development_audit_certificate_id: str = Field(min_length=16)


class DevelopmentAuditIssueRequest(BaseModel):
    strategy_ir_hash: str = Field(min_length=16)
    ensemble_model_id: str
    model_artifact_ids: tuple[str, ...] = Field(min_length=3)
    campaign_id: str = Field(min_length=8)


class SandboxExecutionEvidenceRequest(BaseModel):
    episode_id: str
    expected_round_trip_cost_bps: Decimal = Field(ge=0)
    realized_round_trip_cost_bps: Decimal = Field(ge=0)
    expected_slippage_bps: Decimal = Field(ge=0)
    realized_slippage_bps: Decimal = Field(ge=0)
    native_protection_verified: bool
    reconciled: bool


_live_unlock_challenges: dict[str, dict] = {}
_live_unlock_grants: dict[str, dict] = {}
LIVE_CONFIRMATION_PHRASES = {
    1: "我确认将进入实盘授权",
    2: "我确认使用真实资金自动交易",
}


def strategy_certificate_configuration_fingerprint(
    configuration: UserConfiguration,
) -> str:
    """Bind strategy evidence without making SANDBOX -> LIVE itself a mutation."""

    return stable_fingerprint(
        {
            "scheme": "full-strategy-certificate-config-v2",
            "exchange_id": configuration.exchange_id,
            "market_type": str(configuration.market_type),
            "margin_mode": str(configuration.margin_mode),
            "contract_leverage": str(configuration.contract_leverage),
            "fee_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "risk": configuration.risk.model_dump(mode="json"),
        }
    )


def reconcile_strategy_review_backlog(entry_profile: str = DEFAULT_ENTRY_PROFILE_ID) -> dict:
    """Queue v2 candidates while preserving every legacy candidate and review."""

    payloads = store.list_strategy_review_candidate_payloads(1000)
    configuration = configuration_service.current_configuration()
    contract_version = configuration.strategy.validation.review_contract_version
    policy_fingerprint = review_policy_fingerprint(configuration)
    queued = migrated = rejected = existing = legacy = 0
    for payload in payloads:
        context_payload = payload.get("research_context")
        if not isinstance(context_payload, dict):
            legacy += 1
            continue
        candidate_id = str(payload.get("id") or f"legacy_{stable_fingerprint(payload)}")
        candidate = parse_strategy_candidate_payload(
            payload,
            audit_context="review_backlog_reconcile",
            emit_audit=False,
        )
        if candidate is None:
            raw_spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            legacy_fingerprint = str(
                payload.get("spec_fingerprint") or stable_fingerprint(raw_spec or payload)
            )
            fingerprint = strategy_review_fingerprint(
                legacy_fingerprint,
                entry_profile=str(context_payload.get("entry_profile") or entry_profile),
                candidate_id=candidate_id,
                symbol=str(context_payload.get("symbol") or "unknown"),
                timeframe=str(context_payload.get("timeframe") or "unknown"),
                policy_fingerprint=policy_fingerprint,
                review_contract_version=contract_version,
            )
            if store.load_strategy_review_job_by_fingerprint(fingerprint):
                existing += 1
                continue
            now = datetime.now(UTC)
            store.save_strategy_review_job(
                StrategyReviewJob(
                    candidate_id=candidate_id,
                    candidate_fingerprint=legacy_fingerprint,
                    review_fingerprint=fingerprint,
                    review_contract_version=contract_version,
                    policy_fingerprint=policy_fingerprint,
                    origin_symbol=str(context_payload.get("symbol") or "unknown"),
                    origin_timeframe=str(context_payload.get("timeframe") or "unknown"),
                    entry_profile=str(context_payload.get("entry_profile") or entry_profile),
                    status=StrategyReviewJobStatus.REJECTED,
                    stage="contract_validation",
                    evidence_tier="rejected",
                    progress_percent=100,
                    rejection_reasons=["v2 候选无法通过当前 StrategySpec 合约校验"],
                    evidence_refs=[candidate_id],
                    completed_at=now,
                    updated_at=now,
                )
            )
            rejected += 1
            continue

        if candidate.status in {CandidateStatus.REJECTED, CandidateStatus.RETIRED}:
            existing += 1
            continue

        context = candidate.research_context
        assert context is not None

        fingerprint = strategy_review_fingerprint(
            candidate.spec_fingerprint,
            entry_profile=context.entry_profile,
            candidate_id=candidate.id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            policy_fingerprint=policy_fingerprint,
            review_contract_version=contract_version,
        )
        if store.load_strategy_review_job_by_fingerprint(fingerprint):
            existing += 1
            continue
        now = datetime.now(UTC)
        status = StrategyReviewJobStatus.QUEUED
        stage = "queued"
        tier = "unreviewed"
        progress = 0
        reasons: list[str] = []
        completed_at = None
        if candidate.status in {CandidateStatus.CHALLENGER, CandidateStatus.CHAMPION}:
            status = StrategyReviewJobStatus.ADMITTED
            stage = "admitted"
            tier = (
                "sandbox_mature"
                if candidate.status == CandidateStatus.CHAMPION
                else "sandbox_canary"
            )
            progress = 100
            completed_at = now
        elif candidate.status in {CandidateStatus.REJECTED, CandidateStatus.RETIRED}:
            status = StrategyReviewJobStatus.REJECTED
            stage = "rejected"
            tier = "rejected"
            progress = 100
            reasons = candidate.decision_reasons[-8:]
            completed_at = now
        job = StrategyReviewJob(
            candidate_id=candidate.id,
            candidate_fingerprint=candidate.spec_fingerprint,
            review_fingerprint=fingerprint,
            review_contract_version=contract_version,
            policy_fingerprint=policy_fingerprint,
            origin_symbol=context.symbol,
            origin_timeframe=context.timeframe,
            trial_count=context.trial_count,
            entry_profile=context.entry_profile,
            status=status,
            stage=stage,
            evidence_tier=tier,
            progress_percent=progress,
            dataset_id=candidate.dataset_id,
            parent_candidate_id=candidate.parent_candidate_id,
            rejection_reasons=reasons,
            evidence_refs=[item for item in [candidate.dataset_id, candidate.id] if item],
            completed_at=completed_at,
            updated_at=now,
        )
        store.save_strategy_review_job(job)
        queued += status == StrategyReviewJobStatus.QUEUED
    if queued or migrated or rejected:
        add_audit(
            "review",
            "审核 Agent 已协调全部历史候选并建立持久化队列",
            queued=queued,
            migrated=migrated,
            contract_rejected=rejected,
            existing=existing,
        )
    return {
        "queued": queued,
        "migrated": migrated,
        "contract_rejected": rejected,
        "existing": existing,
        "legacy": legacy,
        "total_candidates": len(payloads),
    }


def missing_review_dataset_features(
    dataset: ResearchDataset,
    required_features: list[str],
) -> list[str]:
    counts = {
        "funding_rate": dataset.funding_count,
        "open_interest": dataset.open_interest_count,
        "positioning": dataset.positioning_count,
        "basis": dataset.basis_count,
        "order_flow": dataset.order_flow_count,
        "liquidation_flow": dataset.liquidation_flow_count,
    }
    return [feature for feature in required_features if feature != "ohlcv" and counts[feature] <= 0]


async def ensure_strategy_review_dataset(
    candidate: StrategyCandidate,
    *,
    symbol: str | None = None,
) -> ResearchDataset:
    context = candidate.research_context
    if context is None:
        raise RuntimeError("v1 候选只读保留，不能进入 v2 审核")
    review_symbol = symbol or context.symbol
    timeframe = context.timeframe
    fetch_features = any(feature != "ohlcv" for feature in context.required_features)
    existing = [
        ResearchDataset.model_validate(item)
        for item in await asyncio.to_thread(store.list_research_datasets, 1000)
        if item.get("symbol") == review_symbol
        and item.get("timeframe") == timeframe
        and item.get("exchange") == context.exchange
        and item.get("market_type") == context.market_type
    ]
    existing.sort(key=lambda item: item.candle_count, reverse=True)
    suitable = [
        item
        for item in existing
        if item.candle_count >= 20000
        and not missing_review_dataset_features(item, context.required_features)
    ]
    if suitable:
        return suitable[0]
    try:
        market, history = await runtime.market_context(
            limit=20000,
            symbol=review_symbol,
            timeframe=timeframe,
        )
        candles = closed_candles(history, timeframe, market.timestamp)
        if len(candles) < 500:
            raise RuntimeError(f"{timeframe} 审核数据不足 500 根，当前 {len(candles)} 根")
        dataset, *_ = await materialize_research_dataset(
            candles,
            exchange=context.exchange,
            market_type=context.market_type,
            symbol=review_symbol,
            timeframe=timeframe,
            quality=runtime.market_quality,
            fetch_derivative_features=fetch_features,
        )
        return dataset
    except Exception:
        if existing and existing[0].candle_count >= 500:
            return existing[0]
        raise


def validate_strategy_review_inputs(
    candle_payloads: list[dict],
    funding_payloads: list[dict],
    open_interest_payloads: list[dict],
    positioning_payloads: list[dict],
    basis_payloads: list[dict],
) -> tuple[
    list[Candle],
    list[FundingRatePoint],
    list[OpenInterestPoint],
    list[PositioningPoint],
    list[BasisPoint],
]:
    """Validate large immutable review inputs outside the API event loop."""

    return (
        [Candle.model_validate(item) for item in candle_payloads],
        [FundingRatePoint.model_validate(item) for item in funding_payloads],
        [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
        [PositioningPoint.model_validate(item) for item in positioning_payloads],
        [BasisPoint.model_validate(item) for item in basis_payloads],
    )


def review_result_metrics(result: BacktestResult, dataset: ResearchDataset) -> dict:
    raw_wins = Decimal("0")
    raw_losses = Decimal("0")
    for episode in result.trade_episodes:
        raw_pnl = episode.pnl + episode.fees + episode.slippage_cost + episode.funding_cost
        if raw_pnl > 0:
            raw_wins += raw_pnl
        elif raw_pnl < 0:
            raw_losses += abs(raw_pnl)
    raw_profit_factor = (
        raw_wins / raw_losses
        if raw_losses > 0
        else Decimal("999.99")
        if raw_wins > 0
        else Decimal("0")
    )
    return {
        "return_percent": str(result.return_percent),
        "max_drawdown_percent": str(result.max_drawdown_percent),
        "profit_factor": str(result.profit_factor),
        "raw_profit_factor": str(raw_profit_factor.quantize(Decimal("0.01"))),
        "sharpe": str(result.sharpe),
        "total_trades": result.total_trades,
        "total_fees": str(result.total_fees),
        "total_slippage": str(result.total_slippage),
        "total_funding": str(result.total_funding),
        "stress_return_percent": str(result.stress_return_percent or Decimal("0")),
        "round_trip_cost_r": str(result.cost_model.get("round_trip_cost_r") or "0"),
        "notional_cap_count": int(result.cost_model.get("notional_cap_count") or 0),
        "gross_exposure_cap_count": int(result.cost_model.get("gross_exposure_cap_count") or 0),
        "leverage_cap_count": int(result.cost_model.get("leverage_cap_count") or 0),
        "volume_cap_count": int(result.cost_model.get("volume_cap_count") or 0),
        "candle_count": dataset.candle_count,
        "robustness_passed": result.robustness_passed,
    }


async def run_candidate_review_scope(
    candidate: StrategyCandidate,
    *,
    symbol: str,
    role: str,
    trial_count: int,
) -> tuple[BacktestResult | None, ReviewScopeResult]:
    context = candidate.research_context
    assert context is not None
    dataset = await ensure_strategy_review_dataset(candidate, symbol=symbol)
    missing = missing_review_dataset_features(dataset, context.required_features)
    if missing:
        reasons = [f"缺少审核所需历史特征：{', '.join(missing)}"]
        return None, ReviewScopeResult(
            symbol=symbol,
            timeframe=context.timeframe,
            role=role,
            status="blocked",
            stage="data_required",
            dataset_id=dataset.id,
            dataset_fingerprint=dataset.data_fingerprint,
            reasons=reasons,
            evidence_refs=[dataset.id, dataset.data_fingerprint],
        )
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
    candles, funding, open_interest, positioning, basis = await to_process.run_sync(
        partial(
            validate_strategy_review_inputs,
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
        ),
        cancellable=True,
        limiter=research_process_limiter,
    )
    order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
    liquidation_flow = [
        LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
    ]
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
        research_trial_count=trial_count,
    )
    return result, ReviewScopeResult(
        symbol=symbol,
        timeframe=context.timeframe,
        role=role,
        status="passed" if result.robustness_passed else "failed",
        stage=("base_and_stress_passed" if result.robustness_passed else "base_or_stress_failed"),
        dataset_id=dataset.id,
        dataset_fingerprint=dataset.data_fingerprint,
        metrics=review_result_metrics(result, dataset),
        reasons=result.validation_reasons[:12],
        evidence_refs=[dataset.id, dataset.data_fingerprint],
    )


def cross_market_review_passed(
    results: list[BacktestResult],
    *,
    minimum_passes: int,
    minimum_profit_factor: Decimal,
    maximum_drawdown_percent: Decimal,
) -> tuple[bool, dict, list[str]]:
    wins = Decimal("0")
    losses = Decimal("0")
    for result in results:
        for episode in result.trade_episodes:
            if episode.pnl > 0:
                wins += episode.pnl
            elif episode.pnl < 0:
                losses += abs(episode.pnl)
    combined_pf = wins / losses if losses > 0 else Decimal("999.99") if wins > 0 else Decimal("0")
    passed_count = sum(result.robustness_passed for result in results)
    combined_stress = sum(
        (result.stress_return_percent or Decimal("0") for result in results),
        Decimal("0"),
    )
    maximum_drawdown = max(
        (result.max_drawdown_percent for result in results),
        default=Decimal("0"),
    )
    reasons: list[str] = []
    if passed_count < minimum_passes:
        reasons.append(f"跨市场仅 {passed_count}/{len(results)} 通过，要求至少 {minimum_passes}")
    if combined_pf < minimum_profit_factor:
        reasons.append(f"跨市场组合 PF {combined_pf:.2f} 低于 {minimum_profit_factor}")
    if combined_stress <= 0:
        reasons.append("跨市场组合压力收益不为正")
    if maximum_drawdown > maximum_drawdown_percent:
        reasons.append(f"跨市场最大回撤 {maximum_drawdown:.2f}% 超过 {maximum_drawdown_percent}%")
    metrics = {
        "cross_market_passed_count": passed_count,
        "cross_market_scope_count": len(results),
        "combined_profit_factor": str(combined_pf.quantize(Decimal("0.01"))),
        "combined_stress_return_percent": str(combined_stress.quantize(Decimal("0.01"))),
        "cross_market_max_drawdown_percent": str(maximum_drawdown),
    }
    return not reasons, metrics, reasons


async def process_strategy_review_job(job: StrategyReviewJob) -> StrategyReviewJob:
    now = datetime.now(UTC)
    job.status = StrategyReviewJobStatus.RUNNING
    job.stage = "binding_dataset"
    job.progress_percent = 10
    job.started_at = job.started_at or now
    job.updated_at = now
    job.attempt_count += 1
    job.error = None
    await asyncio.to_thread(store.save_strategy_review_job, job)
    try:
        candidate_payload = await asyncio.to_thread(
            store.load_strategy_candidate,
            job.candidate_id,
        )
        if not candidate_payload:
            raise RuntimeError("候选记录不存在")
        candidate = StrategyCandidate.model_validate(candidate_payload)
        context = candidate.research_context
        if context is None or job.review_contract_version <= 1:
            raise RuntimeError("v1 审核记录只读保留，不允许进入 v2 worker")
        job.trial_count = max(
            job.trial_count,
            await asyncio.to_thread(
                current_candidate_trial_count,
                trial_group=context.trial_group,
            ),
        )
        job.stage = "origin_validation"
        job.progress_percent = 20
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_strategy_review_job, job)
        origin_result, origin_scope = await run_candidate_review_scope(
            candidate,
            symbol=context.symbol,
            role="origin",
            trial_count=job.trial_count,
        )
        job.scope_results = [origin_scope]
        job.dataset_id = origin_scope.dataset_id
        job.dataset_fingerprint = origin_scope.dataset_fingerprint
        if origin_result is None:
            job.status = StrategyReviewJobStatus.BLOCKED
            job.stage = "data_required"
            job.evidence_tier = "unreviewed"
            job.progress_percent = 20
            job.error = origin_scope.reasons[0]
            job.rejection_reasons = origin_scope.reasons
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_strategy_review_job, job)
            return job
        candidate = attach_backtest(candidate, origin_scope.dataset_id or "", origin_result)
        job.metrics = dict(origin_scope.metrics)
        job.evidence_refs = [candidate.id, *origin_scope.evidence_refs]
        if not origin_result.robustness_passed:
            job.status = StrategyReviewJobStatus.REJECTED
            job.stage = "origin_rejected"
            job.evidence_tier = "rejected"
            job.progress_percent = 100
            job.rejection_reasons = origin_result.validation_reasons[:12]
            job.completed_at = datetime.now(UTC)
            job.updated_at = job.completed_at
            await asyncio.to_thread(store.save_strategy_candidate, candidate)
            await asyncio.to_thread(store.save_strategy_review_job, job)
            return job

        candidate = candidate.model_copy(
            deep=True,
            update={
                "status": CandidateStatus.VALIDATED,
                "updated_at": datetime.now(UTC),
            },
        )
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
        job.stage = "cross_market_validation"
        job.evidence_tier = "paper_eligible"
        job.progress_percent = 45
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_strategy_review_job, job)

        target_symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        cross_results: list[BacktestResult] = []
        blocked_scopes: list[ReviewScopeResult] = []
        for index, target_symbol in enumerate(target_symbols, start=1):
            if target_symbol == context.symbol:
                cross_results.append(origin_result)
                continue
            result, scope = await run_candidate_review_scope(
                candidate,
                symbol=target_symbol,
                role="cross_market",
                trial_count=job.trial_count,
            )
            job.scope_results.append(scope)
            job.evidence_refs.extend(scope.evidence_refs)
            if result is None:
                blocked_scopes.append(scope)
            else:
                cross_results.append(result)
            job.progress_percent = min(90, 45 + index * 15)
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_strategy_review_job, job)

        if blocked_scopes:
            job.status = StrategyReviewJobStatus.BLOCKED
            job.stage = "data_required"
            job.error = "；".join(scope.reasons[0] for scope in blocked_scopes)
            job.rejection_reasons = [reason for scope in blocked_scopes for reason in scope.reasons]
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save_strategy_review_job, job)
            return job

        validation = configuration_service.current_configuration().strategy.validation
        cross_passed, cross_metrics, cross_reasons = cross_market_review_passed(
            cross_results,
            minimum_passes=validation.cross_market_minimum_passes,
            minimum_profit_factor=validation.minimum_profit_factor,
            maximum_drawdown_percent=validation.maximum_drawdown_percent,
        )
        job.metrics.update(cross_metrics)
        job.progress_percent = 100
        job.completed_at = datetime.now(UTC)
        job.updated_at = job.completed_at
        job.status = StrategyReviewJobStatus.ADMITTED
        if cross_passed:
            job.status = StrategyReviewJobStatus.ADMITTED
            job.stage = "cross_market_admitted"
            job.evidence_tier = "sandbox_canary"
            candidate = candidate.model_copy(
                deep=True,
                update={
                    "status": CandidateStatus.CHALLENGER,
                    "decision_reasons": [
                        *candidate.decision_reasons,
                        "来源市场及跨市场净优势审核通过",
                    ],
                    "updated_at": datetime.now(UTC),
                },
            )
            await asyncio.to_thread(store.save_strategy_candidate, candidate)
        else:
            job.stage = "cross_market_hold"
            job.evidence_tier = "paper_eligible"
            job.rejection_reasons = cross_reasons
    except asyncio.CancelledError:
        job.status = StrategyReviewJobStatus.QUEUED
        job.stage = "queued"
        job.error = "审核进程重启，作业已安全回队"
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_strategy_review_job, job)
        raise
    except Exception as exc:
        job.status = StrategyReviewJobStatus.BLOCKED
        job.stage = "infrastructure_blocked"
        job.error = review_failure_detail(exc)
        job.updated_at = datetime.now(UTC)
        logger.exception(
            "strategy review job %s blocked at stage %s",
            job.id,
            job.stage,
        )
    await asyncio.to_thread(store.save_strategy_review_job, job)
    return job


async def strategy_review_worker_loop() -> None:
    review_worker_health.update(status="running", activity="starting")
    while True:
        review_worker_health["heartbeat_at"] = datetime.now(UTC)
        try:
            await asyncio.to_thread(
                reconcile_strategy_review_backlog,
                DEFAULT_ENTRY_PROFILE_ID,
            )
            queued_jobs = [
                StrategyReviewJob.model_validate(item)
                for item in await asyncio.to_thread(store.list_strategy_review_jobs, 1000)
                if item.get("status") == str(StrategyReviewJobStatus.QUEUED)
                and int(item.get("review_contract_version") or 1) > 1
            ]
            if queued_jobs:
                review_worker_health.update(
                    activity="validating",
                    current_job_id=queued_jobs[0].id,
                    last_error=None,
                )
                await process_strategy_review_job(queued_jobs[0])
            else:
                review_worker_health.update(
                    activity="backlog_idle",
                    current_job_id=None,
                    last_error=None,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            review_worker_health.update(
                activity="degraded",
                current_job_id=None,
                last_error=str(exc)[:1000],
            )
            add_audit(
                "review",
                "离线审核后台循环发生可恢复错误，将自动重试",
                severity="warning",
                error=str(exc)[:500],
            )
        try:
            await asyncio.wait_for(review_worker_wakeup.wait(), timeout=5)
            review_worker_wakeup.clear()
        except TimeoutError:
            pass


def build_review_jobs(
    limit: int = 100,
    *,
    candidate_payloads: list[dict] | None = None,
    candidate_summaries: list[dict] | None = None,
    campaign_payloads: list[dict] | None = None,
) -> list[ReviewJobView]:
    views: list[ReviewJobView] = []
    if candidate_summaries is None:
        if candidate_payloads is not None:
            candidate_summaries = [
                {
                    "candidate_id": str(payload.get("id") or ""),
                    "status": str(payload.get("status") or ""),
                    "display_name": str(
                        (payload.get("spec") or {}).get("name")
                        or (payload.get("spec") or {}).get("label")
                        or payload.get("id")
                        or ""
                    ),
                }
                for payload in candidate_payloads
            ]
        else:
            candidate_summaries = store.list_strategy_candidate_summaries(max(500, limit))
    candidate_summary_by_id = {
        str(summary.get("candidate_id") or ""): summary for summary in candidate_summaries
    }
    for payload in store.list_strategy_review_jobs(limit):
        job = StrategyReviewJob.model_validate(payload)
        candidate_summary = candidate_summary_by_id.get(job.candidate_id)
        views.append(
            ReviewJobView(
                id=job.id,
                candidate_id=job.candidate_id,
                candidate_name=str(
                    (candidate_summary or {}).get("display_name") or job.candidate_id
                ),
                candidate_fingerprint=job.candidate_fingerprint,
                review_contract_version=job.review_contract_version,
                policy_fingerprint=job.policy_fingerprint,
                origin_scope={
                    key: value
                    for key, value in {
                        "symbol": job.origin_symbol,
                        "timeframe": job.origin_timeframe,
                    }.items()
                    if value
                },
                scope_results=[result.model_dump(mode="json") for result in job.scope_results],
                status=str(job.status),
                stage=job.stage,
                evidence_tier=job.evidence_tier,
                progress_percent=job.progress_percent,
                rejection_reasons=job.rejection_reasons,
                evidence_refs=job.evidence_refs,
                metrics=job.metrics,
                dataset_id=job.dataset_id,
                attempt_count=job.attempt_count,
                error=job.error,
                updated_at=job.updated_at,
            )
        )
    remaining = max(0, limit - len(views))
    if remaining == 0:
        return views[:limit]
    if campaign_payloads is None:
        campaign_payloads = store.list_research_campaigns(remaining)
    campaign_payload_by_id = {
        str(payload.get("id") or ""): payload for payload in campaign_payloads
    }
    for payload in store.list_research_campaign_jobs(remaining):
        job = ResearchCampaignJob.model_validate(payload)
        report = None
        if job.report_id:
            report_payload = campaign_payload_by_id.get(job.report_id)
            if report_payload:
                report = ResearchCampaignReport.model_validate(report_payload)
        if job.status == ResearchCampaignJobStatus.QUEUED:
            stage = "queued"
            tier = "unreviewed"
        elif job.status == ResearchCampaignJobStatus.RUNNING:
            stage = "validation"
            tier = "unreviewed"
        elif job.status == ResearchCampaignJobStatus.FAILED:
            stage = "rejected"
            tier = "rejected"
        elif report and report.execution_certificate:
            stage = "admitted"
            tier = "sandbox_mature"
        elif report and any(candidate.cross_market_passed for candidate in report.candidates):
            stage = "canary"
            tier = "sandbox_canary"
        else:
            stage = "rejected"
            tier = "rejected"
        total = max(job.total_cells, 1)
        views.append(
            ReviewJobView(
                id=job.id,
                status=str(job.status),
                stage=stage,
                evidence_tier=tier,
                progress_percent=min(100, int(job.completed_cells * 100 / total)),
                report_id=job.report_id,
                candidate_count=len(report.candidates) if report else 0,
                selected_strategy_id=(report.selected_library_entry_id if report else None),
                rejection_reasons=(
                    report.selection_reasons[:8]
                    if report and not report.execution_certificate
                    else []
                ),
                error=job.error,
                updated_at=job.updated_at,
            )
        )
    return views[:limit]


def build_review_overview() -> ReviewOverview:
    all_strategy_jobs = [
        StrategyReviewJob.model_validate(item) for item in store.list_strategy_review_jobs(1000)
    ]
    strategy_jobs = [job for job in all_strategy_jobs if job.review_contract_version >= 2]
    candidate_payloads = store.list_strategy_candidates(500)
    candidate_summaries = [
        {
            "candidate_id": str(payload.get("id") or ""),
            "status": str(payload.get("status") or ""),
            "display_name": str(
                (payload.get("spec") or {}).get("name")
                or (payload.get("spec") or {}).get("label")
                or payload.get("id")
                or ""
            ),
        }
        for payload in candidate_payloads
    ]
    v2_candidate_payloads = [
        payload
        for payload in candidate_payloads
        if isinstance(payload.get("research_context"), dict)
    ]
    active_job_candidate_ids = {job.candidate_id for job in strategy_jobs}
    jobs = build_review_jobs(
        candidate_payloads=candidate_payloads,
        candidate_summaries=candidate_summaries,
    )
    return ReviewOverview(
        queued=sum(job.status == StrategyReviewJobStatus.QUEUED for job in strategy_jobs),
        unreviewed=sum(
            str(candidate.get("status") or "") == str(CandidateStatus.PROPOSED)
            and str(candidate.get("id") or "") not in active_job_candidate_ids
            for candidate in v2_candidate_payloads
        ),
        running=sum(job.status == StrategyReviewJobStatus.RUNNING for job in strategy_jobs),
        blocked=sum(job.status == StrategyReviewJobStatus.BLOCKED for job in strategy_jobs),
        paper_eligible=sum(
            str(candidate.get("status") or "")
            in {
                str(CandidateStatus.VALIDATED),
                str(CandidateStatus.CHALLENGER),
                str(CandidateStatus.CHAMPION),
            }
            for candidate in v2_candidate_payloads
        ),
        sandbox_canary=sum(job.evidence_tier == "sandbox_canary" for job in strategy_jobs),
        sandbox_mature=sum(job.evidence_tier == "sandbox_mature" for job in strategy_jobs),
        live_capped=0,
        rejected=sum(job.status == StrategyReviewJobStatus.REJECTED for job in strategy_jobs)
        + sum(
            str(candidate.get("status") or "") == str(CandidateStatus.REJECTED)
            and str(candidate.get("id") or "") not in active_job_candidate_ids
            for candidate in v2_candidate_payloads
        ),
        legacy=sum(
            not isinstance(candidate.get("research_context"), dict)
            for candidate in candidate_payloads
        ),
        data_blocked=sum(
            job.status == StrategyReviewJobStatus.BLOCKED and job.stage == "data_required"
            for job in strategy_jobs
        ),
        jobs=jobs,
        pipeline={
            "compiler": "deterministic",
            "backtest": "deterministic",
            "comparison": "deterministic",
            "execution_llm_calls": 0,
            "worker_status": str(review_worker_health.get("status") or "stopped"),
            "worker_activity": str(review_worker_health.get("activity") or "unknown"),
            "worker_current_job_id": str(review_worker_health.get("current_job_id") or ""),
            "worker_last_error": str(review_worker_health.get("last_error") or ""),
        },
    )


def build_review_evidence(
    *,
    status: str | None = None,
    search: str = "",
    limit: int = 200,
) -> list[ReviewEvidenceItem]:
    items: list[ReviewEvidenceItem] = []
    normalized_status = (status or "").strip().lower()
    normalized_search = search.strip().lower()
    tier_by_status = {
        str(CandidateStatus.PROPOSED): "unreviewed",
        str(CandidateStatus.VALIDATED): "paper_eligible",
        str(CandidateStatus.CHALLENGER): "sandbox_canary",
        str(CandidateStatus.CHAMPION): "sandbox_mature",
        str(CandidateStatus.REJECTED): "rejected",
        str(CandidateStatus.RETIRED): "retired",
    }
    review_jobs_by_candidate: dict[str, StrategyReviewJob] = {}
    for payload in store.list_strategy_review_jobs(1000):
        job = StrategyReviewJob.model_validate(payload)
        review_jobs_by_candidate.setdefault(job.candidate_id, job)
    for payload in store.list_strategy_candidates(min(max(limit, 1), 500)):
        try:
            candidate = StrategyCandidate.model_validate(payload)
        except ValidationError as exc:
            candidate_id = str(payload.get("id") or "legacy-invalid-candidate")
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            item = ReviewEvidenceItem(
                id=candidate_id,
                name=str(spec.get("name") or spec.get("label") or candidate_id),
                status="invalid",
                evidence_tier="rejected",
                source=str(payload.get("source") or spec.get("source") or "legacy"),
                family=str(spec.get("family") or spec.get("strategy_kind") or "unknown"),
                summary="候选与当前 StrategySpec 合约不兼容，已隔离且不可交易。",
                reasons=[
                    "候选结构、字段范围或指纹无法通过当前 StrategySpec 校验",
                    "历史记录继续保留，但不会进入策略库、审核队列或执行层",
                ],
                evidence_refs=[candidate_id],
                technical_details=str(exc)[:2000],
                review_contract_version=1,
                updated_at=datetime.fromisoformat(str(payload.get("updated_at")))
                if payload.get("updated_at")
                else datetime.now(UTC),
            )
        else:
            candidate_status = str(candidate.status)
            review_job = review_jobs_by_candidate.get(candidate.id)
            is_legacy = candidate.research_context is None
            backtest = candidate.backtest
            reasons = list(candidate.decision_reasons)
            if backtest:
                reasons.extend(backtest.validation_reasons)
            metrics: dict[str, str | int | float | bool | None] = {}
            if backtest:
                metrics = {
                    "return_percent": str(backtest.return_percent),
                    "max_drawdown_percent": str(backtest.max_drawdown_percent),
                    "profit_factor": str(backtest.profit_factor),
                    "win_rate_percent": str(backtest.win_rate_percent),
                    "total_trades": backtest.total_trades,
                    "robustness_passed": backtest.robustness_passed,
                }
            if review_job:
                metrics.update(review_job.metrics)
                reasons.extend(review_job.rejection_reasons)
            evidence_tier = tier_by_status.get(candidate_status, "unreviewed")
            display_status = candidate_status
            if is_legacy and candidate.status == CandidateStatus.PROPOSED:
                display_status = "legacy"
                evidence_tier = "legacy"
                reasons.insert(
                    0,
                    "历史审核 v1：只读保留，不进入活动审核队列，也不会自动重审。",
                )
            elif review_job:
                evidence_tier = review_job.evidence_tier
            context = candidate.research_context
            item = ReviewEvidenceItem(
                id=candidate.id,
                name=str(candidate.spec.get("name") or candidate.spec.get("label") or candidate.id),
                status=display_status,
                evidence_tier=evidence_tier,
                source=candidate.source,
                family=str(candidate.spec.get("family") or "unknown"),
                summary=(reasons[0] if reasons else "候选已登记，等待确定性审核证据。"),
                reasons=list(dict.fromkeys(reasons))[:40],
                metrics=metrics,
                evidence_refs=[
                    reference
                    for reference in (
                        candidate.id,
                        candidate.dataset_id,
                        candidate.spec_fingerprint,
                        review_job.id if review_job else None,
                    )
                    if reference
                ],
                review_contract_version=(
                    review_job.review_contract_version if review_job else (1 if is_legacy else 2)
                ),
                origin_scope=(
                    {
                        "symbol": context.symbol,
                        "timeframe": context.timeframe,
                    }
                    if context
                    else {}
                ),
                scope_results=(
                    [result.model_dump(mode="json") for result in review_job.scope_results]
                    if review_job
                    else []
                ),
                updated_at=candidate.updated_at,
            )
        if normalized_status and normalized_status not in {
            item.status.lower(),
            item.evidence_tier.lower(),
        }:
            continue
        if (
            normalized_search
            and normalized_search
            not in " ".join(
                [item.id, item.name, item.family, item.source, item.summary, *item.reasons]
            ).lower()
        ):
            continue
        items.append(item)
    return sorted(items, key=lambda item: item.updated_at, reverse=True)


@app.get("/api/review/overview", response_model=ReviewOverview)
async def review_agent_overview():
    return await asyncio.to_thread(build_review_overview)


@app.get("/api/review/jobs", response_model=list[ReviewJobView])
async def review_agent_jobs(limit: int = Query(default=100, ge=1, le=500)):
    return await asyncio.to_thread(build_review_jobs, limit)


@app.get("/api/review/jobs/{job_id}", response_model=ReviewJobView)
async def review_agent_job(job_id: str):
    item = next((job for job in build_review_jobs(500) if job.id == job_id), None)
    if item is None:
        raise HTTPException(404, "review job not found")
    return item


@app.post("/api/review/backlog/reconcile")
async def reconcile_review_backlog():
    result = await asyncio.to_thread(reconcile_strategy_review_backlog)
    review_worker_wakeup.set()
    return {
        **result,
        "worker_status": review_worker_health.get("status"),
        "worker_activity": review_worker_health.get("activity"),
    }


@app.get("/api/review/evidence", response_model=list[ReviewEvidenceItem])
async def review_evidence(
    status: str | None = None,
    search: str = "",
    limit: int = Query(default=200, ge=1, le=500),
):
    return await asyncio.to_thread(
        build_review_evidence,
        status=status,
        search=search,
        limit=limit,
    )


@app.post("/api/review/jobs/{job_id}/retry", response_model=ReviewJobView)
async def retry_review_job(job_id: str):
    strategy_payload = await asyncio.to_thread(store.load_strategy_review_job, job_id)
    if strategy_payload:
        strategy_job = StrategyReviewJob.model_validate(strategy_payload)
        if strategy_job.review_contract_version <= 1:
            raise HTTPException(409, "v1 review evidence is read-only")
        if strategy_job.status != StrategyReviewJobStatus.BLOCKED:
            raise HTTPException(409, "only a blocked v2 review can be retried")
        strategy_job.status = StrategyReviewJobStatus.QUEUED
        strategy_job.stage = "queued"
        strategy_job.error = None
        strategy_job.completed_at = None
        strategy_job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save_strategy_review_job, strategy_job)
        review_worker_wakeup.set()
        return next(item for item in build_review_jobs(500) if item.id == strategy_job.id)
    payload = await asyncio.to_thread(store.load_research_campaign_job, job_id)
    if not payload:
        raise HTTPException(404, "review job not found")
    job = ResearchCampaignJob.model_validate(payload)
    if job.status != ResearchCampaignJobStatus.FAILED:
        raise HTTPException(409, "only a failed review job can be retried")
    job.status = ResearchCampaignJobStatus.QUEUED
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.completed_cells = 0
    job.failed_cells = 0
    job.updated_at = datetime.now(UTC)
    await asyncio.to_thread(store.save_research_campaign_job, job)
    schedule_research_campaign_job(job)
    return next(item for item in build_review_jobs() if item.id == job.id)


def sandbox_maturity_snapshot(certificate: SandboxAdmissionCertificateV1) -> dict:
    episodes = [
        item
        for item in agent_store.list_episodes(10_000)
        if item.strategy_certificate_id == certificate.id
    ]
    performance = build_forward_performance(
        episodes,
        configuration_service.current_configuration().agent_forward_validation,
        scope_mode=AgentAutonomyMode.SANDBOX,
    )
    execution_payloads = store.list_sandbox_execution_evidence(certificate.id)
    execution = [
        SandboxExecutionRoundEvidenceV1.model_validate(item) for item in execution_payloads
    ]
    attributable_ids = {
        item.id
        for item in episodes
        if item.status == TradeEpisodeStatus.CLOSED
        and item.realized_pnl is not None
        and item.return_on_risk is not None
    }
    aligned_ids = {item.episode_id for item in execution if item.aligned}
    aligned = bool(attributable_ids) and attributable_ids.issubset(aligned_ids)
    profit_factor = performance.profit_factor
    if profit_factor is None and performance.wins > 0 and performance.losses == 0:
        profit_factor = Decimal("999.99")
    maturity = SandboxMaturityEvidenceV1(
        completed_episodes=performance.attributable_episodes,
        independent_runs=performance.independent_runs,
        recent_episodes=performance.recent_episodes,
        profit_factor=profit_factor or Decimal("0"),
        expected_r=performance.expectancy_r,
        maximum_drawdown_r=performance.max_drawdown_r,
        maximum_consecutive_losses=performance.max_consecutive_losses,
        execution_cost_aligned=aligned,
        slippage_aligned=aligned,
        native_protection_aligned=aligned,
    )
    return {
        "maturity": maturity,
        "performance": performance,
        "execution_evidence_count": len(execution),
        "aligned_episode_count": len(attributable_ids & aligned_ids),
    }


def maybe_issue_full_strategy_certificate_v2(
    certificate_id: str,
) -> FullStrategyCertificateV2 | None:
    status = store.load_research_certificate_status(certificate_id)
    if not status or status["certificate_type"] != "sandbox_admission_v1" or status["revoked"]:
        return None
    admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
    try:
        _development_audit_for_admission(admission)
    except RuntimeError:
        return None
    existing = [
        item
        for item in store.list_full_strategy_certificates_v2()
        if item["certificate"].get("sandbox_admission_certificate_id") == admission.id
        and not item["revoked"]
    ]
    if existing:
        return FullStrategyCertificateV2.model_validate(existing[0]["certificate"])
    blind = store.find_completed_blind_consumption(admission.strategy_ir_hash)
    if blind is None:
        blind = store.find_completed_blind_consumption(admission.artifact_fingerprint)
    if blind and not bool(blind.get("passed")):
        store.revoke_research_certificate(
            CertificateRevocationEvent(
                certificate_id=admission.id,
                reason_code="sealed_blind_test_failed",
                evidence_reference=str(blind.get("evaluation_id") or "sealed_blind"),
            )
        )
        return None
    if not blind:
        return None
    snapshot = sandbox_maturity_snapshot(admission)
    maturity = snapshot["maturity"]
    if not maturity.passed:
        return None
    configuration = configuration_service.current_configuration()
    certificate = FullStrategyCertificateV2(
        sandbox_admission_certificate_id=admission.id,
        strategy_ir_hash=admission.strategy_ir_hash,
        artifact_fingerprint=admission.artifact_fingerprint,
        sealed_blind_evidence_reference=str(blind["evaluation_id"]),
        sandbox_maturity=maturity,
        configuration_fingerprint=strategy_certificate_configuration_fingerprint(configuration),
    )
    store.save_research_certificate(certificate, "full_strategy_v2")
    return certificate


@app.post("/api/research/development-audits")
async def issue_development_audit(request: DevelopmentAuditIssueRequest):
    """Derive admission metrics from immutable server-side campaign cells."""

    datasets = await asyncio.to_thread(store.list_research_datasets, 2_000)
    contract = assess_crypto_5m_contract(datasets)
    if contract.status != "READY":
        raise HTTPException(409, "crypto_5m_v1 is not READY")
    artifacts: list[AlphaModelArtifactV1] = []
    for model_id in request.model_artifact_ids:
        payload = await asyncio.to_thread(store.load_alpha_model_artifact, model_id)
        if not payload:
            raise HTTPException(409, f"model artifact not found: {model_id}")
        artifacts.append(AlphaModelArtifactV1.model_validate(payload))
    ensemble = next(
        (
            item
            for item in artifacts
            if item.id == request.ensemble_model_id and item.model_family == "ensemble_50_50"
        ),
        None,
    )
    families = {item.model_family for item in artifacts}
    if ensemble is None or not (
        families & {"elastic_net_regression", "logistic_classification"}
        and families & {"lightgbm_regression", "lightgbm_classification"}
    ):
        raise HTTPException(409, "a bound linear, LightGBM, and ensemble trio is required")
    if any(
        item.ir_fingerprint != request.strategy_ir_hash or not item.walk_forward_passed
        for item in artifacts
    ):
        raise HTTPException(409, "model trio IR or walk-forward evidence is invalid")
    report_payload = await asyncio.to_thread(store.load_research_campaign, request.campaign_id)
    if report_payload is None:
        raise HTTPException(409, "persisted development campaign not found")
    report = ResearchCampaignReport.model_validate(report_payload)
    candidate = next(
        (
            item
            for item in report.candidates
            if item.strategy_fingerprint == request.strategy_ir_hash
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(409, "campaign does not contain the frozen strategy IR")
    cells = [
        item
        for item in report.cells
        if item.dataset_role == "research"
        and item.strategy_fingerprint == request.strategy_ir_hash
        and item.timeframe == "5m"
    ]
    by_market: dict[str, ResearchCampaignCell] = {}
    for cell in cells:
        market = f"{cell.exchange.lower()}:{cell.symbol.upper()}"
        if market in by_market:
            raise HTTPException(409, f"duplicate development evaluation for {market}")
        by_market[market] = cell
    if set(by_market) != set(ALPHA_MARKETS):
        raise HTTPException(409, "campaign must evaluate the exact six crypto_5m_v1 markets")
    selected_dataset_by_market = {
        item.market: item.dataset_id for item in contract.markets if item.dataset_id
    }
    if any(
        by_market[market].dataset_id != selected_dataset_by_market.get(market)
        for market in ALPHA_MARKETS
    ):
        raise HTTPException(409, "campaign cells do not bind the current frozen data contract")
    passing = tuple(
        market
        for market in ALPHA_MARKETS
        if (
            by_market[market].data_eligible
            and by_market[market].robustness_passed
            and by_market[market].statistical_uncertainty_passed
            and by_market[market].temporal_stability_passed
            and by_market[market].expected_r_passed
            and by_market[market].profit_factor >= Decimal("1.15")
            and (by_market[market].stress_return_percent or Decimal("0")) > 0
            and by_market[market].max_drawdown_percent <= Decimal("20")
        )
    )
    passing_cells = [by_market[market] for market in passing]
    evidence = DevelopmentAuditEvidenceV1(
        expected_r_bootstrap_lower=(
            min(item.expected_r_confidence_lower_bound for item in passing_cells)
            if passing_cells
            else Decimal("0")
        ),
        profit_factor=(
            min(item.profit_factor for item in passing_cells) if passing_cells else Decimal("0")
        ),
        stress_net_return=(
            min(item.stress_return_percent or Decimal("0") for item in passing_cells)
            if passing_cells
            else Decimal("0")
        ),
        maximum_drawdown_fraction=(
            max(item.max_drawdown_percent for item in by_market.values()) / Decimal("100")
        ),
        pbo_95_upper=report.pbo.upper_confidence_bound,
        online_fdr_passed=candidate.online_fdr_passed,
        passing_markets=passing,
    )
    if not candidate.cross_market_passed or not report.pbo.passed or not evidence.passed:
        raise HTTPException(409, "persisted campaign does not pass development admission")
    certificate = DevelopmentAuditCertificateV1(
        strategy_ir_hash=request.strategy_ir_hash,
        artifact_fingerprint=ensemble.fingerprint,
        data_contract_fingerprint=contract.fingerprint,
        model_artifact_ids=tuple(item.id for item in artifacts),
        campaign_id=report.id,
        campaign_fingerprint=stable_fingerprint(report.model_dump(mode="json")),
        market_evaluation_fingerprints={
            market: by_market[market].backtest_fingerprint for market in ALPHA_MARKETS
        },
        evidence=evidence,
    )
    await asyncio.to_thread(
        store.save_research_certificate,
        certificate,
        "development_audit_v1",
    )
    return certificate.model_dump(mode="json")


@app.get("/api/research/development-audits")
async def list_development_audits():
    return await asyncio.to_thread(
        store.list_research_certificates_by_type,
        "development_audit_v1",
    )


@app.post("/api/research/sandbox-admissions")
async def issue_sandbox_admission(request: SandboxAdmissionIssueRequest):
    datasets = await asyncio.to_thread(store.list_research_datasets, 2_000)
    contract = assess_crypto_5m_contract(datasets)
    if contract.status != "READY":
        raise HTTPException(
            409,
            {
                "code": "crypto_5m_v1_not_ready",
                "status": contract.status,
                "markets": [item.model_dump(mode="json") for item in contract.markets],
            },
        )
    artifacts: list[AlphaModelArtifactV1] = []
    for model_id in request.model_artifact_ids:
        payload = await asyncio.to_thread(store.load_alpha_model_artifact, model_id)
        if not payload:
            raise HTTPException(409, f"model artifact not found: {model_id}")
        artifacts.append(AlphaModelArtifactV1.model_validate(payload))
    families = {item.model_family for item in artifacts}
    required_family_groups = (
        {"elastic_net_regression", "logistic_classification"},
        {"lightgbm_regression", "lightgbm_classification"},
        {"ensemble_50_50"},
    )
    if not all(families & group for group in required_family_groups):
        raise HTTPException(409, "linear, LightGBM, and 50/50 ensemble artifacts are required")
    if not all(item.walk_forward_passed for item in artifacts):
        raise HTTPException(409, "linear and LightGBM models must independently pass walk-forward")
    model_tasks = {item.task for item in artifacts if item.task != "ensemble"}
    if len(model_tasks) != 1:
        raise HTTPException(409, "linear and LightGBM model tasks differ")
    artifact_lineages = {
        (
            item.profile,
            item.horizon_bars,
            item.feature_names,
            item.feature_cube_fingerprint,
            item.label_fingerprint,
            item.training_window,
            item.validation_windows,
            item.purge_bars,
            item.embargo_bars,
            item.random_seed,
            item.ir_fingerprint,
            item.cost_model_fingerprint,
        )
        for item in artifacts
    }
    if len(artifact_lineages) != 1:
        raise HTTPException(409, "model trio does not share one frozen training lineage")
    installed_dependencies = dependency_versions()
    if any(
        any(
            installed_dependencies.get(name) != value
            for name, value in item.dependency_versions.items()
        )
        for item in artifacts
    ):
        raise HTTPException(409, "model artifact dependency version drift detected")
    ensemble = next(
        (
            item
            for item in artifacts
            if item.id == request.ensemble_model_id and item.model_family == "ensemble_50_50"
        ),
        None,
    )
    if ensemble is None:
        raise HTTPException(409, "ensemble_model_id is not the supplied ensemble artifact")
    if any(item.ir_fingerprint != request.strategy_ir_hash for item in artifacts):
        raise HTTPException(409, "model artifact and strategy IR fingerprints differ")
    if any(item.canonical_ir_payload is None for item in artifacts):
        raise HTTPException(409, "model artifacts must embed the frozen Canonical IR")
    configuration = configuration_service.current_configuration()
    expected_cost_fingerprint = alpha_transaction_cost_fingerprint(
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
    )
    if {item.cost_model_fingerprint for item in artifacts} != {expected_cost_fingerprint}:
        raise HTTPException(409, "model artifacts do not match the current frozen cost model")
    required_data_features = tuple(
        sorted({feature for item in artifacts for feature in item.required_data_features})
    )
    contract = assess_crypto_5m_contract(
        datasets,
        required_features=required_data_features,
    )
    if contract.status != "READY":
        raise HTTPException(
            409,
            {
                "code": "model_required_features_untestable",
                "required_features": required_data_features,
                "status": contract.status,
                "markets": [item.model_dump(mode="json") for item in contract.markets],
            },
        )
    audit_status = await asyncio.to_thread(
        store.load_research_certificate_status,
        request.development_audit_certificate_id,
    )
    if (
        audit_status is None
        or audit_status["certificate_type"] != "development_audit_v1"
        or audit_status["revoked"]
    ):
        raise HTTPException(409, "valid server-issued development audit is required")
    development_audit = DevelopmentAuditCertificateV1.model_validate(audit_status["certificate"])
    if (
        development_audit.strategy_ir_hash != request.strategy_ir_hash
        or development_audit.artifact_fingerprint != ensemble.fingerprint
        or development_audit.data_contract_fingerprint != contract.fingerprint
        or set(development_audit.model_artifact_ids) != {item.id for item in artifacts}
    ):
        raise HTTPException(409, "development audit does not bind this frozen model lineage")
    try:
        certificate = SandboxAdmissionCertificateV1(
            strategy_ir_hash=request.strategy_ir_hash,
            artifact_fingerprint=ensemble.fingerprint,
            data_contract_fingerprint=contract.fingerprint,
            model_artifact_ids=tuple(item.id for item in artifacts),
            development_audit_certificate_id=development_audit.id,
            audit_evidence=development_audit.evidence,
            strategy_frozen=True,
        )
    except ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(
        store.save_research_certificate,
        certificate,
        "sandbox_admission_v1",
    )
    handoff_status = (
        "admitted_waiting_current_signal"
        if configuration.exchange_id == "okx" and configuration.trading_mode == TradingMode.SANDBOX
        else "queued_until_okx_sandbox"
    )
    handoff = await asyncio.to_thread(
        store.save_sandbox_admission_handoff,
        certificate,
        status=handoff_status,
    )
    if handoff_status == "admitted_waiting_current_signal":
        await process_sandbox_alpha_handoffs()
        handoff = (
            await asyncio.to_thread(
                store.load_sandbox_admission_handoff,
                certificate.id,
            )
            or handoff
        )
    add_audit(
        "research",
        "crypto_5m_v1 开发审核通过并自动进入 OKX 模拟盘准入队列",
        severity="warning",
        certificate_id=certificate.id,
        handoff_status=handoff_status,
    )
    return {"certificate": certificate.model_dump(mode="json"), "handoff": handoff}


@app.get("/api/research/sandbox-admissions")
async def list_sandbox_admissions():
    records = await asyncio.to_thread(
        store.list_research_certificates_by_type,
        "sandbox_admission_v1",
    )
    result = []
    for record in records:
        certificate = SandboxAdmissionCertificateV1.model_validate(record["certificate"])
        audit_valid = True
        try:
            await asyncio.to_thread(_development_audit_for_admission, certificate)
        except RuntimeError:
            audit_valid = False
        result.append(
            {
                **record,
                "revoked": bool(record["revoked"] or not audit_valid),
                "handoff": await asyncio.to_thread(
                    store.load_sandbox_admission_handoff,
                    certificate.id,
                ),
                "progress": await asyncio.to_thread(
                    sandbox_maturity_snapshot,
                    certificate,
                ),
            }
        )
    return result


@app.post("/api/research/sandbox-admissions/{certificate_id}/finalize")
async def finalize_full_strategy_certificate(certificate_id: str):
    certificate = await asyncio.to_thread(
        maybe_issue_full_strategy_certificate_v2,
        certificate_id,
    )
    if certificate is None:
        raise HTTPException(
            409,
            "sealed blind PASS and all 30-episode OKX sandbox maturity gates are required",
        )
    return certificate.model_dump(mode="json")


@app.get("/api/research/data-contracts/crypto-5m-v1")
async def get_crypto_5m_data_contract():
    datasets = await asyncio.to_thread(store.list_research_datasets, 2_000)
    contract = assess_crypto_5m_contract(datasets)
    return {**contract.model_dump(mode="json"), "fingerprint": contract.fingerprint}


@app.get("/api/research/models/{model_id}")
async def get_alpha_model_artifact(model_id: str):
    payload = await asyncio.to_thread(store.load_alpha_model_artifact, model_id)
    if not payload:
        raise HTTPException(404, "alpha model artifact not found")
    return payload


@app.put("/api/settings", response_model=ConfigurationView)
async def update_settings(update: ConfigurationUpdate):
    # Settings cannot change account identity while an entry is being allocated.
    async with v2_entry_allocation_lock:
        return await _update_settings(update)


async def _update_settings(update: ConfigurationUpdate):
    global v2_configuration_generation
    previous = configuration_service.current_configuration()
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
    connection_change = sensitive_change or any(
        getattr(previous, field) != getattr(update.configuration, field)
        for field in ("network_proxy_url", "margin_mode", "contract_leverage")
    )
    if connection_change and (
        trading_intelligence_store.automation_runtime().enabled
        or trading_intelligence_store.list_positions()
        or trading_intelligence_store.list_entry_orders(active_only=True)
    ):
        raise HTTPException(
            409,
            "请先停止自动化并收口 v2 持仓及挂单，再修改账户、凭证或连接配置",
        )
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
        if portfolio.open_positions:
            raise HTTPException(409, "旧执行环境仍有持仓，请先平仓后再切换")
    try:
        view = configuration_service.save(update)
        await runtime.initialize(force=True)
        v2_configuration_generation += 1
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
    next_strategy_configuration_fingerprint = strategy_certificate_configuration_fingerprint(
        view.configuration
    )
    for item in await asyncio.to_thread(store.list_full_strategy_certificates_v2):
        certificate = item["certificate"]
        if (
            not item["revoked"]
            and certificate.get("configuration_fingerprint")
            != next_strategy_configuration_fingerprint
        ):
            event = CertificateRevocationEvent(
                certificate_id=certificate["id"],
                reason_code="configuration_or_cost_model_changed",
                evidence_reference=next_strategy_configuration_fingerprint,
            )
            await asyncio.to_thread(store.revoke_research_certificate, event)
    next_cost_fingerprint = alpha_transaction_cost_fingerprint(
        fee_bps=view.configuration.fee_bps,
        slippage_bps=view.configuration.slippage_bps,
    )
    sandbox_certificates = await asyncio.to_thread(
        store.list_research_certificates_by_type,
        "sandbox_admission_v1",
    )
    for item in sandbox_certificates:
        if item["revoked"]:
            continue
        admission = SandboxAdmissionCertificateV1.model_validate(item["certificate"])
        cost_fingerprints = set()
        for model_id in admission.model_artifact_ids:
            payload = await asyncio.to_thread(store.load_alpha_model_artifact, model_id)
            if payload:
                cost_fingerprints.add(
                    AlphaModelArtifactV1.model_validate(payload).cost_model_fingerprint
                )
        if cost_fingerprints != {next_cost_fingerprint}:
            await asyncio.to_thread(
                store.revoke_research_certificate,
                CertificateRevocationEvent(
                    certificate_id=admission.id,
                    reason_code="configuration_or_cost_model_changed",
                    evidence_reference=next_cost_fingerprint,
                ),
            )
    add_audit(
        "settings",
        "运行设置已更新",
        exchange=view.configuration.exchange_id,
        mode=str(view.configuration.trading_mode),
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
    decision = RiskDecision(
        approved=False,
        checks=[
            RiskCheck(
                code="trading_intelligence_v2_boundary",
                passed=False,
                message="仅在 v2 WakeCondition 触发后为对应 TradePlan 执行预交易风控",
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
    latency_ms = max(round((datetime.now(UTC) - market.timestamp).total_seconds() * 1000), 0)
    quality = runtime.market_quality
    return {
        "market": market,
        "candles": history[-CHART_CANDLE_LIMIT:],
        "portfolio": portfolio,
        "risk": decision,
        "orders": recent_orders,
        "audit": audit[:12],
        "system": {
            "mode": configuration.trading_mode,
            "market_type": configuration.market_type,
            "margin_mode": configuration.margin_mode,
            "contract_leverage": configuration.contract_leverage,
            "data_latency_ms": latency_ms,
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

    if not local_access.trusted_origin(
        websocket.headers.get("origin"),
        str(websocket.url),
    ):
        await websocket.close(code=1008, reason="untrusted origin")
        return
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
    if request.research_profile == "crypto_5m_v1":
        ir = compile_strategy_dsl(request.dsl)
        artifacts: list[AlphaModelArtifactV1] = []
        for model_id in request.model_artifact_ids:
            payload = await asyncio.to_thread(store.load_alpha_model_artifact, model_id)
            if payload is None:
                raise HTTPException(409, f"alpha model artifact not found: {model_id}")
            artifacts.append(AlphaModelArtifactV1.model_validate(payload))
        families = {item.model_family for item in artifacts}
        if not (
            families & {"elastic_net_regression", "logistic_classification"}
            and families & {"lightgbm_regression", "lightgbm_classification"}
            and "ensemble_50_50" in families
        ):
            raise HTTPException(409, "crypto_5m_v1 requires one native model trio")
        lineage = {
            (
                item.profile,
                item.horizon_bars,
                item.feature_names,
                item.ir_fingerprint,
                item.cost_model_fingerprint,
            )
            for item in artifacts
        }
        if len(lineage) != 1 or any(item.ir_fingerprint != ir.fingerprint for item in artifacts):
            raise HTTPException(409, "autonomous run DSL and frozen model lineage differ")
        if any(not item.walk_forward_passed for item in artifacts):
            raise HTTPException(
                409,
                "autonomous run models did not independently pass walk-forward",
            )
        expected_cost = alpha_transaction_cost_fingerprint(
            fee_bps=configuration_service.current_configuration().fee_bps,
            slippage_bps=configuration_service.current_configuration().slippage_bps,
        )
        if {item.cost_model_fingerprint for item in artifacts} != {expected_cost}:
            raise HTTPException(409, "autonomous run model cost lineage changed")
        if any(item.dependency_versions != dependency_versions() for item in artifacts):
            raise HTTPException(409, "autonomous run model dependency versions drifted")
        required = tuple(
            sorted({feature for item in artifacts for feature in item.required_data_features})
        )
        datasets = await asyncio.to_thread(store.list_research_datasets, 2_000)
        contract = assess_crypto_5m_contract(datasets, required_features=required)
        selected_fingerprints = {
            item.get("data_fingerprint")
            for item in datasets
            if item.get("id") in {market.dataset_id for market in contract.markets}
        }
        if (
            contract.status != "READY"
            or request.snapshot.dataset_fingerprint not in selected_fingerprints
        ):
            raise HTTPException(409, "autonomous run snapshot is outside READY crypto_5m_v1")
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


def ai_market_summary(
    market,
    history,
    symbol: str,
    timeframe: str,
    *,
    maximum_bars: int = 2000,
) -> dict:
    candles = closed_candles(history, timeframe, market.timestamp)[-maximum_bars:]
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
    current_atr = atr(candles, 14)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(candles),
        "data_start": candles[0].timestamp.isoformat(),
        "data_end": candles[-1].timestamp.isoformat(),
        "market_timestamp": market.timestamp.isoformat(),
        "last_price": str(market.last),
        "spread_bps": str(market.spread_bps),
        "atr_14": str(current_atr),
        "atr_percent": str(current_atr / market.last * 100 if market.last else Decimal("0")),
        "funding_rate": str(market.funding_rate) if market.funding_rate is not None else None,
        "return_20_bars": str(closes[-1] / closes[-20] - Decimal("1")),
        "return_100_bars": str(closes[-1] / closes[-100] - Decimal("1")),
        "mean_absolute_return": str(average_abs_deviation),
        "recent_high": str(max(item.high for item in candles[-50:])),
        "recent_low": str(min(item.low for item in candles[-50:])),
        "volume_ratio_20_to_100": str(
            recent_volume / baseline_volume if baseline_volume else Decimal("0")
        ),
        "recent_average_volume": str(recent_volume),
        "baseline_average_volume": str(baseline_volume),
        "deterministic_regime": str(selected_kind),
        "closed_bars_only": True,
        "coverage_ratio": str(
            (Decimal(len(candles)) / Decimal(maximum_bars)).quantize(Decimal("0.0001"))
        ),
    }


def strategy_required_features(raw_spec: dict) -> list[str]:
    parameters = raw_spec.get("parameters") if isinstance(raw_spec.get("parameters"), dict) else {}
    entry_model = str(parameters.get("entry_model") or "")
    required = {"ohlcv"}
    if (
        parameters.get("funding_mode") not in {None, "ignore"}
        or entry_model == "funding_contrarian"
    ):
        required.add("funding_rate")
    if parameters.get("open_interest_mode") not in {None, "ignore"}:
        required.add("open_interest")
    if entry_model == "positioning_contrarian":
        required.add("positioning")
    if parameters.get("basis_mode") not in {None, "ignore"} or entry_model == "basis_contrarian":
        required.add("basis")
    if parameters.get("order_flow_mode") not in {None, "ignore"}:
        required.add("order_flow")
    if parameters.get("liquidation_flow_mode") not in {None, "ignore"}:
        required.add("liquidation_flow")
    return sorted(required)


def review_policy_fingerprint(configuration: UserConfiguration) -> str:
    return stable_fingerprint(
        {
            "validation": configuration.strategy.validation.model_dump(mode="json"),
            "fee_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "risk_per_trade": str(configuration.risk.risk_per_trade),
            "max_gross_exposure": str(configuration.risk.max_gross_exposure),
            "max_leverage": str(configuration.risk.max_leverage),
            "contract_leverage": str(configuration.contract_leverage),
        }
    )


def recent_candidate_failure_digest(
    *,
    symbol: str,
    timeframe: str,
    family: StrategyKind | None,
    limit: int = 10,
) -> tuple[list[dict], list[str]]:
    digest: list[dict] = []
    rejected_fingerprints: list[str] = []
    for payload in store.list_strategy_candidates(500):
        context = payload.get("research_context")
        if not isinstance(context, dict):
            continue
        if context.get("symbol") != symbol or context.get("timeframe") != timeframe:
            continue
        spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
        if family is not None and spec.get("family") != family.value:
            continue
        if str(payload.get("status") or "") != str(CandidateStatus.REJECTED):
            continue
        fingerprint = str(payload.get("spec_fingerprint") or "")
        backtest = payload.get("backtest") if isinstance(payload.get("backtest"), dict) else {}
        reasons = list(backtest.get("validation_reasons") or payload.get("decision_reasons") or [])
        digest.append(
            {
                "fingerprint": fingerprint,
                "family": spec.get("family"),
                "reasons": reasons[-5:],
                "cost_r": (backtest.get("cost_model") or {}).get("round_trip_cost_r"),
            }
        )
        if fingerprint:
            rejected_fingerprints.append(fingerprint)
        if len(digest) >= limit:
            break
    return digest, rejected_fingerprints


def candidate_trial_count(*, trial_group: str) -> int:
    return current_candidate_trial_count(trial_group=trial_group) + 1


def current_candidate_trial_count(*, trial_group: str) -> int:
    count = 0
    for payload in store.list_strategy_candidates(500):
        context = payload.get("research_context")
        if isinstance(context, dict) and context.get("trial_group") == trial_group:
            count += 1
    return count


def build_candidate_research_context(
    spec,
    *,
    symbol: str,
    timeframe: str,
    market_as_of: datetime,
    summary_material: dict,
    entry_profile: str = DEFAULT_ENTRY_PROFILE_ID,
) -> CandidateResearchContext:
    configuration = configuration_service.current_configuration()
    spec_payload = spec.model_dump(mode="json", exclude={"fingerprint"})
    trial_group = stable_fingerprint(
        {
            "exchange": configuration.exchange_id,
            "market_type": str(configuration.market_type),
            "symbol": symbol,
            "timeframe": timeframe,
            "family": spec_payload["family"],
            "contract": "strategy_generation_v2",
        }
    )
    return CandidateResearchContext(
        exchange=configuration.exchange_id,
        market_type=str(configuration.market_type),
        symbol=symbol,
        timeframe=timeframe,
        entry_profile=entry_profile,
        market_as_of=market_as_of,
        summary_fingerprint=stable_fingerprint(summary_material),
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
        risk_fraction=configuration.risk.risk_per_trade,
        max_gross_exposure=configuration.risk.max_gross_exposure,
        max_leverage=configuration.risk.max_leverage,
        required_features=strategy_required_features(spec_payload),
        trial_group=trial_group,
        trial_count=candidate_trial_count(trial_group=trial_group),
    )


def build_candidate_preflight(
    spec,
    *,
    origin_summary: dict,
    configuration: UserConfiguration,
) -> dict[str, str | int | float | bool | list[str]]:
    parameters = spec.parameters
    last_price = Decimal(str(origin_summary["last_price"]))
    current_atr = Decimal(str(origin_summary["atr_14"]))
    stop_atr = Decimal(str(getattr(parameters, "stop_atr", Decimal("1.5"))))
    stop_distance = current_atr * stop_atr
    risk_amount = configuration.paper_equity * configuration.risk.risk_per_trade
    intended_quantity = risk_amount / stop_distance if stop_distance > 0 else Decimal("0")
    intended_notional = intended_quantity * last_price
    gross_exposure_limit = configuration.paper_equity * configuration.risk.max_gross_exposure
    leverage_limit = configuration.paper_equity * min(
        configuration.risk.max_leverage,
        configuration.contract_leverage,
    )
    recent_volume = Decimal(str(origin_summary.get("recent_average_volume") or "0"))
    volume_limit = (
        recent_volume * Decimal("0.02") * last_price if recent_volume > 0 else intended_notional
    )
    capped_notional = min(
        intended_notional,
        gross_exposure_limit,
        leverage_limit,
        volume_limit,
    )
    capped_quantity = capped_notional / last_price if last_price > 0 else Decimal("0")
    estimated_participation = (
        min(capped_quantity / recent_volume, Decimal("0.02"))
        if recent_volume > 0
        else Decimal("0.02")
    )
    estimated_impact_bps = Decimal(str(math.sqrt(float(estimated_participation)))) * Decimal("5")
    estimated_round_trip_bps = (
        configuration.fee_bps + configuration.slippage_bps + estimated_impact_bps
    ) * 2
    estimated_cost = capped_notional * estimated_round_trip_bps / Decimal("10000")
    capped_stop_risk = capped_quantity * stop_distance
    cost_r = estimated_cost / capped_stop_risk if capped_stop_risk > 0 else Decimal("999")
    maximum_cost_r = configuration.strategy.validation.maximum_estimated_round_trip_cost_r
    reasons: list[str] = []
    if cost_r > maximum_cost_r:
        reasons.append(f"预计往返成本 {cost_r:.4f}R 超过设计上限 {maximum_cost_r:.4f}R")
    position_limits: list[str] = []
    if capped_notional < intended_notional:
        if capped_notional == gross_exposure_limit:
            position_limits.append("gross_exposure")
        if capped_notional == leverage_limit:
            position_limits.append("leverage")
        if capped_notional == volume_limit:
            position_limits.append("volume_participation")
        reasons.append(f"风险仓位已受 {'+'.join(position_limits)} 上限约束")
    return {
        "eligible": cost_r <= maximum_cost_r,
        "estimated_round_trip_cost_r": str(cost_r.quantize(Decimal("0.0001"))),
        "maximum_round_trip_cost_r": str(maximum_cost_r),
        "intended_notional": str(intended_notional.quantize(Decimal("0.01"))),
        "capped_notional": str(capped_notional.quantize(Decimal("0.01"))),
        "intended_quantity": str(intended_quantity.quantize(Decimal("0.00000001"))),
        "actual_quantity": str(capped_quantity.quantize(Decimal("0.00000001"))),
        "position_limit_reason": "+".join(position_limits) or "stop_risk",
        "estimated_market_impact_bps_per_side": str(
            estimated_impact_bps.quantize(Decimal("0.0001"))
        ),
        "required_features": strategy_required_features(
            spec.model_dump(mode="json", exclude={"fingerprint"})
        ),
        "reasons": reasons,
    }


async def build_ai_generation_summary(
    *,
    symbol: str,
    timeframe: str,
    origin_market,
    origin_history,
) -> dict:
    profile = get_entry_profile(DEFAULT_ENTRY_PROFILE_ID)
    requested = list(
        dict.fromkeys(
            (
                timeframe,
                profile.local_context_timeframe,
                profile.direction_context_timeframe,
                profile.macro_context_timeframe,
            )
        )
    )
    summaries: dict[str, dict] = {}
    for item in requested:
        try:
            if item == timeframe:
                market, history = origin_market, origin_history
            else:
                market, history = await runtime.market_context(
                    limit=2000,
                    symbol=symbol,
                    timeframe=item,
                )
            summaries[item] = ai_market_summary(market, history, symbol, item)
        except (LookupError, ValueError) as exc:
            summaries[item] = {"status": "unavailable", "reason": str(exc)[:300]}
    return {"origin_timeframe": timeframe, "timeframes": summaries}


def unified_research_strategy_library(family: str | None = None) -> dict:
    """Return public strategy priors as offline Research inputs only."""

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
            "scope": "single_leg_research_only",
            "execution_eligible": False,
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
        "version": 4,
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
        "execution_eligible": False,
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
        items = [
            item
            for item in items
            if not item.hidden and item.status not in {"archived", "retired"}
        ]
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
            limit=2000,
            symbol=symbol,
            timeframe=timeframe,
        )
        summary = await build_ai_generation_summary(
            symbol=symbol,
            timeframe=timeframe,
            origin_market=market,
            origin_history=history,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    failure_digest, rejected_fingerprints = await asyncio.to_thread(
        recent_candidate_failure_digest,
        symbol=symbol,
        timeframe=timeframe,
        family=request.family,
    )

    ai_gateway.service = ai_configuration_service
    ai_gateway.proxy_url = configuration.network_proxy_url
    ai_view = ai_configuration_service.get()
    system_prompt = (
        "You are a cost-aware quantitative research assistant. Propose exactly one "
        "low-turnover parameterized strategy candidate. Never propose order execution, "
        "leverage changes, credentials, or guaranteed returns. Use only the supplied causal "
        "closed-bar summaries and failure digest. Prefer parameters whose ATR stop distance "
        "can absorb the supplied round-trip costs. Do not repeat a rejected fingerprint. "
        "The output is only a hypothesis and must pass deterministic validation."
    )
    requested_family = (
        request.family.value if request.family else "select the best supported family"
    )
    user_prompt = (
        f"Operator hypothesis: {request.hypothesis}\n"
        f"Requested family: {requested_family}\n"
        f"Causal multi-timeframe summary: "
        f"{json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
        "Execution economics: "
        f"fee_bps={configuration.fee_bps}, slippage_bps={configuration.slippage_bps}, "
        f"risk_fraction={configuration.risk.risk_per_trade}, "
        f"max_gross_exposure={configuration.risk.max_gross_exposure}, "
        f"max_leverage={configuration.risk.max_leverage}, "
        "market_impact=nonlinear_volume_participation_model_with_2_percent_cap, "
        "maximum_round_trip_cost_r="
        f"{configuration.strategy.validation.maximum_estimated_round_trip_cost_r}.\n"
        f"Recent development failures: "
        f"{json.dumps(failure_digest, ensure_ascii=False, sort_keys=True)}\n"
        f"Rejected fingerprints: {json.dumps(rejected_fingerprints, ensure_ascii=False)}\n"
        "Return exactly one StrategySpec object. Set source to ai and explain the hypothesis "
        "briefly. Keep every parameter inside the schema limits. Favor lower turnover and "
        "wider economically feasible stops over cosmetic parameter variation."
    )
    mode = ai_view.configuration.routing_mode
    providers: list[AIProvider | None]
    if request.provider:
        providers = [request.provider]
        mode = "manual"
    else:
        # Research generation is the only automatic-AI boundary. One market
        # event produces one structured proposal; deterministic review owns
        # validation and comparison, so a second model call adds cost without
        # adding execution authority.
        providers = [None]
        mode = "single_efficient"

    proposals: list[AIProposalItem] = []
    try:
        for provider in providers:
            completion = None
            attempted: list[AIProvider] = []
            spec = None
            last_validation_error: ValueError | None = None
            repair_payload: dict = {}
            repair_error = "provider response was not valid JSON"
            for attempt in range(2):
                try:
                    completion, attempted = await ai_gateway.generate_json(
                        system=system_prompt,
                        prompt=(
                            user_prompt
                            if attempt == 0
                            else (
                                "Repair the previous invalid strategy response. Return one "
                                "compact StrategySpec JSON object only, with every required "
                                f"field inside its bound. Validation error: {repair_error}. "
                                "Previous response: "
                                f"{json.dumps(repair_payload, ensure_ascii=False, sort_keys=True)}"
                            )
                        ),
                        schema=strategy_spec_json_schema(),
                        schema_name="strategy_spec",
                        requested_provider=provider,
                        max_tokens=4000 if attempt == 0 else 6000,
                        # Reliability is more important than hidden chain-of-thought for
                        # a schema-constrained hypothesis. Deterministic review owns the
                        # empirical judgment after this isolated proposal is persisted.
                        thinking_enabled=attempt == 0,
                        operation="strategy_candidate_generation",
                        prompt_version="strategy_candidate_v4_cost_aware",
                        schema_version="strategy_spec_v1",
                    )
                    raw_spec = {
                        **completion.data,
                        "source": "ai",
                        "hypothesis": request.hypothesis,
                    }
                    spec = validate_strategy_spec(raw_spec, expected_kind=request.family)
                    break
                except AIProviderError as exc:
                    if attempt == 0 and exc.code == AIErrorCode.INVALID_RESPONSE:
                        repair_error = str(exc)[:500]
                        continue
                    raise
                except ValueError as exc:
                    last_validation_error = exc
                    repair_payload = dict(completion.data) if completion else {}
                    repair_error = str(exc)[:500]
                    if attempt == 0:
                        continue
                    raise
            if completion is None or spec is None:
                raise last_validation_error or ValueError(
                    "AI strategy candidate repair did not produce a valid StrategySpec"
                )
            spec_payload = spec.model_dump(mode="json", exclude={"fingerprint"})
            trial_group = stable_fingerprint(
                {
                    "exchange": configuration.exchange_id,
                    "market_type": str(configuration.market_type),
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "family": spec_payload["family"],
                    "contract": "strategy_generation_v2",
                }
            )
            context = CandidateResearchContext(
                exchange=configuration.exchange_id,
                market_type=str(configuration.market_type),
                symbol=symbol,
                timeframe=timeframe,
                entry_profile=DEFAULT_ENTRY_PROFILE_ID,
                market_as_of=market.timestamp,
                summary_fingerprint=stable_fingerprint(summary),
                fee_bps=configuration.fee_bps,
                slippage_bps=configuration.slippage_bps,
                risk_fraction=configuration.risk.risk_per_trade,
                max_gross_exposure=configuration.risk.max_gross_exposure,
                max_leverage=configuration.risk.max_leverage,
                required_features=strategy_required_features(spec_payload),
                trial_group=trial_group,
                trial_count=await asyncio.to_thread(
                    candidate_trial_count,
                    trial_group=trial_group,
                ),
            )
            preflight = build_candidate_preflight(
                spec,
                origin_summary=summary["timeframes"][timeframe],
                configuration=configuration,
            )
            preflight_reasons = list(preflight.get("reasons") or [])
            if spec.fingerprint in rejected_fingerprints:
                preflight["eligible"] = False
                preflight_reasons.append("策略指纹与同一研究域的历史拒绝候选重复")
                preflight["reasons"] = preflight_reasons
            candidate = create_candidate(spec, research_context=context)
            if not bool(preflight["eligible"]):
                candidate.status = CandidateStatus.REJECTED
                candidate.decision_reasons.extend(preflight_reasons)
            await asyncio.to_thread(store.save_strategy_candidate, candidate)
            if bool(preflight["eligible"]):
                review_worker_wakeup.set()
            proposals.append(
                AIProposalItem(
                    provider=completion.provider,
                    model=completion.model,
                    candidate=candidate,
                    usage=completion.usage,
                    latency_ms=completion.latency_ms,
                    route=attempted,
                    preflight=preflight,
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
        validated = validate_strategy_spec(request.strategy_spec)
        configuration = configuration_service.current_configuration()
        context = await asyncio.to_thread(
            build_candidate_research_context,
            validated,
            symbol=configuration.default_symbol,
            timeframe=configuration.default_timeframe,
            market_as_of=datetime.now(UTC),
            summary_material={
                "source": "operator",
                "spec_fingerprint": validated.fingerprint,
            },
        )
        candidate = create_candidate(
            validated,
            parent_candidate_id=request.parent_candidate_id,
            research_context=context,
        )
    except ValueError as exc:
        raise HTTPException(422, f"candidate strategy spec is invalid: {exc}") from exc
    await asyncio.to_thread(store.save_strategy_candidate, candidate)
    review_worker_wakeup.set()
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
            "研究候选晋升为 Champion；仅作为离线研究证据保留",
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
        validated = validate_strategy_spec(report.candidate_spec)
        context = await asyncio.to_thread(
            build_candidate_research_context,
            validated,
            symbol=configuration.default_symbol,
            timeframe=configuration.default_timeframe,
            market_as_of=market.timestamp,
            summary_material={
                "source": "ml",
                "candle_count": len(candles),
                "data_as_of": market.timestamp.isoformat(),
                "spec_fingerprint": validated.fingerprint,
            },
        )
        candidate = create_candidate(validated, research_context=context)
        await asyncio.to_thread(store.save_strategy_candidate, candidate)
        review_worker_wakeup.set()
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
    signal = await resolve_current_plan_signal(plan, completed, market)
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
        market, _history = await runtime.market_context()
        portfolio: PortfolioState = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"风险检查所需数据不可用：{exc}") from exc
    return risk_engine().evaluate(None, portfolio, market)


@app.get("/api/risk/state", response_model=RiskControlState)
async def get_risk_state():
    return risk_state_service.get()


@app.post("/api/risk/halt", response_model=RiskControlState)
async def halt_trading(request: HaltRequest):
    state = risk_state_service.halt(request.reason)
    # New submissions now fail their generation check. Drain the one already
    # admitted to the exchange boundary before acknowledging the stop.
    async with v2_submission_lock:
        pass
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


class TradingIntelligenceDecisionRequest(BaseModel):
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    trigger: Literal[
        "NO_ACTIVE_THESIS",
        "WAKE_TRIGGERED",
        "PLAN_EXPIRED",
        "PLAN_INVALIDATED",
        "TRADE_COMPLETED",
        "THESIS_INVALIDATED",
        "MAJOR_MARKET_EVENT",
        "BOUNDED_RETRY",
        "ENTRY_ORDER_TERMINAL",
    ]
    trigger_event_id: str | None = None
    mode: TradingModeV2 = TradingModeV2.SHADOW


class TradingIntelligenceExecuteRequest(BaseModel):
    episode_id: str
    wake_event_id: str | None = None
    plan_version: int | None = Field(default=None, ge=1)
    mode: TradingModeV2 = TradingModeV2.SHADOW


class TradingIntelligenceAutomationRequest(BaseModel):
    mode: Literal[TradingModeV2.SHADOW, TradingModeV2.OKX_DEMO] = TradingModeV2.OKX_DEMO
    symbols: list[Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]] = Field(
        default_factory=lambda: list(V2_SYMBOLS), min_length=1
    )


def v2_llm_portfolio_context(
    portfolio: PortfolioState,
    *,
    symbol: str,
    max_daily_drawdown: Decimal,
    risk_halted: bool,
    unresolved_reconciliation: bool,
    managed_positions: list[PositionRuntimeV2] | None = None,
) -> dict:
    """Expose position context to the LLM without leaking sizing/affordability inputs."""

    normalized_symbol = symbol.removesuffix(":USDT")
    positions = [
        {
            "symbol": item.symbol,
            "side": str(item.side),
            "average_entry": str(item.average_entry),
            "mark_price": str(item.mark_price),
            "stop_loss": str(item.stop_loss) if item.stop_loss is not None else None,
            "take_profit": str(item.take_profit) if item.take_profit is not None else None,
        }
        for item in portfolio.open_positions
    ]
    for position in managed_positions or []:
        if position.state not in {PositionStateV2.OPEN, PositionStateV2.PARTIALLY_FILLED}:
            continue
        row = next(
            (item for item in positions
             if item["symbol"].removesuffix(":USDT") == position.symbol.removesuffix(":USDT")
             and item["side"].upper() == position.side),
            None,
        )
        if row is not None:
            row.update({
                "symbol": position.symbol,
                "side": position.side,
                "position_id": position.position_id,
                "position_version": position.version,
                "state": str(position.state),
                "plan_id": position.plan_id,
                "stop_loss": str(position.current_stop),
            })
    symbol_positions = [
        item for item in positions if item["symbol"].removesuffix(":USDT") == normalized_symbol
    ]
    return {
        "decision_symbol": symbol,
        "position_state": "OPEN" if symbol_positions else "FLAT",
        "account_position_state": "OPEN" if positions else "FLAT",
        "symbol_positions": symbol_positions,
        "has_position_for_symbol": any(
            item.symbol.removesuffix(":USDT") == normalized_symbol
            for item in portfolio.open_positions
        ),
        "open_positions": positions,
        "risk_engine_status": "HALTED" if risk_halted else "AVAILABLE",
        "daily_drawdown_limit_reached": (portfolio.daily_drawdown_fraction >= max_daily_drawdown),
        "unresolved_order_or_position": unresolved_reconciliation,
        "sizing_note": (
            "Equity, cash, margin, quantity and leverage are intentionally withheld; "
            "the deterministic Risk Engine evaluates them only after an entry trigger."
        ),
    }


class HumanLiveGrantPreviewRequest(BaseModel):
    symbols: list[Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]] = Field(min_length=1)
    max_risk_per_trade: Decimal = Field(gt=0, le=Decimal("0.03"))
    max_portfolio_risk: Decimal = Field(gt=0, le=Decimal("0.10"))
    max_leverage: Decimal = Field(ge=1, le=Decimal("20"))
    max_daily_drawdown: Decimal = Field(gt=0, le=Decimal("0.10"))
    max_open_positions: int = Field(ge=1, le=20)
    valid_hours: int = Field(default=24, ge=1, le=168)


class HumanLiveGrantIssueRequest(HumanLiveGrantPreviewRequest):
    configuration_fingerprint: str = Field(min_length=64, max_length=64)
    risk_confirmation_one: bool
    risk_confirmation_two: bool


def v2_live_grant_preview_payload(request: HumanLiveGrantPreviewRequest) -> dict:
    view = configuration_service.get()
    configuration = view.configuration
    fingerprint = v2_configuration_fingerprint(configuration)
    blockers: list[str] = ["live_reconciliation_not_implemented"]
    if configuration.exchange_id != "okx":
        blockers.append("exchange_must_be_okx")
    if str(configuration.market_type) != "swap":
        blockers.append("market_type_must_be_swap")
    credentials = view.credential_scopes.get("okx:live")
    if not credentials or not (
        credentials.api_key_configured
        and credentials.secret_configured
        and credentials.passphrase_configured
    ):
        blockers.append("okx_live_credentials_incomplete")
    commissioning = view.commissioning
    if not commissioning or not commissioning.agent_position_management_passed:
        blockers.append("native_protection_and_position_management_commissioning_required")
    report = runtime.reconciliation_report
    if report is None:
        blockers.append("reconciliation_not_available")
    else:
        if report.uncertain_order_ids or report.unknown_open_order_ids:
            blockers.append("uncertain_or_unknown_orders")
        if report.unknown_position_symbols:
            blockers.append("unknown_positions")
        if report.unprotected_order_ids:
            blockers.append("unprotected_orders")
    return {
        "eligible": not blockers,
        "blockers": blockers,
        "configuration_fingerprint": fingerprint,
        "exchange": "okx",
        "symbols": request.symbols,
        "limits": request.model_dump(mode="json"),
        "expires_at": datetime.now(UTC) + timedelta(hours=request.valid_hours),
        "requires_two_confirmations": True,
    }


def durable_official_events(limit: int = 1000) -> list[OfficialEvent]:
    events: dict[str, OfficialEvent] = {}
    for item in trading_intelligence_store.list_events(limit=limit):
        if item["event_type"] != "OFFICIAL_EVENT":
            continue
        try:
            event = OfficialEvent.model_validate(item["payload"]["event"])
        except (KeyError, ValidationError):
            continue
        events[event.event_id] = event
    return sorted(events.values(), key=lambda value: value.scheduled_at)


async def collect_official_events_once() -> tuple[list[OfficialEvent], dict[str, str], int]:
    events, health = await official_event_client.fetch_all()
    persisted = 0
    for event in events:
        event_id, created = await asyncio.to_thread(
            trading_intelligence_store.append_event,
            dedupe_key=f"official-event:{event.source}:{event.source_hash}",
            event_type="OFFICIAL_EVENT",
            aggregate_type="official_event",
            aggregate_id=event.event_id,
            aggregate_version=1,
            payload={"event": event.model_dump(mode="json")},
        )
        persisted += int(created)
        windows = active_risk_windows([event])
        if windows:
            window_event_id, window_created = await asyncio.to_thread(
                trading_intelligence_store.append_event,
                dedupe_key=(
                    f"official-event-window-active:{event.event_id}:"
                    f"{event.scheduled_at.isoformat()}"
                ),
                event_type="OFFICIAL_EVENT_WINDOW_ACTIVE",
                aggregate_type="official_event",
                aggregate_id=event.event_id,
                aggregate_version=1,
                payload={
                    "official_event_id": event_id,
                    "event": event.model_dump(mode="json"),
                    "active_windows": windows,
                },
            )
            if window_created and trading_intelligence_automation is not None:
                await trading_intelligence_automation.on_major_event(window_event_id)
    return events, health, persisted


@app.get("/api/trading-intelligence/official-events")
async def trading_intelligence_official_events():
    events = await asyncio.to_thread(durable_official_events)
    return {
        "events": events,
        "active_risk_windows": active_risk_windows(events),
        "etf_flow": ETF_FLOW_STATUS,
    }


@app.get("/api/trading-intelligence/migration-summary")
async def trading_intelligence_migration_summary():
    def legacy_counts() -> dict[str, int]:
        result: dict[str, int] = {}
        with store._lock, store.connect() as connection:
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in (
                "agent_goals",
                "agent_runs",
                "agent_events",
                "strategies",
                "orders",
            ):
                if table in names:
                    result[table] = int(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                            "count"
                        ]
                    )
        return result

    return {
        "runtime": "trading_intelligence_v2",
        "legacy_strategy_scheduler_running": False,
        "legacy_agent_scheduler_running": False,
        "legacy_mutations": "not_registered_http_404",
        "legacy_runtime_modules_present": False,
        "legacy_evidence": "read_only",
        "historical_record_counts": await asyncio.to_thread(legacy_counts),
    }


@app.get("/api/trading-intelligence/quant-models")
async def trading_intelligence_quant_models():
    result = []
    for symbol in V2_SYMBOLS:
        try:
            artifact = await asyncio.to_thread(load_quant_v1_artifact, symbol)
            eligible = bool(artifact and quant_model_is_eligible(artifact))
            result.append(
                {
                    "symbol": symbol,
                    "timeframe": "5m",
                    "health": (
                        "healthy" if eligible else ("degraded" if artifact else "unavailable")
                    ),
                    "entry_eligible": eligible,
                    "reason": (
                        None
                        if eligible or not artifact
                        else "purged OOF metrics do not beat the uniform 3-class baseline"
                    ),
                    "model_fingerprint": artifact.model_fingerprint if artifact else None,
                    "data_as_of": artifact.data_as_of if artifact else None,
                    "validation": artifact.validation if artifact else None,
                }
            )
        except (OSError, ValueError, RuntimeError) as exc:
            result.append(
                {
                    "symbol": symbol,
                    "timeframe": "5m",
                    "health": "degraded",
                    "reason": str(exc)[:200],
                }
            )
    return result


@app.post("/api/trading-intelligence/official-events/refresh")
async def refresh_trading_intelligence_official_events():
    events, health, persisted = await collect_official_events_once()
    return {
        "events": events,
        "active_risk_windows": active_risk_windows(events),
        "source_health": health,
        "persisted": persisted,
        "etf_flow": ETF_FLOW_STATUS,
    }


@app.get("/api/trading-intelligence/overview")
async def trading_intelligence_overview():
    market_states = await asyncio.gather(
        *(
            asyncio.to_thread(trading_intelligence_store.latest_market_state, symbol)
            for symbol in V2_SYMBOLS
        )
    )
    quant_predictions = await asyncio.gather(
        *(
            asyncio.to_thread(trading_intelligence_store.latest_quant_prediction, symbol)
            for symbol in V2_SYMBOLS
        )
    )
    return {
        "version": 2,
        "runtime_modes": [str(item) for item in TradingModeV2],
        "symbols": list(V2_SYMBOLS),
        "market_states": [item for item in market_states if item],
        "active_theses": await asyncio.to_thread(trading_intelligence_store.list_active_theses),
        "active_plans": await asyncio.to_thread(trading_intelligence_store.list_open_plans),
        "active_waits": await asyncio.to_thread(
            trading_intelligence_store.list_wait_runtimes,
            active_only=True,
        ),
        "positions": await asyncio.to_thread(trading_intelligence_store.list_positions),
        "entry_orders": await asyncio.to_thread(v2_overview_entry_orders),
        "quant_predictions": [item for item in quant_predictions if item],
        "quant_models": await trading_intelligence_quant_models(),
        "episodes": await asyncio.to_thread(trading_intelligence_store.list_episodes, limit=50),
        "automation": await asyncio.to_thread(trading_intelligence_store.automation_runtime),
        "live_grant": await asyncio.to_thread(trading_intelligence_store.active_grant),
        "llm_health": await asyncio.to_thread(ai_gateway.health_snapshot),
        "learning": await asyncio.to_thread(v2_learning_snapshot),
        "portfolio_policy": configuration_service.current_configuration().risk.model_dump(
            mode="json"
        ),
    }


def v2_overview_entry_orders():
    active = trading_intelligence_store.list_entry_orders(active_only=True)
    recent = trading_intelligence_store.list_entry_orders(limit=100)
    return list({item.entry_id: item for item in [*recent, *active]}.values())


def v2_learning_snapshot():
    from .trading_learning import LearningSnapshot

    artifact = trading_intelligence_store.get_artifact("trading-learning-current")
    return artifact["payload"] if artifact else LearningSnapshot().model_dump(mode="json")


@app.get("/api/trading-intelligence/learning")
async def trading_intelligence_learning():
    return await asyncio.to_thread(v2_learning_snapshot)


@app.get("/api/trading-intelligence/market-state/{symbol:path}")
async def trading_intelligence_market_state(symbol: str):
    if symbol not in V2_SYMBOLS:
        raise HTTPException(400, "v2 supports only OKX BTC/ETH/SOL USDT perpetuals")
    state = await asyncio.to_thread(trading_intelligence_store.latest_market_state, symbol)
    if not state:
        raise HTTPException(404, "market state not available")
    return state


@app.post("/api/trading-intelligence/market-state", response_model=MarketStateV2)
async def ingest_trading_intelligence_market_state(state: MarketStateV2):
    raise HTTPException(403, "生产行情只能由服务端采集器写入，请使用 refresh 接口")


@app.post(
    "/api/trading-intelligence/market-state/{symbol:path}/refresh",
    response_model=MarketStateV2,
)
async def refresh_trading_intelligence_market_state(symbol: str):
    if symbol not in V2_SYMBOLS:
        raise HTTPException(400, "v2 supports only OKX BTC/ETH/SOL USDT perpetuals")
    configuration = configuration_service.current_configuration()
    if configuration.exchange_id != "okx" or str(configuration.market_type) != "swap":
        raise HTTPException(409, "MarketState v2 requires OKX swap market data")
    exchange_symbol = symbol.removesuffix(":USDT")
    try:
        contexts = await asyncio.gather(
            *(
                runtime.market_context(
                    limit=260,
                    symbol=exchange_symbol,
                    timeframe=timeframe,
                )
                for timeframe in ("1m", "5m", "15m", "1h", "4h", "1d")
            )
        )
    except Exception as exc:
        raise HTTPException(503, f"MarketState v2 source data unavailable: {exc}") from exc
    market = contexts[0][0].model_copy(update={"symbol": symbol})
    candles_by_timeframe: dict[str, list[Candle]] = {}
    quality_by_timeframe: dict[str, dict] = {}
    for timeframe, (_, history) in zip(
        ("1m", "5m", "15m", "1h", "4h", "1d"), contexts, strict=True
    ):
        causal = closed_candles(history, timeframe, market.timestamp)
        candles_by_timeframe[timeframe] = causal
        try:
            _, quality = validate_candles(causal, timeframe)
            quality_by_timeframe[timeframe] = quality.model_dump(mode="json")
        except Exception as exc:
            quality_by_timeframe[timeframe] = {
                "status": "unavailable",
                "message": str(exc)[:200],
            }
    microstructure: dict = {}
    if runtime.market is not None:
        with suppress(Exception):
            microstructure = await runtime.market.market_microstructure(exchange_symbol) or {}
    cross_histories: dict[str, list[Candle]] = {symbol: candles_by_timeframe.get("1h", [])}
    other_symbols = [item for item in V2_SYMBOLS if item != symbol]
    other_contexts = await asyncio.gather(
        *(
            runtime.market_context(
                limit=120,
                symbol=item.removesuffix(":USDT"),
                timeframe="1h",
            )
            for item in other_symbols
        ),
        return_exceptions=True,
    )
    for other_symbol, context in zip(other_symbols, other_contexts, strict=True):
        if isinstance(context, Exception):
            continue
        _, history = context
        cross_histories[other_symbol] = closed_candles(history, "1h", market.timestamp)
    state = await asyncio.to_thread(
        build_market_state_v2,
        market=market,
        candles_by_timeframe=candles_by_timeframe,
        quality_by_timeframe=quality_by_timeframe,
        microstructure=microstructure,
        cross_market=build_cross_market_context(cross_histories),
        normalized_events=active_risk_windows(
            await asyncio.to_thread(durable_official_events),
            at=market.timestamp,
        ),
    )
    try:
        await infer_quant_v1_on_five_minute_close(state)
    except (OSError, ValueError, RuntimeError) as exc:
        await asyncio.to_thread(
            trading_intelligence_store.append_event,
            dedupe_key=f"quant-degraded:{state.symbol}:{state.data_as_of:%Y%m%d%H%M}",
            event_type="QUANT_INFERENCE_DEGRADED",
            aggregate_type="quant_model",
            aggregate_id=state.symbol,
            aggregate_version=1,
            payload={"reason_hash": sha256(str(exc).encode()).hexdigest()},
        )
    if risk_state_service.get().halted:
        await asyncio.to_thread(trading_intelligence_store.save_market_state, state)
    else:
        await require_v2_automation().on_market_state(state)
    return state


@app.get("/api/trading-intelligence/episodes")
async def trading_intelligence_episodes(
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    if symbol is not None and symbol not in V2_SYMBOLS:
        raise HTTPException(400, "unsupported v2 symbol")
    return await asyncio.to_thread(
        trading_intelligence_store.list_episodes,
        symbol=symbol,
        limit=limit,
    )


@app.get("/api/trading-intelligence/events")
async def trading_intelligence_events(
    aggregate_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    return await asyncio.to_thread(
        trading_intelligence_store.list_events,
        aggregate_id=aggregate_id,
        limit=limit,
    )


@app.get("/api/trading-intelligence/attribution")
async def trading_intelligence_attribution():
    outcomes: list[DecisionOutcomeV2] = []
    for episode in await asyncio.to_thread(
        trading_intelligence_store.list_episodes,
        limit=5000,
    ):
        if not episode.outcome:
            continue
        try:
            outcomes.append(DecisionOutcomeV2.model_validate(episode.outcome))
        except ValidationError:
            continue
    return build_decision_attribution(outcomes)


@app.get(
    "/api/trading-intelligence/trade-history",
    response_model=TradeHistoryResponseV2,
)
async def trading_intelligence_trade_history(
    symbol: str | None = None,
    days: int = Query(default=30, ge=0, le=3650),
    limit: int = Query(default=500, ge=1, le=2000),
):
    if symbol is not None and symbol not in V2_SYMBOLS:
        raise HTTPException(400, "unsupported v2 symbol")
    positions, episode_list = await asyncio.gather(
        asyncio.to_thread(
            trading_intelligence_store.list_positions,
            symbol=symbol,
            include_closed=True,
        ),
        asyncio.to_thread(trading_intelligence_store.list_episodes, symbol=symbol, limit=5000),
    )
    episodes = {item.episode_id: item for item in episode_list}
    closed_episode_ids = {
        item.episode_id for item in positions if str(item.state) == "CLOSED" and item.episode_id
    }
    allocations: dict[str, RiskAllocation] = {}
    for episode in episode_list:
        if episode.episode_id not in closed_episode_ids:
            continue
        allocation_id = episode.risk_allocation_id
        if not allocation_id or allocation_id in allocations:
            continue
        artifact = await asyncio.to_thread(
            trading_intelligence_store.get_artifact,
            allocation_id,
        )
        if not artifact or artifact["artifact_type"] != "RiskAllocation":
            continue
        try:
            allocations[allocation_id] = RiskAllocation.model_validate(artifact["payload"])
        except ValidationError:
            continue
    return build_trade_history_v2(
        positions,
        episodes,
        allocations,
        symbol=symbol,
        days=days,
        limit=limit,
    )


@app.get("/api/trading-intelligence/artifacts/{artifact_id}")
async def trading_intelligence_artifact(artifact_id: str):
    artifact = await asyncio.to_thread(trading_intelligence_store.get_artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, "trading intelligence artifact not found")
    return artifact


@app.post("/api/trading-intelligence/decide")
async def invoke_trading_intelligence(request: TradingIntelligenceDecisionRequest):
    token = decision_entry_generation.set(risk_state_service.entry_generation)
    try:
        return await _invoke_trading_intelligence(request)
    finally:
        decision_entry_generation.reset(token)


async def _invoke_trading_intelligence(request: TradingIntelligenceDecisionRequest):
    state = await asyncio.to_thread(trading_intelligence_store.latest_market_state, request.symbol)
    if not state:
        raise HTTPException(409, "a current MarketState v2 is required")
    try:
        validate_market_state_time(state)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    active_theses = await asyncio.to_thread(
        trading_intelligence_store.list_active_theses, request.symbol
    )
    if request.trigger == "NO_ACTIVE_THESIS" and active_theses:
        raise HTTPException(409, "an active thesis already exists")
    trigger_event = None
    if request.trigger != "NO_ACTIVE_THESIS":
        if not request.trigger_event_id:
            raise HTTPException(400, "this trigger requires a durable event id")
        trigger_event = await asyncio.to_thread(
            trading_intelligence_store.get_event,
            request.trigger_event_id,
        )
        if not trigger_event:
            raise HTTPException(409, "trigger event is not present in the durable ledger")
        if request.trigger == "WAKE_TRIGGERED" and trigger_event["event_type"] not in {
            "WAKE_TRIGGERED",
            "WAIT_PLAN_EXPIRED",
        }:
            raise HTTPException(409, "event is not a WakeCondition firing")
    quant = await asyncio.to_thread(
        trading_intelligence_store.latest_quant_prediction, request.symbol
    )
    quant_model_eligible = False
    if quant is not None:
        try:
            quant_model = await asyncio.to_thread(load_quant_v1_model, request.symbol)
            quant_model_eligible = bool(
                quant_model and quant_model_is_eligible(quant_model.artifact)
            )
        except (OSError, ValueError, RuntimeError):
            quant_model_eligible = False
    # Demo/Shadow retain the prediction as counterfactual evidence. Only Live
    # treats model quality plus fresh directional edge as a hard entry gate.
    if request.mode == TradingModeV2.LIVE_AUTHORIZED and not quant_model_eligible:
        quant = None
    try:
        portfolio = await runtime.portfolio()
    except Exception as exc:
        raise HTTPException(503, f"portfolio state unavailable: {exc}") from exc
    configuration = configuration_service.current_configuration()
    risk_control = risk_state_service.get()
    reconciliation = runtime.reconciliation_report
    unresolved_reconciliation = bool(
        reconciliation
        and (
            reconciliation.uncertain_order_ids
            or reconciliation.unknown_open_order_ids
            or reconciliation.unknown_position_symbols
            or reconciliation.unprotected_order_ids
        )
    )
    quant_policy = "HARD_GATE" if request.mode == TradingModeV2.LIVE_AUTHORIZED else "ADVISORY"
    previous_plan = next(
        iter(await asyncio.to_thread(trading_intelligence_store.list_open_plans, request.symbol)),
        None,
    )
    previous_state = await asyncio.to_thread(
        trading_intelligence_store.previous_market_state,
        request.symbol,
        before_state_hash=state.state_hash,
        before_data_as_of=state.data_as_of,
    )
    previous_wait = None
    if trigger_event and trigger_event["aggregate_type"] in {"plan", "wait_runtime"}:
        artifact = await asyncio.to_thread(
            trading_intelligence_store.get_artifact,
            trigger_event["aggregate_id"],
        )
        if artifact and artifact["artifact_type"] == "WaitRuntimeV2":
            previous_wait = WaitRuntimeV2.model_validate(artifact["payload"])

    def changed_paths(before: MarketStateV2 | None, current: MarketStateV2) -> list[str]:
        if before is None:
            return []
        prior_payload = before.model_dump(mode="json", exclude_none=True)
        current_payload = current.model_dump(mode="json", exclude_none=True)
        changes: list[str] = []

        def walk(left, right, prefix: str = "") -> None:
            if len(changes) >= 100:
                return
            if isinstance(left, dict) and isinstance(right, dict):
                for key in sorted(set(left) | set(right)):
                    path = f"{prefix}.{key}" if prefix else key
                    if key not in left or key not in right:
                        changes.append(path)
                    else:
                        walk(left[key], right[key], path)
                return
            if left != right:
                changes.append(prefix)

        walk(prior_payload, current_payload)
        return changes

    delta = DeltaContext(
        prior_state_hash=previous_state.state_hash if previous_state else None,
        current_state_hash=state.state_hash,
        changed_paths=changed_paths(previous_state, state),
        material_changes=[
            f"event:{request.trigger}",
            f"quant_policy:{quant_policy.lower()}",
            f"quant_prediction:{'available' if quant is not None else 'unavailable'}",
            f"quant_model_live_eligible:{str(quant_model_eligible).lower()}",
        ],
        trigger_type=request.trigger,
        trigger_evidence=(
            {
                "event_id": trigger_event["event_id"],
                "event_type": trigger_event["event_type"],
                "aggregate_type": trigger_event["aggregate_type"],
                "aggregate_id": trigger_event["aggregate_id"],
                "aggregate_version": trigger_event["aggregate_version"],
                "payload": trigger_event["payload"],
            }
            if trigger_event
            else None
        ),
    )

    async def decision_error_response(
        message: str,
        decision_result=None,
    ) -> dict:
        error_message = message.removeprefix("CONTRACT_ERROR:").strip()
        if decision_result is not None:
            rejected = decision_result.episode.model_copy(deep=True)
            rejected.status = "DECISION_ERROR"
            rejected.trigger_event_id = request.trigger_event_id
            rejected.outcome = {
                "error_code": "CONTRACT_OR_STATE_ERROR",
                "summary": error_message[:2000],
                "trigger": request.trigger,
            }
            rejected.output_hashes = {
                **rejected.output_hashes,
                "rejection": sha256(message.encode()).hexdigest(),
            }
            await asyncio.to_thread(
                trading_intelligence_store.save_artifact,
                decision_result.analysis.analysis_id,
                "MarketAnalysis",
                decision_result.analysis,
            )
            await asyncio.to_thread(
                trading_intelligence_store.save_artifact,
                decision_result.decision.decision_id,
                "PortfolioTraderDecision",
                decision_result.decision,
            )
        else:
            rejected = DecisionEpisode(
                symbol=state.symbol,
                status="DECISION_ERROR",
                market_state_hash=state.state_hash,
                delta=delta,
                quant_prediction_id=quant.prediction_id if quant else None,
                trigger_event_id=request.trigger_event_id,
                prompt_versions={
                    "analyst": "market_analyst_v2",
                    "trader": "portfolio_trader_v2",
                    "repair": "schema_repair_once_v2",
                },
                outcome={
                    "error_code": "CONTRACT_OR_STATE_ERROR",
                    "summary": error_message[:2000],
                    "trigger": request.trigger,
                },
                output_hashes={"rejection": sha256(message.encode()).hexdigest()},
            )
        await asyncio.to_thread(trading_intelligence_store.save_episode, rejected)
        return {
            "analysis": decision_result.analysis if decision_result is not None else None,
            "decision": decision_result.decision if decision_result is not None else None,
            "episode": rejected,
            "decision_error": True,
        }

    decision_positions = await asyncio.to_thread(trading_intelligence_store.list_positions)
    try:
        from .trading_learning import learning_context

        try:
            learned_context = await asyncio.to_thread(
                learning_context, trading_intelligence_store, request.symbol, str(request.mode)
            )
        except Exception:
            logger.exception("Trading learning unavailable; continuing without advisory memory")
            learned_context = {"policy": "ADVISORY_ONLY", "groups": [], "status": "UNAVAILABLE"}
        result = await dual_llm_decision_service.decide(
            market_state=state,
            delta=delta,
            quant=quant,
            portfolio=v2_llm_portfolio_context(
                portfolio,
                symbol=request.symbol,
                max_daily_drawdown=configuration.risk.max_daily_drawdown,
                risk_halted=risk_control.halted,
                unresolved_reconciliation=unresolved_reconciliation,
                managed_positions=decision_positions,
            ),
            previous_thesis=(active_theses[0].model_dump(mode="json") if active_theses else None),
            previous_plan=(previous_plan.model_dump(mode="json") if previous_plan else None),
            previous_wait_plan=(
                previous_wait.wait_plan.model_dump(mode="json") if previous_wait else None
            ),
            previous_market_state=previous_state,
            require_quant_edge=request.mode == TradingModeV2.LIVE_AUTHORIZED,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
            learning=learned_context,
        )
    except ValueError as exc:
        if str(exc).startswith("CONTRACT_ERROR:"):
            return await decision_error_response(str(exc))
        raise HTTPException(503, str(exc)) from exc
    except AIProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    if decision_entry_generation.get() != risk_state_service.entry_generation:
        return await decision_error_response(
            "decision invalidated by a stop while awaiting LLM", result
        )
    if result.decision.action == "WAIT":
        referenced_thesis_id = (
            result.decision.wait_plan.thesis_id if result.decision.wait_plan else None
        )
        valid_thesis_ids = {item.thesis_id for item in active_theses}
        if result.decision.thesis:
            valid_thesis_ids.add(result.decision.thesis.thesis_id)
        if referenced_thesis_id is not None and referenced_thesis_id not in valid_thesis_ids:
            return await decision_error_response(
                "WAIT thesis_id, when provided, must reference an existing "
                "or returned active Thesis",
                result,
            )
    managed_position_context = None
    managed_plan_context = None
    if result.decision.action in {"MANAGE_EXISTING", "EXIT"}:
        active_positions = await asyncio.to_thread(
            trading_intelligence_store.list_positions,
            symbol=request.symbol,
        )
        target_id = result.decision.position_id
        target_version = result.decision.position_version
        try:
            managed_position_context = resolve_position_decision(
                target_id, target_version, decision_positions, active_positions
            )
        except ValueError as exc:
            return await decision_error_response(
                f"MANAGE_EXISTING/EXIT target rejected: {exc}",
                result,
            )
        managed_plan_context = await asyncio.to_thread(
            trading_intelligence_store.get_plan,
            managed_position_context.plan_id,
        )
        if not managed_plan_context:
            return await decision_error_response(
                "position references an unavailable TradePlan",
                result,
            )
    decision_thesis = result.decision.thesis
    if decision_thesis:
        now = datetime.now(UTC)
        maximum_expiry = max(now, state.data_as_of) + timedelta(hours=4)
        if decision_thesis.expires_at <= now:
            return await decision_error_response(
                "returned Thesis is already expired",
                result,
            )
        existing_thesis = await asyncio.to_thread(
            trading_intelligence_store.get_thesis,
            decision_thesis.thesis_id,
        )
        decision_thesis = decision_thesis.model_copy(
            update={
                "version": existing_thesis.version if existing_thesis else 1,
                "state": existing_thesis.state if existing_thesis else ThesisState.CREATED,
                "created_at": (existing_thesis.created_at if existing_thesis else now),
                "expires_at": min(decision_thesis.expires_at, maximum_expiry),
            }
        )
        result.decision.thesis = decision_thesis
    if result.decision.trade_plan and await asyncio.to_thread(
        trading_intelligence_store.get_plan,
        result.decision.trade_plan.plan_id,
    ):
        return await decision_error_response(
            "returned TradePlan id already exists",
            result,
        )
    await asyncio.to_thread(trading_intelligence_store.cancel_active_waits, request.symbol)
    result.episode.trigger_event_id = request.trigger_event_id
    await asyncio.to_thread(
        trading_intelligence_store.save_artifact,
        result.analysis.analysis_id,
        "MarketAnalysis",
        result.analysis,
    )
    await asyncio.to_thread(
        trading_intelligence_store.save_artifact,
        result.decision.decision_id,
        "PortfolioTraderDecision",
        result.decision,
    )
    if result.decision.thesis:
        existing_thesis = await asyncio.to_thread(
            trading_intelligence_store.get_thesis,
            result.decision.thesis.thesis_id,
        )
        if existing_thesis:
            updated_thesis = await asyncio.to_thread(
                trading_intelligence_store.update_thesis_content,
                result.decision.thesis,
                expected_version=existing_thesis.version,
            )
            result.decision.thesis = updated_thesis
        else:
            await asyncio.to_thread(
                trading_intelligence_store.save_thesis,
                result.decision.thesis,
            )
        if not existing_thesis and str(result.decision.thesis.state) == "CREATED":
            await asyncio.to_thread(
                trading_intelligence_store.transition_thesis,
                result.decision.thesis.thesis_id,
                expected_version=result.decision.thesis.version,
                target=ThesisState.ACTIVE,
            )
    if result.decision.trade_plan:
        await asyncio.to_thread(trading_intelligence_store.save_plan, result.decision.trade_plan)
        validated = await asyncio.to_thread(
            trading_intelligence_store.transition_plan,
            result.decision.trade_plan.plan_id,
            expected_version=result.decision.trade_plan.version,
            target=TradingIntelligencePlanState.VALIDATED,
        )
        activated = await asyncio.to_thread(
            trading_intelligence_store.transition_plan,
            result.decision.trade_plan.plan_id,
            expected_version=validated.version,
            target=TradingIntelligencePlanState.ACTIVE,
        )
        if activated.order_type is None:
            activated = await asyncio.to_thread(
                trading_intelligence_store.transition_plan,
                activated.plan_id,
                expected_version=activated.version,
                target=TradingIntelligencePlanState.WAITING_TRIGGER,
            )
        result.decision.trade_plan = activated
    if result.decision.action in {"MANAGE_EXISTING", "EXIT"}:
        async with v2_entry_allocation_lock:
            current_positions = await asyncio.to_thread(
                trading_intelligence_store.list_positions, symbol=request.symbol
            )
            try:
                managed_position_context = resolve_position_decision(
                    result.decision.position_id, result.decision.position_version,
                    decision_positions, current_positions,
                )
            except ValueError as exc:
                return await decision_error_response(
                    f"position changed before management: {exc}", result
                )
            managed_plan_context = await asyncio.to_thread(
                trading_intelligence_store.get_plan, managed_position_context.plan_id
            )
            assert managed_position_context is not None
            assert managed_plan_context is not None
            managed_position = managed_position_context
            managed_plan = managed_plan_context
            result.episode.position_id = managed_position.position_id
            result.episode.plan_id = managed_plan.plan_id
            result.episode.thesis_id = managed_plan.thesis_id
            if result.decision.action == "MANAGE_EXISTING":
                proposed = result.decision.management_plan
                assert proposed is not None
                # Native TP/SL submitted at entry remain immutable fail-safe protection.
                # The LLM may tighten deterministic runtime rules, but cannot silently
                # replace venue protection levels without a commissioned amend protocol.
                effective_management = proposed.model_copy(
                    update={
                        "hard_stop": managed_plan.management.hard_stop,
                        "take_profits": managed_plan.management.take_profits,
                    }
                )
                updated_plan = managed_plan.model_copy(update={"management": effective_management})
                updated_plan = await asyncio.to_thread(
                    trading_intelligence_store.update_plan_content,
                    updated_plan,
                    expected_version=managed_plan.version,
                )
                if effective_management != managed_plan.management:
                    managed_position = await asyncio.to_thread(
                        trading_intelligence_store.update_position,
                        managed_position.position_id,
                        expected_version=managed_position.version,
                        updates={"management_version": managed_position.management_version + 1},
                    )
                await asyncio.to_thread(
                    trading_intelligence_store.append_event,
                    dedupe_key=(
                        f"position-management-updated:{managed_position.position_id}:"
                        f"{updated_plan.version}:{result.decision.decision_id}"
                    ),
                    event_type="POSITION_MANAGEMENT_UPDATED",
                    aggregate_type="position",
                    aggregate_id=managed_position.position_id,
                    aggregate_version=managed_position.version,
                    payload={
                        "plan_id": updated_plan.plan_id,
                        "plan_version": updated_plan.version,
                        "native_protection_levels_preserved": True,
                        "decision_position_version": result.decision.position_version,
                        "applied_position_version": managed_position.version,
                    },
                )
            else:
                exit_reason = "; ".join(
                    item.reference for item in result.decision.invalidation_evidence
                )[:2000]
                updated_position = await asyncio.to_thread(
                    trading_intelligence_store.update_position,
                    managed_position.position_id,
                    expected_version=managed_position.version,
                    updates={
                        "exit_requested_at": datetime.now(UTC),
                        "exit_reason": exit_reason or "LLM thesis invalidation",
                    },
                )
                await asyncio.to_thread(
                    trading_intelligence_store.append_event,
                    dedupe_key=(
                        f"position-exit-requested:{updated_position.position_id}:"
                        f"{updated_position.version}"
                    ),
                    event_type="POSITION_EXIT_REQUESTED",
                    aggregate_type="position",
                    aggregate_id=updated_position.position_id,
                    aggregate_version=updated_position.version,
                    payload={
                        "decision_id": result.decision.decision_id,
                        "invalidation_evidence": [
                            item.model_dump(mode="json")
                            for item in result.decision.invalidation_evidence
                        ],
                    },
                )
    await asyncio.to_thread(trading_intelligence_store.save_episode, result.episode)
    if result.decision.wait_plan:
        await asyncio.to_thread(
            trading_intelligence_store.save_wait_runtime,
            WaitRuntimeV2(
                episode_id=result.episode.episode_id,
                symbol=request.symbol,
                wait_plan=result.decision.wait_plan,
            ),
        )
    execution_result = None
    if result.decision.trade_plan and result.decision.trade_plan.order_type is not None:
        if decision_entry_generation.get() != risk_state_service.entry_generation:
            cancelled = await asyncio.to_thread(
                trading_intelligence_store.transition_plan,
                result.decision.trade_plan.plan_id,
                expected_version=result.decision.trade_plan.version,
                target=TradingIntelligencePlanState.CANCELLED,
            )
            result.decision.trade_plan = cancelled
            return await decision_error_response(
                "decision invalidated before entry submission", result
            )
        execution_result = await execute_automated_trading_intelligence_plan(
            result.decision.trade_plan.plan_id,
            result.episode.episode_id,
            None,
            request.mode,
            plan_version=result.decision.trade_plan.version,
        )
    return {
        "analysis": result.analysis,
        "decision": result.decision,
        "episode": result.episode,
        "execution": execution_result,
    }


v2_entry_allocation_lock = asyncio.Lock()
v2_submission_lock = asyncio.Lock()
v2_configuration_generation = 0
decision_entry_generation: ContextVar[int | None] = ContextVar(
    "decision_entry_generation", default=None
)
critical_workers: dict[str, asyncio.Task] = {}


def on_critical_worker_exit(task: asyncio.Task) -> None:
    if critical_workers.get(task.get_name()) is not task:
        return
    error = None if task.cancelled() else task.exception()
    logger.error("Critical worker exited: %s (%s)", task.get_name(), type(error).__name__)
    risk_state_service.halt(
        f"critical background worker stopped: {task.get_name()}",
        source="worker_failure",
    )


@asynccontextmanager
async def v2_submission_guard(generation: int, configuration_fingerprint: str):
    async with v2_submission_lock:
        if not critical_workers or any(task.done() for task in critical_workers.values()):
            raise EntryRiskBlocked("critical trading workers are not running")
        if generation != risk_state_service.entry_generation or risk_state_service.get().halted:
            raise EntryRiskBlocked("entry invalidated by a risk stop")
        current = configuration_service.current_configuration()
        if v2_configuration_fingerprint(current) != configuration_fingerprint:
            raise EntryRiskBlocked("configuration changed before entry submission")
        yield


@app.post("/api/trading-intelligence/plans/{plan_id}/execute")
async def execute_trading_intelligence_plan(
    plan_id: str,
    request: TradingIntelligenceExecuteRequest,
):
    # Serialize only account allocation/submission. Symbol analysis remains independent.
    # A submitted entry is durable before the next symbol reads its risk reservation.
    token = None
    if decision_entry_generation.get() is None:
        token = decision_entry_generation.set(risk_state_service.entry_generation)
    try:
        async with v2_entry_allocation_lock:
            return await _execute_trading_intelligence_plan(plan_id, request)
    finally:
        if token is not None:
            decision_entry_generation.reset(token)


async def _record_entry_rejection(
    plan_id: str,
    episode: DecisionEpisode | None,
    *,
    reason: str,
    code: str = "ENTRY_REJECTED",
) -> None:
    """Make an entry refusal terminal and keep the decision audit truthful."""

    current_plan = await asyncio.to_thread(trading_intelligence_store.get_plan, plan_id)
    rejected_plan = current_plan
    if current_plan and current_plan.state not in {
        TradingIntelligencePlanState.COMPLETED,
        TradingIntelligencePlanState.EXPIRED,
        TradingIntelligencePlanState.INVALIDATED,
        TradingIntelligencePlanState.CANCELLED,
    }:
        rejected_plan = await asyncio.to_thread(
            trading_intelligence_store.transition_plan,
            plan_id,
            expected_version=current_plan.version,
            target=TradingIntelligencePlanState.CANCELLED,
        )
    if episode is not None:
        rejected_episode = episode.model_copy(deep=True)
        rejected_episode.status = "ENTRY_REJECTED"
        rejected_episode.outcome = {
            "error_code": code,
            "summary": reason[:2000],
            "plan_state": str(rejected_plan.state) if rejected_plan else None,
        }
        await asyncio.to_thread(trading_intelligence_store.save_episode, rejected_episode)
    await asyncio.to_thread(
        trading_intelligence_store.append_event,
        dedupe_key=(
            f"entry-execution-rejected:{plan_id}:"
            f"{rejected_plan.version if rejected_plan else 0}:"
            f"{sha256(reason.encode()).hexdigest()}"
        ),
        event_type="ENTRY_EXECUTION_REJECTED",
        aggregate_type="plan",
        aggregate_id=plan_id,
        aggregate_version=rejected_plan.version if rejected_plan else 1,
        payload={
            "reason": reason,
            "error_code": code,
            "episode_id": episode.episode_id if episode else None,
            "plan_state": str(rejected_plan.state) if rejected_plan else None,
        },
    )


async def _execute_trading_intelligence_plan(
    plan_id: str,
    request: TradingIntelligenceExecuteRequest,
):
    if request.mode == TradingModeV2.LIVE_AUTHORIZED:
        raise HTTPException(
            409, "Live execution is disabled until Live reconciliation is available"
        )
    entry_generation = decision_entry_generation.get()
    if entry_generation is None:
        entry_generation = risk_state_service.entry_generation
    if entry_generation != risk_state_service.entry_generation:
        raise HTTPException(409, "entry invalidated by a risk stop")
    risk_control = risk_state_service.get()
    if risk_control.halted:
        raise HTTPException(409, f"global risk halt is active: {risk_control.source}")
    plan = await asyncio.to_thread(trading_intelligence_store.get_plan, plan_id)
    if not plan:
        raise HTTPException(404, "Trading Intelligence plan not found")
    immediate = plan.order_type is not None
    episodes = await asyncio.to_thread(
        trading_intelligence_store.list_episodes,
        symbol=plan.symbol,
        limit=500,
    )
    episode = next((item for item in episodes if item.episode_id == request.episode_id), None)
    if not episode or episode.plan_id != plan_id:
        raise HTTPException(409, "DecisionEpisode does not own this plan")
    if immediate:
        existing_entry = await asyncio.to_thread(trading_intelligence_store.entry_for_plan, plan_id)
        if existing_entry is not None:
            if existing_entry.episode_id != request.episode_id:
                raise HTTPException(409, "DecisionEpisode does not own this entry order")
            return {
                "mode": str(existing_entry.mode),
                "status": str(existing_entry.status),
                "plan_id": plan_id,
                "entry_order": existing_entry.model_dump(mode="json"),
            }
        if request.plan_version != plan.version:
            raise HTTPException(409, "exact current plan_version is required")
    allowed_states = {
        TradingIntelligencePlanState.WAITING_TRIGGER,
        TradingIntelligencePlanState.ARMED,
    }
    if immediate:
        allowed_states = {TradingIntelligencePlanState.ACTIVE}
    if plan.state not in allowed_states:
        raise HTTPException(409, "plan is not waiting for an entry trigger")
    existing_positions = await asyncio.to_thread(
        trading_intelligence_store.list_positions,
        symbol=plan.symbol,
    )
    same_direction = [item for item in existing_positions if item.side == plan.side]
    if immediate and same_direction:
        if len(same_direction) != 1 or not any(
            item.position_id == plan.position_id and item.version == plan.position_version
            for item in same_direction
        ):
            raise HTTPException(
                409, "same-direction add requires exact position_id and position_version"
            )
    elif immediate and plan.position_id is not None:
        raise HTTPException(409, "target position is no longer open")
    elif not immediate and any(position.plan_id != plan.plan_id for position in existing_positions):
        raise HTTPException(
            409,
            "an attributable position already exists for this symbol; "
            "a second plan cannot share the venue's aggregate position",
        )
    events = await asyncio.to_thread(
        trading_intelligence_store.list_events,
        aggregate_id=plan_id,
        limit=1000,
    )
    wake_event = next(
        (
            item
            for item in events
            if item["event_id"] == request.wake_event_id and item["event_type"] == "WAKE_TRIGGERED"
        ),
        None,
    )
    if not immediate and not wake_event:
        raise HTTPException(409, "a durable WakeCondition firing is required")
    state = await asyncio.to_thread(trading_intelligence_store.latest_market_state, plan.symbol)
    if not state:
        raise HTTPException(409, "current MarketState v2 is unavailable")
    try:
        validate_market_state_time(state)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    quant = await asyncio.to_thread(trading_intelligence_store.latest_quant_prediction, plan.symbol)
    configuration_view = configuration_service.get()
    configuration = configuration_view.configuration
    fingerprint = v2_configuration_fingerprint(configuration)
    grant = await asyncio.to_thread(trading_intelligence_store.active_grant)
    demo_credentials = configuration_view.credential_scopes.get("okx:sandbox")
    demo_available = bool(
        demo_credentials
        and demo_credentials.api_key_configured
        and demo_credentials.secret_configured
        and demo_credentials.passphrase_configured
    )
    actual_mode = resolve_trading_mode(
        requested=request.mode,
        symbol=plan.symbol,
        configuration_fingerprint_value=fingerprint,
        live_grant=grant,
        demo_credentials_available=demo_available,
    )
    adapter = None
    if actual_mode != TradingModeV2.SHADOW:
        credential_scope = (
            "okx:live" if actual_mode == TradingModeV2.LIVE_AUTHORIZED else "okx:sandbox"
        )
        credentials = secret_store.get_credentials(credential_scope)
        adapter = CcxtExchangeAdapter(
            "okx",
            credentials,
            sandbox=actual_mode == TradingModeV2.OKX_DEMO,
            live_enabled=actual_mode == TradingModeV2.LIVE_AUTHORIZED,
            public_only=False,
            market_type=configuration.market_type,
            margin_mode=configuration.margin_mode,
            contract_leverage=configuration.contract_leverage,
            store=store,
            account_id=f"okx:{str(actual_mode).lower()}:swap:v2",
            proxy_url=configuration.network_proxy_url,
            entry_submission_guard=partial(v2_submission_guard, entry_generation, fingerprint),
        )
    try:
        if adapter is not None:
            portfolio = await adapter.portfolio()
            market = await adapter.market_snapshot(plan.symbol.removesuffix(":USDT"))
            execution_adapter = adapter
        else:
            market, _ = await runtime.market_context(
                limit=50,
                symbol=plan.symbol.removesuffix(":USDT"),
                timeframe="5m",
            )
            portfolio = await runtime.portfolio()
            execution_adapter = runtime.execution
            if execution_adapter is None:
                raise RuntimeError("shadow execution runtime is unavailable")
        available_margin = (
            portfolio.available_margin
            if portfolio.available_margin is not None
            else portfolio.equity
        )
        # The MarketState slippage feature is a size-agnostic stress estimate
        # and can sit at its 100 bps cap before RiskAllocation exists. Using it
        # here rejected small Demo orders despite a tight executable spread.
        # Depth remains bounded independently by liquidity_notional_cap.
        estimated_slippage_bps = market.spread_bps
        from .portfolio_risk_v2 import portfolio_risk_snapshot

        managed_positions = await asyncio.to_thread(trading_intelligence_store.list_positions)
        pending_entries = await asyncio.to_thread(
            trading_intelligence_store.list_entry_orders,
            active_only=True,
        )
        portfolio_risk = portfolio_risk_snapshot(
            portfolio,
            managed_positions,
            existing_position_stress=configuration.risk.existing_position_stress,
            pending_entries=pending_entries,
            mode=str(actual_mode),
        )
        existing_position_risk = None
        if plan.position_id is not None:
            target = next(
                (
                    item
                    for item in portfolio_risk["positions"]
                    if item.get("position_id") == plan.position_id
                    and item.get("position_version") == plan.position_version
                ),
                None,
            )
            if target is None:
                raise HTTPException(409, "target position risk context is unavailable")
            existing_position_risk = ExistingPositionRisk(
                position_id=plan.position_id,
                position_version=plan.position_version,
                symbol=plan.symbol,
                side=plan.side,
                quantity=target["quantity"],
                mark_price=target["mark_price"],
                risk_amount=target["risk_amount"],
            )
        risk_context = TradingIntelligenceRiskContext(
            equity=portfolio.equity,
            available_margin=max(available_margin, Decimal("0.00000001")),
            portfolio_risk_used=portfolio_risk["risk_fraction"],
            daily_drawdown=portfolio.daily_drawdown_fraction,
            open_positions=portfolio_risk["open_positions"],
            estimated_slippage_bps=estimated_slippage_bps,
            existing_position=existing_position_risk,
            liquidity_notional_cap=max(
                available_margin * configuration.risk.max_leverage * Decimal("0.25"),
                Decimal("10"),
            ),
            correlation_risk_multiplier=(
                Decimal("0.5")
                if any(item["side"] == plan.side for item in portfolio_risk["positions"])
                else Decimal("1")
            ),
        )
        risk_limits = TradingIntelligenceRiskLimits(
            risk_per_trade=configuration.risk.risk_per_trade,
            max_portfolio_risk=configuration.risk.max_portfolio_risk,
            max_daily_drawdown=configuration.risk.max_daily_drawdown,
            max_open_positions=configuration.risk.max_open_positions,
            max_leverage=configuration.risk.max_leverage,
            max_slippage_bps=configuration.risk.max_spread_bps,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
        )
        allocation = DeterministicRiskEngine().allocate(
            plan,
            risk_context,
            risk_limits,
            grant=grant,
            live=actual_mode == TradingModeV2.LIVE_AUTHORIZED,
        )
        if adapter is not None:
            adapter.contract_leverage = allocation.leverage
        execution = V2ExecutionEngine(
            execution_adapter,
            trading_intelligence_store,
            margin_mode=str(configuration.margin_mode),
            execution_leverage=allocation.leverage,
            demo_credentials_available=demo_available,
        )
        try:
            preflight_mode = await execution.preflight(
                plan=plan,
                market_state=state,
                requested_mode=request.mode,
                configuration_fingerprint=fingerprint,
                live_grant=grant,
                quant=quant,
            )
        except EntryRiskBlocked as exc:
            await _record_entry_rejection(
                plan_id,
                episode,
                reason=str(exc),
                code="ENTRY_RISK_BLOCKED",
            )
            await asyncio.to_thread(
                trading_intelligence_store.append_event,
                dedupe_key=f"entry-execution-blocked:{request.wake_event_id}",
                event_type="ENTRY_EXECUTION_BLOCKED",
                aggregate_type="plan",
                aggregate_id=plan_id,
                aggregate_version=plan.version,
                payload={
                    "wake_event_id": request.wake_event_id,
                    "reason": str(exc),
                    "risk_halted": False,
                },
            )
            return V2ExecutionResult(
                mode=actual_mode,
                status="REJECTED",
                plan_id=plan.plan_id,
                allocation_id=allocation.allocation_id,
                reason=str(exc),
            )
        if immediate:
            result = await execution.execute(
                plan=plan,
                allocation=allocation,
                market_state=state,
                market=market,
                episode=episode,
                requested_mode=request.mode,
                configuration_fingerprint=fingerprint,
                live_grant=grant,
                quant=quant,
                preflight_mode=preflight_mode,
            )
            if actual_mode == TradingModeV2.OKX_DEMO:
                await asyncio.to_thread(reconciliation_protected_plan_ids)
            return result
        armed_plan = plan
        if plan.state == TradingIntelligencePlanState.WAITING_TRIGGER:
            armed_plan = await asyncio.to_thread(
                trading_intelligence_store.transition_plan,
                plan_id,
                expected_version=plan.version,
                target=TradingIntelligencePlanState.ARMED,
            )
        _claim_event_id, claimed = await asyncio.to_thread(
            trading_intelligence_store.append_event,
            dedupe_key=f"entry-execution:{request.wake_event_id}",
            event_type="ENTRY_EXECUTION_CLAIMED",
            aggregate_type="plan",
            aggregate_id=plan_id,
            aggregate_version=armed_plan.version,
            payload={
                "wake_event_id": request.wake_event_id,
                "episode_id": request.episode_id,
                "mode": str(actual_mode),
            },
        )
        if not claimed:
            raise RuntimeError("this WakeCondition execution has already been claimed")
        triggered_plan = await asyncio.to_thread(
            trading_intelligence_store.transition_plan,
            plan_id,
            expected_version=armed_plan.version,
            target=TradingIntelligencePlanState.TRIGGERED,
        )
        result = await execution.execute(
            plan=triggered_plan,
            allocation=allocation,
            market_state=state,
            market=market,
            episode=episode,
            requested_mode=request.mode,
            configuration_fingerprint=fingerprint,
            live_grant=grant,
            quant=quant,
            preflight_mode=preflight_mode,
        )
        if actual_mode == TradingModeV2.OKX_DEMO:
            await asyncio.to_thread(reconciliation_protected_plan_ids)
        return result
    except HTTPException as exc:
        await _record_entry_rejection(plan_id, episode, reason=str(exc.detail))
        raise
    except (PermissionError, ValueError, RuntimeError) as exc:
        current_plan = await asyncio.to_thread(trading_intelligence_store.get_plan, plan_id)
        if (
            current_plan
            and current_plan.order_type is not None
            and await asyncio.to_thread(
                trading_intelligence_store.entry_for_plan,
                plan_id,
            )
            is None
        ):
            await _record_entry_rejection(
                plan_id,
                episode,
                reason=str(exc),
                code="EXCHANGE_OR_EXECUTION_REJECTED",
            )
            raise HTTPException(409, str(exc)) from exc
        if current_plan and current_plan.state == TradingIntelligencePlanState.TRIGGERED:
            await asyncio.to_thread(
                trading_intelligence_store.append_event,
                dedupe_key=f"entry-execution-failed:{request.wake_event_id}",
                event_type="ENTRY_EXECUTION_FAILED",
                aggregate_type="plan",
                aggregate_id=plan_id,
                aggregate_version=current_plan.version,
                payload={
                    "wake_event_id": request.wake_event_id,
                    "reason_hash": sha256(str(exc).encode()).hexdigest(),
                    "risk_halted": True,
                },
            )
            risk_state_service.halt(
                "Trading Intelligence v2 entry failed after the durable trigger claim",
                source="trading_intelligence_v2",
            )
        raise HTTPException(409, str(exc)) from exc
    finally:
        if adapter is not None:
            await adapter.close()


async def invoke_automated_trading_intelligence_decision(
    symbol: str,
    trigger: str,
    trigger_event_id: str | None,
    mode: TradingModeV2,
) -> dict:
    result = await invoke_trading_intelligence(
        TradingIntelligenceDecisionRequest(
            symbol=symbol,
            trigger=trigger,
            trigger_event_id=trigger_event_id,
            mode=mode,
        )
    )
    return result


async def execute_automated_trading_intelligence_plan(
    plan_id: str,
    episode_id: str,
    wake_event_id: str | None,
    mode: TradingModeV2,
    *,
    plan_version: int | None = None,
) -> dict:
    try:
        result = await execute_trading_intelligence_plan(
            plan_id,
            TradingIntelligenceExecuteRequest(
                episode_id=episode_id,
                wake_event_id=wake_event_id,
                plan_version=plan_version,
                mode=mode,
            ),
        )
    except HTTPException as exc:
        reason = str(exc.detail)
        if not is_expected_automated_entry_rejection(exc.status_code, reason):
            raise
        plan = await asyncio.to_thread(trading_intelligence_store.get_plan, plan_id)
        await asyncio.to_thread(
            trading_intelligence_store.append_event,
            dedupe_key=f"entry-execution-blocked:{plan_id}:{plan_version}:{episode_id}:{wake_event_id}",
            event_type="ENTRY_EXECUTION_BLOCKED",
            aggregate_type="plan",
            aggregate_id=plan_id,
            aggregate_version=plan.version if plan else 1,
            payload={
                "wake_event_id": wake_event_id,
                "reason": reason,
                "risk_halted": False,
            },
        )
        return {
            "mode": str(mode),
            "status": "REJECTED",
            "plan_id": plan_id,
            "reason": reason,
        }
    return result.model_dump(mode="json") if isinstance(result, BaseModel) else result


async def v2_demo_commissioning(symbols: list[str]) -> dict:
    view = configuration_service.get()
    blockers: list[str] = []
    credentials_view = view.credential_scopes.get("okx:sandbox")
    if not credentials_view or not (
        credentials_view.api_key_configured
        and credentials_view.secret_configured
        and credentials_view.passphrase_configured
    ):
        blockers.append("okx_demo_credentials_incomplete")
        return {"eligible": False, "blockers": blockers}
    risk_state = risk_state_service.get()
    if risk_state.halted:
        blockers.append(f"global_risk_halt:{risk_state.source}")
    for attempt in range(3):
        adapter = build_v2_demo_adapter()
        try:
            health = await adapter.health()
            account = await adapter.validate_account_configuration()
            portfolio = await adapter.portfolio()
            open_orders = await adapter.open_orders()
            known_order_ids = {
                order_id
                for episode in trading_intelligence_store.list_episodes(limit=1000)
                for order_id in episode.order_ids
            }
            unknown_orders = [item.id for item in open_orders if item.id not in known_order_ids]
            if unknown_orders:
                blockers.append("unknown_demo_open_orders")
            known_position_scopes = {
                (
                    item.symbol.removesuffix(":USDT"),
                    Side.LONG if item.side == "LONG" else Side.SHORT,
                )
                for item in trading_intelligence_store.list_positions()
            }
            unknown_positions = [
                item
                for item in portfolio.open_positions
                if (item.symbol, item.side) not in known_position_scopes
            ]
            if unknown_positions:
                blockers.append("unknown_demo_open_positions")
            protection = {}
            for symbol in symbols:
                protection[symbol] = all(
                    [
                        await adapter.protection_available(
                            symbol.removesuffix(":USDT"), position_side
                        )
                        for position_side in (PositionSide.LONG, PositionSide.SHORT)
                    ]
                )
                if not protection[symbol]:
                    blockers.append(f"native_protection_unavailable:{symbol}")
            return {
                "eligible": not blockers,
                "blockers": blockers,
                "health": health,
                "account": account,
                "equity": str(portfolio.equity),
                "open_position_count": len(portfolio.open_positions),
                "open_order_count": len(open_orders),
                "unknown_order_count": len(unknown_orders),
                "unknown_position_count": len(unknown_positions),
                "protection": protection,
            }
        except Exception as exc:
            if attempt == 2:
                return {
                    "eligible": False,
                    "blockers": [*blockers, "okx_demo_temporarily_unavailable"],
                    "error_code": type(exc).__name__,
                    "error_hash": sha256(str(exc).encode()).hexdigest(),
                }
            await asyncio.sleep(0.5 * (attempt + 1))
        finally:
            await adapter.close()
    raise AssertionError("bounded Demo commissioning loop did not return")


def require_v2_automation() -> TradingIntelligenceAutomation:
    global trading_intelligence_automation
    if trading_intelligence_automation is None:
        trading_intelligence_automation = TradingIntelligenceAutomation(
            trading_intelligence_store,
            trading_intelligence_engine,
            decide=invoke_automated_trading_intelligence_decision,
            execute=execute_automated_trading_intelligence_plan,
            reconcile=reconcile_v2_before_resume,
        )
    return trading_intelligence_automation


@app.get("/api/trading-intelligence/automation", response_model=AutomationRuntimeV2)
async def get_trading_intelligence_automation():
    return await asyncio.to_thread(require_v2_automation().runtime)


@app.post("/api/trading-intelligence/automation/commission")
async def commission_trading_intelligence_automation(
    request: TradingIntelligenceAutomationRequest,
):
    if request.mode == TradingModeV2.SHADOW:
        return {"eligible": True, "blockers": [], "mode": str(request.mode)}
    return await v2_demo_commissioning(request.symbols)


@app.post(
    "/api/trading-intelligence/automation/start",
    response_model=AutomationRuntimeV2,
)
async def start_trading_intelligence_automation(
    request: TradingIntelligenceAutomationRequest,
):
    generation = risk_state_service.entry_generation
    configuration_generation = v2_configuration_generation
    if request.mode == TradingModeV2.OKX_DEMO:
        commissioning = await v2_demo_commissioning(request.symbols)
        if not commissioning["eligible"]:
            raise HTTPException(409, commissioning)
    service = require_v2_automation()
    async with v2_submission_lock:
        if generation != risk_state_service.entry_generation:
            raise HTTPException(409, "自动化启动已被停止操作取消，请重新发起")
        if configuration_generation != v2_configuration_generation:
            raise HTTPException(409, "验收期间账户配置已改变，请重新验收")
        if risk_state_service.get().halted:
            raise HTTPException(409, "风险暂停尚未解除，不能启动自动化")
        # No await between the generation check and the durable state transition.
        service.configure(enabled=True, requested_mode=request.mode, symbols=request.symbols)
    try:
        await service.resume(evaluate_market_states=False)
    except Exception:
        if generation == risk_state_service.entry_generation:
            service.configure(
                enabled=False,
                requested_mode=request.mode,
                symbols=request.symbols,
            )
        raise
    if generation != risk_state_service.entry_generation or risk_state_service.get().halted:
        return service.runtime()
    current = service.runtime()
    running = current.model_copy(
        update={
            "status": (
                AutomationRuntimeStatus.DEGRADED
                if current.symbol_failures
                else AutomationRuntimeStatus.RUNNING
            ),
            "updated_at": datetime.now(UTC),
        }
    )
    await asyncio.to_thread(
        trading_intelligence_store.save_automation_runtime,
        running,
        expected_updated_at=current.updated_at,
    )
    return service.runtime()


@app.post(
    "/api/trading-intelligence/automation/stop",
    response_model=AutomationRuntimeV2,
)
async def stop_trading_intelligence_automation():
    risk_state_service.invalidate_entries()
    service = require_v2_automation()
    stopped = await asyncio.to_thread(
        service.configure,
        enabled=False,
        requested_mode=service.runtime().requested_mode,
        symbols=service.runtime().symbols,
    )
    await reconcile_v2_before_resume()
    return stopped


@app.post("/api/trading-intelligence/live-grants/preview")
async def preview_trading_intelligence_live_grant(request: HumanLiveGrantPreviewRequest):
    return v2_live_grant_preview_payload(request)


@app.post("/api/trading-intelligence/live-grants", response_model=HumanLiveGrant)
async def issue_trading_intelligence_live_grant(request: HumanLiveGrantIssueRequest):
    preview = v2_live_grant_preview_payload(request)
    if preview["blockers"]:
        raise HTTPException(409, {"blockers": preview["blockers"]})
    if request.configuration_fingerprint != preview["configuration_fingerprint"]:
        raise HTTPException(409, "configuration changed after grant preview")
    try:
        grant = HumanLiveGrant(
            symbols=request.symbols,
            max_risk_per_trade=request.max_risk_per_trade,
            max_portfolio_risk=request.max_portfolio_risk,
            max_leverage=request.max_leverage,
            max_daily_drawdown=request.max_daily_drawdown,
            max_open_positions=request.max_open_positions,
            configuration_fingerprint=request.configuration_fingerprint,
            expires_at=datetime.now(UTC) + timedelta(hours=request.valid_hours),
            risk_confirmation_one=request.risk_confirmation_one,
            risk_confirmation_two=request.risk_confirmation_two,
        )
    except ValidationError as exc:
        raise HTTPException(400, exc.errors(include_url=False)) from exc
    await asyncio.to_thread(trading_intelligence_store.issue_grant, grant)
    add_audit(
        "trading_intelligence",
        "HumanLiveGrant v2 issued",
        severity="critical",
        grant_id=grant.grant_id,
        expires_at=grant.expires_at.isoformat(),
    )
    return grant


@app.delete(
    "/api/trading-intelligence/live-grants/{grant_id}",
    response_model=HumanLiveGrant,
)
async def revoke_trading_intelligence_live_grant(grant_id: str):
    try:
        grant = await asyncio.to_thread(trading_intelligence_store.revoke_grant, grant_id)
    except KeyError as exc:
        raise HTTPException(404, "HumanLiveGrant not found") from exc
    add_audit(
        "trading_intelligence",
        "HumanLiveGrant v2 revoked",
        severity="critical",
        grant_id=grant_id,
    )
    return grant


@app.api_route(
    "/api/{unregistered_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def unregistered_api_route(unregistered_path: str):
    del unregistered_path
    raise HTTPException(404, "API route not found")


web_distribution = Path(__file__).resolve().parent.parent.parent / "apps" / "web" / "dist"
if web_distribution.exists():
    app.mount(
        "/",
        StaticFiles(directory=web_distribution, html=True),
        name="cosmos-web",
    )
