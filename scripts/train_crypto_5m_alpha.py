from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.alpha_factory import (  # noqa: E402
    alpha_transaction_cost_fingerprint,
    assess_crypto_5m_contract,
)
from cosmos_quant.alpha_pipeline import (  # noqa: E402
    build_frozen_alpha_feature_cube,
    train_frozen_alpha_horizons,
)
from cosmos_quant.domain import (  # noqa: E402
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.research import ResearchDataset  # noqa: E402
from cosmos_quant.strategy_dsl import (  # noqa: E402
    StrategyDSLV2,
    ValueType,
    compile_strategy_dsl,
)


def _feature_history(dataset_id: str) -> dict[str, list[dict]]:
    return {
        "funding": [
            FundingRatePoint.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_funding(dataset_id)
        ],
        "open_interest": [
            OpenInterestPoint.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_open_interest(dataset_id)
        ],
        "positioning": [
            PositioningPoint.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_positioning(dataset_id)
        ],
        "basis": [
            BasisPoint.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_basis(dataset_id)
        ],
        "order_flow": [
            OrderFlowBar.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_order_flow(dataset_id)
        ],
        "liquidation_flow": [
            LiquidationFlowPoint.model_validate(item).model_dump(mode="json")
            for item in api.store.load_research_dataset_liquidation_flow(dataset_id)
        ],
    }


def run(args: argparse.Namespace) -> int:
    dsl_payload = json.loads(Path(args.dsl).read_text(encoding="utf-8"))
    ir = compile_strategy_dsl(StrategyDSLV2.model_validate(dsl_payload))
    contract = assess_crypto_5m_contract(api.store.list_research_datasets(2_000))
    if contract.status != "READY":
        raise RuntimeError(
            "crypto_5m_v1 is not READY; offline production training is prohibited"
        )
    selected_ids = {item.dataset_id for item in contract.markets if item.dataset_id}
    if args.dataset_id not in selected_ids:
        raise RuntimeError("dataset is not a selected immutable crypto_5m_v1 market unit")
    payload = api.store.load_research_dataset(args.dataset_id)
    if payload is None:
        raise LookupError("research dataset not found")
    dataset = ResearchDataset.model_validate(payload)
    if dataset.timeframe != "5m" or dataset.point_in_time_status != "verified":
        raise RuntimeError("training dataset is not verified closed 5m point-in-time data")
    candles = [
        Candle.model_validate(item)
        for item in api.store.load_research_dataset_candles(dataset.id)
    ]
    feature_names = tuple(
        item.strip()
        for item in args.features.split(",")
        if item.strip()
    ) or tuple(
        node.id for node in ir.nodes if node.output_type == ValueType.SERIES
    )
    configuration = api.configuration_service.current_configuration()
    cube = build_frozen_alpha_feature_cube(
        candles,
        _feature_history(dataset.id),
        ir,
        feature_names=feature_names,
        dataset_fingerprint=dataset.data_fingerprint,
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
    )
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "profile": "crypto_5m_v1",
                    "dataset_id": dataset.id,
                    "ir_fingerprint": ir.fingerprint,
                    "feature_cube_fingerprint": cube.fingerprint,
                    "feature_names": feature_names,
                    "finite_rows": {
                        str(horizon): int(np.isfinite(labels).sum())
                        for horizon, labels in cube.labels.items()
                    },
                    "notice": "rerun with --execute to train and persist native artifacts",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    cost_fingerprint = alpha_transaction_cost_fingerprint(
        fee_bps=configuration.fee_bps,
        slippage_bps=configuration.slippage_bps,
    )
    results = train_frozen_alpha_horizons(
        cube,
        ir,
        cost_model_fingerprint=cost_fingerprint,
        task=args.task,
        random_seed=args.seed,
    )
    artifacts = []
    for horizon, result in results.items():
        for artifact in (result.linear, result.gbdt, result.ensemble):
            api.store.save_alpha_model_artifact(artifact)
            artifacts.append(
                {
                    "horizon": horizon,
                    "id": artifact.id,
                    "family": artifact.model_family,
                    "fingerprint": artifact.fingerprint,
                    "walk_forward_passed": artifact.walk_forward_passed,
                }
            )
    output = ROOT / "output" / "alpha-factory"
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / f"{dataset.id}-{cube.fingerprint[:12]}.json"
    manifest.write_text(
        json.dumps(
            {
                "profile": "crypto_5m_v1",
                "dataset_id": dataset.id,
                "data_fingerprint": dataset.data_fingerprint,
                "feature_cube_fingerprint": cube.fingerprint,
                "cost_model_fingerprint": cost_fingerprint,
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"manifest": str(manifest), "artifacts": artifacts}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline crypto_5m_v1 native model trainer")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dsl", required=True)
    parser.add_argument("--features", default="")
    parser.add_argument("--task", choices=("regression", "classification"), default="regression")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--execute", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
