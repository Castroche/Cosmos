from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.liquidation_archive import (  # noqa: E402
    import_binance_coinm_liquidation_history,
    verify_binance_coinm_liquidation_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-10-16")
    parser.add_argument("--end", default="2024-02-24")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--proxy-url", default="")
    args = parser.parse_args()
    start_day = date.fromisoformat(args.start)
    end_day = date.fromisoformat(args.end)
    manifests = []
    verification = []
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        manifest = import_binance_coinm_liquidation_history(
            symbol,
            start_day,
            end_day,
            workspace_root=ROOT,
            proxy_url=args.proxy_url,
            workers=args.workers,
        )
        manifests.append(manifest)
        verification.append(
            verify_binance_coinm_liquidation_manifest(manifest, workspace_root=ROOT)
        )
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    path = output / "binance-coinm-liquidations-latest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_scope": "binance_coin_m_inverse_perpetual_cross_contract_context",
                "manifests": [item.model_dump(mode="json") for item in manifests],
                "verification": verification,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest": str(path),
                "symbols": [
                    {
                        "symbol": item.research_symbol,
                        "source_instrument": item.source_instrument_id,
                        "days": item.archive_day_count,
                        "unique_events": item.unique_event_count,
                        "duplicate_rows": item.duplicate_row_count,
                        "flow_bars": item.flow_bar_count,
                    }
                    for item in manifests
                ],
                "verified": all(bool(item["verified"]) for item in verification),
            },
            ensure_ascii=False,
        )
    )
    return 0 if all(bool(item["verified"]) for item in verification) else 1


if __name__ == "__main__":
    raise SystemExit(main())
