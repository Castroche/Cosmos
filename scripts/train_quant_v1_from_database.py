from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
from cosmos_quant.domain import Candle
from cosmos_quant.quant_intelligence import train_quant_intelligence_v1

MODEL_FILENAMES = {
    "BTC/USDT": "btc-usdt-swap.json",
    "ETH/USDT": "eth-usdt-swap.json",
    "SOL/USDT": "sol-usdt-swap.json",
}
FEATURE_NAMES = [
    "timeframe_features.5m.return_1",
    "timeframe_features.5m.ema_20_slope",
    "timeframe_features.5m.ema_50_slope",
    "timeframe_features.5m.roc_12",
    "timeframe_features.5m.realized_volatility_20",
    "timeframe_features.5m.realized_volatility_100",
    "timeframe_features.5m.relative_volume_20",
    "timeframe_features.5m.volume_acceleration",
]


def rolling_std(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    cumulative_squared = np.concatenate(([0.0], np.cumsum(values * values)))
    for index in range(period - 1, len(values)):
        start = index - period + 1
        total = cumulative[index + 1] - cumulative[start]
        total_squared = cumulative_squared[index + 1] - cumulative_squared[start]
        variance = max(
            (total_squared - total * total / period) / max(period - 1, 1),
            0.0,
        )
        result[index] = variance**0.5
    return result


def ema_slope(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    result[period - 1] = float(np.mean(values[:period]))
    alpha = 2.0 / (period + 1)
    for index in range(period, len(values)):
        result[index] = alpha * values[index] + (1 - alpha) * result[index - 1]
    slope = np.full(len(values), np.nan)
    slope[period:] = result[period:] / result[period - 1 : -1] - 1
    return slope


def causal_features(candles: list[Candle]) -> list[list[float]]:
    closes = np.asarray([float(item.close) for item in candles])
    volumes = np.asarray([float(item.volume) for item in candles])
    returns = np.full(len(candles), np.nan)
    returns[1:] = closes[1:] / closes[:-1] - 1
    roc = np.full(len(candles), np.nan)
    roc[12:] = closes[12:] / closes[:-12] - 1
    volatility_20 = rolling_std(returns[1:], 20)
    volatility_20 = np.concatenate(([np.nan], volatility_20))
    volatility_100 = rolling_std(returns[1:], 100)
    volatility_100 = np.concatenate(([np.nan], volatility_100))
    relative_volume = np.full(len(candles), np.nan)
    cumulative_volume = np.concatenate(([0.0], np.cumsum(volumes)))
    for index in range(19, len(candles)):
        prior_total = cumulative_volume[index] - cumulative_volume[index - 19]
        prior_mean = prior_total / 19
        if prior_mean > 0:
            relative_volume[index] = volumes[index] / prior_mean
    acceleration = np.full(len(candles), np.nan)
    acceleration[1:] = relative_volume[1:] - relative_volume[:-1]
    matrix = np.column_stack(
        (
            returns,
            ema_slope(closes, 20),
            ema_slope(closes, 50),
            roc,
            volatility_20,
            volatility_100,
            relative_volume,
            acceleration,
        )
    )
    return matrix.tolist()


def latest_dataset(connection: sqlite3.Connection, symbol: str) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT dataset_id, data_fingerprint FROM research_datasets
        WHERE lower(exchange_id) = 'okx' AND market_type = 'swap'
          AND symbol = ? AND timeframe = '5m'
          AND CAST(json_extract(payload, '$.candle_count') AS INTEGER) >= 50000
          AND CAST(json_extract(payload, '$.gap_count') AS INTEGER) = 0
          AND CAST(json_extract(payload, '$.invalid_count') AS INTEGER) = 0
        ORDER BY json_extract(payload, '$.data_end') DESC, created_at DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"no eligible OKX 5m dataset for {symbol}")
    return str(row[0]), str(row[1])


def load_candles(
    connection: sqlite3.Connection,
    dataset_id: str,
    limit: int,
) -> list[Candle]:
    rows = connection.execute(
        """
        SELECT timestamp, open, high, low, close, volume
        FROM research_dataset_candles
        WHERE dataset_id = ? ORDER BY sequence DESC LIMIT ?
        """,
        (dataset_id, limit),
    ).fetchall()
    return [
        Candle(
            timestamp=row[0],
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
        )
        for row in reversed(rows)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Train all Quant v1 models from SQLite")
    parser.add_argument("--database", type=Path, default=Path("data/cosmos.db"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/models/quant-intelligence-v1"),
    )
    parser.add_argument("--bars", type=int, default=80000)
    args = parser.parse_args()
    if args.bars < 10000:
        raise SystemExit("--bars must be at least 10000")
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    try:
        for research_symbol, filename in MODEL_FILENAMES.items():
            dataset_id, data_fingerprint = latest_dataset(connection, research_symbol)
            candles = load_candles(connection, dataset_id, args.bars)
            artifact = train_quant_intelligence_v1(
                symbol=f"{research_symbol}:USDT",
                candles=candles,
                features_by_candle=causal_features(candles),
                feature_names=FEATURE_NAMES,
                source_dataset_id=dataset_id,
                source_data_fingerprint=data_fingerprint,
            )
            destination = args.output / filename
            destination.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
            reports.append(
                {
                    "symbol": artifact.symbol,
                    "dataset_id": dataset_id,
                    "bars": len(candles),
                    "training_rows": artifact.training_rows,
                    "model_fingerprint": artifact.model_fingerprint,
                    "brier_score": str(artifact.validation.brier_score),
                    "log_loss": str(artifact.validation.log_loss),
                    "output": str(destination.resolve()),
                }
            )
    finally:
        connection.close()
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
