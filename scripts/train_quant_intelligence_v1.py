from __future__ import annotations

import argparse
import json
from pathlib import Path

from cosmos_quant.domain import Candle
from cosmos_quant.quant_intelligence import train_quant_intelligence_v1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the COSMOS Quant Intelligence v1 LightGBM bundle from causal 5m data."
    )
    parser.add_argument("input", type=Path, help="JSON training contract")
    parser.add_argument("output", type=Path, help="Destination model artifact JSON")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    artifact = train_quant_intelligence_v1(
        symbol=str(payload["symbol"]),
        candles=[Candle.model_validate(item) for item in payload["candles"]],
        features_by_candle=payload["features_by_candle"],
        feature_names=payload["feature_names"],
        source_dataset_id=payload.get("source_dataset_id"),
        source_data_fingerprint=payload.get("source_data_fingerprint"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "model_fingerprint": artifact.model_fingerprint,
                "training_rows": artifact.training_rows,
                "brier_score": str(artifact.validation.brier_score),
                "log_loss": str(artifact.validation.log_loss),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
