from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .ai_configuration import AIProvider
from .ai_gateway import AIUsage
from .backtest import CAUSAL_LIBRARY_POLICY_VERSION, CausalStrategyPrior
from .catalog_journal import MarketCatalogChangeEvent, MarketCatalogJournalSnapshot
from .configuration import StrategyKind
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    OrderSide,
    PositioningPoint,
)
from .research import ResearchDataset, ResearchJobStatus
from .research_hypothesis import FailureAttribution, HypothesisRevision
from .research_lifecycle import StrategyCandidate
from .research_universe import (
    HistoricalCatalogArchiveEvidence,
    MarketCatalogEntry,
    PointInTimeUniversePolicy,
)
from .strategy_portfolio import StrategyHealthReport
from .trading_intelligence import V2_SYMBOLS, TradingModeV2


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


class CandidatePromotionRequest(BaseModel):
    confirmation: str


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ResumeRequest(BaseModel):
    confirmation: str


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
