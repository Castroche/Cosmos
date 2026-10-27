"""Versioned, outcome-driven trading memory. Advisory only; never an entry gate."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field

from .trading_intelligence_store import TradingIntelligenceStore


class LearningSnapshot(BaseModel):
    version: str = "empty"
    policy: str = "ADVISORY_ONLY"
    algorithm: str = "outcome-memory-v1"
    sample_count: int = 0
    skipped_count: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    groups: list[dict[str, Any]] = Field(default_factory=list)
    recent: list[dict[str, Any]] = Field(default_factory=list)


def refresh_learning(store: TradingIntelligenceStore) -> LearningSnapshot:
    # Query completed outcomes directly: active episodes cannot evict trade evidence.
    # A rolling window bounds prompt and compute cost. Stable content keys make replay idempotent.
    with store.store._lock, store.store.connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM ti_decision_episodes_v2 WHERE status = 'COMPLETED' "
            "ORDER BY updated_at DESC, episode_id DESC LIMIT 500"
        ).fetchall()
    episodes = [json.loads(row["payload"]) for row in rows]
    records = []
    skipped = 0
    campaigns: set[str] = set()
    for episode in episodes:
        outcome = episode.get("outcome") or {}
        # Historical/partial cash flows must not be interpreted as successful trades.
        if (
            outcome.get("action") not in {"LONG", "SHORT"}
            or not outcome.get("evidence_complete")
            or outcome.get("realized_pnl") is None
            or outcome.get("attribution_role") == "CONTRIBUTOR"
        ):
            skipped += 1
            continue
        campaign_id = (
            outcome.get("campaign_id") or episode.get("position_id") or episode["episode_id"]
        )
        if campaign_id in campaigns:
            skipped += 1
            continue
        campaigns.add(campaign_id)
        records.append(
            {
                "campaign_id": campaign_id,
                "episode_id": episode["episode_id"],
                "symbol": episode["symbol"],
                "mode": outcome.get("trading_mode", "UNKNOWN"),
                "setup": outcome.get("setup", "unknown"),
                "regime": outcome.get("regime", "unknown"),
                "side": outcome["action"],
                "return_on_risk": str(outcome.get("return_on_risk", "0")),
                "realized_pnl": str(outcome["realized_pnl"]),
                "fees": str(outcome.get("fees", "0")),
                "mfe": str(outcome.get("mfe", "0")),
                "mae": str(outcome.get("mae", "0")),
            }
        )
    version = sha256(
        json.dumps(
            {"records": records, "skipped": skipped, "algorithm": "outcome-memory-v1"},
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    previous = store.get_artifact("trading-learning-current")
    if previous and previous["payload"]["version"] == version:
        return LearningSnapshot.model_validate(previous["payload"])
    grouped = defaultdict(list)
    for record in records:
        key = (record["mode"], record["symbol"], record["setup"], record["regime"], record["side"])
        grouped[key].append(record)
    groups = []
    for (mode, symbol, setup, regime, side), items in grouped.items():
        n = len(items)
        wins = sum(Decimal(item["realized_pnl"]) > 0 for item in items)
        mean_r = sum((Decimal(item["return_on_risk"]) for item in items), Decimal(0)) / n
        mean_mfe = sum((Decimal(item["mfe"]) for item in items), Decimal(0)) / n
        # Beta(2,2) smoothing and shrinkage reduce the impact of tiny samples.
        posterior = Decimal(wins + 2) / Decimal(n + 4)
        learned_r = mean_r * Decimal(n) / Decimal(n + 10)
        lessons = ["结合当前结构选择具体入场点，历史统计仅作为决策参考。"]
        if mean_mfe > 1 and mean_r < mean_mfe / 2:
            lessons.append("历史浮盈回吐较多，优先比较分批止盈与保本触发点。")
        if mean_r < 0:
            lessons.append("该情景历史成本后收益偏弱，调整入场位置或止损距离；不据此否决开仓。")
        if n < 10:
            lessons.append("样本较少，采用收缩估计，避免将短期胜负视为稳定优势。")
        groups.append(
            {
                "mode": mode,
                "symbol": symbol,
                "setup": setup,
                "regime": regime,
                "side": side,
                "sample_count": n,
                "wins": wins,
                "posterior_win_rate": str(posterior),
                "expectancy_r": str(mean_r),
                "learned_expectancy_r": str(learned_r),
                "average_mfe_r": str(mean_mfe),
                "lessons": lessons,
            }
        )
    snapshot = LearningSnapshot(
        version=version,
        sample_count=len(records),
        skipped_count=skipped,
        groups=groups,
        recent=records[:12],
    )
    store.save_artifact(f"learning-{version}", "TradingLearningVersion", snapshot)
    store.save_artifact("trading-learning-current", "TradingLearningSnapshot", snapshot)
    store.append_event(
        dedupe_key=f"learning-updated:{version}",
        event_type="LEARNING_UPDATED",
        aggregate_type="learning",
        aggregate_id="trading-learning",
        aggregate_version=1,
        payload={"version": version, "sample_count": len(records), "policy": snapshot.policy},
    )
    return snapshot


def learning_context(store: TradingIntelligenceStore, symbol: str, mode: str) -> dict:
    snapshot = refresh_learning(store)
    return {
        "version": snapshot.version,
        "policy": snapshot.policy,
        "groups": [g for g in snapshot.groups if g["symbol"] == symbol and g["mode"] == mode],
        "note": "Historical evidence informs entry and management choices, never vetoes entry. "
        "No automatic changes to risk limits, approval rules or the base Quant model.",
    }
