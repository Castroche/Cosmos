from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .strategy_dsl import CanonicalStrategyIR


class EvolutionOperation(StrEnum):
    REPLACE_FEATURE = "replace_feature"
    REPLACE_TRANSFORM = "replace_transform"
    ADD_REGIME_FILTER = "add_regime_filter"
    REMOVE_REGIME_FILTER = "remove_regime_filter"
    CHANGE_EXIT_RULE = "change_exit_rule"
    CHANGE_HOLDING_PERIOD = "change_holding_period"
    COMBINE_SIGNALS = "combine_signals"
    SIMPLIFY_STRATEGY = "simplify_strategy"
    CROSS_STRATEGY = "cross_strategy"


class ResearchHierarchy(BaseModel, frozen=True):
    campaign_id: str
    hypothesis_family_id: str
    structural_candidate_id: str
    parameter_instance_id: str
    execution_variant_id: str


class CandidateLineageRecord(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"lineage_v2_{uuid4().hex}")
    research_event_id: str
    family_id: str
    candidate_id: str
    parent_ids: tuple[str, ...]
    operation: EvolutionOperation
    changed_nodes: tuple[str, ...]
    reason: str
    before_fingerprints: tuple[str, ...]
    after_fingerprint: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvolutionBudget(BaseModel, frozen=True):
    research_event_id: str
    family_id: str
    semantic_proposals: int = Field(default=0, ge=0, le=12)
    empirical_trials: int = Field(default=0, ge=0, le=3)
    exact_finalists: int = Field(default=0, ge=0, le=2)
    evolution_rounds: int = Field(default=0, ge=0, le=1)

    def consume(
        self,
        *,
        semantic_proposals: int = 0,
        empirical_trials: int = 0,
        exact_finalists: int = 0,
        evolution_rounds: int = 0,
    ) -> EvolutionBudget:
        return EvolutionBudget(
            research_event_id=self.research_event_id,
            family_id=self.family_id,
            semantic_proposals=self.semantic_proposals + semantic_proposals,
            empirical_trials=self.empirical_trials + empirical_trials,
            exact_finalists=self.exact_finalists + exact_finalists,
            evolution_rounds=self.evolution_rounds + evolution_rounds,
        )


class NoveltyFingerprints(BaseModel, frozen=True):
    ast: str
    feature_graph: str
    pre_return_behavior: str


def novelty_fingerprints(
    ir: CanonicalStrategyIR,
    *,
    pre_return_behavior_fingerprint: str,
) -> NoveltyFingerprints:
    ast_payload = ir.model_dump(
        mode="json",
        exclude={"risk_budget_fraction", "maximum_holding_bars"},
    )
    feature_payload = [node.model_dump(mode="json", exclude={"parameters"}) for node in ir.nodes]

    def digest(payload) -> str:
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    return NoveltyFingerprints(
        ast=digest(ast_payload),
        feature_graph=digest(feature_payload),
        pre_return_behavior=pre_return_behavior_fingerprint,
    )


class ResearchToolName(StrEnum):
    DETECT_RESEARCH_OPPORTUNITY = "detect_research_opportunity"
    PROPOSE_HYPOTHESIS = "propose_hypothesis"
    PROPOSE_STRATEGY_DSL = "propose_strategy_dsl"
    PROPOSE_MUTATION = "propose_mutation"
    REVIEW_CANDIDATE = "review_candidate"
    SUMMARIZE_FAILURE = "summarize_failure"


class ResearchToolCapability(BaseModel, frozen=True):
    name: ResearchToolName
    can_read_market_snapshot: bool = True
    can_write_database: Literal[False] = False
    can_issue_certificate: Literal[False] = False
    can_adjust_risk: Literal[False] = False
    can_place_orders: Literal[False] = False
    can_execute_code: Literal[False] = False


def research_tool_capabilities() -> tuple[ResearchToolCapability, ...]:
    return tuple(ResearchToolCapability(name=name) for name in ResearchToolName)
