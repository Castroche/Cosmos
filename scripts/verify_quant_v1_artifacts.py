from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from cosmos_quant.quant_intelligence import (
    LightGBMQuantIntelligenceV1,
    QuantModelArtifactV1,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Quant v1 model artifacts")
    parser.add_argument(
        "--models",
        type=Path,
        default=Path("data/models/quant-intelligence-v1"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/upgrade-v2/quant-training.json"),
    )
    args = parser.parse_args()
    models = args.models.resolve()
    results = []
    for path in sorted(models.glob("*.json")):
        raw = path.read_bytes()
        artifact = QuantModelArtifactV1.model_validate_json(raw)
        runtime = LightGBMQuantIntelligenceV1(artifact)
        prediction = runtime.infer(
            data_as_of=artifact.data_as_of,
            features=[0.001 * (index + 1) for index in range(len(artifact.feature_names))],
        )
        probability_sum = sum(
            (
                prediction.probability_long_tp_first,
                prediction.probability_short_tp_first,
                prediction.probability_timeout,
            )
        )
        if abs(probability_sum - 1) > 0.000001:
            raise RuntimeError(f"invalid probability sum for {path}: {probability_sum}")
        results.append(
            {
                "path": str(path.resolve()),
                "size_bytes": len(raw),
                "sha256": sha256(raw).hexdigest(),
                "symbol": artifact.symbol,
                "timeframe": artifact.timeframe,
                "source_dataset_id": artifact.source_dataset_id,
                "source_data_fingerprint": artifact.source_data_fingerprint,
                "training_rows": artifact.training_rows,
                "validation_sample_count": artifact.validation.sample_count,
                "brier_score": str(artifact.validation.brier_score),
                "log_loss": str(artifact.validation.log_loss),
                "data_as_of": artifact.data_as_of.isoformat(),
                "trained_at": artifact.trained_at.isoformat(),
                "model_fingerprint": artifact.model_fingerprint,
                "inference_verified": True,
                "prediction_ttl_seconds": int(
                    (prediction.expires_at - prediction.data_as_of).total_seconds()
                ),
            }
        )
    if len(results) != 3:
        raise RuntimeError(f"expected exactly three Quant v1 artifacts, got {len(results)}")
    report = {
        "schema_version": "cosmos-quant-training-v1/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "models": results,
        "summary": {
            "model_count": len(results),
            "all_inference_verified": all(
                item["inference_verified"] for item in results
            ),
        },
    }
    destination = args.output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(destination)


if __name__ == "__main__":
    main()
