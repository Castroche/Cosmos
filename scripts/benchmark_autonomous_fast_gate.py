from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import quantiles
from time import perf_counter

from cosmos_quant.autonomous_research import (
    AutonomousResearchRunRequest,
    CandidateOrigin,
    FastGateEngine,
    MarketSnapshotV2,
)
from cosmos_quant.build_identity import capture_runtime_build_identity
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_dsl import (
    DSLNode,
    ResearchHypothesisV2,
    SignalDefinition,
    StrategyDSLV2,
)

ROOT = Path(__file__).resolve().parents[1]
MARKETS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
TIMEFRAMES = ("1h", "4h")


def percentile(values: list[float], percentile_value: int) -> float:
    if len(values) == 1:
        return values[0]
    index = {50: 49, 90: 89, 95: 94, 99: 98}[percentile_value]
    return quantiles(values, n=100, method="inclusive")[index]


def latency_summary(values: list[float]) -> dict[str, float]:
    return {f"p{item}_seconds": round(percentile(values, item), 6) for item in (50, 90, 95, 99)} | {
        "maximum_seconds": round(max(values), 6)
    }


def select_dataset(store: SqliteStore, symbol: str, timeframe: str) -> dict:
    candidates = [
        item
        for item in store.list_research_datasets(1_000)
        if item.get("symbol") == symbol
        and item.get("timeframe") == timeframe
        and item.get("research_role") == "general"
        and item.get("closed_bars_only") is True
        and item.get("candle_count", 0) >= 200
    ]
    if not candidates:
        raise RuntimeError(f"no local closed-bar benchmark dataset for {symbol} {timeframe}")
    return max(candidates, key=lambda item: item["candle_count"])


def build_snapshot(store: SqliteStore, metadata: dict) -> MarketSnapshotV2:
    candles = store.load_research_dataset_candles(metadata["id"])[-2_000:]
    timeframe_seconds = int(metadata["timeframe_seconds"])
    event_times = [
        int(datetime.fromisoformat(item["timestamp"]).timestamp() * 1_000_000_000)
        for item in candles
    ]
    available_times = [item + timeframe_seconds * 1_000_000_000 for item in event_times]
    return MarketSnapshotV2(
        id=f"benchmark:{metadata['id']}",
        dataset_fingerprint=metadata["data_fingerprint"],
        dataset_schema_version=int(metadata.get("schema_version", 8)),
        availability_policy="legacy_closed_bar_adapter_v1",
        market_scope=(f"{metadata['exchange']}:{metadata['symbol']}:{metadata['timeframe']}",),
        timeframe_seconds=timeframe_seconds,
        features={
            name: [float(item[name]) for item in candles]
            for name in ("open", "high", "low", "close", "volume")
        },
        event_time_ns=event_times,
        available_time_ns=available_times,
        decision_time_ns=available_times,
        ready_at=datetime.now(UTC),
    )


def request_for(snapshot: MarketSnapshotV2, symbol: str, timeframe: str, variant: int):
    hypothesis_id = f"benchmark_{symbol.split('/')[0].lower()}_{timeframe}_{variant}"
    fast_window = 8 + variant * 3
    slow_window = 32 + variant * 7
    hypothesis = ResearchHypothesisV2(
        id=hypothesis_id,
        family_id=f"benchmark_family:{symbol}:{timeframe}",
        observation="Closed-bar prices may exhibit persistent directional strength.",
        economic_rationale="Persistent demand imbalance may create bounded trend continuation.",
        causal_mechanism="Closed-bar trend strength may attract delayed orders over later bars.",
        prediction="Subsequent bars may preserve positive net risk-adjusted expectancy.",
        expected_direction="both",
        intended_regime=["trend"],
        holding_horizon="1-48 bars",
        required_features=["open", "high", "low", "close", "volume"],
        falsification_criteria=["net Expected R lower confidence bound is non-positive"],
        invalidation_conditions=["directional persistence disappears"],
    )
    dsl = StrategyDSLV2(
        hypothesis_id=hypothesis_id,
        family="trend",
        evaluation_clock="closed_bar",
        required_features=["open", "high", "low", "close", "volume"],
        intended_regime=["trend"],
        holding_horizon="1-48 bars",
        nodes=[
            DSLNode(
                id="fast",
                operator="ema",
                inputs=["close"],
                parameters={"window": fast_window},
            ),
            DSLNode(
                id="slow",
                operator="ema",
                inputs=["close"],
                parameters={"window": slow_window},
            ),
            DSLNode(id="long", operator="CROSS_ABOVE", inputs=["fast", "slow"]),
            DSLNode(id="short", operator="CROSS_BELOW", inputs=["fast", "slow"]),
            DSLNode(
                id="stop",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 14},
            ),
        ],
        signals=SignalDefinition(long="long", short="short"),
        risk={
            "stop_distance": "stop",
            "risk_budget_fraction": Decimal("0.005"),
            "maximum_holding_bars": 48,
        },
        invalidation=["trend reversal, stop, or maximum holding period"],
    )
    return AutonomousResearchRunRequest(
        idempotency_key=f"benchmark:{hypothesis_id}:request:v1",
        research_event_id=f"benchmark-market-event:{symbol}:{timeframe}",
        hypothesis=hypothesis,
        dsl=dsl,
        origin=CandidateOrigin.FREE_DSL,
        snapshot=snapshot,
        validation_config_fingerprint="benchmark-fast-gate-v1",
        transaction_cost_bps=Decimal("8"),
    )


def main() -> None:
    production = SqliteStore(ROOT / "data" / "cosmos.db")
    identity = capture_runtime_build_identity(ROOT)
    cases: list[dict] = []
    with tempfile.TemporaryDirectory(
        prefix="cosmos-fast-gate-", ignore_cleanup_errors=True
    ) as directory:
        benchmark_store = SqliteStore(Path(directory) / "benchmark.db")
        engine = FastGateEngine(benchmark_store, identity)
        for symbol in MARKETS:
            for timeframe in TIMEFRAMES:
                metadata = select_dataset(production, symbol, timeframe)
                cold_started = perf_counter()
                snapshot = build_snapshot(production, metadata)
                cold_feature_seconds = perf_counter() - cold_started
                for variant in (1, 2):
                    request = request_for(snapshot, symbol, timeframe, variant)
                    started = perf_counter()
                    result = engine.run(request)
                    elapsed = perf_counter() - started
                    cases.append(
                        {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "variant": variant,
                            "bars": len(snapshot.event_time_ns),
                            "cold_feature_seconds": round(cold_feature_seconds, 6),
                            "snapshot_ready_to_commit_seconds": round(elapsed, 6),
                            "disposition": str(result.disposition),
                            "deadline_exceeded": result.deadline_exceeded,
                        }
                    )
    timings = [item["snapshot_ready_to_commit_seconds"] for item in cases]
    cold_timings = [
        item["cold_feature_seconds"] + item["snapshot_ready_to_commit_seconds"] for item in cases
    ]
    report = {
        "benchmark_version": "autonomous-fast-gate-v1",
        "measured_at": datetime.now(UTC).isoformat(),
        "scenario": "local snapshot ready, warm Python/NumPy process, cold per-market feature load",
        "case_count": len(cases),
        "warm_snapshot_ready_latency": latency_summary(timings),
        "cold_feature_plus_gate_diagnostic": latency_summary(cold_timings),
        "p95_slo_seconds": 240,
        "hard_deadline_seconds": 300,
        "p95_passed": percentile(timings, 95) <= 240,
        "hard_deadline_passed": max(timings) <= 300
        and not any(item["deadline_exceeded"] for item in cases),
        "cases": cases,
    }
    destination = ROOT / "output" / "acceptance" / "autonomous_fast_gate_benchmark.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
