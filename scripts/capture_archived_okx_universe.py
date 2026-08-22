from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.archived_exchange_catalog import (  # noqa: E402
    capture_archived_okx_catalog,
    save_archived_catalog_manifest,
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
        description="Capture and seal an archived OKX point-in-time swap catalog."
    )
    parser.add_argument("--timestamp", default="20240216004338")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    )
    args = parser.parse_args()

    output_root = ROOT / "output" / "data-ingestion" / "historical-catalog"
    manifest = capture_archived_okx_catalog(
        timestamp=args.timestamp,
        workspace_root=ROOT,
        output_root=output_root,
    )
    manifest_path = output_root / f"{manifest.id}.json"
    save_archived_catalog_manifest(manifest, manifest_path)
    verification = verify_archived_okx_catalog(manifest, workspace_root=ROOT)

    symbols = sorted(set(args.symbols))
    snapshot = build_point_in_time_universe_snapshot(
        {"okx": manifest.entries},
        policy=PointInTimeUniversePolicy(
            quote_currency="USDT",
            market_type="swap",
            timeframes=["1h"],
            allowed_symbols=symbols,
            selection_scope="per_exchange",
            minimum_listing_age_days=90,
            required_bars_by_timeframe={"1h": 500},
            minimum_symbols_per_exchange=len(symbols),
            maximum_symbols_per_exchange=len(symbols),
        ),
        as_of=manifest.original_server_time,
        archive_evidence=[manifest.archive_evidence()],
    )
    validate_universe_snapshot(snapshot)
    api.store.save_research_universe_snapshot(snapshot)

    summary = {
        "manifest_id": manifest.id,
        "manifest_path": manifest_path.relative_to(ROOT).as_posix(),
        "verification": verification,
        "universe_snapshot_id": snapshot.id,
        "universe_snapshot_fingerprint": snapshot.fingerprint,
        "universe_snapshot_version": snapshot.version,
        "as_of": snapshot.as_of.isoformat(),
        "historical_survivorship_complete": snapshot.historical_survivorship_complete,
        "point_in_time_selection_passed": snapshot.point_in_time_selection_passed,
        "selected_markets": snapshot.selected_markets,
        "archive_evidence": [item.model_dump(mode="json") for item in snapshot.archive_evidence],
    }
    _write_json(
        ROOT / "output" / "data-ingestion" / "archived-okx-universe-latest.json",
        summary,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
