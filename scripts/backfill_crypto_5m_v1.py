from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.alpha_factory import ALPHA_MARKETS, assess_crypto_5m_contract  # noqa: E402


def _selected_scope(market: str | None) -> tuple[list[str], list[str]]:
    if market is None:
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT"], ["binance", "okx"]
    normalized = market.strip()
    if normalized not in ALPHA_MARKETS:
        raise ValueError(f"market must be one of: {', '.join(ALPHA_MARKETS)}")
    exchange, symbol = normalized.split(":", 1)
    return [symbol], [exchange]


async def run(*, execute: bool, feature_mode: str, market: str | None) -> int:
    before = assess_crypto_5m_contract(api.store.list_research_datasets(2_000))
    symbols, exchange_ids = _selected_scope(market)
    request = api.ResearchUniverseRequest(
        symbols=symbols,
        timeframes=["5m"],
        exchange_ids=exchange_ids,
        bars=220_000,
        max_markets=len(symbols) * len(exchange_ids),
        selection_mode="explicit",
        market_type="swap",
        feature_mode=feature_mode,
        minimum_history_coverage_ratio="0.999",
        minimum_symbols_per_exchange=len(symbols),
        maximum_symbols_per_exchange=len(symbols),
        # Single-market resumable jobs are implementation slices of this
        # frozen profile. The complete six-market contract is assessed below.
        research_profile="crypto_5m_v1" if market is None else None,
        horizons=(3, 6, 12),
    )
    if not execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "market": market or "all",
                    "current_status": before.status,
                    "current_fingerprint": before.fingerprint,
                    "request": request.model_dump(mode="json"),
                    "notice": "rerun with --execute to start the resumable public-data backfill",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    async def progress(completed, total, cell):
        print(
            json.dumps(
                {
                    "event": "market_completed",
                    "completed": completed,
                    "total": total,
                    "exchange": cell.exchange,
                    "symbol": cell.symbol,
                    "dataset_id": cell.dataset_id,
                    "candle_count": cell.candle_count,
                    "ok": cell.ok,
                    "error": cell.error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    print(
        json.dumps(
            {
                "event": "backfill_started",
                "market": market or "all",
                "feature_mode": feature_mode,
                "bars": request.bars,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    report = await api.prepare_research_universe(request, progress_callback=progress)
    after = assess_crypto_5m_contract(api.store.list_research_datasets(2_000))
    manifest = {
        "profile": "crypto_5m_v1",
        "market_slice": market,
        "started_from_contract_fingerprint": before.fingerprint,
        "report": report.model_dump(mode="json"),
        "resulting_contract": {
            **after.model_dump(mode="json"),
            "fingerprint": after.fingerprint,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    suffix = "all" if market is None else market.replace(":", "-").replace("/", "-")
    destination = (
        ROOT / "output" / "data-ingestion" / f"crypto-5m-v1-backfill-{suffix}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"manifest": str(destination), **manifest}, ensure_ascii=False))
    slice_passed = bool(report.cells) and all(cell.dataset_id and cell.ok for cell in report.cells)
    return 0 if (slice_passed if market is not None else after.status == "READY") else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the frozen BTC/ETH/SOL x Binance/OKX 5m research scope. "
            "The command never fabricates missing candles."
        )
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--feature-mode",
        choices=("full", "ohlcv_only"),
        default="full",
    )
    parser.add_argument(
        "--market",
        choices=ALPHA_MARKETS,
        help="run one resumable market slice, for example binance:BTC/USDT",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(execute=args.execute, feature_mode=args.feature_mode, market=args.market)
    )


if __name__ == "__main__":
    raise SystemExit(main())
