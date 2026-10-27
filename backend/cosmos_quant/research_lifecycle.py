from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .backtest import BacktestResult
from .strategy_spec import StrategySpec, validate_strategy_spec


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    CHALLENGER = "challenger"
    CHAMPION = "champion"
    REJECTED = "rejected"
    RETIRED = "retired"


class CandidateResearchContext(BaseModel):
    """Immutable generation-time context for v2 research candidates."""

    generation_contract_version: str = "strategy_generation_v2"
    exchange: str
    market_type: str
    symbol: str
    timeframe: str
    entry_profile: str = "balanced_5m_v1"
    market_as_of: datetime
    summary_fingerprint: str
    fee_bps: Decimal
    slippage_bps: Decimal
    risk_fraction: Decimal
    max_gross_exposure: Decimal
    max_leverage: Decimal
    required_features: list[str] = Field(default_factory=lambda: ["ohlcv"])
    trial_group: str
    trial_count: int = Field(default=1, ge=1)


class ValidatedStrategyArtifact(BaseModel):
    """Immutable attestation that an AI/ML candidate passed research isolation."""

    candidate_id: str
    candidate_status: Literal["champion"] = "champion"
    source: Literal["ai", "ml"]
    spec_fingerprint: str
    dataset_id: str
    data_fingerprint: str
    backtest_fingerprint: str
    drift_status: str
    compiled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def reject_unsafe_artifact(self):
        if self.drift_status in {"", "critical", "unavailable"}:
            raise ValueError("compiled strategy artifact requires non-critical drift evidence")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:20]


class StrategyCandidate(BaseModel):
    id: str = Field(default_factory=lambda: f"cand_{uuid4().hex[:16]}")
    spec: dict
    spec_fingerprint: str
    source: Literal["system", "operator", "ai", "ml"]
    status: CandidateStatus = CandidateStatus.PROPOSED
    dataset_id: str | None = None
    backtest: BacktestResult | None = None
    execution_artifact: ValidatedStrategyArtifact | None = None
    parent_candidate_id: str | None = None
    research_context: CandidateResearchContext | None = None
    decision_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_spec_identity(self):
        validated = validate_strategy_spec(self.spec)
        if validated.fingerprint != self.spec_fingerprint:
            raise ValueError("candidate spec fingerprint does not match payload")
        if validated.source != self.source:
            raise ValueError("candidate source does not match StrategySpec")
        return self


def backtest_artifact_fingerprint(result: BacktestResult) -> str:
    payload = result.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256(encoded).hexdigest()[:20]


def compile_champion(
    candidate: StrategyCandidate,
    *,
    dataset_id: str,
    data_fingerprint: str,
    drift_status: str,
) -> ValidatedStrategyArtifact:
    """Compile a promoted AI/ML Champion into auditable execution provenance."""

    if candidate.status != CandidateStatus.CHAMPION:
        raise ValueError("only a Champion can be compiled for execution")
    if candidate.source not in {"ai", "ml"}:
        raise ValueError("only AI/ML candidates require a compiled execution artifact")
    if candidate.dataset_id != dataset_id or candidate.backtest is None:
        raise ValueError("Champion is not bound to the requested research dataset")
    if not candidate.backtest.robustness_passed:
        raise ValueError("Champion backtest did not pass robustness validation")
    if candidate.backtest.dataset_id != dataset_id:
        raise ValueError("Champion backtest dataset binding does not match")
    if candidate.backtest.data_fingerprint != data_fingerprint:
        raise ValueError("Champion backtest data fingerprint does not match")
    if candidate.backtest.strategy_fingerprint != candidate.spec_fingerprint:
        raise ValueError("Champion backtest strategy fingerprint does not match")
    return ValidatedStrategyArtifact(
        candidate_id=candidate.id,
        source=candidate.source,
        spec_fingerprint=candidate.spec_fingerprint,
        dataset_id=dataset_id,
        data_fingerprint=data_fingerprint,
        backtest_fingerprint=backtest_artifact_fingerprint(candidate.backtest),
        drift_status=drift_status,
    )


def validate_compiled_champion(
    candidate: StrategyCandidate,
    artifact: ValidatedStrategyArtifact,
    *,
    dataset_id: str,
    data_fingerprint: str,
) -> None:
    """Reject stale, retired or tampered AI/ML execution provenance."""

    if candidate.status != CandidateStatus.CHAMPION:
        raise ValueError("research candidate is no longer the active Champion")
    if candidate.source not in {"ai", "ml"}:
        raise ValueError("compiled Champion validation only applies to AI/ML candidates")
    if candidate.execution_artifact is None:
        raise ValueError("Champion has no compiled execution artifact")
    expected = candidate.execution_artifact
    if artifact.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("compiled execution artifact does not match the stored Champion")
    if (
        artifact.candidate_id != candidate.id
        or artifact.spec_fingerprint != candidate.spec_fingerprint
        or artifact.dataset_id != dataset_id
        or artifact.data_fingerprint != data_fingerprint
    ):
        raise ValueError("compiled Champion provenance binding is invalid")
    if candidate.backtest is None:
        raise ValueError("Champion backtest evidence is unavailable")
    if artifact.backtest_fingerprint != backtest_artifact_fingerprint(candidate.backtest):
        raise ValueError("Champion backtest evidence changed after compilation")


class PromotionDecision(BaseModel):
    approved: bool
    checks: dict[str, bool]
    reasons: list[str]


def create_candidate(
    raw_spec: StrategySpec | dict,
    *,
    parent_candidate_id: str | None = None,
    research_context: CandidateResearchContext | None = None,
) -> StrategyCandidate:
    spec = validate_strategy_spec(raw_spec)
    return StrategyCandidate(
        spec=spec.model_dump(mode="json", exclude={"fingerprint"}),
        spec_fingerprint=spec.fingerprint,
        source=spec.source,
        parent_candidate_id=parent_candidate_id,
        research_context=research_context,
        decision_reasons=[
            "候选规格已通过字段范围、策略家族和交叉参数约束",
            "候选仍不可执行，必须绑定不可变数据集并通过独立回测",
        ],
    )


def attach_backtest(
    candidate: StrategyCandidate,
    dataset_id: str,
    backtest: BacktestResult,
) -> StrategyCandidate:
    passed = backtest.robustness_passed
    return candidate.model_copy(
        deep=True,
        update={
            "dataset_id": dataset_id,
            "backtest": backtest,
            "status": CandidateStatus.CHALLENGER if passed else CandidateStatus.REJECTED,
            "decision_reasons": [
                *candidate.decision_reasons,
                *backtest.validation_reasons,
            ],
            "updated_at": datetime.now(UTC),
        },
    )


def compare_for_promotion(
    challenger: StrategyCandidate,
    champion: StrategyCandidate | None,
) -> PromotionDecision:
    result = challenger.backtest
    if result is None:
        return PromotionDecision(
            approved=False,
            checks={"backtest_present": False},
            reasons=["挑战者尚未完成不可变数据集回测"],
        )
    if challenger.status != CandidateStatus.CHALLENGER:
        return PromotionDecision(
            approved=False,
            checks={"challenger_status": False},
            reasons=[f"候选状态 {challenger.status} 不允许晋升"],
        )
    if champion and challenger.spec.get("family") != champion.spec.get("family"):
        return PromotionDecision(
            approved=False,
            checks={"strategy_family_match": False},
            reasons=["挑战者只能与同一策略家族的 Champion 比较"],
        )
    checks = {
        "challenger_status": True,
        "backtest_present": True,
        "robustness_passed": result.robustness_passed,
        "dataset_bound": challenger.dataset_id == result.dataset_id,
    }
    reasons = list(result.validation_reasons)
    if champion and champion.backtest:
        incumbent = champion.backtest
        same_dataset = challenger.dataset_id == champion.dataset_id
        checks.update(
            {
                "same_dataset": same_dataset,
                "profit_factor_not_worse": (result.profit_factor >= incumbent.profit_factor),
                "return_not_worse": result.return_percent >= incumbent.return_percent,
                "drawdown_controlled": (
                    result.max_drawdown_percent
                    <= incumbent.max_drawdown_percent * Decimal("1.10") + Decimal("0.25")
                ),
                "trade_count_not_lower": result.total_trades >= incumbent.total_trades,
            }
        )
        reasons.append("挑战者必须在同一数据集上与当前 Champion 公平比较")
    else:
        reasons.append("当前没有 Champion；通过全部稳健性门槛后可成为首个 Champion")
    approved = all(checks.values())
    return PromotionDecision(approved=approved, checks=checks, reasons=reasons)
