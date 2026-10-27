"""Validate LLM position ownership across routine reconciliation updates."""

from .trading_intelligence import PositionRuntimeV2, PositionStateV2


def resolve_position_decision(
    position_id: str | None,
    position_version: int | None,
    snapshots: list[PositionRuntimeV2],
    current: list[PositionRuntimeV2],
) -> PositionRuntimeV2:
    snapshot = next(
        (item for item in snapshots
         if item.position_id == position_id and item.version == position_version),
        None,
    )
    position = next((item for item in current if item.position_id == position_id), None)
    if snapshot is None or position is None:
        raise ValueError("position target does not match the decision snapshot")
    if position.state not in {PositionStateV2.OPEN, PositionStateV2.PARTIALLY_FILLED}:
        raise ValueError("position target is no longer manageable")
    # Quote extrema and observation timestamps change during LLM latency.
    # Fills, stops, targets, management, ownership and exit requests must match.
    telemetry = {"version", "updated_at", "highest_mark", "lowest_mark"}
    if snapshot.model_dump(exclude=telemetry) != position.model_dump(exclude=telemetry):
        raise ValueError("position changed materially while awaiting the decision")
    return position
