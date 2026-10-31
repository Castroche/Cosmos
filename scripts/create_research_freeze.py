from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.application_services import ApplicationServices  # noqa: E402
api = ApplicationServices()
from cosmos_quant.research import ResearchDataset  # noqa: E402
from cosmos_quant.research_campaign import CAMPAIGN_ENGINE_VERSION  # noqa: E402
from cosmos_quant.research_lifecycle import StrategyCandidate  # noqa: E402
from cosmos_quant.strategy_library import list_strategy_library  # noqa: E402


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(payload: dict) -> str:
    return sha256(_canonical_bytes(payload)).hexdigest()[:24]


def _load_verified_dataset(dataset_id: str) -> ResearchDataset:
    payload = api.store.load_research_dataset(dataset_id)
    if not payload:
        raise SystemExit(f"research dataset not found: {dataset_id}")
    return ResearchDataset.model_validate(payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the complete pre-Campaign hypothesis, data and validation contract. "
            "The output is content-addressed and must be created before any blind evaluation."
        )
    )
    parser.add_argument("blindset_manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "research-freeze")
    parser.add_argument("--candidate-id", action="append", default=[])
    parser.add_argument("--parent-campaign-id")
    args = parser.parse_args()

    manifest_path = args.blindset_manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("datasets") or []
    if not rows:
        raise SystemExit("blind-set manifest contains no datasets")
    if not all(row.get("development_verified") and row.get("blind_verified") for row in rows):
        raise SystemExit("blind-set manifest contains an unverified split")

    source_datasets = []
    development_datasets = []
    blind_datasets = []
    for row in rows:
        source = _load_verified_dataset(row["source_dataset_id"])
        development = _load_verified_dataset(row["development_dataset_id"])
        blind = _load_verified_dataset(row["blind_dataset_id"])
        if source.data_fingerprint != row["source_fingerprint"]:
            raise SystemExit(f"source fingerprint mismatch: {source.id}")
        if (
            development.research_role != "development"
            or development.blind_set_id != manifest["blind_set_id"]
        ):
            raise SystemExit(f"invalid development split binding: {development.id}")
        if (
            blind.research_role != "blind"
            or not blind.blind_sealed
            or blind.blind_set_id != manifest["blind_set_id"]
        ):
            raise SystemExit(f"invalid blind split binding: {blind.id}")
        source_datasets.append(source)
        development_datasets.append(development)
        blind_datasets.append(blind)

    configuration = api.configuration_service.current_configuration()
    strategies = list_strategy_library()
    candidate_ids = list(dict.fromkeys(args.candidate_id))
    candidates = []
    for candidate_id in candidate_ids:
        candidate_payload = api.store.load_strategy_candidate(candidate_id)
        if not candidate_payload:
            raise SystemExit(f"strategy candidate not found: {candidate_id}")
        candidates.append(StrategyCandidate.model_validate(candidate_payload))
    contract = {
        "schema_version": 1,
        "purpose": "pre-campaign immutable research contract; no profitability claim",
        "blind_set": {
            "blind_set_id": manifest["blind_set_id"],
            "policy": manifest["contract"]["policy"],
            "development_ratio": manifest["contract"]["development_ratio"],
            "embargo_days": manifest["contract"]["embargo_days"],
            "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "source_datasets": [
            {
                "id": dataset.id,
                "fingerprint": dataset.data_fingerprint,
                "exchange": dataset.exchange,
                "symbol": dataset.symbol,
                "timeframe": dataset.timeframe,
                "data_start": dataset.data_start.isoformat(),
                "data_end": dataset.data_end.isoformat(),
                "candle_count": dataset.candle_count,
            }
            for dataset in source_datasets
        ],
        "development_datasets": [
            {
                "id": dataset.id,
                "fingerprint": dataset.data_fingerprint,
                "source_dataset_id": dataset.source_dataset_id,
                "split_boundary": dataset.split_boundary.isoformat()
                if dataset.split_boundary
                else None,
                "embargo_bars": dataset.embargo_bars,
            }
            for dataset in development_datasets
        ],
        "blind_datasets": [
            {
                "id": dataset.id,
                "fingerprint": dataset.data_fingerprint,
                "source_dataset_id": dataset.source_dataset_id,
                "split_boundary": dataset.split_boundary.isoformat()
                if dataset.split_boundary
                else None,
                "embargo_bars": dataset.embargo_bars,
                "sealed": dataset.blind_sealed,
            }
            for dataset in blind_datasets
        ],
        "hypothesis_scope": {
            "policy": "exactly the listed public priors and bounded revisions",
            "parent_campaign_id": args.parent_campaign_id,
            "strategy_count": len(strategies) + len(candidates),
            "builtin_strategy_count": len(strategies),
            "revision_candidate_count": len(candidates),
            "strategies": [
                {
                    "id": entry.id,
                    "version": entry.version,
                    "family": entry.family.value,
                    "archetype": entry.archetype,
                    "required_data": sorted(entry.required_data),
                    "strategy_fingerprint": entry.validated_spec.fingerprint,
                    "source_url": entry.source_url,
                }
                for entry in strategies
            ],
            "revision_candidates": [
                {
                    "id": candidate.id,
                    "parent_candidate_id": candidate.parent_candidate_id,
                    "source": candidate.source,
                    "strategy_fingerprint": candidate.spec_fingerprint,
                    "spec": candidate.spec,
                }
                for candidate in candidates
            ],
        },
        "validation_contract": {
            "campaign_engine_version": CAMPAIGN_ENGINE_VERSION,
            "fees_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "risk_fraction": str(configuration.risk.risk_per_trade),
            "minimum_reward_risk": str(configuration.risk.minimum_reward_risk),
            "single_use_blind_holdout": True,
            "causal_required_data_gate": True,
            "campaign_thresholds": {
                "minimum_positive_stress_ratio": "0.60",
                "minimum_high_quality_market_ratio": "0.50",
                "minimum_temporal_stable_market_ratio": "0.50",
                "minimum_markets": 3,
                "minimum_unique_symbols": 3,
                "minimum_unique_exchanges": 2,
                "minimum_total_trades": 300,
                "minimum_median_win_rate_percent": "50",
                "minimum_median_payoff_ratio": "1.30",
                "minimum_median_profit_factor": "1.25",
                "maximum_worst_drawdown_percent": "20",
                "minimum_pooled_win_rate_lower_bound_percent": "45",
                "maximum_false_discovery_rate": "0.05",
                "maximum_pbo": "0.20",
                "strategy_significance_test": (
                    "one-sided exact trade-win binomial p-values; within-symbol "
                    "timeframes Bonferroni-corrected; symbol p-values combined "
                    "with a dependence-robust Cauchy test; BH FDR across frozen strategies"
                ),
            },
            "per_backtest_validation": configuration.strategy.validation.model_dump(mode="json"),
        },
    }
    freeze_fingerprint = _fingerprint(contract)
    artifact = {
        "freeze_id": f"research_freeze_{freeze_fingerprint}",
        "freeze_fingerprint": freeze_fingerprint,
        "frozen_at": datetime.now(UTC).isoformat(),
        "contract": contract,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"research_freeze_{freeze_fingerprint}.json"
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            existing.get("freeze_fingerprint") != freeze_fingerprint
            or existing.get("contract") != contract
        ):
            raise SystemExit(f"immutable freeze collision: {output_path}")
        artifact = existing
    else:
        output_path.write_text(encoded, encoding="utf-8")
    print(json.dumps({**artifact, "artifact": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
