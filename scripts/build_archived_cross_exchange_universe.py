from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.application_services import ApplicationServices  # noqa: E402
api = ApplicationServices()
from cosmos_quant.archived_exchange_catalog import (  # noqa: E402
    ArchivedExchangeCatalogManifest,
    ArchivedOkxCatalogManifest,
    verify_archived_binance_catalog,
    verify_archived_okx_catalog,
)
from cosmos_quant.research_universe import (  # noqa: E402
    PointInTimeUniversePolicy,
    build_point_in_time_universe_snapshot,
    validate_universe_snapshot,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a fixed-symbol cross-exchange universe from sealed archives."
    )
    parser.add_argument(
        "--binance-manifest",
        default=(
            "output/data-ingestion/historical-catalog/"
            "archived_catalog_49e047ae66a6a81dcef9a64f.json"
        ),
    )
    parser.add_argument(
        "--okx-manifest",
        default=(
            "output/data-ingestion/historical-catalog/"
            "archived_okx_catalog_f07d393cbb77ced793925d14.json"
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    )
    args = parser.parse_args()

    binance = ArchivedExchangeCatalogManifest.model_validate_json(
        (ROOT / args.binance_manifest).read_text(encoding="utf-8")
    )
    okx = ArchivedOkxCatalogManifest.model_validate_json(
        (ROOT / args.okx_manifest).read_text(encoding="utf-8")
    )
    verification = {
        "binance": verify_archived_binance_catalog(binance, workspace_root=ROOT),
        "okx": verify_archived_okx_catalog(okx, workspace_root=ROOT),
    }
    symbols = sorted(set(args.symbols))
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": binance.entries, "okx": okx.entries},
        policy=PointInTimeUniversePolicy(
            quote_currency="USDT",
            market_type="swap",
            timeframes=["1h"],
            allowed_symbols=symbols,
            selection_scope="common_across_exchanges",
            minimum_listing_age_days=90,
            required_bars_by_timeframe={"1h": 500},
            minimum_symbols_per_exchange=len(symbols),
            maximum_symbols_per_exchange=len(symbols),
        ),
        as_of=max(binance.original_server_time, okx.original_server_time),
        archive_evidence=[binance.archive_evidence(), okx.archive_evidence()],
    )
    validate_universe_snapshot(snapshot)
    api.store.save_research_universe_snapshot(snapshot)

    summary = {
        "universe_snapshot_id": snapshot.id,
        "universe_snapshot_fingerprint": snapshot.fingerprint,
        "universe_snapshot_version": snapshot.version,
        "as_of": snapshot.as_of.isoformat(),
        "selection_scope": "fixed_allowed_symbols_across_archived_exchanges",
        "allowed_symbols": symbols,
        "historical_survivorship_complete": snapshot.historical_survivorship_complete,
        "point_in_time_selection_passed": snapshot.point_in_time_selection_passed,
        "selected_markets": snapshot.selected_markets,
        "source_observation_times": {
            "binance": binance.original_server_time.isoformat(),
            "okx": okx.original_server_time.isoformat(),
        },
        "verification": verification,
        "archive_evidence": [item.model_dump(mode="json") for item in snapshot.archive_evidence],
    }
    destination = (
        ROOT / "output" / "data-ingestion" / "archived-cross-exchange-universe-latest.json"
    )
    _write_json(destination, summary)
    print(json.dumps({**summary, "manifest": str(destination)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
