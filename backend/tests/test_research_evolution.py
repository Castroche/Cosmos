from __future__ import annotations

import pytest
from cosmos_quant.research_evolution import (
    EvolutionBudget,
    EvolutionOperation,
    ResearchToolName,
    research_tool_capabilities,
)
from cosmos_quant.storage import SqliteStore
from pydantic import ValidationError


def test_per_event_evolution_budget_cannot_be_reset_by_extra_mutation():
    budget = EvolutionBudget(research_event_id="run", family_id="family").consume(
        semantic_proposals=12,
        empirical_trials=3,
        exact_finalists=2,
        evolution_rounds=1,
    )
    with pytest.raises(ValidationError):
        budget.consume(evolution_rounds=1)


def test_exactly_nine_controlled_evolution_operations_are_exposed():
    assert len(EvolutionOperation) == 9


def test_research_agent_tools_have_no_write_risk_certificate_or_execution_capability():
    capabilities = research_tool_capabilities()
    assert {item.name for item in capabilities} == set(ResearchToolName)
    for item in capabilities:
        assert not item.can_write_database
        assert not item.can_issue_certificate
        assert not item.can_adjust_risk
        assert not item.can_place_orders
        assert not item.can_execute_code


def test_research_event_budgets_survive_restart_and_enforce_12_3_2_1(tmp_path):
    path = tmp_path / "budgets.db"
    store = SqliteStore(path)
    limits = {
        "semantic_proposals": 12,
        "empirical_trials": 3,
        "exact_finalists": 2,
        "evolution_rounds": 1,
    }
    for kind, limit in limits.items():
        for index in range(limit):
            assert store.consume_autonomous_research_budget(
                research_event_id="event",
                family_id="family",
                budget_kind=kind,
                amount=1,
                limit=limit,
                idempotency_key=f"{kind}:{index}",
            )
        restarted = SqliteStore(path)
        assert not restarted.consume_autonomous_research_budget(
            research_event_id="event",
            family_id="family",
            budget_kind=kind,
            amount=1,
            limit=limit,
            idempotency_key=f"{kind}:overflow",
        )
