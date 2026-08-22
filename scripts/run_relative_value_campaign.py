from __future__ import annotations

import argparse
import asyncio
import json

from cosmos_quant import api
from cosmos_quant.relative_value import PAIR_STRATEGY_LIBRARY


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the bounded cross-pair relative-value campaign on development datasets. "
            "Sealed blind datasets are rejected by the API."
        )
    )
    parser.add_argument("dataset_ids", nargs="+", help="verified development dataset ids")
    parser.add_argument("--fold-count", type=int, default=6)
    parser.add_argument(
        "--run-revisions",
        action="store_true",
        help="run one bounded parent-plus-revisions successor campaign",
    )
    return parser.parse_args()


def campaign_summary(report) -> dict:
    return {
        "id": report.id,
        "parent_campaign_id": report.parent_campaign_id,
        "predecessor_campaign_id": report.predecessor_campaign_id,
        "experiment_kind": report.experiment_kind,
        "research_lineage_id": report.research_lineage_id,
        "research_experiment_id": report.research_experiment_id,
        "cumulative_hypothesis_count": report.cumulative_hypothesis_count,
        "sequential_experiment_count": report.sequential_experiment_count,
        "effective_trial_count": report.effective_trial_count,
        "new_trial_variant_count": report.new_trial_variant_count,
        "cells": len(report.cells),
        "strategies": report.strategy_ids,
        "pbo": report.pbo.model_dump(mode="json"),
        "provisional_selected_strategy_id": report.provisional_selected_strategy_id,
        "failure_attribution": (
            report.failure_attribution.model_dump(mode="json")
            if report.failure_attribution is not None
            else None
        ),
        "recommended_revisions": [
            item.model_dump(mode="json") for item in report.recommended_revisions
        ],
        "candidates": [
            {
                "id": candidate.library_entry_id,
                "passed": candidate.cross_market_passed,
                "rank_score": str(candidate.rank_score),
                "markets": candidate.market_count,
                "exchanges": candidate.unique_exchange_count,
                "trades": candidate.total_trades,
                "median_win_rate_percent": str(candidate.median_win_rate_percent),
                "median_payoff_ratio": str(candidate.median_payoff_ratio),
                "median_profit_factor": str(candidate.median_profit_factor),
                "positive_stress_ratio": str(candidate.positive_stress_ratio),
                "temporal_stable_ratio": str(candidate.temporal_stable_market_ratio),
                "adjusted_p_value": str(candidate.multiple_testing_adjusted_p_value),
                "reasons": candidate.reasons,
            }
            for candidate in report.candidates
        ],
        "selection_reasons": report.selection_reasons,
        "execution_eligible": report.execution_eligible,
        "profitability_claim": report.profitability_claim,
    }


def main() -> None:
    args = parse_args()
    report = asyncio.run(
        api.run_relative_value_campaign(
            api.RelativeValueCampaignRequest(
                dataset_ids=args.dataset_ids,
                fold_count=args.fold_count,
            )
        )
    )
    result = {"base_campaign": campaign_summary(report)}
    if args.run_revisions and report.recommended_revisions:
        successor_specs = [
            *[item.model_dump(mode="json") for item in PAIR_STRATEGY_LIBRARY],
            *[
                item.strategy_spec.model_dump(mode="json")
                for item in report.recommended_revisions
            ],
        ]
        successor = asyncio.run(
            api.run_relative_value_campaign(
                api.RelativeValueCampaignRequest(
                    dataset_ids=args.dataset_ids,
                    strategy_specs=successor_specs,
                    fold_count=args.fold_count,
                    parent_campaign_id=report.id,
                    generate_failure_revisions=False,
                )
            )
        )
        result["bounded_revision_campaign"] = campaign_summary(successor)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
