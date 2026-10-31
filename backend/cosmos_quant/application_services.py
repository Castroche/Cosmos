from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path
from threading import RLock

from anyio import CapacityLimiter

from .agent.store import AgentStore
from .ai_config_service import AIConfigurationService
from .ai_gateway import AIGateway
from .backtest import BacktestResult
from .backup import BackupService
from .build_identity import capture_runtime_build_identity
from .carry_campaign import run_carry_research_campaign
from .config_service import ConfigurationService
from .domain import AuditEvent, StrategyPlan
from .exchanges.ccxt_adapter import CcxtExchangeAdapter
from .liquidation_stream import BinanceLiquidationCollector
from .local_access import LocalAccess
from .ml_challenger import detect_return_drift
from .observability import Metrics
from .official_events import OfficialEventClient, active_risk_windows
from .quant_intelligence import LightGBMQuantIntelligenceV1, quant_model_is_eligible
from .relative_value_campaign import run_pair_research_campaign
from .research_campaign import CAMPAIGN_CELL_CONCURRENCY
from .risk_state import RiskStateService
from .runtime import RuntimeManager
from .secret_store import SecretStore
from .service_account import AccountService
from .service_audit import AuditService
from .service_carry import CarryService
from .service_collection_workers import CollectionWorkersService
from .service_commissioning import CommissioningService
from .service_intelligence_automation import IntelligenceAutomationService
from .service_intelligence_decisions import IntelligenceDecisionsService
from .service_intelligence_execution import IntelligenceExecutionService
from .service_intelligence_queries import IntelligenceQueriesService
from .service_lifecycle import LifecycleService
from .service_market import MarketService
from .service_market_workers import MarketWorkersService
from .service_operations import OperationsService
from .service_position_workers import PositionWorkersService
from .service_relative_value import RelativeValueService
from .service_research_admission_runtime import ResearchAdmissionRuntimeService
from .service_research_admissions import ResearchAdmissionsService
from .service_research_backtests import ResearchBacktestsService
from .service_research_campaign import ResearchCampaignService
from .service_research_candidates import ResearchCandidatesService
from .service_research_data import ResearchDataService
from .service_research_helpers import ResearchHelpersService
from .service_research_jobs import ResearchJobsService
from .service_research_proposals import ResearchProposalsService
from .service_research_revisions import ResearchRevisionsService
from .service_research_universe import ResearchUniverseService
from .service_review import ReviewService
from .service_review_worker import ReviewWorkerService
from .service_settings import SettingsService
from .service_strategy_library import StrategyLibraryService
from .settings import Settings
from .storage import SqliteStore
from .strategy_factory import StrategyFactory
from .trading_intelligence_automation import TradingIntelligenceAutomation
from .trading_intelligence_engine import DualLLMDecisionService, EventDrivenTradingEngine
from .trading_intelligence_store import TradingIntelligenceStore


class ApplicationServices(
    ResearchHelpersService,
    ResearchDataService,
    ResearchBacktestsService,
    ResearchAdmissionRuntimeService,
    CollectionWorkersService,
    MarketWorkersService,
    PositionWorkersService,
    LifecycleService,
    AccountService,
    OperationsService,
    SettingsService,
    ReviewWorkerService,
    ReviewService,
    ResearchAdmissionsService,
    CommissioningService,
    MarketService,
    StrategyLibraryService,
    ResearchCampaignService,
    ResearchJobsService,
    RelativeValueService,
    CarryService,
    ResearchRevisionsService,
    ResearchUniverseService,
    ResearchProposalsService,
    ResearchCandidatesService,
    AuditService,
    IntelligenceQueriesService,
    IntelligenceDecisionsService,
    IntelligenceExecutionService,
    IntelligenceAutomationService,
):
    def __init__(self, settings=None):
        self.run_pair_research_campaign = run_pair_research_campaign
        self.run_carry_research_campaign = run_carry_research_campaign
        self.quant_model_is_eligible = quant_model_is_eligible
        self.detect_return_drift = detect_return_drift
        self.active_risk_windows = active_risk_windows
        self.LightGBMQuantIntelligenceV1 = LightGBMQuantIntelligenceV1
        self.CcxtExchangeAdapter = CcxtExchangeAdapter
        self.env_settings = settings or Settings()
        self.store = SqliteStore(self.env_settings.cosmos_data_dir / "cosmos.db")
        self.trading_intelligence_store = TradingIntelligenceStore(self.store)
        self.runtime_build_identity = capture_runtime_build_identity()
        self.secret_store = SecretStore(self.env_settings.cosmos_data_dir / "cosmos.db")
        self.configuration_service = ConfigurationService(self.store, self.secret_store)
        self.ai_configuration_service = AIConfigurationService(self.store, self.secret_store)
        self.ai_gateway = AIGateway(self.ai_configuration_service)
        self.dual_llm_decision_service = DualLLMDecisionService(self.ai_gateway)
        self.trading_intelligence_engine = EventDrivenTradingEngine(self.trading_intelligence_store)
        self.trading_intelligence_automation: TradingIntelligenceAutomation | None = None
        self.official_event_client = OfficialEventClient()
        self.quant_model_cache: dict[str, self.LightGBMQuantIntelligenceV1] = {}
        self.quant_model_load_lock = RLock()
        self.risk_state_service = RiskStateService(self.store)
        self.runtime = RuntimeManager(self.configuration_service, self.risk_state_service)
        self.agent_store = AgentStore(self.store)
        self.backup_service = BackupService(
            self.store, self.env_settings.cosmos_data_dir / "backups"
        )
        self.metrics = Metrics()
        self.dashboard_backtest_cache: dict[tuple, BacktestResult] = {}
        self.backtest_job_semaphore = asyncio.Semaphore(2)
        self.RESEARCH_PROCESS_CONCURRENCY = CAMPAIGN_CELL_CONCURRENCY
        self.research_process_limiter = CapacityLimiter(self.RESEARCH_PROCESS_CONCURRENCY)
        self.backtest_job_tasks: set[asyncio.Task] = set()
        self.campaign_job_tasks: set[asyncio.Task] = set()
        self.universe_job_tasks: set[asyncio.Task] = set()
        self.review_worker_task: asyncio.Task | None = None
        self.v2_position_worker_task: asyncio.Task | None = None
        self.review_worker_wakeup = asyncio.Event()
        self.review_worker_health: dict[str, object] = {
            "status": "stopped",
            "activity": "idle",
            "heartbeat_at": None,
            "current_job_id": None,
            "last_error": None,
        }
        self.agent_stream_snapshot_lock = asyncio.Lock()
        self.agent_stream_snapshot_cache: dict[str, object] = {}
        self.AGENT_STREAM_SNAPSHOT_TTL_SECONDS = 2.0
        self.agent_commissioning_lock = asyncio.Lock()
        self.multi_leg_commissioning_lock = asyncio.Lock()
        self.stale_candidate_audit_ids: set[str] = set()
        self.liquidation_collector: BinanceLiquidationCollector | None = None
        self.catalog_journal_lock = asyncio.Lock()
        self.CHART_CANDLE_LIMIT = 300
        self.SUPPORTED_RESEARCH_EXCHANGES = {"okx", "binance", "bybit"}
        self.CATALOG_JOURNAL_SOURCE_URLS = {
            ("binance", "spot"): "https://api.binance.com/api/v3/exchangeInfo",
            ("binance", "swap"): "https://fapi.binance.com/fapi/v1/exchangeInfo",
            ("okx", "spot"): "https://www.okx.com/api/v5/public/instruments?instType=SPOT",
            ("okx", "swap"): "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            ("bybit", "spot"): "https://api.bybit.com/v5/market/instruments-info?category=spot",
            ("bybit", "swap"): "https://api.bybit.com/v5/market/instruments-info?category=linear",
        }
        self.PRODUCT_ACCEPTANCE_REPORT_PATH = (
            Path(__file__).resolve().parents[2] / "output" / "acceptance" / "latest.json"
        )
        self.QUANT_V1_MODEL_DIR = (
            self.env_settings.cosmos_data_dir / "models" / "quant-intelligence-v1"
        )
        self.local_access = LocalAccess(
            [*self.env_settings.cors_origins, "http://127.0.0.1:5173", "http://localhost:5173"],
        )
        self.strategy_factory = StrategyFactory()
        self.plans: dict[str, StrategyPlan] = {
            item["id"]: StrategyPlan.model_validate(item) for item in self.store.load_strategies()
        }
        self.audit: list[AuditEvent] = [
            AuditEvent.model_validate(item) for item in self.store.load_audit()
        ]
        self._live_unlock_challenges: dict[str, dict] = {}
        self._live_unlock_grants: dict[str, dict] = {}
        self.LIVE_CONFIRMATION_PHRASES = {
            1: "我确认将进入实盘授权",
            2: "我确认使用真实资金自动交易",
        }
        self.v2_entry_allocation_lock = asyncio.Lock()
        self.v2_submission_lock = asyncio.Lock()
        self.v2_configuration_generation = 0
        self.decision_entry_generation: ContextVar[int | None] = ContextVar(
            "decision_entry_generation", default=None
        )
        self.critical_workers: dict[str, asyncio.Task] = {}
        self.runtime.reconciliation_plan_ids_provider = self.reconciliation_protected_plan_ids

